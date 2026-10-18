//! Terminal emulation for the desktop app: [`Terminal`] wraps
//! `alacritty_terminal` (parsing, grid, scrollback, selection) and turns it
//! into a flat [`Frame`] of resolved cells a renderer can draw without
//! knowing about alacritty; [`input`] encodes keys, mouse and pastes the
//! way xterm does.

pub mod input;
pub mod palette;

use std::sync::{Arc, Mutex};

use alacritty_terminal::event::{Event as TermEvent, EventListener};
use alacritty_terminal::grid::{Dimensions, Scroll};
use alacritty_terminal::index::{Column, Line, Point, Side};
use alacritty_terminal::selection::{Selection, SelectionType};
use alacritty_terminal::term::cell::Flags;
use alacritty_terminal::term::{Config, Term, TermMode};
use alacritty_terminal::vte::ansi::{CursorShape, Processor};

pub use palette::{Palette, Rgb};

/// Lines of history kept per terminal (the web keeps 5,000 too).
pub const SCROLLBACK: usize = 5_000;

/// Cell attributes the renderer needs, after colours are resolved.
pub mod attr {
    pub const BOLD: u16 = 1;
    pub const ITALIC: u16 = 1 << 1;
    pub const UNDERLINE: u16 = 1 << 2;
    pub const STRIKE: u16 = 1 << 3;
    /// The left half of a double-width character.
    pub const WIDE: u16 = 1 << 4;
    /// The right half of a double-width character: draw nothing.
    pub const SPACER: u16 = 1 << 5;
    pub const DOUBLE_UNDERLINE: u16 = 1 << 6;
    pub const UNDERCURL: u16 = 1 << 7;
    pub const SELECTED: u16 = 1 << 8;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cell {
    pub ch: char,
    pub fg: Rgb,
    pub bg: Rgb,
    pub attrs: u16,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CursorKind {
    Block,
    Underline,
    Beam,
    HollowBlock,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cursor {
    pub row: usize,
    pub col: usize,
    pub kind: CursorKind,
}

/// What is on screen (the visible rows, scrolled back or not), resolved.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Frame {
    pub rows: usize,
    pub cols: usize,
    /// `rows * cols`, row-major.
    pub cells: Vec<Cell>,
    pub cursor: Option<Cursor>,
    /// Lines scrolled back from the bottom (0 = live).
    pub display_offset: usize,
    pub history: usize,
    pub default_bg: Rgb,
}

impl Frame {
    pub fn cell(&self, row: usize, col: usize) -> &Cell {
        &self.cells[row * self.cols + col]
    }

    /// One row as text (spacers skipped), for tests and accessibility.
    pub fn row_text(&self, row: usize) -> String {
        self.cells[row * self.cols..(row + 1) * self.cols]
            .iter()
            .filter(|c| c.attrs & attr::SPACER == 0)
            .map(|c| c.ch)
            .collect::<String>()
            .trim_end()
            .to_string()
    }

    pub fn text(&self) -> String {
        (0..self.rows).map(|r| self.row_text(r)).collect::<Vec<_>>().join("\n")
    }
}

/// What the program asked of the terminal besides drawing.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Requests {
    /// Bytes the terminal answers with (DSR cursor position, DA, colour
    /// queries…): write them back to the PTY.
    pub replies: Vec<u8>,
    pub title: Option<String>,
    /// OSC 52 copy.
    pub clipboard: Option<String>,
    pub bell: bool,
}

#[derive(Clone, Default)]
struct Listener(Arc<Mutex<(Requests, Palette)>>);

impl EventListener for Listener {
    fn send_event(&self, event: TermEvent) {
        let mut guard = self.0.lock().unwrap();
        let (req, palette) = &mut *guard;
        match event {
            TermEvent::PtyWrite(s) => req.replies.extend_from_slice(s.as_bytes()),
            TermEvent::Title(t) => req.title = Some(t),
            TermEvent::ResetTitle => req.title = Some(String::new()),
            TermEvent::ClipboardStore(_, text) => req.clipboard = Some(text),
            TermEvent::ColorRequest(index, format) => {
                let c = palette.indexed(index);
                let rgb = alacritty_terminal::vte::ansi::Rgb { r: c.0, g: c.1, b: c.2 };
                req.replies.extend_from_slice(format(rgb).as_bytes());
            }
            TermEvent::Bell => req.bell = true,
            _ => {}
        }
    }
}

struct Size {
    rows: usize,
    cols: usize,
}

impl Dimensions for Size {
    fn total_lines(&self) -> usize {
        self.rows
    }
    fn screen_lines(&self) -> usize {
        self.rows
    }
    fn columns(&self) -> usize {
        self.cols
    }
}

/// One emulated terminal.
pub struct Terminal {
    term: Term<Listener>,
    parser: Processor,
    listener: Listener,
    /// Bumped on every change a renderer must see.
    generation: u64,
}

impl Terminal {
    pub fn new(rows: usize, cols: usize, palette: Palette) -> Terminal {
        let listener = Listener(Arc::new(Mutex::new((Requests::default(), palette))));
        let config = Config { scrolling_history: SCROLLBACK, ..Config::default() };
        let size = Size { rows: rows.max(1), cols: cols.max(1) };
        Terminal { term: Term::new(config, &size, listener.clone()), parser: Processor::new(), listener, generation: 0 }
    }

    pub fn feed(&mut self, bytes: &[u8]) {
        self.parser.advance(&mut self.term, bytes);
        self.generation += 1;
    }

    /// A fresh screen of `rows × cols` (before a snapshot is fed).
    pub fn reset(&mut self, rows: usize, cols: usize) {
        let palette = self.palette();
        *self = Terminal { generation: self.generation + 1, ..Terminal::new(rows, cols, palette) };
    }

    pub fn resize(&mut self, rows: usize, cols: usize) {
        if (rows, cols) != self.size() {
            self.term.resize(Size { rows: rows.max(1), cols: cols.max(1) });
            self.generation += 1;
        }
    }

    pub fn size(&self) -> (usize, usize) {
        (self.term.screen_lines(), self.term.columns())
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn palette(&self) -> Palette {
        self.listener.0.lock().unwrap().1.clone()
    }

    pub fn set_palette(&mut self, palette: Palette) {
        self.listener.0.lock().unwrap().1 = palette;
        self.generation += 1;
    }

    /// Takes what the program asked for since the last call.
    pub fn take_requests(&mut self) -> Requests {
        std::mem::take(&mut self.listener.0.lock().unwrap().0)
    }

    pub fn modes(&self) -> input::Modes {
        let m = *self.term.mode();
        input::Modes {
            app_cursor: m.contains(TermMode::APP_CURSOR),
            bracketed_paste: m.contains(TermMode::BRACKETED_PASTE),
            mouse_report: m.intersects(TermMode::MOUSE_MODE),
            mouse_motion: m.contains(TermMode::MOUSE_MOTION),
            mouse_drag: m.contains(TermMode::MOUSE_DRAG),
            sgr_mouse: m.contains(TermMode::SGR_MOUSE),
            alt_screen: m.contains(TermMode::ALT_SCREEN),
            alternate_scroll: m.contains(TermMode::ALTERNATE_SCROLL),
            focus_reports: m.contains(TermMode::FOCUS_IN_OUT),
        }
    }

    /// Scrolls the view by `lines` (positive = back into history).
    pub fn scroll(&mut self, lines: i32) {
        self.term.scroll_display(Scroll::Delta(lines));
        self.generation += 1;
    }

    pub fn scroll_to_bottom(&mut self) {
        if self.term.grid().display_offset() != 0 {
            self.term.scroll_display(Scroll::Bottom);
            self.generation += 1;
        }
    }

    pub fn display_offset(&self) -> usize {
        self.term.grid().display_offset()
    }

    fn point(&self, row: usize, col: usize) -> Point {
        let offset = self.term.grid().display_offset() as i32;
        let (rows, cols) = self.size();
        Point::new(Line(row.min(rows - 1) as i32 - offset), Column(col.min(cols - 1)))
    }

    /// Starts a selection at a visible cell; `kind` 1/2/3 = simple, word, line.
    pub fn select_start(&mut self, row: usize, col: usize, right_half: bool, kind: u8) {
        let ty = match kind {
            2 => SelectionType::Semantic,
            3 => SelectionType::Lines,
            _ => SelectionType::Simple,
        };
        let side = if right_half { Side::Right } else { Side::Left };
        self.term.selection = Some(Selection::new(ty, self.point(row, col), side));
        self.generation += 1;
    }

    pub fn select_update(&mut self, row: usize, col: usize, right_half: bool) {
        let point = self.point(row, col);
        if let Some(sel) = &mut self.term.selection {
            sel.update(point, if right_half { Side::Right } else { Side::Left });
            self.generation += 1;
        }
    }

    pub fn select_clear(&mut self) {
        if self.term.selection.take().is_some() {
            self.generation += 1;
        }
    }

    pub fn selection_text(&self) -> Option<String> {
        self.term.selection_to_string().filter(|s| !s.is_empty())
    }

    /// The visible screen, resolved into `frame` (reusing its buffer).
    pub fn render(&self, frame: &mut Frame) {
        let palette = self.listener.0.lock().unwrap().1.clone();
        let content = self.term.renderable_content();
        let (rows, cols) = self.size();
        let offset = content.display_offset;
        frame.rows = rows;
        frame.cols = cols;
        frame.display_offset = offset;
        frame.history = self.term.grid().history_size();
        frame.default_bg = palette.background;
        frame.cells.clear();
        frame.cells.resize(rows * cols, Cell { ch: ' ', fg: palette.foreground, bg: palette.background, attrs: 0 });
        let selection = content.selection;
        let colors = content.colors;
        for indexed in content.display_iter {
            let row = indexed.point.line.0 + offset as i32;
            if row < 0 || row as usize >= rows {
                continue;
            }
            let (row, col) = (row as usize, indexed.point.column.0);
            if col >= cols {
                continue;
            }
            let cell = indexed.cell;
            let flags = cell.flags;
            let mut fg =
                palette.resolve(cell.fg, colors, flags.contains(Flags::BOLD), flags.contains(Flags::DIM), true);
            let mut bg = palette.resolve(cell.bg, colors, false, false, false);
            if flags.contains(Flags::INVERSE) {
                std::mem::swap(&mut fg, &mut bg);
            }
            if flags.contains(Flags::HIDDEN) {
                fg = bg;
            }
            let mut attrs = 0;
            let selected = selection.is_some_and(|s| s.contains(indexed.point));
            if selected {
                attrs |= attr::SELECTED;
                bg = palette.selection;
            }
            for (f, a) in [
                (Flags::BOLD, attr::BOLD),
                (Flags::ITALIC, attr::ITALIC),
                (Flags::UNDERLINE, attr::UNDERLINE),
                (Flags::DOUBLE_UNDERLINE, attr::DOUBLE_UNDERLINE),
                (Flags::UNDERCURL, attr::UNDERCURL),
                (Flags::DOTTED_UNDERLINE, attr::UNDERLINE),
                (Flags::DASHED_UNDERLINE, attr::UNDERLINE),
                (Flags::STRIKEOUT, attr::STRIKE),
                (Flags::WIDE_CHAR, attr::WIDE),
                (Flags::WIDE_CHAR_SPACER, attr::SPACER),
                (Flags::LEADING_WIDE_CHAR_SPACER, attr::SPACER),
            ] {
                if flags.contains(f) {
                    attrs |= a;
                }
            }
            let ch = if cell.c == '\t' { ' ' } else { cell.c };
            frame.cells[row * cols + col] = Cell { ch, fg, bg, attrs };
        }
        let cursor = content.cursor;
        let visible_row = cursor.point.line.0 + offset as i32;
        frame.cursor = match cursor.shape {
            CursorShape::Hidden => None,
            _ if visible_row < 0 || visible_row as usize >= rows => None,
            shape => Some(Cursor {
                row: visible_row as usize,
                col: cursor.point.column.0.min(cols - 1),
                kind: match shape {
                    CursorShape::Underline => CursorKind::Underline,
                    CursorShape::Beam => CursorKind::Beam,
                    CursorShape::HollowBlock => CursorKind::HollowBlock,
                    _ => CursorKind::Block,
                },
            }),
        };
    }

    pub fn frame(&self) -> Frame {
        let mut f = Frame::default();
        self.render(&mut f);
        f
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn term(rows: usize, cols: usize) -> Terminal {
        Terminal::new(rows, cols, Palette::dark())
    }

    #[test]
    fn text_colours_and_cursor() {
        let mut t = term(4, 20);
        t.feed(b"hi \x1b[31mred\x1b[0m \x1b[1;7mrev\x1b[0m\r\nnext");
        let f = t.frame();
        assert_eq!(f.row_text(0), "hi red rev");
        assert_eq!(f.row_text(1), "next");
        let p = Palette::dark();
        assert_eq!(f.cell(0, 3).fg, p.ansi[1]);
        assert_eq!((f.cell(0, 7).bg, f.cell(0, 7).fg), (p.foreground, p.background), "inverse swaps");
        assert_eq!(f.cell(0, 7).attrs & attr::BOLD, attr::BOLD);
        assert_eq!(f.cursor, Some(Cursor { row: 1, col: 4, kind: CursorKind::Block }));
        t.feed(b"\x1b[?25l");
        assert_eq!(t.frame().cursor, None);
    }

    #[test]
    fn truecolor_256_and_wide_chars() {
        let mut t = term(2, 10);
        t.feed("\x1b[38;2;1;2;3mA\x1b[48;5;196mB\x1b[0m日x".as_bytes());
        let f = t.frame();
        assert_eq!(f.cell(0, 0).fg, Rgb(1, 2, 3));
        assert_eq!(f.cell(0, 1).bg, Rgb(255, 0, 0));
        assert_eq!(f.cell(0, 2).ch, '日');
        assert_ne!(f.cell(0, 2).attrs & attr::WIDE, 0);
        assert_ne!(f.cell(0, 3).attrs & attr::SPACER, 0);
        assert_eq!(f.row_text(0), "AB日x");
    }

    #[test]
    fn scrollback_and_scrolling() {
        let mut t = term(3, 10);
        for i in 0..10 {
            t.feed(format!("line {i}\r\n").as_bytes());
        }
        let f = t.frame();
        assert_eq!(f.row_text(0), "line 8");
        assert_eq!(f.history, 8);
        t.scroll(2);
        let f = t.frame();
        assert_eq!((f.display_offset, f.row_text(0).as_str()), (2, "line 6"));
        assert_eq!(f.cursor, None, "the cursor row is below the view");
        t.scroll_to_bottom();
        assert_eq!(t.frame().row_text(0), "line 8");
    }

    #[test]
    fn modes_follow_the_program() {
        let mut t = term(2, 10);
        assert!(!t.modes().bracketed_paste);
        t.feed(b"\x1b[?2004h\x1b[?1h\x1b[?1002h\x1b[?1006h\x1b[?1049h");
        let m = t.modes();
        assert!(m.bracketed_paste && m.app_cursor && m.mouse_report && m.mouse_drag && m.sgr_mouse && m.alt_screen);
    }

    #[test]
    fn replies_to_status_reports() {
        let mut t = term(5, 10);
        t.feed(b"\x1b[3;4H\x1b[6n\x1b]0;my title\x07");
        let r = t.take_requests();
        assert_eq!(r.replies, b"\x1b[3;4R");
        assert_eq!(r.title.as_deref(), Some("my title"));
        assert_eq!(t.take_requests(), Requests::default());
    }

    #[test]
    fn selection_copies_text_and_marks_cells() {
        let mut t = term(2, 20);
        t.feed(b"hello world");
        t.select_start(0, 6, false, 1);
        t.select_update(0, 10, true);
        assert_eq!(t.selection_text().as_deref(), Some("world"));
        let f = t.frame();
        assert_ne!(f.cell(0, 6).attrs & attr::SELECTED, 0);
        assert_eq!(f.cell(0, 5).attrs & attr::SELECTED, 0);
        t.select_start(0, 1, false, 2);
        assert_eq!(t.selection_text().as_deref(), Some("hello"), "a double click selects the word");
        t.select_clear();
        assert_eq!(t.selection_text(), None);
    }

    #[test]
    fn resize_keeps_content_and_reset_clears_it() {
        let mut t = term(5, 20);
        t.feed(b"abc");
        let g = t.generation();
        t.resize(10, 40);
        assert!(t.generation() > g);
        assert_eq!(t.size(), (10, 40));
        assert_eq!(t.frame().row_text(0), "abc");
        t.reset(3, 7);
        assert_eq!(t.size(), (3, 7));
        assert_eq!(t.frame().text(), "\n\n");
    }

    #[test]
    fn sequences_split_across_feeds() {
        let mut t = term(2, 10);
        for b in "\x1b[32mgr€\x1b[0m".as_bytes() {
            t.feed(std::slice::from_ref(b));
        }
        let f = t.frame();
        assert_eq!(f.row_text(0), "gr€");
        assert_eq!(f.cell(0, 0).fg, Palette::dark().ansi[2]);
    }
}
