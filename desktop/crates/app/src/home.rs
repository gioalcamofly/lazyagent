//! Home (proposal 0013, I3): the work items, one line each. The inbox on
//! top in two levels, **Now** and **Round**, grouped by work item (its top
//! item and "+N"); everything else **in flight** below, with who has the
//! ball, a short fact and its age.
//!
//! The lines come from the engine (`status_list` rows and the
//! `actionables_list` items with no work item, kept current by `status.*` /
//! `actionable.*`) or, with an engine that lacks those features, from the
//! client's own attention queue and sidebar state ([`fallback_lines`]).
//!
//! [`Home`] decides what is shown where. Now lines show at once. A line that
//! enters Round waits for a **cut point** (the user closes an item, comes back
//! from a terminal, or [`ROUND_EVERY`] passes; Q-B9), counted as "next round"
//! and left where it was until then. Between cut points nothing reorders:
//! lines keep their places, new ones are added at the end of their zone, and
//! only a cut sorts again.

use std::collections::{BTreeMap, HashMap, HashSet};

use lazyagent_proto::types::{Actionable, Claim, PrInfo, StatusRow};

use crate::attention::{self, Kind};
use crate::store::{self, ORCHESTRATOR_KEY, Store};

/// A Round is delivered at least this often (seconds).
pub const ROUND_EVERY: f64 = 30.0 * 60.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Zone {
    Now,
    Round,
    Flight,
}

/// An inbox item, as a line shows it.
#[derive(Debug, Clone, PartialEq)]
pub struct Top {
    pub id: String,
    pub kind: String,
    pub agent_id: Option<String>,
    pub label: String,
    pub detail: String,
    pub pr_url: Option<String>,
    pub since: f64,
}

/// One line of Home: a work item, or an item that has none (the
/// orchestrator's dialogs, a failed job).
#[derive(Debug, Clone, PartialEq)]
pub struct Line {
    pub key: String,
    pub name: String,
    /// The worktree path (or the orchestrator key) to open.
    pub place: Option<String>,
    pub zone: Zone,
    pub ball: String,
    pub state: String,
    pub priority: String,
    pub blocks_agent: bool,
    pub changed_at: f64,
    pub top: Option<Top>,
    /// Items behind the top one.
    pub more: u32,
    pub url: Option<String>,
    pub row: Option<StatusRow>,
    pub claim: Option<Claim>,
}

impl Line {
    /// The one-word action an inbox line asks for.
    pub fn verb(&self) -> &'static str {
        let Some(top) = &self.top else {
            return if self.row.as_ref().is_some_and(|r| r.rule == 4) { "Merge" } else { "Look" };
        };
        match top.kind.as_str() {
            "approval" => "Approve",
            "question" => "Answer",
            "done" => "Review",
            "ci" => "Fix CI",
            "review" => "Changes",
            "conflict" => "Conflict",
            "hanged" => "Check",
            "failed" | "exited" => "Relaunch",
            "job" => "Job",
            "delegation" => "Decide",
            _ => "Look",
        }
    }

    /// The agent to open for this line: the top item's, else the first.
    pub fn agent(&self) -> Option<String> {
        self.top
            .as_ref()
            .and_then(|t| t.agent_id.clone())
            .or_else(|| self.row.as_ref().and_then(|r| r.agents.first().map(|a| a.id.clone())))
    }
}

fn level_zone(level: Option<&str>) -> Zone {
    match level {
        Some("now") => Zone::Now,
        Some(_) => Zone::Round,
        None => Zone::Flight,
    }
}

fn top_of(item: &Actionable) -> Top {
    Top {
        id: item.id.clone(),
        kind: item.kind.clone(),
        agent_id: item.agent_id.clone(),
        label: item.label.clone(),
        detail: item.detail.clone(),
        pr_url: item.pr.as_ref().and_then(|p| p.url.clone()),
        since: item.since,
    }
}

/// `branch #12`, the way the engine's table names an item.
pub fn row_name(row: &StatusRow) -> String {
    let mut name =
        row.branch.clone().unwrap_or_else(|| row.key.split_once(':').map(|x| x.1).unwrap_or(&row.key).into());
    if let Some(pr) = &row.pr {
        name.push_str(&format!(" #{}", pr.number));
    }
    name
}

/// The lines of an engine with the `status` and `actionables` features.
pub fn engine_lines(rows: &BTreeMap<String, StatusRow>, items: &BTreeMap<String, Actionable>) -> Vec<Line> {
    let mut out = vec![];
    for row in rows.values() {
        if row.stale || row.ball == "done" {
            continue;
        }
        let zone = if row.zone == "inbox" { level_zone(row.level.as_deref()) } else { Zone::Flight };
        let top = row.needs_you.as_ref().and_then(|n| items.get(&n.top)).map(top_of);
        let count = row.needs_you.as_ref().map(|n| n.count).unwrap_or(0);
        out.push(Line {
            key: row.key.clone(),
            name: row_name(row),
            place: row.worktree_path.clone(),
            zone,
            ball: row.ball.clone(),
            state: row.state.clone(),
            priority: row.priority.clone(),
            blocks_agent: row.blocks_agent,
            changed_at: row.changed_at,
            more: count.saturating_sub(1),
            url: row.urls.first().cloned().or_else(|| top.as_ref().and_then(|t| t.pr_url.clone())),
            top,
            row: Some(row.clone()),
            claim: row.claim.clone(),
        });
    }
    out.extend(loose_lines(items.values().filter(|i| i.work_item.is_none()).map(|i| Loose {
        top: top_of(i),
        zone: level_zone(Some(&i.level)),
        state: i.state.clone(),
        blocks: i.blocks_agent,
        place: i.worktree_path.clone(),
    })));
    out
}

/// An item with no work item.
#[derive(Clone)]
struct Loose {
    top: Top,
    zone: Zone,
    state: String,
    blocks: bool,
    place: Option<String>,
}

/// Items with no work item, one line per place (the orchestrator; a job).
fn loose_lines(items: impl Iterator<Item = Loose>) -> Vec<Line> {
    let mut groups: BTreeMap<String, Vec<Loose>> = BTreeMap::new();
    for it in items {
        let group = it.place.clone().unwrap_or_else(|| it.top.id.clone());
        groups.entry(group).or_default().push(it);
    }
    groups
        .into_iter()
        .map(|(group, mut its)| {
            its.sort_by(|a, b| {
                a.zone.cmp(&b.zone).then(b.blocks.cmp(&a.blocks)).then(a.top.since.total_cmp(&b.top.since))
            });
            let Loose { top, zone, state, blocks, place } = its[0].clone();
            let name = if place.as_deref() == Some(ORCHESTRATOR_KEY) {
                "orchestrator".to_string()
            } else if place.is_none() {
                top.label.clone()
            } else {
                store::basename(place.as_deref().unwrap()).to_string()
            };
            Line {
                key: format!("loose:{group}"),
                name,
                place,
                zone,
                ball: "you".into(),
                state,
                priority: "medium".into(),
                blocks_agent: blocks,
                changed_at: top.since,
                more: its.len() as u32 - 1,
                url: top.pr_url.clone(),
                top: Some(top),
                row: None,
                claim: None,
            }
        })
        .collect()
}

fn kind_name(kind: Kind) -> &'static str {
    match kind {
        Kind::Approval => "approval",
        Kind::Question => "question",
        Kind::Hanged => "hanged",
        Kind::Failed => "failed",
        Kind::Exited => "exited",
        Kind::Ci => "ci",
        Kind::Review => "review",
        Kind::Conflict => "conflict",
        Kind::Done => "done",
        Kind::Job => "job",
    }
}

fn fallback_state(item: &attention::Item) -> String {
    match item.kind {
        Kind::Approval => format!("approve: {}", item.detail),
        Kind::Question => "answer".into(),
        Kind::Done => "review: agent finished".into(),
        Kind::Ci => format!("CI red: {}", item.detail),
        _ => item.kind.title().to_lowercase(),
    }
}

/// The lines of an engine without the `status` / `actionables` features:
/// the client's attention queue, by worktree, and every worktree's facts.
/// `changed` keeps when each line's ball and zone last changed.
pub fn fallback_lines(
    store: &Store,
    items: &[attention::Item],
    changed: &mut HashMap<String, (String, f64)>,
    now: f64,
) -> Vec<Line> {
    let level = |k: Kind| if matches!(k, Kind::Approval | Kind::Question) { Zone::Now } else { Zone::Round };
    let top = |i: &attention::Item| Top {
        id: i.id.clone(),
        kind: kind_name(i.kind).into(),
        agent_id: i.agent_id.clone(),
        label: i.label.clone(),
        detail: i.detail.clone(),
        pr_url: i.pr.as_ref().and_then(|p| p.url.clone()),
        since: i.since,
    };
    let mut by_place: HashMap<&str, Vec<&attention::Item>> = HashMap::new();
    for i in items {
        by_place.entry(i.key.as_str()).or_default().push(i);
    }
    let mut out = vec![];
    let mut keys = HashSet::new();
    for w in &store.worktrees {
        if w.is_bare || (w.is_main && w.agents.is_empty() && !by_place.contains_key(w.path.as_str())) {
            continue;
        }
        let pr: Option<&PrInfo> = store.pr_info.get(&w.path).and_then(|p| p.as_ref());
        let name = {
            let mut n = w.branch.clone().unwrap_or_else(|| store::basename(&w.path).to_string());
            if let Some(pr) = pr {
                n.push_str(&format!(" #{}", pr.number));
            }
            n
        };
        let mut mine: Vec<&attention::Item> = by_place.get(w.path.as_str()).cloned().unwrap_or_default();
        mine.sort_by(|a, b| level(a.kind).cmp(&level(b.kind)).then(a.kind.rank().cmp(&b.kind.rank())));
        let running = w.agents.iter().find(|a| a.status == "running");
        let (zone, ball, state) = match mine.first() {
            Some(i) => (level(i.kind), "you".to_string(), fallback_state(i)),
            None if pr.is_some_and(|p| p.state == "MERGED" || p.state == "CLOSED") => continue,
            None => match (running, pr) {
                (Some(a), _) => {
                    let d = if a.detail.is_empty() { "working".to_string() } else { a.detail.clone() };
                    (Zone::Flight, "agent".into(), d)
                }
                (None, Some(p)) if store::ci_summary(p) == Some("pending") => {
                    (Zone::Flight, "agent".into(), "CI running".into())
                }
                (None, Some(_)) => (Zone::Flight, "others".into(), "PR open".into()),
                (None, None) if !w.agents.is_empty() => (Zone::Flight, "agent".into(), "agent idle".into()),
                (None, None) => (Zone::Flight, "stalled".into(), "no agent".into()),
            },
        };
        let mark = format!("{zone:?}/{ball}");
        let at = match changed.get(&w.path) {
            Some((m, at)) if *m == mark => *at,
            _ => {
                changed.insert(w.path.clone(), (mark, now));
                now
            }
        };
        keys.insert(w.path.clone());
        out.push(Line {
            key: w.path.clone(),
            name,
            place: Some(w.path.clone()),
            zone,
            ball,
            state,
            priority: "medium".into(),
            blocks_agent: mine.first().is_some_and(|i| level(i.kind) == Zone::Now),
            changed_at: at,
            top: mine.first().map(|i| top(i)),
            more: mine.len().saturating_sub(1) as u32,
            url: pr.and_then(|p| p.url.clone()),
            row: None,
            claim: None,
        });
    }
    changed.retain(|k, _| keys.contains(k));
    let listed: HashSet<&str> = store.worktrees.iter().map(|w| w.path.as_str()).collect();
    out.extend(loose_lines(items.iter().filter(|i| !listed.contains(i.key.as_str())).map(|i| Loose {
        top: top(i),
        zone: level(i.kind),
        state: fallback_state(i),
        blocks: level(i.kind) == Zone::Now,
        place: (!i.key.is_empty()).then(|| i.key.clone()),
    })));
    out
}

fn priority(p: &str) -> u8 {
    match p {
        "high" => 0,
        "low" => 2,
        _ => 1,
    }
}

fn ball(b: &str) -> u8 {
    match b {
        "agent" => 0,
        "others" => 1,
        "stalled" => 2,
        "done" => 3,
        _ => 4,
    }
}

/// The engine's order (status.py `sort_key`): inbox by level, blocks an
/// agent, priority, oldest first; in flight by ball, priority, newest first.
pub fn cmp(a: &Line, b: &Line) -> std::cmp::Ordering {
    a.zone.cmp(&b.zone).then_with(|| {
        if a.zone == Zone::Flight {
            ball(&a.ball)
                .cmp(&ball(&b.ball))
                .then(priority(&a.priority).cmp(&priority(&b.priority)))
                .then(b.changed_at.total_cmp(&a.changed_at))
                .then(a.key.cmp(&b.key))
        } else {
            b.blocks_agent
                .cmp(&a.blocks_agent)
                .then(priority(&a.priority).cmp(&priority(&b.priority)))
                .then(a.changed_at.total_cmp(&b.changed_at))
                .then(a.key.cmp(&b.key))
        }
    })
}

/// What Home shows, and in which order; see the module docstring.
#[derive(Debug, Default)]
pub struct Home {
    pub lines: HashMap<String, Line>,
    order: [Vec<String>; 3],
    /// The Round as delivered at the last cut point.
    delivered: HashSet<String>,
    pub round_at: f64,
    /// Round lines that left on their own since the cut.
    pub resolved: u32,
    pub selected: Option<String>,
    pub expanded: Option<String>,
    /// The park choices are open on this line.
    pub parking: Option<String>,
    /// The first complete lines came: from then on, nothing reorders
    /// between cut points (before, every update is a cut).
    pub ready: bool,
}

impl Home {
    /// Where a line shows: Round only once delivered, else where it was.
    fn shown(&self, line: &Line, before: Option<Zone>) -> Zone {
        match line.zone {
            Zone::Round if self.delivered.contains(&line.key) => Zone::Round,
            Zone::Round => match before {
                Some(Zone::Round) | None | Some(Zone::Now) => Zone::Flight,
                Some(z) => z,
            },
            z => z,
        }
    }

    fn zone_of(&self, key: &str) -> Option<Zone> {
        [Zone::Now, Zone::Round, Zone::Flight].into_iter().find(|z| self.order[*z as usize].iter().any(|k| k == key))
    }

    /// Takes the latest lines; nothing already shown moves but a Now line,
    /// a line leaving the inbox, or a line that is gone.
    pub fn update(&mut self, lines: Vec<Line>, now: f64) {
        self.lines = lines.into_iter().map(|l| (l.key.clone(), l)).collect();
        if !self.ready {
            self.cut(now);
            return;
        }
        for z in 0..3 {
            let lines = &self.lines;
            let before = self.order[z].len();
            self.order[z].retain(|k| lines.contains_key(k));
            if z == Zone::Round as usize {
                self.resolved += (before - self.order[z].len()) as u32;
            }
        }
        let mut keys: Vec<&Line> = self.lines.values().collect();
        keys.sort_by(|a, b| cmp(a, b));
        let keys: Vec<String> = keys.into_iter().map(|l| l.key.clone()).collect();
        for key in keys {
            let before = self.zone_of(&key);
            let target = self.shown(&self.lines[&key], before);
            if before == Some(target) {
                continue;
            }
            if let Some(b) = before {
                self.order[b as usize].retain(|k| k != &key);
                if b == Zone::Round {
                    self.resolved += 1;
                }
            }
            self.order[target as usize].push(key);
        }
        self.delivered.retain(|k| self.lines.get(k).is_some_and(|l| l.zone == Zone::Round));
    }

    /// A cut point: deliver the waiting Round, sort everything again.
    pub fn cut(&mut self, now: f64) {
        let mut all: Vec<&Line> = self.lines.values().collect();
        all.sort_by(|a, b| cmp(a, b));
        self.order = Default::default();
        self.delivered.clear();
        for l in all {
            if l.zone == Zone::Round {
                self.delivered.insert(l.key.clone());
            }
            self.order[l.zone as usize].push(l.key.clone());
        }
        self.round_at = now;
        self.resolved = 0;
    }

    /// Every [`ROUND_EVERY`]: a cut, when something waits for it.
    pub fn tick(&mut self, now: f64) {
        if now - self.round_at >= ROUND_EVERY {
            if self.waiting() > 0 || self.resolved > 0 {
                self.cut(now);
            } else {
                self.round_at = now;
            }
        }
    }

    /// Round lines held back until the next cut point.
    pub fn waiting(&self) -> usize {
        self.lines.values().filter(|l| l.zone == Zone::Round && !self.delivered.contains(&l.key)).count()
    }

    pub fn shown_in(&self, zone: Zone) -> Vec<&Line> {
        self.order[zone as usize].iter().filter_map(|k| self.lines.get(k)).collect()
    }

    /// The selectable lines, top to bottom.
    pub fn visible(&self, folded: bool) -> Vec<&Line> {
        let mut v = self.shown_in(Zone::Now);
        v.extend(self.shown_in(Zone::Round));
        if !folded {
            v.extend(self.shown_in(Zone::Flight));
        }
        v
    }

    /// Now and Round as the engine counts them (for the badges).
    pub fn counts(&self) -> (usize, usize) {
        let n = |z| self.lines.values().filter(|l| l.zone == z).count();
        (n(Zone::Now), n(Zone::Round))
    }

    pub fn select_next(&mut self, delta: i32, folded: bool) {
        let keys: Vec<String> = self.visible(folded).iter().map(|l| l.key.clone()).collect();
        if keys.is_empty() {
            self.selected = None;
            return;
        }
        let at = self.selected.as_ref().and_then(|s| keys.iter().position(|k| k == s));
        let next = match at {
            None => 0,
            Some(i) => (i as i32 + delta).clamp(0, keys.len() as i32 - 1) as usize,
        };
        self.selected = Some(keys[next].clone());
    }

    /// The selected line (the first one when none is, or it went away).
    pub fn current(&mut self, folded: bool) -> Option<Line> {
        let visible = self.visible(folded);
        let found = self.selected.as_ref().and_then(|s| visible.iter().find(|l| &l.key == s)).map(|l| (*l).clone());
        let line = found.or_else(|| visible.first().map(|l| (*l).clone()));
        self.selected = line.as_ref().map(|l| l.key.clone());
        line
    }
}

/// `<1m`, `4m`, `2h`, `3d`: never `0s` (the engine's `format_age`).
pub fn age(seconds: f64) -> String {
    let s = seconds.max(0.0);
    if s < 60.0 {
        return "<1m".into();
    }
    let m = (s / 60.0) as u64;
    if m < 60 {
        return format!("{m}m");
    }
    let h = m / 60;
    if h < 48 { format!("{h}h") } else { format!("{}d", h / 24) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    pub(crate) fn row(key: &str, zone: &str, level: Option<&str>, ball: &str, changed_at: f64) -> StatusRow {
        serde_json::from_value(json!({
            "key": format!("shop:{key}"), "repo": "shop", "branch": key, "worktree_path": format!("/w/shop-{key}"),
            "title": key, "urls": [], "zone": zone, "level": level, "ball": ball, "priority": "medium",
            "state": "working", "rule": 9, "blocks_agent": level == Some("now"), "needs_you": null, "agents": [],
            "pr": null, "note": null, "gesture": null, "claim": null, "stale": false, "changed_at": changed_at,
            "fingerprint": "f"
        }))
        .unwrap()
    }

    fn lines(rows: &[StatusRow]) -> Vec<Line> {
        engine_lines(&rows.iter().map(|r| (r.key.clone(), r.clone())).collect(), &BTreeMap::new())
    }

    fn keys(h: &Home, z: Zone) -> Vec<String> {
        h.shown_in(z).iter().map(|l| l.name.clone()).collect()
    }

    #[test]
    fn nothing_reorders_between_cut_points() {
        let mut h = Home::default();
        let mut rows = vec![row("a", "in_flight", None, "agent", 30.0), row("b", "in_flight", None, "agent", 20.0)];
        h.update(lines(&rows), 100.0);
        h.ready = true;
        assert_eq!(keys(&h, Zone::Flight), ["a", "b"], "newest first at a cut");
        h.selected = Some("shop:b".into());
        // b changes (would sort first now) and a new c arrives that sorts first too.
        rows[1].changed_at = 90.0;
        rows.push(row("c", "in_flight", None, "agent", 95.0));
        h.update(lines(&rows), 110.0);
        assert_eq!(keys(&h, Zone::Flight), ["a", "b", "c"], "kept in place; the new one at the end");
        assert_eq!(h.selected.as_deref(), Some("shop:b"));
        h.cut(120.0);
        assert_eq!(keys(&h, Zone::Flight), ["c", "b", "a"]);
    }

    #[test]
    fn round_waits_for_a_cut_point_and_now_does_not() {
        let mut h = Home::default();
        let mut rows = vec![row("a", "in_flight", None, "agent", 1.0), row("b", "in_flight", None, "agent", 2.0)];
        h.update(lines(&rows), 100.0);
        h.ready = true;
        rows[0] = row("a", "inbox", Some("round"), "you", 101.0);
        rows[1] = row("b", "inbox", Some("now"), "you", 101.0);
        rows.push(row("n", "inbox", Some("round"), "you", 101.0));
        h.update(lines(&rows), 102.0);
        assert_eq!(keys(&h, Zone::Now), ["b"], "Now at once");
        assert!(keys(&h, Zone::Round).is_empty());
        assert_eq!(keys(&h, Zone::Flight), ["a", "n"], "each shown once, where it was");
        assert_eq!(h.waiting(), 2);
        assert_eq!(h.counts(), (1, 2));
        h.tick(100.0 + ROUND_EVERY - 1.0);
        assert_eq!(h.waiting(), 2);
        h.tick(100.0 + ROUND_EVERY);
        assert_eq!(keys(&h, Zone::Round), ["a", "n"]);
        assert_eq!(h.waiting(), 0);
        // One resolves on its own: it leaves the Round, counted.
        rows[0] = row("a", "in_flight", None, "agent", 200.0);
        h.update(lines(&rows), 2000.0);
        assert_eq!(keys(&h, Zone::Round), ["n"]);
        assert_eq!(h.resolved, 1);
        assert_eq!(keys(&h, Zone::Flight), ["a"]);
    }

    #[test]
    fn a_work_item_shows_once_with_its_top_item() {
        let mut r = row("cart", "inbox", Some("now"), "you", 1.0);
        r.needs_you = Some(lazyagent_proto::types::NeedsYou { count: 3, top: "Approval:/w/shop-cart/a1".into() });
        let item: Actionable = serde_json::from_value(json!({
            "id": "Approval:/w/shop-cart/a1", "occurrence": "x@1", "kind": "approval", "level": "now",
            "title": "Needs approval", "label": "Agent a1", "detail": "npm test", "state": "approve: npm test",
            "work_item": "shop:cart", "worktree_path": "/w/shop-cart", "agent_id": "a1", "pr": null, "job_id": null,
            "exit_code": null, "blocks_agent": true, "priority": "medium", "since": 1.0, "park": null
        }))
        .unwrap();
        let mut orch = item.clone();
        (orch.id, orch.work_item, orch.worktree_path) =
            ("Approval:__orchestrator__/".into(), None, Some(ORCHESTRATOR_KEY.into()));
        let rows = BTreeMap::from([(r.key.clone(), r)]);
        let items = BTreeMap::from([(item.id.clone(), item), (orch.id.clone(), orch)]);
        let got = engine_lines(&rows, &items);
        assert_eq!(got.len(), 2, "the work item once, the orchestrator's dialog on its own line");
        assert_eq!((got[0].more, got[0].verb(), got[0].agent().as_deref()), (2, "Approve", Some("a1")));
        assert_eq!((got[1].name.as_str(), got[1].zone), ("orchestrator", Zone::Now));
    }

    #[test]
    fn ages_never_say_zero_seconds() {
        assert_eq!(
            [age(0.0), age(59.0), age(240.0), age(7200.0), age(3.0 * 86400.0)],
            ["<1m", "<1m", "4m", "2h", "3d"]
        );
    }
}
