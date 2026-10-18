//! The terminal renderer: a glyph atlas rasterised on demand with
//! `ab_glyph` at the exact pixel size, and per terminal two meshes (merged
//! background runs, then glyphs and decorations) that egui draws in one
//! call each. Cells sit on whole pixels, so glyphs stay crisp.

use std::collections::HashMap;
use std::sync::Arc;

use ab_glyph::{Font, PxScale, ScaleFont};
use eframe::egui::{self, Color32, ColorImage, Mesh, Pos2, Rect, TextureHandle, TextureOptions, Vec2};
use lazyagent_term::{CursorKind, Frame, Rgb, attr};

use crate::fonts::FontSet;

const ATLAS: usize = 1024;

#[derive(Clone, Copy)]
struct Slot {
    uv: Rect,
    /// From the cell's top-left, in physical pixels.
    offset: Vec2,
    size: Vec2,
}

/// Glyphs of one font set at one pixel size.
pub struct Atlas {
    fonts: FontSet,
    pub px: f32,
    /// Cell size in physical pixels (whole numbers).
    pub cell_w: f32,
    pub cell_h: f32,
    ascent: f32,
    texture: Option<TextureHandle>,
    glyphs: HashMap<(char, bool), Option<Slot>>,
    x: usize,
    y: usize,
    row_h: usize,
    uploads: Vec<([usize; 2], ColorImage)>,
    /// Bumped when the atlas is cleared (every slot moved).
    pub generation: u64,
}

pub fn rgb(c: Rgb) -> Color32 {
    Color32::from_rgb(c.0, c.1, c.2)
}

impl Atlas {
    pub fn new(fonts: FontSet, px: f32) -> Atlas {
        let primary = fonts.regular[0].clone();
        let scaled = primary.as_scaled(PxScale::from(px));
        let cell_w = scaled.h_advance(primary.glyph_id('M')).round().max(1.0);
        let cell_h = (scaled.ascent() - scaled.descent() + scaled.line_gap()).ceil().max(1.0);
        Atlas {
            ascent: scaled.ascent().round(),
            fonts,
            px,
            cell_w,
            cell_h,
            texture: None,
            glyphs: HashMap::new(),
            x: 1,
            y: 1,
            row_h: 0,
            uploads: vec![],
            generation: 0,
        }
    }

    fn clear(&mut self) {
        self.glyphs.clear();
        self.uploads.clear();
        (self.x, self.y, self.row_h) = (1, 1, 0);
        self.generation += 1;
        if let Some(t) = &mut self.texture {
            t.set(ColorImage::filled([ATLAS, ATLAS], Color32::TRANSPARENT), TextureOptions::NEAREST);
        }
    }

    fn slot(&mut self, c: char, bold: bool) -> Option<Slot> {
        if let Some(s) = self.glyphs.get(&(c, bold)) {
            return *s;
        }
        let s = self.rasterize(c, bold);
        self.glyphs.insert((c, bold), s);
        s
    }

    fn rasterize(&mut self, c: char, bold: bool) -> Option<Slot> {
        let chain = if bold { &self.fonts.bold } else { &self.fonts.regular };
        let font = chain.iter().find(|f| f.glyph_id(c).0 != 0)?.clone();
        let glyph = font.glyph_id(c).with_scale_and_position(PxScale::from(self.px), ab_glyph::point(0.0, self.ascent));
        let outline = font.outline_glyph(glyph)?;
        let b = outline.px_bounds();
        let (w, h) = (b.width().ceil() as usize + 1, b.height().ceil() as usize + 1);
        if w + 2 >= ATLAS || h + 2 >= ATLAS {
            return None;
        }
        if self.x + w + 1 >= ATLAS {
            (self.x, self.y, self.row_h) = (1, self.y + self.row_h + 1, 0);
        }
        if self.y + h + 1 >= ATLAS {
            // Full: start over. Every terminal rebuilds its mesh (generation).
            self.clear();
        }
        let mut image = ColorImage::filled([w, h], Color32::TRANSPARENT);
        outline.draw(|x, y, coverage| {
            let a = (coverage.clamp(0.0, 1.0) * 255.0) as u8;
            image.pixels[y as usize * w + x as usize] = Color32::from_white_alpha(a);
        });
        let (ox, oy) = (self.x, self.y);
        self.uploads.push(([ox, oy], image));
        self.x += w + 1;
        self.row_h = self.row_h.max(h);
        let a = ATLAS as f32;
        Some(Slot {
            uv: Rect::from_min_size(Pos2::new(ox as f32 / a, oy as f32 / a), Vec2::new(w as f32 / a, h as f32 / a)),
            offset: Vec2::new(b.min.x.floor(), b.min.y.floor()),
            size: Vec2::new(w as f32, h as f32),
        })
    }

    /// Sends new glyphs to the GPU; call once per frame after building.
    pub fn upload(&mut self, ctx: &egui::Context) -> egui::TextureId {
        let texture = self.texture.get_or_insert_with(|| {
            ctx.load_texture(
                "lazyagent-glyphs",
                ColorImage::filled([ATLAS, ATLAS], Color32::TRANSPARENT),
                TextureOptions::NEAREST,
            )
        });
        for (pos, image) in self.uploads.drain(..) {
            texture.set_partial(pos, image, TextureOptions::NEAREST);
        }
        texture.id()
    }
}

/// What a built mesh depends on; equal keys reuse the meshes.
#[derive(Clone, Copy, PartialEq, Debug)]
pub struct MeshKey {
    pub generation: u64,
    pub atlas: u64,
    pub origin: (i32, i32),
    pub ppp: u32,
    pub focused: bool,
}

#[derive(Default)]
pub struct Meshes {
    pub key: Option<MeshKey>,
    pub background: Arc<Mesh>,
    pub glyphs: Arc<Mesh>,
}

/// Builds the meshes of `frame` with its top-left cell at `origin`
/// (points, snapped to a pixel by the caller).
pub fn build(
    frame: &Frame,
    origin: Pos2,
    ppp: f32,
    atlas: &mut Atlas,
    focused: bool,
    cursor_color: Rgb,
) -> (Mesh, Mesh) {
    let (cw, ch) = (atlas.cell_w / ppp, atlas.cell_h / ppp);
    let mut bg = Mesh::default();
    let mut fg = Mesh::default();
    let px = 1.0 / ppp;
    let cursor = frame.cursor;
    for row in 0..frame.rows {
        let y = origin.y + row as f32 * ch;
        // Background runs of one colour as one quad.
        let mut col = 0;
        while col < frame.cols {
            let color = frame.cell(row, col).bg;
            let start = col;
            while col < frame.cols && frame.cell(row, col).bg == color {
                col += 1;
            }
            if color != frame.default_bg {
                let r = Rect::from_min_size(
                    Pos2::new(origin.x + start as f32 * cw, y),
                    Vec2::new((col - start) as f32 * cw, ch),
                );
                bg.add_colored_rect(r, rgb(color));
            }
        }
        for col in 0..frame.cols {
            let cell = frame.cell(row, col);
            let x = origin.x + col as f32 * cw;
            let mut color = cell.fg;
            let on_cursor =
                focused && cursor.is_some_and(|c| c.kind == CursorKind::Block && c.row == row && c.col == col);
            if on_cursor {
                color = cell.bg;
            }
            if cell.ch != ' '
                && cell.attrs & attr::SPACER == 0
                && let Some(slot) = atlas.slot(cell.ch, cell.attrs & attr::BOLD != 0)
            {
                let min = Pos2::new(x + slot.offset.x / ppp, y + slot.offset.y / ppp);
                fg.add_rect_with_uv(Rect::from_min_size(min, slot.size / ppp), slot.uv, rgb(color));
            }
            let line = |mesh: &mut Mesh, top: f32, height: f32| {
                let w = if cell.attrs & attr::WIDE != 0 { 2.0 * cw } else { cw };
                mesh.add_colored_rect(Rect::from_min_size(Pos2::new(x, y + top), Vec2::new(w, height)), rgb(color));
            };
            if cell.attrs & (attr::UNDERLINE | attr::UNDERCURL | attr::DOUBLE_UNDERLINE) != 0 {
                line(&mut bg, ch - 2.0 * px, px);
                if cell.attrs & attr::DOUBLE_UNDERLINE != 0 {
                    line(&mut bg, ch - 4.0 * px, px);
                }
            }
            if cell.attrs & attr::STRIKE != 0 {
                line(&mut bg, (ch / 2.0).round(), px);
            }
        }
    }
    if let Some(c) = cursor {
        let x = origin.x + c.col as f32 * cw;
        let y = origin.y + c.row as f32 * ch;
        let wide = frame.cell(c.row, c.col).attrs & attr::WIDE != 0;
        let w = if wide { 2.0 * cw } else { cw };
        let color = rgb(cursor_color);
        let cell = Rect::from_min_size(Pos2::new(x, y), Vec2::new(w, ch));
        match (c.kind, focused) {
            (CursorKind::Block, true) => bg.add_colored_rect(cell, color),
            (CursorKind::Beam, _) => bg.add_colored_rect(Rect::from_min_size(cell.min, Vec2::new(2.0 * px, ch)), color),
            (CursorKind::Underline, _) => {
                bg.add_colored_rect(Rect::from_min_size(Pos2::new(x, y + ch - 2.0 * px), Vec2::new(w, 2.0 * px)), color)
            }
            _ => {
                // Hollow: the window or the terminal is not focused.
                for r in [
                    Rect::from_min_size(cell.min, Vec2::new(w, px)),
                    Rect::from_min_size(Pos2::new(x, y + ch - px), Vec2::new(w, px)),
                    Rect::from_min_size(cell.min, Vec2::new(px, ch)),
                    Rect::from_min_size(Pos2::new(x + w - px, y), Vec2::new(px, ch)),
                ] {
                    bg.add_colored_rect(r, color);
                }
            }
        }
    }
    (bg, fg)
}

#[cfg(test)]
mod tests {
    use super::*;
    use lazyagent_term::{Palette, Terminal};

    #[test]
    fn cells_are_whole_pixels_and_meshes_cover_the_text() {
        let mut atlas = Atlas::new(FontSet::bundled_only(), 13.0);
        assert_eq!(atlas.cell_w.fract(), 0.0);
        assert_eq!(atlas.cell_h.fract(), 0.0);
        let mut t = Terminal::new(2, 10, Palette::dark());
        t.feed(b"ab \x1b[41mX\x1b[0m");
        let frame = t.frame();
        let (bg, fg) = build(&frame, Pos2::ZERO, 1.0, &mut atlas, true, Palette::dark().cursor);
        // a, b, X: three glyph quads.
        assert_eq!(fg.vertices.len(), 3 * 4);
        // The red cell, plus the cursor block after X.
        assert_eq!(bg.vertices.len(), 2 * 4);
        assert_eq!(atlas.uploads.len(), 3);
        // Same glyphs again: nothing new to rasterise.
        build(&frame, Pos2::ZERO, 1.0, &mut atlas, false, Palette::dark().cursor);
        assert_eq!(atlas.uploads.len(), 3);
    }

    #[test]
    fn a_full_atlas_starts_over() {
        let mut atlas = Atlas::new(FontSet::bundled_only(), 60.0);
        for c in (0x21u32..0x2600).filter_map(char::from_u32) {
            atlas.slot(c, false);
        }
        assert!(atlas.generation > 0, "{} glyphs fit in one atlas", atlas.glyphs.len());
        assert!(atlas.slot('A', false).is_some(), "it keeps working after a reset");
    }
}
