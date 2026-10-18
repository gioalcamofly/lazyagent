//! Box-drawing (U+2500-257F) and block elements (U+2580-259F) drawn as
//! rectangles snapped to the pixel grid, as terminals do: font glyphs for
//! these rarely fill the cell exactly, so lines do not join and logos made
//! of blocks (Claude Code's `▐▛███▜▌`) show seams.

use eframe::egui::{Color32, Mesh, Pos2, Rect, Vec2};

/// Arms of a box-drawing character: left, right, up, down; 0 none,
/// 1 light, 2 heavy, 3 double. Generated from the Unicode names.
fn arms(c: char) -> Option<[u8; 4]> {
    Some(match c {
        '\u{2500}' => [1, 1, 0, 0], // ─ Box Drawings Light Horizontal
        '\u{2501}' => [2, 2, 0, 0], // ━ Box Drawings Heavy Horizontal
        '\u{2502}' => [0, 0, 1, 1], // │ Box Drawings Light Vertical
        '\u{2503}' => [0, 0, 2, 2], // ┃ Box Drawings Heavy Vertical
        '\u{2504}' => [1, 1, 0, 0], // ┄ Box Drawings Light Triple Dash Horizontal
        '\u{2505}' => [2, 2, 0, 0], // ┅ Box Drawings Heavy Triple Dash Horizontal
        '\u{2506}' => [0, 0, 1, 1], // ┆ Box Drawings Light Triple Dash Vertical
        '\u{2507}' => [0, 0, 2, 2], // ┇ Box Drawings Heavy Triple Dash Vertical
        '\u{2508}' => [1, 1, 0, 0], // ┈ Box Drawings Light Quadruple Dash Horizontal
        '\u{2509}' => [2, 2, 0, 0], // ┉ Box Drawings Heavy Quadruple Dash Horizontal
        '\u{250a}' => [0, 0, 1, 1], // ┊ Box Drawings Light Quadruple Dash Vertical
        '\u{250b}' => [0, 0, 2, 2], // ┋ Box Drawings Heavy Quadruple Dash Vertical
        '\u{250c}' => [0, 1, 0, 1], // ┌ Box Drawings Light Down And Right
        '\u{250d}' => [0, 2, 0, 1], // ┍ Box Drawings Down Light And Right Heavy
        '\u{250e}' => [0, 1, 0, 2], // ┎ Box Drawings Down Heavy And Right Light
        '\u{250f}' => [0, 2, 0, 2], // ┏ Box Drawings Heavy Down And Right
        '\u{2510}' => [1, 0, 0, 1], // ┐ Box Drawings Light Down And Left
        '\u{2511}' => [2, 0, 0, 1], // ┑ Box Drawings Down Light And Left Heavy
        '\u{2512}' => [1, 0, 0, 2], // ┒ Box Drawings Down Heavy And Left Light
        '\u{2513}' => [2, 0, 0, 2], // ┓ Box Drawings Heavy Down And Left
        '\u{2514}' => [0, 1, 1, 0], // └ Box Drawings Light Up And Right
        '\u{2515}' => [0, 2, 1, 0], // ┕ Box Drawings Up Light And Right Heavy
        '\u{2516}' => [0, 1, 2, 0], // ┖ Box Drawings Up Heavy And Right Light
        '\u{2517}' => [0, 2, 2, 0], // ┗ Box Drawings Heavy Up And Right
        '\u{2518}' => [1, 0, 1, 0], // ┘ Box Drawings Light Up And Left
        '\u{2519}' => [2, 0, 1, 0], // ┙ Box Drawings Up Light And Left Heavy
        '\u{251a}' => [1, 0, 2, 0], // ┚ Box Drawings Up Heavy And Left Light
        '\u{251b}' => [2, 0, 2, 0], // ┛ Box Drawings Heavy Up And Left
        '\u{251c}' => [0, 1, 1, 1], // ├ Box Drawings Light Vertical And Right
        '\u{251d}' => [0, 2, 1, 1], // ┝ Box Drawings Vertical Light And Right Heavy
        '\u{251e}' => [0, 1, 2, 1], // ┞ Box Drawings Up Heavy And Right Down Light
        '\u{251f}' => [0, 1, 1, 2], // ┟ Box Drawings Down Heavy And Right Up Light
        '\u{2520}' => [0, 1, 2, 2], // ┠ Box Drawings Vertical Heavy And Right Light
        '\u{2521}' => [0, 2, 2, 1], // ┡ Box Drawings Down Light And Right Up Heavy
        '\u{2522}' => [0, 2, 1, 2], // ┢ Box Drawings Up Light And Right Down Heavy
        '\u{2523}' => [0, 2, 2, 2], // ┣ Box Drawings Heavy Vertical And Right
        '\u{2524}' => [1, 0, 1, 1], // ┤ Box Drawings Light Vertical And Left
        '\u{2525}' => [2, 0, 1, 1], // ┥ Box Drawings Vertical Light And Left Heavy
        '\u{2526}' => [1, 0, 2, 1], // ┦ Box Drawings Up Heavy And Left Down Light
        '\u{2527}' => [1, 0, 1, 2], // ┧ Box Drawings Down Heavy And Left Up Light
        '\u{2528}' => [1, 0, 2, 2], // ┨ Box Drawings Vertical Heavy And Left Light
        '\u{2529}' => [2, 0, 2, 1], // ┩ Box Drawings Down Light And Left Up Heavy
        '\u{252a}' => [2, 0, 1, 2], // ┪ Box Drawings Up Light And Left Down Heavy
        '\u{252b}' => [2, 0, 2, 2], // ┫ Box Drawings Heavy Vertical And Left
        '\u{252c}' => [1, 1, 0, 1], // ┬ Box Drawings Light Down And Horizontal
        '\u{252d}' => [2, 1, 0, 1], // ┭ Box Drawings Left Heavy And Right Down Light
        '\u{252e}' => [1, 2, 0, 1], // ┮ Box Drawings Right Heavy And Left Down Light
        '\u{252f}' => [2, 2, 0, 1], // ┯ Box Drawings Down Light And Horizontal Heavy
        '\u{2530}' => [1, 1, 0, 2], // ┰ Box Drawings Down Heavy And Horizontal Light
        '\u{2531}' => [2, 1, 0, 2], // ┱ Box Drawings Right Light And Left Down Heavy
        '\u{2532}' => [1, 2, 0, 2], // ┲ Box Drawings Left Light And Right Down Heavy
        '\u{2533}' => [2, 2, 0, 2], // ┳ Box Drawings Heavy Down And Horizontal
        '\u{2534}' => [1, 1, 1, 0], // ┴ Box Drawings Light Up And Horizontal
        '\u{2535}' => [2, 1, 1, 0], // ┵ Box Drawings Left Heavy And Right Up Light
        '\u{2536}' => [1, 2, 1, 0], // ┶ Box Drawings Right Heavy And Left Up Light
        '\u{2537}' => [2, 2, 1, 0], // ┷ Box Drawings Up Light And Horizontal Heavy
        '\u{2538}' => [1, 1, 2, 0], // ┸ Box Drawings Up Heavy And Horizontal Light
        '\u{2539}' => [2, 1, 2, 0], // ┹ Box Drawings Right Light And Left Up Heavy
        '\u{253a}' => [1, 2, 2, 0], // ┺ Box Drawings Left Light And Right Up Heavy
        '\u{253b}' => [2, 2, 2, 0], // ┻ Box Drawings Heavy Up And Horizontal
        '\u{253c}' => [1, 1, 1, 1], // ┼ Box Drawings Light Vertical And Horizontal
        '\u{253d}' => [2, 1, 1, 1], // ┽ Box Drawings Left Heavy And Right Vertical Light
        '\u{253e}' => [1, 2, 1, 1], // ┾ Box Drawings Right Heavy And Left Vertical Light
        '\u{253f}' => [2, 2, 1, 1], // ┿ Box Drawings Vertical Light And Horizontal Heavy
        '\u{2540}' => [1, 1, 2, 1], // ╀ Box Drawings Up Heavy And Down Horizontal Light
        '\u{2541}' => [1, 1, 1, 2], // ╁ Box Drawings Down Heavy And Up Horizontal Light
        '\u{2542}' => [1, 1, 2, 2], // ╂ Box Drawings Vertical Heavy And Horizontal Light
        '\u{2543}' => [2, 1, 2, 1], // ╃ Box Drawings Left Up Heavy And Right Down Light
        '\u{2544}' => [1, 2, 2, 1], // ╄ Box Drawings Right Up Heavy And Left Down Light
        '\u{2545}' => [2, 1, 1, 2], // ╅ Box Drawings Left Down Heavy And Right Up Light
        '\u{2546}' => [1, 2, 1, 2], // ╆ Box Drawings Right Down Heavy And Left Up Light
        '\u{2547}' => [2, 2, 2, 1], // ╇ Box Drawings Down Light And Up Horizontal Heavy
        '\u{2548}' => [2, 2, 1, 2], // ╈ Box Drawings Up Light And Down Horizontal Heavy
        '\u{2549}' => [2, 1, 2, 2], // ╉ Box Drawings Right Light And Left Vertical Heavy
        '\u{254a}' => [1, 2, 2, 2], // ╊ Box Drawings Left Light And Right Vertical Heavy
        '\u{254b}' => [2, 2, 2, 2], // ╋ Box Drawings Heavy Vertical And Horizontal
        '\u{254c}' => [1, 1, 0, 0], // ╌ Box Drawings Light Double Dash Horizontal
        '\u{254d}' => [2, 2, 0, 0], // ╍ Box Drawings Heavy Double Dash Horizontal
        '\u{254e}' => [0, 0, 1, 1], // ╎ Box Drawings Light Double Dash Vertical
        '\u{254f}' => [0, 0, 2, 2], // ╏ Box Drawings Heavy Double Dash Vertical
        '\u{2550}' => [3, 3, 0, 0], // ═ Box Drawings Double Horizontal
        '\u{2551}' => [0, 0, 3, 3], // ║ Box Drawings Double Vertical
        '\u{2552}' => [0, 3, 0, 1], // ╒ Box Drawings Down Single And Right Double
        '\u{2553}' => [0, 1, 0, 3], // ╓ Box Drawings Down Double And Right Single
        '\u{2554}' => [0, 3, 0, 3], // ╔ Box Drawings Double Down And Right
        '\u{2555}' => [3, 0, 0, 1], // ╕ Box Drawings Down Single And Left Double
        '\u{2556}' => [1, 0, 0, 3], // ╖ Box Drawings Down Double And Left Single
        '\u{2557}' => [3, 0, 0, 3], // ╗ Box Drawings Double Down And Left
        '\u{2558}' => [0, 3, 1, 0], // ╘ Box Drawings Up Single And Right Double
        '\u{2559}' => [0, 1, 3, 0], // ╙ Box Drawings Up Double And Right Single
        '\u{255a}' => [0, 3, 3, 0], // ╚ Box Drawings Double Up And Right
        '\u{255b}' => [3, 0, 1, 0], // ╛ Box Drawings Up Single And Left Double
        '\u{255c}' => [1, 0, 3, 0], // ╜ Box Drawings Up Double And Left Single
        '\u{255d}' => [3, 0, 3, 0], // ╝ Box Drawings Double Up And Left
        '\u{255e}' => [0, 3, 1, 1], // ╞ Box Drawings Vertical Single And Right Double
        '\u{255f}' => [0, 1, 3, 3], // ╟ Box Drawings Vertical Double And Right Single
        '\u{2560}' => [0, 3, 3, 3], // ╠ Box Drawings Double Vertical And Right
        '\u{2561}' => [3, 0, 1, 1], // ╡ Box Drawings Vertical Single And Left Double
        '\u{2562}' => [1, 0, 3, 3], // ╢ Box Drawings Vertical Double And Left Single
        '\u{2563}' => [3, 0, 3, 3], // ╣ Box Drawings Double Vertical And Left
        '\u{2564}' => [3, 3, 0, 1], // ╤ Box Drawings Down Single And Horizontal Double
        '\u{2565}' => [1, 1, 0, 3], // ╥ Box Drawings Down Double And Horizontal Single
        '\u{2566}' => [3, 3, 0, 3], // ╦ Box Drawings Double Down And Horizontal
        '\u{2567}' => [3, 3, 1, 0], // ╧ Box Drawings Up Single And Horizontal Double
        '\u{2568}' => [1, 1, 3, 0], // ╨ Box Drawings Up Double And Horizontal Single
        '\u{2569}' => [3, 3, 3, 0], // ╩ Box Drawings Double Up And Horizontal
        '\u{256a}' => [3, 3, 1, 1], // ╪ Box Drawings Vertical Single And Horizontal Double
        '\u{256b}' => [1, 1, 3, 3], // ╫ Box Drawings Vertical Double And Horizontal Single
        '\u{256c}' => [3, 3, 3, 3], // ╬ Box Drawings Double Vertical And Horizontal
        '\u{2574}' => [1, 0, 0, 0], // ╴ Box Drawings Light Left
        '\u{2575}' => [0, 0, 1, 0], // ╵ Box Drawings Light Up
        '\u{2576}' => [0, 1, 0, 0], // ╶ Box Drawings Light Right
        '\u{2577}' => [0, 0, 0, 1], // ╷ Box Drawings Light Down
        '\u{2578}' => [2, 0, 0, 0], // ╸ Box Drawings Heavy Left
        '\u{2579}' => [0, 0, 2, 0], // ╹ Box Drawings Heavy Up
        '\u{257a}' => [0, 2, 0, 0], // ╺ Box Drawings Heavy Right
        '\u{257b}' => [0, 0, 0, 2], // ╻ Box Drawings Heavy Down
        '\u{257c}' => [1, 1, 0, 0], // ╼ Box Drawings Light Left And Heavy Right
        '\u{257d}' => [0, 0, 1, 1], // ╽ Box Drawings Light Up And Heavy Down
        '\u{257e}' => [2, 2, 0, 0], // ╾ Box Drawings Heavy Left And Light Right
        '\u{257f}' => [0, 0, 2, 2], // ╿ Box Drawings Heavy Up And Light Down
        // Rounded corners: drawn square.
        '╭' => [0, 1, 0, 1],
        '╮' => [1, 0, 0, 1],
        '╯' => [1, 0, 1, 0],
        '╰' => [0, 1, 1, 0],
        _ => return None,
    })
}

/// Snaps a length in points to whole physical pixels (at least one).
fn snap(v: f32, px: f32) -> f32 {
    ((v / px).round().max(1.0)) * px
}

fn rect(mesh: &mut Mesh, min: Pos2, size: Vec2, color: Color32) {
    if size.x > 0.0 && size.y > 0.0 {
        mesh.add_colored_rect(Rect::from_min_size(min, size), color);
    }
}

fn draw_arms(a: [u8; 4], cell: Rect, px: f32, color: Color32, mesh: &mut Mesh) {
    let light = snap(cell.width() / 8.0, px);
    let thick = |w: u8| if w == 2 { light * 2.0 } else { light };
    let cx = cell.left() + snap(cell.width() / 2.0, px) - light / 2.0;
    let cy = cell.top() + snap(cell.height() / 2.0, px) - light / 2.0;
    // How far past the centre each arm reaches, so arms always join.
    let reach = a.iter().map(|w| if *w == 3 { light * 2.0 } else { thick(*w) }).fold(light, f32::max);
    let [l, r, u, d] = a;
    let horizontal = |mesh: &mut Mesh, w: u8, from: f32, to: f32| {
        if w == 3 {
            rect(mesh, Pos2::new(from, cy - light), Vec2::new(to - from, light), color);
            rect(mesh, Pos2::new(from, cy + light), Vec2::new(to - from, light), color);
        } else if w > 0 {
            let t = thick(w);
            rect(mesh, Pos2::new(from, cy + (light - t) / 2.0), Vec2::new(to - from, t), color);
        }
    };
    let vertical = |mesh: &mut Mesh, w: u8, from: f32, to: f32| {
        if w == 3 {
            rect(mesh, Pos2::new(cx - light, from), Vec2::new(light, to - from), color);
            rect(mesh, Pos2::new(cx + light, from), Vec2::new(light, to - from), color);
        } else if w > 0 {
            let t = thick(w);
            rect(mesh, Pos2::new(cx + (light - t) / 2.0, from), Vec2::new(t, to - from), color);
        }
    };
    horizontal(mesh, l, cell.left(), cx + reach);
    horizontal(mesh, r, cx - reach + light, cell.right());
    vertical(mesh, u, cell.top(), cy + reach);
    vertical(mesh, d, cy - reach + light, cell.bottom());
}

fn disc(mesh: &mut Mesh, center: Pos2, r: f32, color: Color32) {
    const SEGMENTS: u32 = 24;
    let first = mesh.vertices.len() as u32;
    mesh.colored_vertex(center, color);
    for i in 0..=SEGMENTS {
        let a = i as f32 / SEGMENTS as f32 * std::f32::consts::TAU;
        mesh.colored_vertex(center + Vec2::new(a.cos(), a.sin()) * r, color);
        if i > 0 {
            mesh.add_triangle(first, first + i, first + i + 1);
        }
    }
}

/// Draws `c` into `cell` if it is one of ours; false otherwise.
pub fn draw(c: char, cell: Rect, px: f32, color: Color32, background: Color32, mesh: &mut Mesh) -> bool {
    let (x, y, w, h) = (cell.left(), cell.top(), cell.width(), cell.height());
    let part = |fx: f32, fy: f32, fw: f32, fh: f32| {
        let x0 = x + snap(w * fx, px) * (fx > 0.0) as u8 as f32;
        let y0 = y + snap(h * fy, px) * (fy > 0.0) as u8 as f32;
        let x1 = if fx + fw >= 1.0 { x + w } else { x + snap(w * (fx + fw), px) };
        let y1 = if fy + fh >= 1.0 { y + h } else { y + snap(h * (fy + fh), px) };
        Rect::from_min_max(Pos2::new(x0, y0), Pos2::new(x1, y1))
    };
    let mut fill = |r: Rect, color: Color32| mesh.add_colored_rect(r, color);
    match c {
        // Agents' status bullet (Claude Code), coloured by SGR: only colour
        // emoji fonts have it, and they would draw a fixed orange square.
        '⏺' => {
            drop(fill);
            disc(mesh, cell.center(), (w.min(h) * 0.42).max(px), color);
        }
        '▀' => fill(part(0.0, 0.0, 1.0, 0.5), color),
        '▁'..='▇' => {
            let eighths = (c as u32 - 0x2580) as f32;
            fill(part(0.0, 1.0 - eighths / 8.0, 1.0, eighths / 8.0), color)
        }
        '█' => fill(cell, color),
        '▉'..='▏' => {
            let eighths = (0x2590 - c as u32) as f32;
            fill(part(0.0, 0.0, eighths / 8.0, 1.0), color)
        }
        '▐' => fill(part(0.5, 0.0, 0.5, 1.0), color),
        '░' | '▒' | '▓' => {
            let alpha = match c {
                '░' => 0.25,
                '▒' => 0.5,
                _ => 0.75,
            };
            let mix = |a: u8, b: u8| (a as f32 * alpha + b as f32 * (1.0 - alpha)) as u8;
            fill(
                cell,
                Color32::from_rgb(
                    mix(color.r(), background.r()),
                    mix(color.g(), background.g()),
                    mix(color.b(), background.b()),
                ),
            )
        }
        '▔' => fill(part(0.0, 0.0, 1.0, 0.125), color),
        '▕' => fill(part(0.875, 0.0, 0.125, 1.0), color),
        '▖'..='▟' => {
            // Quadrants: upper left, upper right, lower left, lower right.
            let q: [bool; 4] = match c {
                '▖' => [false, false, true, false],
                '▗' => [false, false, false, true],
                '▘' => [true, false, false, false],
                '▙' => [true, false, true, true],
                '▚' => [true, false, false, true],
                '▛' => [true, true, true, false],
                '▜' => [true, true, false, true],
                '▝' => [false, true, false, false],
                '▞' => [false, true, true, false],
                _ => [false, true, true, true],
            };
            for (i, on) in q.iter().enumerate() {
                if *on {
                    let (fx, fy) = ((i % 2) as f32 * 0.5, (i / 2) as f32 * 0.5);
                    fill(part(fx, fy, 0.5, 0.5), color);
                }
            }
        }
        _ => match arms(c) {
            Some(a) => draw_arms(a, cell, px, color, mesh),
            None => return false,
        },
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn covered(c: char) -> f32 {
        let cell = Rect::from_min_size(Pos2::new(10.0, 20.0), Vec2::new(8.0, 16.0));
        let mut m = Mesh::default();
        assert!(draw(c, cell, 1.0, Color32::WHITE, Color32::BLACK, &mut m), "{c}");
        // Area of the quads (they do not overlap for blocks).
        let area = |q: &[eframe::epaint::Vertex]| {
            let xs = q.iter().map(|v| v.pos.x);
            let ys = q.iter().map(|v| v.pos.y);
            (xs.clone().fold(f32::MIN, f32::max) - xs.fold(f32::MAX, f32::min))
                * (ys.clone().fold(f32::MIN, f32::max) - ys.fold(f32::MAX, f32::min))
        };
        m.vertices.chunks(4).map(area).sum::<f32>() / (8.0 * 16.0)
    }

    #[test]
    fn blocks_fill_their_share_of_the_cell() {
        assert_eq!(covered('█'), 1.0);
        assert_eq!(covered('▀'), 0.5);
        assert_eq!(covered('▐'), 0.5);
        assert_eq!(covered('▛'), 0.75);
        assert_eq!(covered('▄'), 0.5);
        assert_eq!(covered('▌'), 0.5);
    }

    #[test]
    fn lines_reach_the_cell_edges_they_point_to() {
        let cell = Rect::from_min_size(Pos2::new(0.0, 0.0), Vec2::new(8.0, 16.0));
        let bounds = |c| {
            let mut m = Mesh::default();
            draw(c, cell, 1.0, Color32::WHITE, Color32::BLACK, &mut m);
            m.calc_bounds()
        };
        assert_eq!((bounds('─').left(), bounds('─').right()), (0.0, 8.0));
        assert_eq!((bounds('│').top(), bounds('│').bottom()), (0.0, 16.0));
        let corner = bounds('┌');
        assert_eq!((corner.right(), corner.bottom()), (8.0, 16.0));
        assert!(corner.left() > 0.0 && corner.top() > 0.0);
        assert!(!draw('a', cell, 1.0, Color32::WHITE, Color32::BLACK, &mut Mesh::default()));
        assert!(!draw('╱', cell, 1.0, Color32::WHITE, Color32::BLACK, &mut Mesh::default()), "diagonals use the font");
    }
}
