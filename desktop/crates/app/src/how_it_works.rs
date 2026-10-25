//! "How lazyagent works" (proposal 0014, R6): a page, not a dialog. Help →
//! How lazyagent works, `F1` anywhere; `?` on Home opens it at Home.
//!
//! Everything about states, verbs, actions, inbox items, facts and the
//! user's rules is drawn from the engine's `rules_catalog`; an engine
//! without the feature `rules` gets the copy built into the app
//! (assets/rules_catalog.json, written by `lazyagent rules doc --write`). The
//! hand-written part is short: the concepts (`concepts.toml`, in the engine),
//! carried in the same payload, and the diagrams' arrows. The screenshots
//! (assets/how/*.png) come from the offscreen harness and fixed fake data
//! (tests/how.rs regenerates them and fails when they are stale).

use std::collections::HashMap;
use std::sync::OnceLock;

use eframe::egui::{
    self, Align, Align2, Color32, CornerRadius, FontId, Key, Layout, Modifiers, Pos2, Rect, RichText, Sense, Stroke,
    Ui, pos2, vec2,
};
use lazyagent_proto::types::{CatalogRule, ConceptKey, Concepts, RulesCatalog};
use serde_json::json;

use crate::app::{App, State, View};
use crate::theme::Theme;

/// The built-in catalog at build time (`lazyagent rules doc --write`).
pub const COPY: &str = include_str!("../assets/rules_catalog.json");

/// The screenshots the concepts name, embedded (assets/how/<name>.png).
pub const SCREENSHOTS: &[(&str, &[u8])] = &[
    ("home", include_bytes!("../assets/how/home.png")),
    ("expanded", include_bytes!("../assets/how/expanded.png")),
    ("decision", include_bytes!("../assets/how/decision.png")),
    ("park", include_bytes!("../assets/how/park.png")),
    ("strip", include_bytes!("../assets/how/strip.png")),
];

/// The page's section Home's `?` opens.
pub const HOME_SECTION: &str = "home";
/// The navigation column's width.
const NAV_W: f32 = 180.0;
/// The widest the content gets (long lines are hard to read).
const CONTENT_MAX: f32 = 980.0;

/// The copy, parsed once.
pub fn builtin() -> &'static RulesCatalog {
    static CATALOG: OnceLock<RulesCatalog> = OnceLock::new();
    CATALOG.get_or_init(|| serde_json::from_str(COPY).expect("assets/rules_catalog.json is a rules_catalog"))
}

/// The page's state: where to go back to, where to scroll, the filters, the
/// engine's catalog once it answered.
pub struct Guide {
    pub back: View,
    pub scroll_to: Option<String>,
    pub current: String,
    pub key_filter: String,
    pub fact_filter: String,
    /// The engine's `rules_catalog` (feature `rules`), asked for on each open.
    pub live: Option<RulesCatalog>,
    textures: HashMap<&'static str, egui::TextureHandle>,
}

impl Default for Guide {
    fn default() -> Self {
        Guide {
            back: View::Home,
            scroll_to: None,
            current: "overview".into(),
            key_filter: String::new(),
            fact_filter: String::new(),
            live: None,
            textures: HashMap::new(),
        }
    }
}

impl Guide {
    /// The catalog the page draws: the engine's, else the copy.
    pub fn catalog(&self) -> &RulesCatalog {
        self.live.as_ref().unwrap_or_else(|| builtin())
    }

    /// The concepts: the engine's when it sent them, else the copy's.
    pub fn concepts(&self) -> &Concepts {
        match &self.live {
            Some(c) if !c.concepts.sections.is_empty() => &c.concepts,
            _ => &builtin().concepts,
        }
    }

    /// The keys are this app's own: always the copy's (built with it).
    pub fn keys(&self) -> &'static [ConceptKey] {
        &builtin().concepts.keys
    }
}

impl State {
    /// Opens the page at `section` (another view: remembered for Esc).
    pub fn open_guide(&mut self, section: &str) {
        if self.view != View::Guide {
            self.guide.back = self.view;
            if self.view == View::Home {
                self.mark_seen();
            }
        }
        self.view = View::Guide;
        self.guide.scroll_to = Some(section.to_string());
        self.guide.current = section.to_string();
        if self.has("rules") {
            self.conn.request("rules_catalog", json!({}), |s, reply| {
                if let Ok(c) = reply.and_then(|v| {
                    serde_json::from_value::<RulesCatalog>(v)
                        .map_err(|e| lazyagent_proto::ClientError::Decode(e.to_string()))
                }) {
                    s.guide.live = Some(c);
                }
            });
        }
    }

    /// Back where the page was opened from.
    pub fn close_guide(&mut self) {
        if self.view == View::Guide {
            match self.guide.back {
                View::Home | View::Guide => {
                    self.view = View::Worktrees;
                    self.go_home();
                }
                v => self.view = v,
            }
        }
    }

    /// The page's keys while it is shown: Esc goes back.
    pub fn guide_key(&mut self, take: &impl Fn(Modifiers, Key) -> bool) -> bool {
        if take(Modifiers::NONE, Key::Escape) {
            self.close_guide();
            return true;
        }
        false
    }
}

/// A theme colour by its concepts name.
pub fn colour(t: &Theme, name: &str) -> Color32 {
    match name {
        "yellow" => t.yellow,
        "orange" => t.accent,
        "purple" => t.purple,
        "cyan" => t.cyan,
        "blue" => t.primary,
        "red" => t.red,
        "green" => t.green,
        _ => t.muted,
    }
}

/// `Shift+X` as the app writes it (`⇧X`).
pub fn key_text(key: &str) -> String {
    key.replace("Shift+", "⇧")
}

fn zone_colour(t: &Theme, concepts: &Concepts, zone: &str) -> Color32 {
    match zone {
        "done" => t.muted,
        "inbox" => t.accent,
        z => concepts.zones.iter().find(|c| c.id == z).map(|c| colour(t, &c.colour)).unwrap_or(t.muted),
    }
}

fn zone_name(concepts: &Concepts, zone: &str) -> String {
    match zone {
        "done" => "Done".into(),
        "inbox" => "Inbox · the top item decides".into(),
        z => concepts.zones.iter().find(|c| c.id == z).map(|c| c.name.clone()).unwrap_or_else(|| z.to_string()),
    }
}

impl App {
    pub(crate) fn guide_view(&mut self, ui: &mut Ui) {
        let t = self.state.theme;
        let ctx = ui.ctx().clone();
        let guide = &mut self.state.guide;
        for (name, bytes) in SCREENSHOTS {
            if !guide.textures.contains_key(name)
                && let Ok(img) = image::load_from_memory(bytes)
            {
                let img = img.to_rgba8();
                let size = [img.width() as usize, img.height() as usize];
                let color = egui::ColorImage::from_rgba_unmultiplied(size, img.as_raw());
                guide
                    .textures
                    .insert(name, ctx.load_texture(format!("how-{name}"), color, egui::TextureOptions::LINEAR));
            }
        }
        let live = self.state.guide.live.is_some();
        let rules_feature = self.state.has("rules");
        ui.add_space(6.0);
        let mut back = false;
        ui.horizontal(|ui| {
            if ui.button("←  Back  (Esc)").clicked() {
                back = true;
            }
            ui.add_space(8.0);
            ui.label(RichText::new("How lazyagent works").heading().strong().color(t.text));
            ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                let (text, hover) = if live {
                    ("rules from the engine", "Your rules included: this is what the engine applies now.")
                } else if rules_feature {
                    ("…", "Asking the engine for its rules")
                } else {
                    (
                        "built-in rules (copy)",
                        "This engine has no rules catalog: the page shows the built-ins this app was built with.",
                    )
                };
                ui.label(RichText::new(text).small().color(t.muted)).on_hover_text(hover);
            });
        });
        ui.separator();
        if back {
            self.state.close_guide();
            return;
        }
        let sections: Vec<(String, String)> =
            self.state.guide.concepts().sections.iter().map(|s| (s.id.clone(), s.title.clone())).collect();
        let mut jump: Option<String> = None;
        egui::Panel::left("how-nav")
            .resizable(false)
            .exact_size(NAV_W)
            .frame(egui::Frame::new().inner_margin(egui::Margin { left: 0, right: 8, top: 4, bottom: 4 }))
            .show(ui, |ui| {
                egui::ScrollArea::vertical().id_salt("how-nav-scroll").show(ui, |ui| {
                    for (id, title) in &sections {
                        let on = *id == self.state.guide.current;
                        let text = RichText::new(title).color(if on { t.text } else { t.muted });
                        if ui.selectable_label(on, text).clicked() {
                            jump = Some(id.clone());
                        }
                    }
                });
            });
        if let Some(id) = jump {
            self.state.guide.scroll_to = Some(id.clone());
            self.state.guide.current = id;
        }
        egui::CentralPanel::default().frame(egui::Frame::new()).show(ui, |ui| {
            egui::ScrollArea::vertical().id_salt("how-page").auto_shrink([false, false]).show(ui, |ui| {
                // The scroll bar takes its share on the right.
                let width = (ui.available_width() - 18.0).min(CONTENT_MAX);
                ui.set_max_width(width);
                page(ui, &t, &mut self.state.guide, width);
            });
        });
    }
}

fn page(ui: &mut Ui, t: &Theme, guide: &mut Guide, width: f32) {
    let concepts = guide.concepts().clone();
    let target = guide.scroll_to.take();
    for s in &concepts.sections {
        ui.add_space(14.0);
        let heading = ui.label(RichText::new(&s.title).size(20.0).strong().color(t.text));
        if target.as_deref() == Some(s.id.as_str()) {
            heading.scroll_to_me(Some(Align::TOP));
        }
        if !s.intro.is_empty() {
            ui.add(egui::Label::new(RichText::new(&s.intro).color(t.muted)).wrap());
        }
        if !s.cards.is_empty() {
            ui.add_space(6.0);
            cards(ui, t, s.cards.iter().map(|c| (c.title.as_str(), c.text.as_str(), None)), width);
        }
        if !s.example.is_empty() {
            ui.add_space(6.0);
            egui::Frame::new().fill(t.panel).corner_radius(CornerRadius::same(4)).inner_margin(8).show(ui, |ui| {
                ui.label(RichText::new(&s.example).monospace().color(t.text));
            });
        }
        if let Some(name) = &s.screenshot {
            ui.add_space(6.0);
            screenshot(ui, t, guide.textures.get(name.as_str()), &s.caption, width);
        }
        if let Some(part) = &s.generated {
            ui.add_space(8.0);
            match part.as_str() {
                "overview" => overview(ui, t, &concepts, width),
                "zones" => zones(ui, t, &concepts, width),
                "line" => line(ui, t, &concepts, width),
                "ball" => balls(ui, t, &concepts, width),
                "states" => states(ui, t, guide.catalog(), &concepts, width),
                "kinds" => kinds(ui, t, guide.catalog(), width),
                "keys" => keys(ui, t, guide, width),
                "rules" => your_rules(ui, t, guide.catalog(), width),
                "facts" => facts(ui, t, guide, width),
                _ => {}
            }
        }
    }
    ui.add_space(40.0);
}

/// Cards in rows: a title, a short text, an optional colour stripe.
fn cards<'a>(ui: &mut Ui, t: &Theme, items: impl Iterator<Item = (&'a str, &'a str, Option<Color32>)>, width: f32) {
    let items: Vec<_> = items.collect();
    let per_row = ((width + 8.0) / 228.0).floor().clamp(1.0, 4.0) as usize;
    let card_w = (width - 8.0 * (per_row as f32 - 1.0)) / per_row as f32;
    for row in items.chunks(per_row) {
        ui.horizontal_top(|ui| {
            ui.spacing_mut().item_spacing.x = 8.0;
            for (title, text, stripe) in row {
                let frame = egui::Frame::new()
                    .fill(t.surface)
                    .stroke(Stroke::new(1.0, t.border))
                    .corner_radius(CornerRadius::same(6))
                    .inner_margin(egui::Margin::symmetric(10, 8));
                let r = frame.show(ui, |ui| {
                    ui.vertical(|ui| {
                        ui.set_width(card_w - 22.0);
                        ui.set_min_height(54.0);
                        ui.label(RichText::new(*title).strong().color(stripe.unwrap_or(t.text)));
                        ui.add(egui::Label::new(RichText::new(*text).small().color(t.text)).wrap());
                    });
                });
                if let Some(c) = stripe {
                    let rect = r.response.rect;
                    ui.painter().rect_filled(
                        Rect::from_min_max(rect.min, pos2(rect.min.x + 3.0, rect.max.y)),
                        CornerRadius { nw: 6, sw: 6, ne: 0, se: 0 },
                        *c,
                    );
                }
            }
        });
        ui.add_space(6.0);
    }
}

fn screenshot(ui: &mut Ui, t: &Theme, texture: Option<&egui::TextureHandle>, caption: &str, width: f32) {
    let Some(tex) = texture else { return };
    let size = tex.size_vec2();
    let scale = (width / size.x).min(1.0);
    egui::Frame::new().stroke(Stroke::new(1.0, t.border)).show(ui, |ui| {
        ui.add(egui::Image::from_texture((tex.id(), size * scale)));
    });
    ui.label(RichText::new(caption).small().italics().color(t.muted));
}

// --- Diagrams --------------------------------------------------------------

/// A box with a title and a colour.
fn node(painter: &egui::Painter, t: &Theme, rect: Rect, title: &str, colour: Color32) {
    painter.rect(rect, CornerRadius::same(8), t.surface, Stroke::new(1.5, colour), egui::StrokeKind::Inside);
    painter.text(rect.center(), Align2::CENTER_CENTER, title, FontId::proportional(15.0), colour);
}

/// A double arrow between two boxes, with a label over it.
fn link(painter: &egui::Painter, t: &Theme, a: Pos2, b: Pos2, label: &str) {
    let stroke = Stroke::new(1.2, t.muted);
    painter.line_segment([a, b], stroke);
    let dir = (b - a).normalized();
    let normal = vec2(-dir.y, dir.x);
    for (tip, d) in [(b, dir), (a, -dir)] {
        painter.line_segment([tip, tip - d * 7.0 + normal * 4.0], stroke);
        painter.line_segment([tip, tip - d * 7.0 - normal * 4.0], stroke);
    }
    let mid = a + (b - a) * 0.5;
    let offset = if dir.x.abs() > 0.5 { vec2(0.0, -10.0) } else { vec2(8.0, 0.0) };
    let align = if dir.x.abs() > 0.5 { Align2::CENTER_BOTTOM } else { Align2::LEFT_CENTER };
    painter.text(mid + offset, align, label, FontId::proportional(11.0), t.muted);
}

/// You ⇄ Home ⇄ engine ⇄ agents, with the orchestrator under the engine.
fn overview(ui: &mut Ui, t: &Theme, c: &Concepts, width: f32) {
    let w = width.min(760.0);
    let (rect, _) = ui.allocate_exact_size(vec2(w, 170.0), Sense::hover());
    let p = ui.painter_at(rect);
    let box_w = (w - 3.0 * 70.0) / 4.0;
    let at = |i: f32, row: f32| {
        Rect::from_min_size(pos2(rect.min.x + i * (box_w + 70.0), rect.min.y + 22.0 + row * 92.0), vec2(box_w, 40.0))
    };
    let title = |id: &str| c.sections.iter().flat_map(|s| &s.cards).find(|k| k.title == id).map(|k| k.title.clone());
    let you = at(0.0, 0.0);
    let home = at(1.0, 0.0);
    let engine = at(2.0, 0.0);
    let agents = at(3.0, 0.0);
    let orch = Rect::from_center_size(
        pos2((engine.center().x + agents.center().x) / 2.0, engine.center().y + 92.0),
        vec2(box_w, 40.0),
    );
    node(&p, t, you, &title("You").unwrap_or("You".into()), t.accent);
    node(&p, t, home, "Home", t.primary);
    node(&p, t, engine, &title("Engine").unwrap_or("Engine".into()), t.text);
    node(&p, t, agents, &title("Agents").unwrap_or("Agents".into()), t.cyan);
    node(&p, t, orch, &title("Orchestrator").unwrap_or("Orchestrator".into()), t.cyan);
    let gap = 6.0;
    link(&p, t, you.right_center() + vec2(gap, 0.0), home.left_center() - vec2(gap, 0.0), "answers");
    link(&p, t, home.right_center() + vec2(gap, 0.0), engine.left_center() - vec2(gap, 0.0), "lines");
    link(&p, t, engine.right_center() + vec2(gap, 0.0), agents.left_center() - vec2(gap, 0.0), "spawns");
    link(&p, t, engine.center_bottom() + vec2(0.0, gap), orch.left_top() + vec2(14.0, -gap), "routes");
    link(&p, t, orch.right_top() + vec2(-14.0, -gap), agents.center_bottom() + vec2(0.0, gap), "relays");
    p.text(engine.center_top() - vec2(0.0, 6.0), Align2::CENTER_BOTTOM, "no LLM", FontId::proportional(11.0), t.muted);
}

/// The zones, top to bottom, with what is yours bracketed; then the
/// colour legend.
fn zones(ui: &mut Ui, t: &Theme, c: &Concepts, width: f32) {
    let row_h = 34.0;
    let n = c.zones.len() as f32;
    let (rect, _) = ui.allocate_exact_size(vec2(width, row_h * n + 8.0), Sense::hover());
    let p = ui.painter_at(rect);
    let left = rect.min.x + 64.0;
    let name_w = 150.0;
    let mut wrapped = vec![];
    for (i, z) in c.zones.iter().enumerate() {
        let y = rect.min.y + 4.0 + i as f32 * row_h;
        let row = Rect::from_min_size(pos2(left, y), vec2(width - 64.0, row_h - 4.0));
        let col = colour(t, &z.colour);
        p.rect_filled(row, CornerRadius::same(4), t.surface);
        p.rect_filled(Rect::from_min_size(row.min, vec2(4.0, row.height())), CornerRadius::same(2), col);
        p.text(row.left_center() + vec2(14.0, 0.0), Align2::LEFT_CENTER, &z.name, FontId::proportional(14.0), col);
        wrapped.push((row, z.text.clone()));
    }
    for (row, text) in wrapped {
        let galley = p.layout(text, FontId::proportional(12.0), t.text, row.width() - name_w - 20.0);
        let pos = pos2(row.min.x + name_w, row.center().y - galley.size().y / 2.0);
        p.galley(pos, galley, t.text);
    }
    // Brackets: the first three are yours, the rest are not.
    let bracket = |from: usize, to: usize, label: &str, col: Color32| {
        let y0 = rect.min.y + 6.0 + from as f32 * row_h;
        let y1 = rect.min.y + 2.0 + to as f32 * row_h;
        let x = left - 10.0;
        let s = Stroke::new(1.5, col);
        p.line_segment([pos2(x, y0), pos2(x, y1)], s);
        p.line_segment([pos2(x, y0), pos2(x + 5.0, y0)], s);
        p.line_segment([pos2(x, y1), pos2(x + 5.0, y1)], s);
        p.text(pos2(x - 6.0, (y0 + y1) / 2.0), Align2::RIGHT_CENTER, label, FontId::proportional(11.0), col);
    };
    let inbox = c.zones.iter().take_while(|z| z.id != "flight").count();
    bracket(0, inbox, "yours", t.accent);
    bracket(inbox, c.zones.len(), "not yours", t.muted);
    ui.add_space(10.0);
    ui.label(RichText::new("Colours").strong().color(t.text));
    legend(ui, t, c.colours.iter().map(|x| (x.id.as_str(), x.text.as_str())), width);
}

/// Colour chips with their meaning, two per row on a wide page.
fn legend<'a>(ui: &mut Ui, t: &Theme, items: impl Iterator<Item = (&'a str, &'a str)>, width: f32) {
    let cols = if width > 700.0 { 2 } else { 1 };
    egui::Grid::new(ui.next_auto_id()).num_columns(cols * 2).spacing([10.0, 6.0]).show(ui, |ui| {
        for (i, (name, text)) in items.enumerate() {
            let (r, _) = ui.allocate_exact_size(vec2(64.0, 16.0), Sense::hover());
            ui.painter().circle_filled(pos2(r.min.x + 7.0, r.center().y), 6.0, colour(t, name));
            ui.painter().text(
                pos2(r.min.x + 18.0, r.center().y),
                Align2::LEFT_CENTER,
                name,
                FontId::proportional(12.0),
                colour(t, name),
            );
            ui.allocate_ui(vec2(width / cols as f32 - 90.0, 16.0), |ui| {
                ui.add(egui::Label::new(RichText::new(text).small().color(t.text)).wrap());
            });
            if i % cols == cols - 1 {
                ui.end_row();
            }
        }
    });
}

/// One line, drawn as Home draws it, with numbered callouts.
fn line(ui: &mut Ui, t: &Theme, c: &Concepts, width: f32) {
    let parts = &c.line.example;
    if parts.is_empty() {
        return;
    }
    let colours = [t.primary, t.accent, t.text, t.cyan, t.muted, t.muted];
    let (rect, _) = ui.allocate_exact_size(vec2(width.min(820.0), 58.0), Sense::hover());
    let p = ui.painter_at(rect);
    let row = Rect::from_min_size(pos2(rect.min.x, rect.min.y + 22.0), vec2(rect.width(), 28.0));
    p.rect_filled(row, CornerRadius::same(4), t.surface);
    let mut x = row.min.x + 10.0;
    for (i, part) in parts.iter().enumerate() {
        let col = colours.get(i).copied().unwrap_or(t.text);
        let galley = p.layout_no_wrap(part.clone(), FontId::proportional(13.0), col);
        let w = galley.size().x;
        let pos = pos2(x, row.center().y - galley.size().y / 2.0);
        p.galley(pos, galley, col);
        // The callout's number, over the part.
        let centre = pos2(x + w / 2.0, rect.min.y + 9.0);
        p.circle_filled(centre, 8.0, t.panel);
        p.circle_stroke(centre, 8.0, Stroke::new(1.0, t.muted));
        p.text(centre, Align2::CENTER_CENTER, format!("{}", i + 1), FontId::proportional(10.0), t.text);
        p.line_segment([pos2(centre.x, centre.y + 8.0), pos2(centre.x, row.min.y + 3.0)], Stroke::new(1.0, t.border));
        x += w + 18.0;
    }
    ui.add_space(4.0);
    egui::Grid::new("how-line-parts").num_columns(3).spacing([10.0, 4.0]).show(ui, |ui| {
        for (i, part) in c.line.parts.iter().enumerate() {
            ui.label(RichText::new(format!("{}", i + 1)).small().strong().color(t.muted));
            ui.label(RichText::new(&part.part).monospace().color(colours.get(i).copied().unwrap_or(t.text)));
            ui.label(RichText::new(&part.text).color(t.text));
            ui.end_row();
        }
    });
}

/// The ball's values as coloured pills with what each means.
fn balls(ui: &mut Ui, t: &Theme, c: &Concepts, width: f32) {
    let items: Vec<_> = c.balls.iter().map(|b| (b.id.as_str(), b.text.as_str(), Some(colour(t, &b.colour)))).collect();
    let per_row = ((width + 8.0) / 160.0).floor().clamp(1.0, 6.0) as usize;
    let w = (width - 8.0 * (per_row as f32 - 1.0)) / per_row as f32;
    for row in items.chunks(per_row) {
        ui.horizontal_top(|ui| {
            ui.spacing_mut().item_spacing.x = 8.0;
            for (id, text, col) in row {
                ui.allocate_ui_with_layout(vec2(w, 70.0), Layout::top_down(Align::Min), |ui| {
                    ui.set_width(w);
                    pill(ui, id, col.unwrap_or(t.muted));
                    ui.add(egui::Label::new(RichText::new(*text).small().color(t.text)).wrap());
                });
            }
        });
        ui.add_space(4.0);
    }
    ui.label(RichText::new("you → ⇧O orchestrator · d agent · l others · its PR merged → done").small().color(t.muted));
}

fn pill(ui: &mut Ui, text: &str, colour: Color32) {
    egui::Frame::new()
        .stroke(Stroke::new(1.2, colour))
        .corner_radius(CornerRadius::same(10))
        .inner_margin(egui::Margin::symmetric(10, 2))
        .show(ui, |ui| {
            ui.label(RichText::new(text).strong().color(colour));
        });
}

// --- Generated tables -------------------------------------------------------

/// A cell of a table that wraps at `w`.
fn wrap_cell(ui: &mut Ui, w: f32, text: RichText) {
    ui.vertical(|ui| {
        ui.set_width(w);
        ui.add(egui::Label::new(text).wrap());
    });
}

fn action_cell(catalog: &RulesCatalog, action: Option<&str>) -> String {
    match action.and_then(|a| catalog.actions.iter().find(|x| x.id == a)) {
        Some(a) => format!("{}  {}", key_text(&a.key), a.label),
        None => String::new(),
    }
}

/// One table per zone: the inbox items and the rules that lead there.
fn states(ui: &mut Ui, t: &Theme, catalog: &RulesCatalog, concepts: &Concepts, width: f32) {
    let rules: Vec<&CatalogRule> = catalog.rules.iter().filter(|r| !r.queue_off).collect();
    let (verb_w, key_w) = (120.0, 150.0);
    let state_w = ((width - verb_w - key_w) * 0.38).max(140.0);
    let when_w = (width - verb_w - key_w - state_w - 40.0).max(160.0);
    for zone in ["now", "round", "deep", "inbox", "flight", "parked", "done"] {
        let kinds: Vec<_> = catalog.kinds.iter().filter(|k| k.level == zone).collect();
        let zr: Vec<_> = rules.iter().filter(|r| r.zone.as_deref() == Some(zone)).collect();
        if kinds.is_empty() && zr.is_empty() {
            continue;
        }
        let col = zone_colour(t, concepts, zone);
        let title =
            RichText::new(format!("{}  ·  {}", zone_name(concepts, zone), kinds.len() + zr.len())).strong().color(col);
        egui::CollapsingHeader::new(title).id_salt(("how-zone", zone)).default_open(true).show(ui, |ui| {
            egui::Grid::new(("how-states", zone)).num_columns(4).striped(true).spacing([10.0, 4.0]).show(ui, |ui| {
                for h in ["verb", "state", "when", "key"] {
                    ui.label(RichText::new(h).small().color(t.muted));
                }
                ui.end_row();
                for k in &kinds {
                    wrap_cell(ui, verb_w, RichText::new(k.verb.as_deref().unwrap_or("")).strong().color(col));
                    let state = k.state_text.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| k.title.clone());
                    wrap_cell(ui, state_w, RichText::new(state).color(t.text));
                    let when =
                        k.sentence.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| format!("({})", k.detector));
                    wrap_cell(ui, when_w, RichText::new(when).small().color(t.muted));
                    wrap_cell(
                        ui,
                        key_w,
                        RichText::new(action_cell(catalog, k.action.as_deref())).small().color(t.text),
                    );
                    ui.end_row();
                }
                for r in &zr {
                    let verb = match &r.verb {
                        Some(v) => RichText::new(v).strong().color(col),
                        None => RichText::new(r.ball_text.clone().unwrap_or_default()).color(t.muted),
                    };
                    wrap_cell(ui, verb_w, verb);
                    wrap_cell(ui, state_w, RichText::new(r.state_text.clone().unwrap_or_default()).color(t.text));
                    let when = r.sentence.clone().unwrap_or_else(|| r.when.clone());
                    wrap_cell(ui, when_w, RichText::new(when).small().color(t.muted));
                    wrap_cell(
                        ui,
                        key_w,
                        RichText::new(action_cell(catalog, r.action.as_deref())).small().color(t.text),
                    );
                    ui.end_row();
                }
            });
        });
    }
    egui::CollapsingHeader::new(
        RichText::new(format!("All rules, in order  ·  {}", catalog.rules.len())).color(t.text),
    )
    .id_salt("how-all-rules")
    .default_open(false)
    .show(ui, |ui| {
        ui.label(
            RichText::new("First match wins. Locked rules come first; queue off: an engine without the inbox queue.")
                .small()
                .color(t.muted),
        );
        egui::Grid::new("how-all-rules-grid").num_columns(4).striped(true).spacing([10.0, 4.0]).show(ui, |ui| {
            for (i, r) in catalog.rules.iter().enumerate() {
                ui.label(RichText::new(format!("{}", i + 1)).small().color(t.muted));
                let mut id = r.id.clone();
                if r.locked {
                    id.push_str("  locked");
                }
                if r.queue_off {
                    id.push_str("  queue off");
                }
                wrap_cell(ui, 190.0, RichText::new(id).monospace().small().color(t.text));
                wrap_cell(ui, 200.0, RichText::new(r.state_text.clone().unwrap_or_default()).small().color(t.text));
                wrap_cell(
                    ui,
                    (width - 460.0).max(160.0),
                    RichText::new(r.sentence.clone().unwrap_or_default()).small().color(t.muted),
                );
                ui.end_row();
            }
        });
    });
}

fn kinds(ui: &mut Ui, t: &Theme, catalog: &RulesCatalog, width: f32) {
    let rest = (width - 120.0 - 170.0 - 70.0 - 60.0 - 50.0).max(140.0);
    egui::Grid::new("how-kinds").num_columns(5).striped(true).spacing([10.0, 4.0]).show(ui, |ui| {
        for h in ["verb", "item", "level", "blocks", "key"] {
            ui.label(RichText::new(h).small().color(t.muted));
        }
        ui.end_row();
        for k in &catalog.kinds {
            let col = match k.level.as_str() {
                "now" => t.yellow,
                "deep" => t.purple,
                _ => t.accent,
            };
            wrap_cell(ui, 120.0, RichText::new(k.verb.as_deref().unwrap_or("")).strong().color(col));
            wrap_cell(ui, 170.0, RichText::new(&k.title).color(t.text));
            ui.label(RichText::new(&k.level).small().color(col));
            ui.label(RichText::new(if k.blocks_agent { "an agent" } else { "" }).small().color(t.yellow));
            wrap_cell(ui, rest, RichText::new(action_cell(catalog, k.action.as_deref())).small().color(t.text));
            ui.end_row();
        }
    });
}

fn place_name(place: &str) -> &'static str {
    match place {
        "home" => "Home",
        "decision" => "Decision view",
        "page" => "This page",
        "why" => "Why?",
        _ => "",
    }
}

fn matches(filter: &str, texts: &[&str]) -> bool {
    let f = filter.trim().to_lowercase();
    f.is_empty() || texts.iter().any(|t| t.to_lowercase().contains(&f))
}

/// The keys, searchable, then the catalog's actions.
fn keys(ui: &mut Ui, t: &Theme, guide: &mut Guide, width: f32) {
    ui.horizontal(|ui| {
        ui.label(RichText::new("Filter").small().color(t.muted));
        ui.add(
            egui::TextEdit::singleline(&mut guide.key_filter)
                .hint_text("a key or a word: park, ⇧, brief")
                .desired_width(260.0),
        );
        if !guide.key_filter.is_empty() && ui.small_button("clear").clicked() {
            guide.key_filter.clear();
        }
    });
    let filter = guide.key_filter.clone();
    let keys = guide.keys();
    let actions = guide.catalog().actions.clone();
    let does_w = (width - 120.0 - 110.0 - 30.0).max(160.0);
    let mut shown = 0;
    egui::Grid::new("how-keys").num_columns(3).striped(true).spacing([12.0, 4.0]).show(ui, |ui| {
        for k in keys.iter().filter(|k| matches(&filter, &[&k.keys, &k.does, place_name(&k.place)])) {
            ui.label(RichText::new(&k.keys).monospace().strong().color(t.text));
            ui.label(RichText::new(place_name(&k.place)).small().color(t.muted));
            wrap_cell(ui, does_w, RichText::new(&k.does).color(t.text));
            ui.end_row();
            shown += 1;
        }
    });
    if shown == 0 {
        ui.label(RichText::new("No key matches.").small().color(t.muted));
    }
    ui.add_space(8.0);
    ui.label(RichText::new("Actions (what a rule's line offers)").strong().color(t.text));
    egui::Grid::new("how-actions").num_columns(3).striped(true).spacing([12.0, 4.0]).show(ui, |ui| {
        for a in actions.iter().filter(|a| matches(&filter, &[&a.id, &a.key, &a.label])) {
            ui.label(RichText::new(key_text(&a.key)).monospace().strong().color(t.text));
            ui.label(RichText::new(&a.id).small().monospace().color(t.muted));
            wrap_cell(ui, does_w, RichText::new(&a.label).color(t.text));
            ui.end_row();
        }
    });
}

fn your_rules(ui: &mut Ui, t: &Theme, catalog: &RulesCatalog, width: f32) {
    for f in &catalog.files {
        let state =
            if !f.exists { "absent".to_string() } else { f.applied.clone().unwrap_or_default().replace('_', " ") };
        ui.label(RichText::new(format!("{} · {} · {state}, {} rules", f.scope, f.path, f.rules)).small().color(t.text));
    }
    let mine: Vec<_> = catalog.rules.iter().filter(|r| r.origin != "builtin").collect();
    if mine.is_empty() {
        ui.label(RichText::new("No rules of yours: the built-ins apply.").color(t.muted));
    }
    for r in mine {
        egui::Frame::new().fill(t.surface).corner_radius(CornerRadius::same(4)).inner_margin(6).show(ui, |ui| {
            ui.set_width(width - 12.0);
            ui.horizontal_wrapped(|ui| {
                ui.label(RichText::new(&r.id).monospace().strong().color(t.accent));
                let at = match (&r.file, r.line) {
                    (Some(f), Some(l)) => format!("{} · {f}:{l}", r.origin),
                    _ => r.origin.clone(),
                };
                ui.label(RichText::new(at).small().color(t.muted));
                if r.replaces.as_ref().is_some_and(|v| !v.is_null()) {
                    ui.label(RichText::new("replaces the built-in").small().color(t.purple));
                }
            });
            if let Some(s) = &r.sentence {
                ui.add(egui::Label::new(RichText::new(s).small().color(t.text)).wrap());
            }
            let out = r.state_text.clone().filter(|s| !s.is_empty()).or(r.ball_text.clone()).unwrap_or_default();
            ui.label(RichText::new(format!("→ {out}")).small().color(t.text));
        });
    }
    for p in &catalog.errors {
        let at = format!(
            "{}{}",
            p.file.clone().unwrap_or("rules".into()),
            p.line.map(|l| format!(":{l}")).unwrap_or_default()
        );
        ui.add(
            egui::Label::new(
                RichText::new(format!("error · {at}: {} ({})", p.message, p.fallback)).small().color(t.red),
            )
            .wrap(),
        );
    }
    for p in &catalog.warnings {
        let at = format!(
            "{}{}",
            p.file.clone().unwrap_or("rules".into()),
            p.line.map(|l| format!(":{l}")).unwrap_or_default()
        );
        ui.add(
            egui::Label::new(RichText::new(format!("warning · {at}: {}", p.message)).small().color(t.yellow)).wrap(),
        );
    }
}

fn facts(ui: &mut Ui, t: &Theme, guide: &mut Guide, width: f32) {
    ui.horizontal(|ui| {
        ui.label(RichText::new("Filter").small().color(t.muted));
        ui.add(egui::TextEdit::singleline(&mut guide.fact_filter).hint_text("pr, ci, agent…").desired_width(260.0));
    });
    let filter = guide.fact_filter.clone();
    let facts = &guide.catalog().facts;
    let phrase_w = (width - 200.0 - 140.0 - 130.0 - 60.0).max(140.0);
    egui::Grid::new("how-facts").num_columns(4).striped(true).spacing([10.0, 3.0]).show(ui, |ui| {
        for h in ["fact", "is", "type", "source"] {
            ui.label(RichText::new(h).small().color(t.muted));
        }
        ui.end_row();
        for f in facts.iter().filter(|f| matches(&filter, &[&f.name, &f.phrase, &f.source])) {
            wrap_cell(ui, 200.0, RichText::new(&f.name).monospace().small().color(t.text));
            wrap_cell(ui, phrase_w, RichText::new(&f.phrase).small().color(t.text));
            let kind =
                if f.values.is_empty() { f.kind.clone() } else { format!("{} ({})", f.kind, f.values.join(", ")) };
            wrap_cell(ui, 140.0, RichText::new(kind).small().color(t.muted));
            wrap_cell(ui, 130.0, RichText::new(format!("{} · {}", f.source, f.cost)).small().color(t.muted));
            ui.end_row();
        }
    });
}

/// The zone and the "when" of a catalog rule, for the Why view (the
/// engine's catalog, else the copy).
pub fn rule_text<'a>(catalog: &'a RulesCatalog, id: &str) -> Option<&'a CatalogRule> {
    catalog.rules.iter().find(|r| r.id == id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_copy_parses_and_names_every_screenshot_embedded() {
        let c = builtin();
        assert!(c.rules.iter().any(|r| r.id == "merge" && r.verb.as_deref() == Some("Merge")));
        let named: Vec<&str> = c.concepts.sections.iter().filter_map(|s| s.screenshot.as_deref()).collect();
        let embedded: Vec<&str> = SCREENSHOTS.iter().map(|(n, _)| *n).collect();
        assert_eq!(named.len(), embedded.len());
        assert!(named.iter().all(|n| embedded.contains(n)), "{named:?} vs {embedded:?}");
        assert!(c.concepts.sections.iter().any(|s| s.id == HOME_SECTION));
    }

    #[test]
    fn the_hint_bar_names_only_keys_the_page_lists() {
        let bar = crate::home_view::hint_keys(true, true, true, true, true);
        let keys = &builtin().concepts.keys;
        for key in ["⇧X", "⇧U", "⇧O", "⇧Z", "⇧F", "⇧M", "⇧Y"] {
            assert!(bar.contains(key), "the hint bar lacks {key}");
            assert!(keys.iter().any(|k| k.keys.contains(key)), "the page lacks {key}");
        }
        // Every single key the bar names ("x dismiss", "w why") is on the page.
        for hint in bar.split(" · ") {
            let raw = hint.split_whitespace().next().unwrap();
            let key = if raw.chars().count() == 1 { raw.to_uppercase() } else { raw.to_string() };
            assert!(
                keys.iter()
                    .any(|k| (k.place == "home" && k.keys.split_whitespace().any(|x| x == key)) || k.keys == key),
                "the page lacks the key of {hint:?}"
            );
        }
        assert!(!bar.contains("Shift+"), "one way to write a shifted key");
        assert!(keys.iter().all(|k| !k.keys.contains("Shift+")));
    }

    #[test]
    fn the_doc_names_the_same_keys_and_screenshots() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../docs/guides/how-lazyagent-works.md");
        let doc = std::fs::read_to_string(path).unwrap();
        for k in &builtin().concepts.keys {
            assert!(doc.contains(&format!("`{}`", k.keys)), "how-lazyagent-works.md lacks the key `{}`", k.keys);
        }
        for (name, _) in SCREENSHOTS {
            assert!(doc.contains(&format!("assets/how/{name}.png")), "the doc lacks the {name} screenshot");
        }
        for s in &builtin().concepts.sections {
            assert!(doc.contains(&format!("## {}", s.title)), "the doc lacks ## {}", s.title);
        }
    }
}
