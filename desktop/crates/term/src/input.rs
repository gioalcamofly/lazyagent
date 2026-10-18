//! Keys, mouse and pastes → the bytes a program in an xterm expects.

/// The terminal modes input encoding depends on.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Modes {
    pub app_cursor: bool,
    pub bracketed_paste: bool,
    /// Any mouse reporting mode is on (1000/1002/1003).
    pub mouse_report: bool,
    /// 1003: report every motion.
    pub mouse_motion: bool,
    /// 1002: report motion while a button is held.
    pub mouse_drag: bool,
    /// 1006: SGR encoding.
    pub sgr_mouse: bool,
    pub alt_screen: bool,
    /// 1007: the wheel sends arrow keys on the alternate screen.
    pub alternate_scroll: bool,
    pub focus_reports: bool,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Mods {
    pub shift: bool,
    pub alt: bool,
    pub ctrl: bool,
}

impl Mods {
    pub const NONE: Mods = Mods { shift: false, alt: false, ctrl: false };

    /// xterm's modifier parameter: 1 + shift + 2·alt + 4·ctrl.
    fn param(self) -> u8 {
        1 + self.shift as u8 + 2 * self.alt as u8 + 4 * self.ctrl as u8
    }

    fn any(self) -> bool {
        self.shift || self.alt || self.ctrl
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Key {
    /// A printable key, for Ctrl/Alt combinations (plain text arrives as text).
    Char(char),
    Enter,
    Tab,
    Backspace,
    Escape,
    Up,
    Down,
    Left,
    Right,
    Home,
    End,
    PageUp,
    PageDown,
    Insert,
    Delete,
    /// F1-F12.
    F(u8),
}

fn csi_mod(final_byte: char, mods: Mods) -> Vec<u8> {
    format!("\x1b[1;{}{final_byte}", mods.param()).into_bytes()
}

fn tilde(code: u8, mods: Mods) -> Vec<u8> {
    if mods.any() { format!("\x1b[{code};{}~", mods.param()) } else { format!("\x1b[{code}~") }.into_bytes()
}

fn alt_prefixed(alt: bool, bytes: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(bytes.len() + 1);
    if alt {
        out.push(0x1b);
    }
    out.extend_from_slice(bytes);
    out
}

/// The control byte of Ctrl+`c`, as xterm sends it.
pub fn ctrl_byte(c: char) -> Option<u8> {
    Some(match c.to_ascii_lowercase() {
        c @ 'a'..='z' => c as u8 - b'a' + 1,
        '@' | ' ' | '2' => 0,
        '[' | '3' => 0x1b,
        '\\' | '4' => 0x1c,
        ']' | '5' => 0x1d,
        '^' | '6' => 0x1e,
        '_' | '/' | '-' | '7' => 0x1f,
        '?' | '8' => 0x7f,
        _ => return None,
    })
}

/// Bytes for a key press, or `None` when the key sends nothing.
pub fn encode_key(key: Key, mods: Mods, modes: Modes) -> Option<Vec<u8>> {
    let cursor = |c: char| {
        Some(if mods.any() {
            csi_mod(c, mods)
        } else if modes.app_cursor {
            format!("\x1bO{c}").into_bytes()
        } else {
            format!("\x1b[{c}").into_bytes()
        })
    };
    match key {
        Key::Char(c) => {
            if mods.ctrl {
                return ctrl_byte(c).map(|b| alt_prefixed(mods.alt, &[b]));
            }
            let mut buf = [0u8; 4];
            Some(alt_prefixed(mods.alt, c.encode_utf8(&mut buf).as_bytes()))
        }
        // Shift+Enter is Alt+Enter (ESC CR): agents such as Claude Code
        // take it as a newline in the prompt instead of a submit.
        Key::Enter => Some(alt_prefixed(mods.alt || mods.shift, b"\r")),
        Key::Tab if mods.shift => Some(b"\x1b[Z".to_vec()),
        Key::Tab => Some(alt_prefixed(mods.alt, b"\t")),
        Key::Backspace if mods.ctrl => Some(alt_prefixed(mods.alt, b"\x08")),
        Key::Backspace => Some(alt_prefixed(mods.alt, b"\x7f")),
        Key::Escape => Some(alt_prefixed(mods.alt, b"\x1b")),
        Key::Up => cursor('A'),
        Key::Down => cursor('B'),
        Key::Right => cursor('C'),
        Key::Left => cursor('D'),
        Key::Home => cursor('H'),
        Key::End => cursor('F'),
        Key::Insert => Some(tilde(2, mods)),
        Key::Delete => Some(tilde(3, mods)),
        Key::PageUp => Some(tilde(5, mods)),
        Key::PageDown => Some(tilde(6, mods)),
        Key::F(n @ 1..=4) => {
            let c = (b'P' + n - 1) as char;
            Some(if mods.any() { csi_mod(c, mods) } else { format!("\x1bO{c}").into_bytes() })
        }
        Key::F(n @ 5..=12) => Some(tilde([15, 17, 18, 19, 20, 21, 23, 24][n as usize - 5], mods)),
        Key::F(_) => None,
    }
}

/// Text pasted into the program: bracketed when it asked for it (and any
/// end marker inside the text dropped, so the paste cannot end early),
/// else with newlines as carriage returns, as a terminal does.
pub fn encode_paste(text: &str, modes: Modes) -> Vec<u8> {
    if modes.bracketed_paste {
        let clean = text.replace("\x1b[201~", "");
        let mut out = Vec::with_capacity(clean.len() + 12);
        out.extend_from_slice(b"\x1b[200~");
        out.extend_from_slice(clean.as_bytes());
        out.extend_from_slice(b"\x1b[201~");
        out
    } else {
        text.replace("\r\n", "\r").replace('\n', "\r").into_bytes()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MouseButton {
    Left,
    Middle,
    Right,
    WheelUp,
    WheelDown,
    /// Motion with no button held (mode 1003).
    None,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MouseAction {
    Press,
    Release,
    Motion,
}

/// A mouse report for the program (0-based cell), or `None` when its
/// modes do not ask for this event.
pub fn encode_mouse(
    button: MouseButton,
    action: MouseAction,
    row: usize,
    col: usize,
    mods: Mods,
    modes: Modes,
    held: bool,
) -> Option<Vec<u8>> {
    if !modes.mouse_report {
        return None;
    }
    if action == MouseAction::Motion && !(modes.mouse_motion || (modes.mouse_drag && held)) {
        return None;
    }
    let mut code: u32 = match button {
        MouseButton::Left => 0,
        MouseButton::Middle => 1,
        MouseButton::Right => 2,
        MouseButton::None => 3,
        MouseButton::WheelUp => 64,
        MouseButton::WheelDown => 65,
    };
    if action == MouseAction::Motion {
        code += 32;
    }
    code += 4 * mods.shift as u32 + 8 * mods.alt as u32 + 16 * mods.ctrl as u32;
    let (x, y) = (col + 1, row + 1);
    if modes.sgr_mouse {
        let end = if action == MouseAction::Release { 'm' } else { 'M' };
        return Some(format!("\x1b[<{code};{x};{y}{end}").into_bytes());
    }
    // X10 encoding: a release is button 3; coordinates stop at 223.
    if action == MouseAction::Release {
        code = 3 + (code & !3);
    }
    if x > 223 || y > 223 {
        return None;
    }
    Some(vec![0x1b, b'[', b'M', 32 + code as u8, 32 + x as u8, 32 + y as u8])
}

#[cfg(test)]
mod tests {
    use super::*;

    const CTRL: Mods = Mods { shift: false, alt: false, ctrl: true };
    const ALT: Mods = Mods { shift: false, alt: true, ctrl: false };
    const SHIFT: Mods = Mods { shift: true, alt: false, ctrl: false };

    fn key(k: Key, m: Mods) -> Vec<u8> {
        encode_key(k, m, Modes::default()).unwrap()
    }

    #[test]
    fn control_and_meta() {
        assert_eq!(key(Key::Char('c'), CTRL), b"\x03");
        assert_eq!(key(Key::Char('D'), CTRL), b"\x04");
        assert_eq!(key(Key::Char('['), CTRL), b"\x1b");
        assert_eq!(key(Key::Char(' '), CTRL), b"\x00");
        assert_eq!(key(Key::Char('b'), ALT), b"\x1bb");
        assert_eq!(key(Key::Char('x'), Mods { ctrl: true, alt: true, shift: false }), b"\x1b\x18");
        assert_eq!(key(Key::Enter, Mods::NONE), b"\r");
        assert_eq!(key(Key::Enter, SHIFT), b"\x1b\r");
        assert_eq!(key(Key::Tab, SHIFT), b"\x1b[Z");
        assert_eq!(key(Key::Backspace, Mods::NONE), b"\x7f");
        assert_eq!(key(Key::Backspace, ALT), b"\x1b\x7f");
    }

    #[test]
    fn cursor_keys_follow_decckm_and_modifiers() {
        assert_eq!(key(Key::Up, Mods::NONE), b"\x1b[A");
        let app = Modes { app_cursor: true, ..Modes::default() };
        assert_eq!(encode_key(Key::Up, Mods::NONE, app).unwrap(), b"\x1bOA");
        assert_eq!(encode_key(Key::Left, CTRL, app).unwrap(), b"\x1b[1;5D");
        assert_eq!(key(Key::Home, SHIFT), b"\x1b[1;2H");
        assert_eq!(key(Key::PageUp, Mods::NONE), b"\x1b[5~");
        assert_eq!(key(Key::Delete, CTRL), b"\x1b[3;5~");
        assert_eq!(key(Key::F(1), Mods::NONE), b"\x1bOP");
        assert_eq!(key(Key::F(5), Mods::NONE), b"\x1b[15~");
        assert_eq!(key(Key::F(12), SHIFT), b"\x1b[24;2~");
    }

    #[test]
    fn pastes() {
        let plain = Modes::default();
        assert_eq!(encode_paste("a\nb\r\nc", plain), b"a\rb\rc");
        let bracketed = Modes { bracketed_paste: true, ..Modes::default() };
        assert_eq!(encode_paste("x\x1b[201~y", bracketed), b"\x1b[200~xy\x1b[201~");
    }

    #[test]
    fn mouse_reports() {
        let off = Modes::default();
        assert_eq!(encode_mouse(MouseButton::Left, MouseAction::Press, 0, 0, Mods::NONE, off, false), None);
        let sgr = Modes { mouse_report: true, sgr_mouse: true, ..Modes::default() };
        let m = |b, a, held| encode_mouse(b, a, 4, 9, Mods::NONE, sgr, held);
        assert_eq!(m(MouseButton::Left, MouseAction::Press, false).unwrap(), b"\x1b[<0;10;5M");
        assert_eq!(m(MouseButton::Left, MouseAction::Release, false).unwrap(), b"\x1b[<0;10;5m");
        assert_eq!(m(MouseButton::WheelDown, MouseAction::Press, false).unwrap(), b"\x1b[<65;10;5M");
        assert_eq!(m(MouseButton::Left, MouseAction::Motion, true), None, "no motion without 1002/1003");
        let drag = Modes { mouse_drag: true, ..sgr };
        assert_eq!(
            encode_mouse(MouseButton::Left, MouseAction::Motion, 4, 9, Mods::NONE, drag, true).unwrap(),
            b"\x1b[<32;10;5M"
        );
        let x10 = Modes { mouse_report: true, ..Modes::default() };
        assert_eq!(
            encode_mouse(MouseButton::Right, MouseAction::Press, 0, 0, CTRL, x10, false).unwrap(),
            vec![0x1b, b'[', b'M', 32 + 2 + 16, 33, 33]
        );
        assert_eq!(
            encode_mouse(MouseButton::Left, MouseAction::Release, 0, 0, Mods::NONE, x10, false).unwrap(),
            vec![0x1b, b'[', b'M', 35, 33, 33]
        );
    }
}
