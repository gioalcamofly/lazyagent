//! The web page's design tokens (app.css `:root` / `[data-theme=light]`),
//! applied to egui.

use eframe::egui::{self, Color32, CornerRadius, FontFamily, FontId, Stroke, TextStyle, Visuals};
use lazyagent_term::Palette;

use crate::store::Tone;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ThemeKind {
    Dark,
    Light,
}

#[derive(Debug, Clone, Copy)]
pub struct Theme {
    pub kind: ThemeKind,
    pub bg: Color32,
    pub surface: Color32,
    pub panel: Color32,
    pub hover: Color32,
    pub border: Color32,
    pub text: Color32,
    pub muted: Color32,
    pub primary: Color32,
    pub accent: Color32,
    pub current_bg: Color32,
    pub green: Color32,
    pub yellow: Color32,
    pub cyan: Color32,
    pub red: Color32,
    pub purple: Color32,
}

const fn hex(v: u32) -> Color32 {
    Color32::from_rgb((v >> 16) as u8, (v >> 8) as u8, v as u8)
}

impl Theme {
    pub const DARK: Theme = Theme {
        kind: ThemeKind::Dark,
        bg: hex(0x121212),
        surface: hex(0x1e1e1e),
        panel: hex(0x262626),
        hover: hex(0x333333),
        border: hex(0x333333),
        text: hex(0xe0e0e0),
        muted: hex(0x8a8a8a),
        primary: hex(0x0178d4),
        accent: hex(0xffa62b),
        current_bg: hex(0x2b2416),
        green: hex(0x4ebf71),
        yellow: hex(0xffcc4d),
        cyan: hex(0x4fc1e9),
        red: hex(0xe5534b),
        purple: hex(0xb083f0),
    };

    pub const LIGHT: Theme = Theme {
        kind: ThemeKind::Light,
        bg: hex(0xf6f6f6),
        surface: hex(0xffffff),
        panel: hex(0xececec),
        hover: hex(0xe2e2e2),
        border: hex(0xd0d0d0),
        text: hex(0x1f1f1f),
        muted: hex(0x5f5f5f),
        primary: hex(0x0063b1),
        accent: hex(0xa35a00),
        current_bg: hex(0xfbefdc),
        green: hex(0x1a7f37),
        yellow: hex(0x8a5a00),
        cyan: hex(0x0b6e82),
        red: hex(0xc4262e),
        purple: hex(0x7c4dcc),
    };

    pub fn of(kind: ThemeKind) -> Theme {
        match kind {
            ThemeKind::Dark => Theme::DARK,
            ThemeKind::Light => Theme::LIGHT,
        }
    }

    pub fn palette(&self) -> Palette {
        match self.kind {
            ThemeKind::Dark => Palette::dark(),
            ThemeKind::Light => Palette::light(),
        }
    }

    pub fn tone(&self, tone: Tone) -> Color32 {
        match tone {
            Tone::Running => self.green,
            Tone::Waiting => self.yellow,
            Tone::Done => self.cyan,
            Tone::Bad => self.red,
            Tone::Idle => self.muted,
        }
    }

    pub fn apply(&self, ctx: &egui::Context) {
        let mut v = match self.kind {
            ThemeKind::Dark => Visuals::dark(),
            ThemeKind::Light => Visuals::light(),
        };
        v.panel_fill = self.surface;
        v.window_fill = self.surface;
        v.extreme_bg_color = self.bg;
        // Fields must stand out from the main area, which is `bg` too.
        v.text_edit_bg_color = Some(self.panel);
        v.faint_bg_color = self.panel;
        v.override_text_color = Some(self.text);
        v.hyperlink_color = self.primary;
        v.selection.bg_fill = self.primary.linear_multiply(0.5);
        v.selection.stroke = Stroke::new(1.0, self.text);
        v.window_stroke = Stroke::new(1.0, self.border);
        v.widgets.noninteractive.bg_stroke = Stroke::new(1.0, self.border);
        v.widgets.noninteractive.fg_stroke = Stroke::new(1.0, self.text);
        for w in [&mut v.widgets.inactive, &mut v.widgets.hovered, &mut v.widgets.active, &mut v.widgets.open] {
            w.corner_radius = CornerRadius::same(4);
        }
        v.widgets.inactive.weak_bg_fill = self.panel;
        v.widgets.inactive.bg_fill = self.panel;
        v.widgets.hovered.weak_bg_fill = self.hover;
        v.widgets.hovered.bg_fill = self.hover;
        ctx.set_visuals(v);
        ctx.all_styles_mut(|s| {
            s.text_styles.insert(TextStyle::Body, FontId::new(13.0, FontFamily::Proportional));
            s.text_styles.insert(TextStyle::Button, FontId::new(13.0, FontFamily::Proportional));
            s.text_styles.insert(TextStyle::Small, FontId::new(11.0, FontFamily::Proportional));
            s.text_styles.insert(TextStyle::Monospace, FontId::new(12.0, FontFamily::Monospace));
            s.text_styles.insert(TextStyle::Heading, FontId::new(15.0, FontFamily::Proportional));
            s.spacing.item_spacing = egui::vec2(6.0, 4.0);
            s.spacing.button_padding = egui::vec2(8.0, 3.0);
        });
    }
}
