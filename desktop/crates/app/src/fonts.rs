//! Terminal fonts, found without libfontconfig: `fontdb` scans the font
//! dirs (and reads fonts.conf for them), then each face is loaded into
//! `ab_glyph`. A chain per style: the configured family, other monospace
//! families, then fonts with the symbols agents print (`⏺ ✻ ⎿ ▐▛`), then
//! egui's bundled Hack and Noto Emoji, so a glyph is never missing for
//! lack of a system font.

use ab_glyph::FontArc;

/// `LAZYAGENT_DESKTOP_FONT` (a family name) comes first when set.
pub const FONT_ENV: &str = "LAZYAGENT_DESKTOP_FONT";
pub const FONT_SIZE_ENV: &str = "LAZYAGENT_DESKTOP_FONT_SIZE";
/// The web page's terminal size (terminals.js: 13 px).
pub const DEFAULT_SIZE: f32 = 13.0;

const MONO: &[&str] = &["DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono", "Ubuntu Mono", "FreeMono"];
const SYMBOLS: &[&str] = &[
    "DejaVu Sans",
    "Noto Sans Symbols 2",
    "Noto Sans Symbols",
    "Noto Sans",
    "FreeSerif",
    "FreeSans",
    "Droid Sans Fallback",
    "Noto Sans CJK SC",
];

/// Colour emoji (bitmap) fonts: used for what no outline font has.
const COLOR: &[&str] = &["Noto Color Emoji", "Twemoji", "JoyPixels", "EmojiOne Color"];

#[derive(Clone)]
pub struct FontSet {
    pub regular: Vec<FontArc>,
    pub bold: Vec<FontArc>,
    /// Bitmap (CBDT/sbix) faces, after the outline ones.
    pub color: Vec<FontArc>,
    /// The family of the first regular face, for display.
    pub family: String,
    /// System faces for egui's own text (names, paths, other scripts).
    pub ui_fallbacks: Vec<(String, Vec<u8>)>,
}

/// Every symbol the UI itself draws: all in the bundled Hack or Noto
/// Emoji (tested), so no system font is needed for them.
#[cfg(test)]
pub const UI_SYMBOLS: &str = "●◆↑↓↻✔✖…▸▾◐·─";

/// Fallbacks for the UI text, in order.
const UI_FALLBACKS: &[&str] = &["DejaVu Sans", "Noto Sans Symbols 2", "Noto Sans Symbols", "FreeSans"];

fn raw(db: &fontdb::Database, family: &str) -> Option<Vec<u8>> {
    let query = fontdb::Query { families: &[fontdb::Family::Name(family)], ..fontdb::Query::default() };
    let id = db.query(&query)?;
    // egui takes the first face of a collection only.
    db.with_face_data(id, |data, index| (index == 0).then(|| data.to_vec())).flatten()
}

/// A font file mapped into memory for the life of the process: its pages
/// are the file's (clean, shared, reclaimable), not private memory. Noto
/// Color Emoji alone is 10 MB.
fn mapped(path: &std::path::Path) -> Option<&'static [u8]> {
    use std::collections::HashMap;
    use std::sync::Mutex;
    static MAPS: Mutex<Option<HashMap<std::path::PathBuf, &'static [u8]>>> = Mutex::new(None);
    let mut maps = MAPS.lock().unwrap();
    let maps = maps.get_or_insert_with(HashMap::new);
    if let Some(data) = maps.get(path) {
        return Some(data);
    }
    let file = std::fs::File::open(path).ok()?;
    // SAFETY: font files are not rewritten while in use; a font package
    // upgrade replaces the file (a new inode), leaving this mapping valid.
    let map = unsafe { memmap2::Mmap::map(&file) }.ok()?;
    let data: &'static [u8] = Box::leak(Box::new(map));
    maps.insert(path.to_path_buf(), data);
    Some(data)
}

fn face(db: &fontdb::Database, id: fontdb::ID) -> Option<FontArc> {
    let face = db.face(id)?;
    let path = match &face.source {
        fontdb::Source::File(p) | fontdb::Source::SharedFile(p, _) => p,
        fontdb::Source::Binary(_) => return None,
    };
    let data = mapped(path)?;
    ab_glyph::FontRef::try_from_slice_and_index(data, face.index).ok().map(FontArc::new)
}

fn load(db: &fontdb::Database, family: &str, bold: bool) -> Option<FontArc> {
    let query = fontdb::Query {
        families: &[fontdb::Family::Name(family)],
        weight: if bold { fontdb::Weight::BOLD } else { fontdb::Weight::NORMAL },
        stretch: fontdb::Stretch::Normal,
        style: fontdb::Style::Normal,
    };
    let id = db.query(&query)?;
    // A bold query falls back to the regular face; keep it only if it is bold.
    if bold && db.face(id)?.weight.0 < 600 {
        return None;
    }
    face(db, id)
}

fn bundled() -> Vec<FontArc> {
    [epaint_default_fonts::HACK_REGULAR, epaint_default_fonts::NOTO_EMOJI_REGULAR]
        .iter()
        .filter_map(|bytes| FontArc::try_from_slice(bytes).ok())
        .collect()
}

impl FontSet {
    pub fn discover() -> FontSet {
        let mut db = fontdb::Database::new();
        db.load_system_fonts();
        let wanted = std::env::var(FONT_ENV).ok().filter(|f| !f.is_empty());
        let mut families: Vec<&str> = wanted.iter().map(String::as_str).collect();
        families.extend(MONO.iter().filter(|f| Some(**f) != wanted.as_deref()));
        let mut regular = vec![];
        let mut bold = vec![];
        let mut family = String::new();
        for f in families.iter().chain(SYMBOLS) {
            if let Some(font) = load(&db, f, false) {
                if family.is_empty() {
                    family = f.to_string();
                }
                bold.push(load(&db, f, true).unwrap_or_else(|| font.clone()));
                regular.push(font);
            }
        }
        regular.extend(bundled());
        bold.extend(bundled());
        if family.is_empty() {
            family = "Hack (bundled)".into();
        }
        let ui_fallbacks = UI_FALLBACKS.iter().filter_map(|f| Some((f.to_string(), raw(&db, f)?))).collect();
        let color = COLOR.iter().filter_map(|f| load(&db, f, false)).collect();
        FontSet { regular, bold, color, family, ui_fallbacks }
    }

    /// Only the bundled fonts: deterministic, for tests.
    pub fn bundled_only() -> FontSet {
        FontSet {
            regular: bundled(),
            bold: bundled(),
            color: vec![],
            family: "Hack (bundled)".into(),
            ui_fallbacks: vec![],
        }
    }
}

pub fn font_size() -> f32 {
    std::env::var(FONT_SIZE_ENV)
        .ok()
        .and_then(|s| s.parse::<f32>().ok())
        .filter(|s| (6.0..=48.0).contains(s))
        .unwrap_or(DEFAULT_SIZE)
}

/// egui's fonts plus the system fallbacks, for both families.
pub fn egui_definitions(set: &FontSet) -> eframe::egui::FontDefinitions {
    use eframe::egui::{FontData, FontFamily};
    let mut defs = eframe::egui::FontDefinitions::default();
    let bundled = [
        ("Hack (bundled)".to_string(), epaint_default_fonts::HACK_REGULAR.to_vec()),
        ("Noto Emoji (bundled)".to_string(), epaint_default_fonts::NOTO_EMOJI_REGULAR.to_vec()),
    ];
    for (name, bytes) in set.ui_fallbacks.iter().cloned().chain(bundled) {
        defs.font_data.insert(name.clone(), std::sync::Arc::new(FontData::from_owned(bytes)));
        for family in [FontFamily::Proportional, FontFamily::Monospace] {
            defs.families.entry(family).or_default().push(name.clone());
        }
    }
    defs
}

#[cfg(test)]
mod tests {
    use ab_glyph::Font;

    #[test]
    fn bundled_hack_covers_the_ui_symbols() {
        let hack = ab_glyph::FontRef::try_from_slice(epaint_default_fonts::HACK_REGULAR).unwrap();
        let emoji = ab_glyph::FontRef::try_from_slice(epaint_default_fonts::NOTO_EMOJI_REGULAR).unwrap();
        let missing: String = crate::fonts::UI_SYMBOLS
            .chars()
            .filter(|c| hack.glyph_id(*c).0 == 0 && emoji.glyph_id(*c).0 == 0)
            .collect();
        assert!(missing.is_empty(), "the bundled fonts lack {missing:?}");
    }
}
