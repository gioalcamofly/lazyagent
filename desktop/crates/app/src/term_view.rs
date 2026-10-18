//! The terminal widget: sizes the pane's PTY (claims), turns keys, pastes
//! and the mouse into bytes, keeps a local selection and scrollback view,
//! and draws the pane with the cached meshes.

use std::sync::Arc;
use std::sync::atomic::Ordering;

use eframe::egui::{self, CursorIcon, Event, EventFilter, Key as EKey, Modifiers, Pos2, Rect, Sense, Shape, Ui, Vec2};
use lazyagent_proto::client::Client;
use lazyagent_term::Frame;
use lazyagent_term::input::{self, Key, Mods, MouseAction, MouseButton};

use crate::panes::Pane;
use crate::render::{self, Atlas, MeshKey, Meshes};
use crate::theme::Theme;

const PAD: f32 = 6.0;

/// Per-pane view state the UI keeps between frames.
#[derive(Default)]
pub struct ViewState {
    pub meshes: Meshes,
    pub frame: Frame,
    /// The size this view last asked for.
    last_view: Option<(u16, u16)>,
    had_focus: bool,
    wheel: f32,
    /// A button held for a mouse report (drag reports).
    held: Option<MouseButton>,
    selecting: bool,
    /// An Alt+key sent as ESC+key: drop the text event it also produces.
    skip_text: Option<String>,
}

pub struct Output {
    pub response: egui::Response,
    /// The pane drew new content this frame.
    pub drew: bool,
}

fn mods(m: Modifiers) -> Mods {
    Mods { shift: m.shift, alt: m.alt, ctrl: m.ctrl }
}

fn map_key(key: EKey) -> Option<Key> {
    Some(match key {
        EKey::Enter => Key::Enter,
        EKey::Tab => Key::Tab,
        EKey::Backspace => Key::Backspace,
        EKey::Escape => Key::Escape,
        EKey::ArrowUp => Key::Up,
        EKey::ArrowDown => Key::Down,
        EKey::ArrowLeft => Key::Left,
        EKey::ArrowRight => Key::Right,
        EKey::Home => Key::Home,
        EKey::End => Key::End,
        EKey::PageUp => Key::PageUp,
        EKey::PageDown => Key::PageDown,
        EKey::Insert => Key::Insert,
        EKey::Delete => Key::Delete,
        EKey::Space => Key::Char(' '),
        EKey::F1 => Key::F(1),
        EKey::F2 => Key::F(2),
        EKey::F3 => Key::F(3),
        EKey::F4 => Key::F(4),
        EKey::F5 => Key::F(5),
        EKey::F6 => Key::F(6),
        EKey::F7 => Key::F(7),
        EKey::F8 => Key::F(8),
        EKey::F9 => Key::F(9),
        EKey::F10 => Key::F(10),
        EKey::F11 => Key::F(11),
        EKey::F12 => Key::F(12),
        other => {
            let s = other.symbol_or_name();
            let mut chars = s.chars();
            match (chars.next(), chars.next()) {
                (Some(c), None) => Key::Char(c.to_ascii_lowercase()),
                _ => return None,
            }
        }
    })
}

/// The bytes a key press sends, or `None` when it is not the terminal's
/// (plain printable keys arrive as text events instead).
fn key_bytes(key: EKey, m: Modifiers, modes: input::Modes) -> Option<Vec<u8>> {
    let k = map_key(key)?;
    if let Key::Char(_) = k
        && !m.ctrl
        && !m.alt
    {
        return None;
    }
    input::encode_key(k, mods(m), modes)
}

/// Cell under `pos` (and whether it is in the cell's right half).
fn cell_at(pos: Pos2, origin: Pos2, cw: f32, ch: f32, rows: usize, cols: usize) -> (usize, usize, bool) {
    let x = ((pos.x - origin.x) / cw).max(0.0);
    let y = ((pos.y - origin.y) / ch).max(0.0);
    let col = (x as usize).min(cols.saturating_sub(1));
    let row = (y as usize).min(rows.saturating_sub(1));
    (row, col, x.fract() >= 0.5)
}

#[allow(clippy::too_many_arguments)]
pub fn show(
    ui: &mut Ui,
    pane: &Arc<Pane>,
    view: &mut ViewState,
    atlas: &mut Atlas,
    client: Option<&Client>,
    theme: &Theme,
    focus_now: bool,
    window_focused: bool,
) -> Output {
    let rect = ui.available_rect_before_wrap();
    let id = ui.scope_id().with(("terminal", &pane.session_id));
    let response = ui.interact(rect, id, Sense::click_and_drag());
    if focus_now || response.clicked() || response.drag_started() {
        response.request_focus();
    }
    let focused = response.has_focus();
    if focused {
        ui.memory_mut(|m| {
            m.set_focus_lock_filter(
                id,
                EventFilter { tab: true, horizontal_arrows: true, vertical_arrows: true, escape: true },
            )
        });
    }
    pane.visible.store(true, Ordering::Relaxed);
    pane.focused.store(focused && window_focused, Ordering::Relaxed);

    let ctx = ui.ctx().clone();
    let ppp = ctx.pixels_per_point();
    let (cw, ch) = (atlas.cell_w / ppp, atlas.cell_h / ppp);
    let inner = rect.shrink(PAD);
    let view_cols = ((inner.width() * ppp) / atlas.cell_w).floor().max(2.0) as u16;
    let view_rows = ((inner.height() * ppp) / atlas.cell_h).floor().max(1.0) as u16;
    let origin = Pos2::new((inner.min.x * ppp).round() / ppp, (inner.min.y * ppp).round() / ppp);

    let (pty_rows, pty_cols, attached, modes, has_selection) = {
        let i = pane.inner.lock().unwrap();
        let (r, c) = i.term.size();
        (r, c, i.stream_id.is_some(), i.term.modes(), i.term.selection_text().is_some())
    };
    let view_size = (view_rows, view_cols);
    let pty_size = (pty_rows as u16, pty_cols as u16);

    // Size ownership (D3 "latest"): claim on focus and on a box change
    // while focused; typing with another size claims first (below).
    if let Some(c) = client
        && attached
        && focused
        && (!view.had_focus || view.last_view != Some(view_size))
        && view_size != pty_size
    {
        pane.claim(c, view_rows, view_cols);
    }
    view.had_focus = focused;
    view.last_view = Some(view_size);

    let mut out = Vec::new();
    if focused {
        let events = ui.input(|i| i.events.clone());
        let mut copy = None;
        for event in &events {
            match event {
                Event::Text(t) => {
                    if view.skip_text.take().as_deref() == Some(t.as_str()) {
                        continue;
                    }
                    out.extend_from_slice(t.as_bytes());
                }
                Event::Key { key, pressed: true, modifiers, .. } => {
                    view.skip_text = None;
                    // Ctrl+Shift+C / V: copy and paste, as in most terminals.
                    if modifiers.ctrl && modifiers.shift && *key == EKey::C {
                        copy = pane.inner.lock().unwrap().term.selection_text();
                        continue;
                    }
                    if (modifiers.ctrl && modifiers.shift && *key == EKey::V)
                        || (modifiers.shift && *key == EKey::Insert)
                    {
                        if let Some(text) = clipboard_text() {
                            out.extend(input::encode_paste(&text, modes));
                        }
                        continue;
                    }
                    if let Some(bytes) = key_bytes(*key, *modifiers, modes) {
                        if modifiers.alt
                            && !modifiers.ctrl
                            && let Some(Key::Char(c)) = map_key(*key)
                        {
                            view.skip_text = Some(if modifiers.shift { c.to_ascii_uppercase() } else { c }.to_string());
                        }
                        out.extend(bytes);
                    }
                }
                // egui turns Ctrl+C/X/V into these.
                Event::Copy => {
                    if has_selection {
                        copy = pane.inner.lock().unwrap().term.selection_text();
                        pane.inner.lock().unwrap().term.select_clear();
                    } else {
                        out.push(0x03);
                    }
                }
                Event::Cut => out.push(0x18),
                Event::Paste(text) => out.extend(input::encode_paste(text, modes)),
                // Only an image on the clipboard: let the agent fetch it (Claude Code reads ^V).
                Event::PasteImage(_) => out.push(0x16),
                _ => {}
            }
        }
        if let Some(text) = copy {
            ctx.copy_text(text);
        }
    }

    // Mouse: reports for programs that asked (unless Shift), else selection
    // and scrollback.
    let (rows, cols) = (pty_rows, pty_cols);
    let hovered = response.hovered() || view.selecting || view.held.is_some();
    if hovered {
        let events = ui.input(|i| i.events.clone());
        for event in &events {
            match event {
                Event::PointerButton { pos, button, pressed, modifiers } if rect.contains(*pos) || !pressed => {
                    let (row, col, right) = cell_at(*pos, origin, cw, ch, rows, cols);
                    let b = match button {
                        egui::PointerButton::Primary => MouseButton::Left,
                        egui::PointerButton::Middle => MouseButton::Middle,
                        egui::PointerButton::Secondary => MouseButton::Right,
                        _ => continue,
                    };
                    if modes.mouse_report && !modifiers.shift {
                        let action = if *pressed { MouseAction::Press } else { MouseAction::Release };
                        if let Some(bytes) = input::encode_mouse(b, action, row, col, mods(*modifiers), modes, false) {
                            out.extend(bytes);
                        }
                        view.held = if *pressed { Some(b) } else { None };
                    } else if b == MouseButton::Left {
                        let mut i = pane.inner.lock().unwrap();
                        if *pressed {
                            let clicks = if response.triple_clicked() {
                                3
                            } else if response.double_clicked() {
                                2
                            } else {
                                1
                            };
                            if clicks == 1 {
                                i.term.select_clear();
                            }
                            i.term.select_start(row, col, right, clicks);
                            view.selecting = true;
                        } else {
                            view.selecting = false;
                        }
                    } else if b == MouseButton::Middle
                        && *pressed
                        && let Some(text) = clipboard_text()
                    {
                        out.extend(input::encode_paste(&text, modes));
                    }
                }
                Event::PointerMoved(pos) => {
                    let (row, col, right) = cell_at(*pos, origin, cw, ch, rows, cols);
                    if view.selecting {
                        pane.inner.lock().unwrap().term.select_update(row, col, right);
                    } else if modes.mouse_report && rect.contains(*pos) {
                        let b = view.held.unwrap_or(MouseButton::None);
                        let m = ui.input(|i| i.modifiers);
                        if let Some(bytes) =
                            input::encode_mouse(b, MouseAction::Motion, row, col, mods(m), modes, view.held.is_some())
                        {
                            out.extend(bytes);
                        }
                    }
                }
                Event::MouseWheel { unit, delta, modifiers, .. } if response.hovered() => {
                    let lines = match unit {
                        egui::MouseWheelUnit::Point => delta.y / ch,
                        egui::MouseWheelUnit::Line => delta.y,
                        egui::MouseWheelUnit::Page => delta.y * rows as f32,
                    };
                    view.wheel += lines;
                    let whole = view.wheel.trunc() as i32;
                    view.wheel -= whole as f32;
                    if whole == 0 {
                        continue;
                    }
                    let pos = ui.input(|i| i.pointer.hover_pos()).unwrap_or(origin);
                    let (row, col, _) = cell_at(pos, origin, cw, ch, rows, cols);
                    if modes.mouse_report && !modifiers.shift {
                        let b = if whole > 0 { MouseButton::WheelUp } else { MouseButton::WheelDown };
                        for _ in 0..whole.unsigned_abs().min(10) {
                            if let Some(bytes) =
                                input::encode_mouse(b, MouseAction::Press, row, col, mods(*modifiers), modes, false)
                            {
                                out.extend(bytes);
                            }
                        }
                    } else if modes.alt_screen && modes.alternate_scroll {
                        let k = if whole > 0 { Key::Up } else { Key::Down };
                        for _ in 0..whole.unsigned_abs().min(10) {
                            out.extend(input::encode_key(k, Mods::NONE, modes).unwrap());
                        }
                    } else {
                        pane.inner.lock().unwrap().term.scroll(whole * 3);
                    }
                }
                _ => {}
            }
        }
        if !modes.mouse_report || ui.input(|i| i.modifiers.shift) {
            ctx.set_cursor_icon(CursorIcon::Text);
        }
    }

    if !out.is_empty() {
        // Typing with a size other than the PTY's claims it first; the
        // bytes wait for the answer (Pane::write holds them).
        if let Some(c) = client
            && view_size != pty_size
        {
            pane.claim(c, view_rows, view_cols);
        }
        pane.write(&out);
    }

    // Draw.
    let painter = ui.painter_at(rect);
    let palette_bg = {
        let i = pane.inner.lock().unwrap();
        i.term.palette().background
    };
    painter.rect_filled(rect, 0.0, render::rgb(palette_bg));
    let key = {
        let i = pane.inner.lock().unwrap();
        MeshKey {
            generation: i.term.generation(),
            atlas: atlas.generation,
            origin: ((origin.x * ppp) as i32, (origin.y * ppp) as i32),
            ppp: ppp.to_bits(),
            focused: focused && window_focused,
        }
    };
    let mut drew = false;
    let texture = atlas.upload(&ctx);
    if view.meshes.key != Some(key) {
        let (cursor_color, closed) = {
            let i = pane.inner.lock().unwrap();
            i.term.render(&mut view.frame);
            (i.term.palette().cursor, i.closed.is_some())
        };
        if closed {
            view.frame.cursor = None;
        }
        let (bg, mut fg) = render::build(&view.frame, origin, ppp, atlas, key.focused, cursor_color);
        fg.texture_id = texture;
        view.meshes = Meshes { key: Some(key), background: Arc::new(bg), glyphs: Arc::new(fg) };
        drew = true;
        // Building may have rasterised new glyphs (and cleared the atlas).
        if atlas.generation != key.atlas {
            view.meshes.key = None;
        }
    }
    // New glyphs of this build go up before the frame is painted.
    atlas.upload(&ctx);
    painter.add(Shape::mesh(view.meshes.background.clone()));
    painter.add(Shape::mesh(view.meshes.glyphs.clone()));

    // Overlays: scrolled back, attaching, closed.
    let i = pane.inner.lock().unwrap();
    let offset = i.term.display_offset();
    let small = egui::FontId::proportional(11.0);
    if offset > 0 {
        let text = format!("↑ {offset} lines  ·  type or scroll down to return");
        let galley = painter.layout_no_wrap(text, small.clone(), theme.text);
        let r = Rect::from_min_size(
            Pos2::new(rect.right() - galley.size().x - 18.0, rect.top() + 6.0),
            galley.size() + Vec2::new(12.0, 6.0),
        );
        painter.rect_filled(r, 4.0, theme.panel);
        painter.galley(r.min + Vec2::new(6.0, 3.0), galley, theme.text);
    }
    let note = match (&i.closed, i.attaching && i.received.is_none(), &i.error) {
        (Some(code), _, _) => Some(match code {
            Some(c) => format!("[the session ended, exit code {c}]"),
            None => "[the session ended]".to_string(),
        }),
        (None, true, _) => Some("Attaching…".to_string()),
        (None, false, Some(e)) if i.stream_id.is_none() => Some(format!("Not attached: {e}")),
        _ => None,
    };
    drop(i);
    if let Some(note) = note {
        let galley = painter.layout_no_wrap(note, small, theme.muted);
        let r = Rect::from_min_size(
            Pos2::new(rect.left() + 8.0, rect.bottom() - galley.size().y - 14.0),
            galley.size() + Vec2::new(12.0, 6.0),
        );
        painter.rect_filled(r, 4.0, theme.panel);
        painter.galley(r.min + Vec2::new(6.0, 3.0), galley, theme.muted);
    }
    Output { response, drew }
}

fn clipboard_text() -> Option<String> {
    arboard::Clipboard::new().ok()?.get_text().ok().filter(|t| !t.is_empty())
}
