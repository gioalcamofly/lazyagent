//! Colours: the 16 ANSI colours, the theme's foreground/background, and the
//! xterm 256-colour cube. Same values as the web page's xterm.js (its
//! default Tango ANSI set; foreground, background and selection from the
//! page's theme tokens).

use alacritty_terminal::term::color::Colors;
use alacritty_terminal::vte::ansi::{Color, NamedColor};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub struct Rgb(pub u8, pub u8, pub u8);

impl Rgb {
    pub const fn hex(v: u32) -> Rgb {
        Rgb((v >> 16) as u8, (v >> 8) as u8, v as u8)
    }

    /// `self` drawn over `under` with `alpha` (0-255).
    pub fn over(self, under: Rgb, alpha: u8) -> Rgb {
        let mix = |a: u8, b: u8| ((a as u32 * alpha as u32 + b as u32 * (255 - alpha as u32)) / 255) as u8;
        Rgb(mix(self.0, under.0), mix(self.1, under.1), mix(self.2, under.2))
    }

    fn dim(self) -> Rgb {
        Rgb((self.0 as u32 * 2 / 3) as u8, (self.1 as u32 * 2 / 3) as u8, (self.2 as u32 * 2 / 3) as u8)
    }
}

const TANGO: [u32; 16] = [
    0x2e3436, 0xcc0000, 0x4e9a06, 0xc4a000, 0x3465a4, 0x75507b, 0x06989a, 0xd3d7cf, 0x555753, 0xef2929, 0x8ae234,
    0xfce94f, 0x729fcf, 0xad7fa8, 0x34e2e2, 0xeeeeec,
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Palette {
    pub ansi: [Rgb; 16],
    pub foreground: Rgb,
    pub background: Rgb,
    pub cursor: Rgb,
    pub selection: Rgb,
    /// Bold text in colours 0-7 uses the bright variant (xterm.js does).
    pub bold_is_bright: bool,
}

impl Default for Palette {
    fn default() -> Self {
        Palette::dark()
    }
}

impl Palette {
    fn with(fg: u32, bg: u32, selection: u32, selection_alpha: u8) -> Palette {
        let (foreground, background) = (Rgb::hex(fg), Rgb::hex(bg));
        Palette {
            ansi: TANGO.map(Rgb::hex),
            foreground,
            background,
            cursor: foreground,
            selection: Rgb::hex(selection).over(background, selection_alpha),
            bold_is_bright: true,
        }
    }

    /// `--term-*` tokens of the dark theme (app.css).
    pub fn dark() -> Palette {
        Palette::with(0xe0e0e0, 0x121212, 0x0178d4, 0x66)
    }

    pub fn light() -> Palette {
        Palette::with(0x1f1f1f, 0xfbfbfb, 0x0063b1, 0x40)
    }

    /// Colour `index` of the 256 (plus 256 = fg, 257 = bg, 258 = cursor).
    pub fn indexed(&self, index: usize) -> Rgb {
        match index {
            0..=15 => self.ansi[index],
            16..=231 => {
                let i = index - 16;
                let level = |v: usize| if v == 0 { 0 } else { (55 + v * 40) as u8 };
                Rgb(level(i / 36), level((i / 6) % 6), level(i % 6))
            }
            232..=255 => {
                let v = (8 + (index - 232) * 10) as u8;
                Rgb(v, v, v)
            }
            256 => self.foreground,
            258 => self.cursor,
            _ => self.background,
        }
    }

    /// A cell colour, with the program's overrides (OSC 4/10/11) first.
    pub fn resolve(&self, color: Color, overrides: &Colors, bold: bool, dim: bool, is_fg: bool) -> Rgb {
        let from = |index: usize| overrides[index].map(|c| Rgb(c.r, c.g, c.b)).unwrap_or_else(|| self.indexed(index));
        let rgb = match color {
            Color::Spec(c) => Rgb(c.r, c.g, c.b),
            Color::Indexed(i) => {
                let i = i as usize;
                from(if bold && is_fg && self.bold_is_bright && i < 8 { i + 8 } else { i })
            }
            Color::Named(n) => {
                let i = n as usize;
                match n {
                    NamedColor::Foreground | NamedColor::BrightForeground => from(256),
                    NamedColor::Background => from(257),
                    NamedColor::Cursor => from(258),
                    NamedColor::DimForeground => from(256).dim(),
                    _ if i < 8 && bold && is_fg && self.bold_is_bright => from(i + 8),
                    _ if i < 16 => from(i),
                    // Dim black … dim white.
                    _ => from(i.saturating_sub(259) % 8).dim(),
                }
            }
        };
        if dim && is_fg { rgb.dim() } else { rgb }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cube_and_grey_ramp_match_xterm() {
        let p = Palette::dark();
        assert_eq!(p.indexed(16), Rgb(0, 0, 0));
        assert_eq!(p.indexed(196), Rgb(255, 0, 0));
        assert_eq!(p.indexed(67), Rgb(95, 135, 175));
        assert_eq!(p.indexed(232), Rgb(8, 8, 8));
        assert_eq!(p.indexed(255), Rgb(238, 238, 238));
    }

    #[test]
    fn selection_is_blended_over_the_background() {
        let p = Palette::dark();
        assert_eq!(p.selection, Rgb::hex(0x0178d4).over(Rgb::hex(0x121212), 0x66));
        assert_ne!(p.selection, p.background);
    }

    #[test]
    fn bold_brightens_only_the_first_eight_foreground_colours() {
        let p = Palette::dark();
        let none = Colors::default();
        let red = Color::Named(NamedColor::Red);
        assert_eq!(p.resolve(red, &none, true, false, true), p.ansi[9]);
        assert_eq!(p.resolve(red, &none, true, false, false), p.ansi[1]);
        assert_eq!(p.resolve(Color::Indexed(100), &none, true, false, true), p.indexed(100));
    }
}
