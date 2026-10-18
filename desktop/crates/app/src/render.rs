//! The terminal renderer: a glyph atlas rasterised on demand with
//! `ab_glyph` at the exact pixel size, and per terminal two meshes (merged
//! background runs, then glyphs and decorations) that egui draws in one
//! call each. Cells sit on whole pixels, so glyphs stay crisp.

use std::collections::HashMap;
use std::sync::Arc;

use ab_glyph::{Font, PxScale, ScaleFont};
use eframe::egui::{self, Color32, ColorImage, Mesh, Pos2, Rect, TextureHandle, TextureOptions, Vec2};
use lazyagent_term::{CursorKind, Frame, Rgb, attr};

use crate::boxdraw;
use crate::fonts::FontSet;

const ATLAS: usize = 1024;

#[derive(Clone, Copy)]
struct Slot {
    /// A colour bitmap: drawn as it is, not tinted with the cell's colour.
    color: bool,
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
    /// (char, bold, cells wide)
    glyphs: HashMap<(char, bool, u8), Option<Slot>>,
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

    fn slot(&mut self, c: char, bold: bool, cells: u8) -> Option<Slot> {
        if let Some(s) = self.glyphs.get(&(c, bold, cells)) {
            return *s;
        }
        let s = self.rasterize(c, bold).or_else(|| self.rasterize_color(c, cells));
        self.glyphs.insert((c, bold, cells), s);
        s
    }

    /// Room in the atlas for a `w × h` image; its top-left corner.
    fn place(&mut self, w: usize, h: usize) -> Option<(usize, usize)> {
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
        let at = (self.x, self.y);
        self.x += w + 1;
        self.row_h = self.row_h.max(h);
        Some(at)
    }

    fn uv(at: (usize, usize), w: usize, h: usize) -> Rect {
        let a = ATLAS as f32;
        Rect::from_min_size(Pos2::new(at.0 as f32 / a, at.1 as f32 / a), Vec2::new(w as f32 / a, h as f32 / a))
    }

    fn rasterize(&mut self, c: char, bold: bool) -> Option<Slot> {
        let chain = if bold { &self.fonts.bold } else { &self.fonts.regular };
        let font = chain.iter().find(|f| f.glyph_id(c).0 != 0)?.clone();
        let glyph = font.glyph_id(c).with_scale_and_position(PxScale::from(self.px), ab_glyph::point(0.0, self.ascent));
        let outline = font.outline_glyph(glyph)?;
        let b = outline.px_bounds();
        let (w, h) = (b.width().ceil() as usize + 1, b.height().ceil() as usize + 1);
        let at = self.place(w, h)?;
        let mut image = ColorImage::filled([w, h], Color32::TRANSPARENT);
        outline.draw(|x, y, coverage| {
            let a = (coverage.clamp(0.0, 1.0) * 255.0) as u8;
            image.pixels[y as usize * w + x as usize] = Color32::from_white_alpha(a);
        });
        self.uploads.push(([at.0, at.1], image));
        Some(Slot {
            color: false,
            uv: Atlas::uv(at, w, h),
            offset: Vec2::new(b.min.x.floor(), b.min.y.floor()),
            size: Vec2::new(w as f32, h as f32),
        })
    }

    /// A colour emoji: the font's PNG strike, scaled to fit the cell(s).
    fn rasterize_color(&mut self, c: char, cells: u8) -> Option<Slot> {
        let font = self.fonts.color.iter().find(|f| f.glyph_id(c).0 != 0)?.clone();
        let raster = font.glyph_raster_image2(font.glyph_id(c), self.cell_h as u16)?;
        if !matches!(raster.format, ab_glyph::GlyphImageFormat::Png) {
            return None;
        }
        let png = image::load_from_memory_with_format(raster.data, image::ImageFormat::Png).ok()?.to_rgba8();
        let (box_w, box_h) = (self.cell_w * cells as f32, self.cell_h);
        let scale = (box_w / png.width() as f32).min(box_h / png.height() as f32);
        let (w, h) = (
            ((png.width() as f32 * scale).round() as u32).max(1),
            ((png.height() as f32 * scale).round() as u32).max(1),
        );
        let small = image::imageops::resize(&png, w, h, image::imageops::FilterType::Triangle);
        let (w, h) = (w as usize, h as usize);
        let at = self.place(w, h)?;
        let pixels = small.pixels().map(|p| Color32::from_rgba_unmultiplied(p[0], p[1], p[2], p[3])).collect();
        self.uploads.push(([at.0, at.1], ColorImage::new([w, h], pixels)));
        Some(Slot {
            color: true,
            uv: Atlas::uv(at, w, h),
            offset: Vec2::new(((box_w - w as f32) / 2.0).floor(), ((box_h - h as f32) / 2.0).floor()),
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
            let cells = if cell.attrs & attr::WIDE != 0 { 2 } else { 1 };
            let drawn = cell.ch != ' '
                && cell.attrs & attr::SPACER == 0
                && boxdraw::draw(
                    cell.ch,
                    Rect::from_min_size(Pos2::new(x, y), Vec2::new(cw * cells as f32, ch)),
                    px,
                    rgb(color),
                    rgb(cell.bg),
                    &mut bg,
                );
            if !drawn
                && cell.ch != ' '
                && cell.attrs & attr::SPACER == 0
                && let Some(slot) = atlas.slot(cell.ch, cell.attrs & attr::BOLD != 0, cells)
            {
                let min = Pos2::new(x + slot.offset.x / ppp, y + slot.offset.y / ppp);
                let tint = if slot.color { Color32::WHITE } else { rgb(color) };
                fg.add_rect_with_uv(Rect::from_min_size(min, slot.size / ppp), slot.uv, tint);
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
            atlas.slot(c, false, 1);
        }
        assert!(atlas.generation > 0, "{} glyphs fit in one atlas", atlas.glyphs.len());
        assert!(atlas.slot('A', false, 1).is_some(), "it keeps working after a reset");
    }
}
