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
//!
//! I4 adds a third inbox zone, **To sit down with** ([`Zone::Deep`]): items
//! that need reading (briefs, changes requested, a finished branch). They
//! show at once and stay outside the rounds; `deep_max` is shown, never
//! enforced. Done rows (merged, closed by the user) stay 24 h, dimmed.
//!
//! I4d adds **Parked** ([`Zone::Parked`]): a work item the user parked, its
//! inbox items with it, in a group folded by default under In flight. Its
//! questions and briefs go to the orchestrator when it may answer them;
//! otherwise the parked line says it needs you.
//!
//! I4g: a deep item under a quicker top one is a **ghost line** in To sit
//! down with ([`Line::ghost`]): a line of its own, a keyboard stop like any
//! other, whose top is that deep item (its brief, its gestures). Home picks
//! lines by [`Line::sel`], so a ghost is never its Round line.

use std::collections::{BTreeMap, HashMap, HashSet};

use lazyagent_proto::types::{
    Actionable, Approvable, Attestation, CiRun, Claim, Decision, NeedsYouItem, PrInfo, ReviewCommits, StatusRow,
};

use crate::attention::{self, Kind};
use crate::store::{self, ORCHESTRATOR_KEY, Store};

/// A Round is delivered at least this often (seconds).
pub const ROUND_EVERY: f64 = 30.0 * 60.0;

/// A done row (a tombstone) stays on Home this long, dimmed.
pub const DONE_FOR: f64 = 86400.0;

/// An orchestrator's decision shows on its row this long.
pub const DECIDED_FOR: f64 = 86400.0;

/// An agent's report on a working line is cut to this many characters.
pub const REPORT_CHARS: usize = 80;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Zone {
    Now,
    Round,
    /// To sit down with (level `deep`).
    Deep,
    Flight,
    /// The user parked the whole work item (feature `parked`).
    Parked,
}

pub const ZONES: [Zone; 5] = [Zone::Now, Zone::Round, Zone::Deep, Zone::Flight, Zone::Parked];

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
    /// A `decision` item's brief.
    pub decision_id: Option<String>,
    /// A CI item's red run on the head (feature `actions`).
    pub run: Option<CiRun>,
    /// A `rule` item's verb (feature `rules`: Merge).
    pub verb: Option<String>,
    /// An `attestation_confirm` item's attestation (feature `ci_attest`).
    pub attestation: Option<Attestation>,
    /// The engine's (feature `item_capabilities`); None from older ones.
    pub delegable: Option<bool>,
    pub passable: Option<bool>,
}

impl Top {
    /// The branch's agent can resolve it (Delegate, d): the engine's
    /// catalog says, else the kinds an older engine delegated.
    pub fn delegable(&self) -> bool {
        self.delegable.unwrap_or_else(|| !NOT_DELEGABLE.contains(&self.kind.as_str()))
    }

    /// The orchestrator can act on it (Pass, ⇧O); older engines: yes.
    pub fn passable(&self) -> bool {
        self.passable.unwrap_or(true)
    }

    /// A red CI item: on a PR (`ci`) or on a branch with none (`ci_failed`).
    pub fn is_ci(&self) -> bool {
        matches!(self.kind.as_str(), "ci" | "ci_failed")
    }

    /// What a CI line says: the red workflow, its failed job and step when
    /// the engine has read them; None with no run.
    pub fn ci_text(&self) -> Option<String> {
        let run = self.run.as_ref()?;
        let mut text = format!("CI red · {}", run.workflow);
        if let Some(job) = run.failed_jobs.as_ref().and_then(|j| j.first()) {
            text += &format!(" / {}", job.name);
            if !job.step.is_empty() {
                text += &format!(" (step: {})", job.step);
            }
            let more = run.failed_jobs.as_ref().map_or(0, |j| j.len()) - 1;
            if more > 0 {
                text += &format!(" +{more}");
            }
        }
        Some(text)
    }

    /// A brief that failed validation: a quick "agent a2 asks: …", answered
    /// in the agent's terminal.
    /// An agent's question in prose at the end of its turn (I4h): the same.
    pub fn asks(&self, decisions: Option<&Decision>) -> bool {
        if self.kind == "turn_question" {
            return true;
        }
        // The engine's own brief (auto-delegation failed) is answered here.
        let engine = |d: &Decision| d.source.as_deref() == Some("auto_delegate");
        self.kind == "decision" && decisions.map(|d| !d.valid && !engine(d)).unwrap_or(self.detail == "open terminal")
    }
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
    /// The top item's brief, when the client has it.
    pub brief: Option<Decision>,
    /// The orchestrator's latest decision on this work item (first responder).
    pub decided: Option<Decision>,
    /// The items behind the top one, as the client has them.
    pub under: Vec<Under>,
    /// A ghost line (I4g): the deep item it stands for, under the top item
    /// of the line of the same key.
    pub ghost: Option<Ghost>,
}

/// What a ghost line stands for, and where its line is.
#[derive(Debug, Clone, PartialEq)]
pub struct Ghost {
    pub item: String,
    /// Its line's zone (Now or Round) and verb: "under its Fix CI in the Round".
    pub zone: Zone,
    pub verb: String,
}

/// An item behind a line's top one.
#[derive(Debug, Clone, PartialEq)]
pub struct Under {
    pub item: NeedsYouItem,
    /// None when the client does not have the item (yet).
    pub top: Option<Top>,
    pub brief: Option<Decision>,
}

/// The selection id of a ghost line: its key and its item.
pub fn ghost_sel(key: &str, item: &str) -> String {
    format!("{key}\n{item}")
}

impl Line {
    /// The row's `ci_note`: every failing check is unrelated to the branch.
    pub fn ci_note(&self) -> Option<&str> {
        self.row.as_ref().and_then(|r| r.ci_note.as_deref()).filter(|n| !n.is_empty())
    }
}

/// The project a line belongs to (a feeder's `project`): In flight groups
/// by it.
pub fn project_of(line: &Line) -> Option<&lazyagent_proto::types::RowProject> {
    line.row.as_ref().and_then(|r| r.project.as_ref())
}

/// In flight, grouped: lines with no project first, then each project's
/// lines together, projects in the order their first line came. Inside a
/// group the order is kept.
pub fn group_by_project(lines: Vec<&Line>) -> Vec<&Line> {
    let mut out: Vec<&Line> = lines.iter().copied().filter(|l| project_of(l).is_none()).collect();
    let mut seen: HashSet<&str> = HashSet::new();
    for l in &lines {
        if let Some(p) = project_of(l)
            && seen.insert(p.id.as_str())
        {
            out.extend(lines.iter().copied().filter(|m| project_of(m).is_some_and(|q| q.id == p.id)));
        }
    }
    out
}

/// The project lets the orchestrator start the next task while review.
pub fn continues_while_review(p: &lazyagent_proto::types::RowProject) -> bool {
    p.policy.as_ref().is_some_and(|v| v["continue_while_review"] == true)
}

/// "high" and "low" get a marker; medium (the default) none.
pub fn priority_marker(priority: &str) -> &'static str {
    match priority {
        "high" => "▴",
        "low" => "▾",
        _ => "",
    }
}

impl Line {
    /// The user's gesture on the row: `label`, `park` or `close`.
    pub fn gesture(&self) -> Option<&serde_json::Value> {
        self.row.as_ref().and_then(|r| r.gesture.as_ref())
    }

    /// Delegate (d) applies: the top item is one the branch's agent can
    /// resolve (CI red, a conflict, changes requested, a dead agent), not
    /// the user's own review, merge, answer or approval (I4h).
    pub fn delegable(&self) -> bool {
        match self.delegates() {
            Some(d) => !d.is_empty(),
            None => self.top.as_ref().is_some_and(Top::delegable),
        }
    }

    /// The items Delegate sends, in one letter (feature `delegate_all`);
    /// None from older engines (the top item alone).
    pub fn delegates(&self) -> Option<&[String]> {
        self.row.as_ref().and_then(|r| r.delegates.as_deref())
    }

    /// The Delegate button: "Delegate all (2)" when it sends several.
    pub fn delegate_label(&self) -> String {
        match self.delegates().map(<[String]>::len) {
            Some(n) if n > 1 => format!("Delegate all ({n})"),
            _ => "Delegate to its agent".into(),
        }
    }

    /// Pass (⇧O) applies: the orchestrator can act on something on the
    /// row. The engine says (status_list field `passable`); else the top
    /// item; a row with neither, yes.
    /// Approve (⇧A) applies here, at this head: the engine says (status_list
    /// field `approvable`, R3d).
    pub fn approvable(&self) -> Option<&Approvable> {
        self.row.as_ref().and_then(|r| r.approvable.as_ref()).filter(|_| !self.done() && self.ghost.is_none())
    }

    /// What to review on the line (status_list field `review_commits`,
    /// I4j): only where Approve applies.
    pub fn review_commits(&self) -> Option<&ReviewCommits> {
        self.approvable().and(self.row.as_ref().and_then(|r| r.review_commits.as_ref()))
    }

    pub fn passable(&self) -> bool {
        match (self.row.as_ref().and_then(|r| r.passable), &self.top) {
            (Some(p), _) => p,
            (None, Some(t)) => t.passable(),
            (None, None) => true,
        }
    }

    /// A done row (merged, closed): shown dimmed for a day.
    pub fn done(&self) -> bool {
        self.ball == "done"
    }

    /// A done row with nothing left to do (no suggestion): it goes into In
    /// flight's folded "N done today" group.
    pub fn folds(&self) -> bool {
        self.done() && self.row.as_ref().is_none_or(|r| r.suggestion.is_none())
    }

    /// Nothing but a branch, unchanged for days: In flight's folded "N
    /// stale" group (never hidden without a trace).
    pub fn stale(&self) -> bool {
        self.row.as_ref().is_some_and(|r| r.stale) && !self.done()
    }

    /// The row's open inbox items, top first (I4f engines; else none).
    pub fn items(&self) -> &[NeedsYouItem] {
        self.row.as_ref().and_then(|r| r.needs_you.as_ref()).map(|n| n.items.as_slice()).unwrap_or(&[])
    }

    /// What the items behind the top one are: "+ changes requested" (or
    /// "2 items" from an engine that does not list them).
    pub fn more_text(&self) -> Option<String> {
        if self.more == 0 {
            return None;
        }
        let rest: Vec<String> = self.items().iter().skip(1).map(item_words).collect();
        if rest.is_empty() {
            return Some(format!("{} items", self.more + 1));
        }
        Some(format!("+ {}", rest.join(", ")))
    }

    /// What Home selects and expands this line by: its key, or for a ghost
    /// line its key and its item ([`ghost_sel`]).
    pub fn sel(&self) -> String {
        match &self.ghost {
            Some(g) => ghost_sel(&self.key, &g.item),
            None => self.key.clone(),
        }
    }

    /// The deep items under this line's quicker top one (a changes
    /// requested under a red CI): each shows in To sit down with too, as a
    /// ghost line.
    pub fn deep_under(&self) -> impl Iterator<Item = &NeedsYouItem> {
        let inbox = matches!(self.zone, Zone::Now | Zone::Round) && self.ghost.is_none();
        self.items().iter().skip(1).filter(move |i| inbox && i.level == "deep")
    }

    /// The ghost line of `item` (one of [`Line::deep_under`]): the same
    /// work item, in To sit down with, its top that item.
    pub fn ghost_of(&self, item: &NeedsYouItem) -> Line {
        let under = self.under.iter().find(|u| u.item.id == item.id);
        Line {
            zone: Zone::Deep,
            state: item_words(item),
            blocks_agent: false,
            changed_at: item.since,
            more: 0,
            top: under.and_then(|u| u.top.clone()),
            brief: under.and_then(|u| u.brief.clone()),
            decided: None,
            under: vec![],
            ghost: Some(Ghost { item: item.id.clone(), zone: self.zone, verb: self.verb().to_string() }),
            ..self.clone()
        }
    }

    /// The item behind the top one with this id.
    pub fn under_item(&self, id: &str) -> Option<&Under> {
        self.under.iter().find(|u| u.item.id == id)
    }

    /// A routed question the orchestrator is on (first responder).
    pub fn looking(&self) -> bool {
        self.row.as_ref().and_then(|r| r.holder.as_deref()) == Some("orchestrator")
    }

    /// The orchestrator's own hold on the item (`orchestrator_hold`).
    pub fn hold(&self) -> Option<&lazyagent_proto::types::Hold> {
        self.row.as_ref().and_then(|r| r.hold.as_ref())
    }

    /// Parked, with inbox items still the user's (not routed).
    pub fn parked_needs_you(&self) -> bool {
        self.zone == Zone::Parked && self.row.as_ref().is_some_and(|r| r.needs_you.is_some())
    }

    /// The orchestrator's note from its last release of a pass or hold.
    pub fn released_note(&self) -> Option<&str> {
        self.row.as_ref().and_then(|r| r.note.as_ref()).filter(|n| n.by == "orchestrator").map(|n| n.text.as_str())
    }

    /// Something happened since the last look: the engine's per-item seen
    /// mark when it keeps them, else the client's own last look. An item
    /// never looked at (no `seen_at` yet) counts as seen now: the first
    /// launch shows no dots (the app then marks it seen, once).
    pub fn unseen(&self, look: f64) -> bool {
        match self.row.as_ref().and_then(|r| r.last_event_at) {
            Some(last) => self.row.as_ref().and_then(|r| r.seen_at).is_some_and(|seen| last > seen),
            None => self.changed_at > look,
        }
    }

    /// The first agent working on the line, when one runs.
    pub fn running_agent(&self) -> Option<&lazyagent_proto::types::RowAgent> {
        self.row.as_ref()?.agents.iter().find(|a| matches!(a.status.as_str(), "running" | "starting"))
    }

    /// The agent's own last word on the item (`status_report`), not the
    /// notes' bullet nor the orchestrator's release note.
    pub fn report(&self) -> Option<&str> {
        let note = self.row.as_ref()?.note.as_ref()?;
        note.by.starts_with("agent:").then_some(note.text.as_str())
    }

    /// A running agent's line: "working · <its last report, else its notes'
    /// line>", never its tool calls ("Bash done"); the age is the line's
    /// own column. None when no agent runs or the row says something more
    /// specific (fixing CI, a delegation).
    pub fn working(&self) -> Option<String> {
        let agent = self.running_agent()?;
        let rule9 = self.row.as_ref().is_some_and(|r| r.rule == 9) && self.state != "fixing CI";
        if !rule9 || self.zone != Zone::Flight {
            return None;
        }
        if agent.status == "starting" {
            return Some("starting".into());
        }
        let said = self
            .report()
            .or_else(|| self.row.as_ref()?.note.as_ref().filter(|n| n.by == "notes").map(|n| n.text.as_str()));
        Some(match said {
            Some(text) => format!("working · {}", middle_ellipsis(text, REPORT_CHARS)),
            None => "working".into(),
        })
    }

    /// The action an inbox line asks for, always an imperative: the
    /// engine's (`status_list {fields}`, feature `rules`: a user rule's
    /// verb) for a line with no item of a kind this table knows; else this
    /// table, the fallback for older engines.
    pub fn verb(&self) -> &str {
        // The row's verb is its top item's, never a ghost's.
        let engine =
            self.row.as_ref().and_then(|r| r.verb.as_deref()).filter(|v| !v.is_empty() && self.ghost.is_none());
        let Some(top) = &self.top else {
            if let Some(v) = engine {
                return v;
            }
            return match self.row.as_ref() {
                Some(r) if r.rule == 4 => "Merge",
                Some(r) if r.state == "PR has no reviewer" => "Request reviewer",
                _ => "Look",
            };
        };
        match top.kind.as_str() {
            "decision" if top.asks(self.brief.as_ref()) => "Answer",
            // An item rule's (the built-in Merge, a user rule): its own verb.
            "rule" => top.verb.as_deref().filter(|v| !v.is_empty()).or(engine).unwrap_or("Look"),
            k => match kind_verb(k) {
                "Look" => engine.unwrap_or("Look"),
                v => v,
            },
        }
    }

    /// What expanding the line shows of its ask, in full: the command an
    /// approval waits on, the question an agent asks.
    pub fn ask_text(&self) -> Option<String> {
        let top = self.top.as_ref()?;
        let text = match top.kind.as_str() {
            "approval" => top.detail.clone(),
            "question" => top.detail.clone(),
            "turn_question" => top.label.clone(),
            "decision" => self.brief.as_ref().map(|d| d.brief.question.clone()).unwrap_or_else(|| top.label.clone()),
            _ => return None,
        };
        let text = text.trim();
        (!text.is_empty() && !matches!(text, "waiting_for_user" | "waiting_for_approval" | "needs approval"))
            .then(|| text.to_string())
    }

    /// The agent to open for this line: the top item's, else the first.
    pub fn agent(&self) -> Option<String> {
        self.top
            .as_ref()
            .and_then(|t| t.agent_id.clone())
            .or_else(|| self.row.as_ref().and_then(|r| r.agents.first().map(|a| a.id.clone())))
    }
}

/// The items that are answered, never delegated, for an engine that does not
/// say (feature `item_capabilities` says per item).
pub const NOT_DELEGABLE: [&str; 6] =
    ["approval", "question", "turn_question", "decision", "delegation", "review_request"];

/// The verb of an inbox item's kind, always an imperative ("Look" for one
/// this table does not know).
pub fn kind_verb(kind: &str) -> &'static str {
    match kind {
        "approval" => "Approve",
        "question" | "turn_question" => "Answer",
        "done" => "Review",
        "ci" | "ci_failed" => "Fix CI",
        "ci_policy" => "Fix",
        "review" => "Address review",
        "conflict" => "Resolve conflict",
        "hanged" => "Check agent",
        "failed" | "exited" => "Relaunch",
        "job" => "Fix job",
        "delegation" => "Decide",
        "tracker_status" => "Update task",
        "decision" => "Decide",
        "report" => "Read",
        "review_request" => "Review",
        "attestation_confirm" => "Confirm",
        _ => "Look",
    }
}

fn level_zone(level: Option<&str>) -> Zone {
    match level {
        Some("now") => Zone::Now,
        Some("deep") => Zone::Deep,
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
        // A tracker item's detail is the task's URL.
        pr_url: item.pr.as_ref().and_then(|p| p.url.clone()).or_else(|| {
            (item.kind == "tracker_status" && item.detail.starts_with("http")).then(|| item.detail.clone())
        }),
        since: item.since,
        decision_id: item.decision_id.clone(),
        run: item.run.clone(),
        verb: item.verb.clone(),
        attestation: item.attestation.clone(),
        delegable: item.delegable,
        passable: item.passable,
    }
}

/// The orchestrator's latest decision on `key`, within [`DECIDED_FOR`].
fn decided_on(decisions: &BTreeMap<String, Decision>, key: &str, now: f64) -> Option<Decision> {
    decisions
        .values()
        .filter(|d| d.key == key && d.answered_by.as_deref() == Some("orchestrator"))
        .filter(|d| now - d.closed_at.unwrap_or(d.created_at) < DECIDED_FOR)
        .max_by(|a, b| a.closed_at.unwrap_or(a.created_at).total_cmp(&b.closed_at.unwrap_or(b.created_at)))
        .cloned()
}

/// An item's state in a few words: "changes requested", "CI red: tests".
pub fn item_words(item: &NeedsYouItem) -> String {
    let text = if item.state.is_empty() { item.kind.replace('_', " ") } else { item.state.clone() };
    text.replace('_', " ")
}

/// `branch #12`, the way the engine's table names an item; a detached
/// head by its worktree's directory, not `@6bf0c804d69b`.
pub fn row_name(row: &StatusRow) -> String {
    let mut name = row.branch.clone().unwrap_or_else(|| match row.worktree_path.as_deref() {
        Some(p) if !store::basename(p).is_empty() => store::basename(p).to_string(),
        _ => row.key.split_once(':').map(|x| x.1).unwrap_or(&row.key).into(),
    });
    if let Some(pr) = &row.pr {
        name.push_str(&format!(" #{}", pr.number));
    }
    name
}

/// The lines of an engine with the `status` and `actionables` features
/// (and, from I4, the briefs it holds).
pub fn engine_lines(
    rows: &BTreeMap<String, StatusRow>,
    items: &BTreeMap<String, Actionable>,
    decisions: &BTreeMap<String, Decision>,
    now: f64,
) -> Vec<Line> {
    let mut out = vec![];
    for row in rows.values() {
        // Stale rows stay, folded at the end of In flight (Home::stale_group).
        if row.ball == "done" && now - row.changed_at >= DONE_FOR {
            continue;
        }
        let zone = match row.zone.as_str() {
            "inbox" => level_zone(row.level.as_deref()),
            "parked" => Zone::Parked,
            _ => Zone::Flight,
        };
        let top = row.needs_you.as_ref().and_then(|n| items.get(&n.top)).map(top_of);
        let count = row.needs_you.as_ref().map(|n| n.count).unwrap_or(0);
        let under: Vec<Under> = row
            .needs_you
            .iter()
            .flat_map(|n| n.items.iter().filter(|i| i.id != n.top))
            .map(|i| {
                let top = items.get(&i.id).map(top_of);
                let brief = top.as_ref().and_then(|t| t.decision_id.as_ref()).and_then(|id| decisions.get(id)).cloned();
                Under { item: i.clone(), top, brief }
            })
            .collect();
        // A label is the user's word next to the fact: "QA · CI running".
        let label = row.gesture.as_ref().filter(|g| g["kind"] == "label").and_then(|g| g["text"].as_str());
        let mut state = match label {
            Some(text) if !text.is_empty() => format!("{text} · {}", row.state),
            _ => row.state.clone(),
        };
        // An agent's question in prose: the question, not five words of it (I4h).
        if let Some(t) = top.as_ref().filter(|t| t.kind == "turn_question" && !t.label.is_empty())
            && label.is_none_or(str::is_empty)
        {
            state = t.label.clone();
        }
        // A feeder's dependencies still open: "waits on <title>".
        if !row.waits_on.is_empty() && row.ball != "done" {
            let titles: Vec<&str> = row.waits_on.iter().map(|w| w.title.as_str()).collect();
            state.push_str(&format!(" · waits on {}", titles.join(", ")));
        }
        // Parked: until when, and what still needs the user there.
        if zone == Zone::Parked
            && row.holder.as_deref() != Some("orchestrator")
            && let Some(first) = row.needs_you.as_ref().and_then(|n| n.items.first())
        {
            state.push_str(&format!(" · needs you: {}", item_words(first)));
        }
        // A teammate's PR that asks for the user's review: its title, since
        // the branch is theirs ("review requested by ana · Fix the export").
        if let Some(rr) = &row.review_request
            && row.needs_you.is_some()
            && !rr.title.is_empty()
        {
            state.push_str(&format!(" · {}", rr.title));
        }
        // Stale: what would be lost with the worktree.
        if row.stale {
            match row.dirty {
                Some(0) => state.push_str(" · no uncommitted files"),
                Some(1) => state.push_str(" · 1 uncommitted file"),
                Some(n) => state.push_str(&format!(" · {n} uncommitted files")),
                None => {}
            }
        }
        out.push(Line {
            key: row.key.clone(),
            name: row_name(row),
            place: row.worktree_path.clone(),
            zone,
            ball: row.ball.clone(),
            state,
            priority: row.priority.clone(),
            blocks_agent: row.blocks_agent,
            changed_at: row.changed_at,
            more: count.saturating_sub(1),
            url: row.urls.first().cloned().or_else(|| top.as_ref().and_then(|t| t.pr_url.clone())),
            brief: top.as_ref().and_then(|t| t.decision_id.as_ref()).and_then(|id| decisions.get(id)).cloned(),
            top,
            row: Some(row.clone()),
            claim: row.claim.clone(),
            decided: decided_on(decisions, &row.key, now),
            under,
            ghost: None,
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
                brief: None,
                decided: None,
                under: vec![],
                ghost: None,
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
        decision_id: None,
        run: None,
        verb: None,
        attestation: None,
        delegable: None,
        passable: None,
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
                // "working", never the agent's tool calls ("Bash done").
                (Some(_), _) => (Zone::Flight, "agent".into(), "working".into()),
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
            brief: None,
            decided: None,
            under: vec![],
            ghost: None,
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
    /// The ghost lines (I4g), by [`Line::sel`]; made again with the lines.
    pub ghosts: HashMap<String, Line>,
    order: [Vec<String>; 5],
    /// The Round as delivered at the last cut point.
    delivered: HashSet<String>,
    /// Round lines waiting for the next cut point, in arrival order: drawn
    /// dimmed under the Round (never in In flight).
    next: Vec<String>,
    pub round_at: f64,
    /// Round lines that left on their own since the cut.
    pub resolved: u32,
    /// The selected and the expanded line, by [`Line::sel`].
    pub selected: Option<String>,
    pub expanded: Option<String>,
    /// An item behind the top one opened in its expanded line (a brief
    /// answered in place, I4g): the line's sel and the item's id.
    pub item_open: Option<(String, String)>,
    /// The park choices are open on this line (by sel).
    pub parking: Option<String>,
    /// The park menu parks the whole work item (not only its top item).
    pub park_whole: bool,
    /// The Parked group is unfolded (folded by default).
    pub parked_open: bool,
    /// In flight's "N done today" group is unfolded (folded by default;
    /// kept per repo in `Saved::home_done_open`).
    pub done_open: bool,
    /// In flight's "N stale" group is unfolded (folded by default).
    pub stale_open: bool,
    /// The close choices (done / dropped) are open on this line (by sel).
    pub closing: Option<String>,
    /// The label form is open on this line.
    pub labeling: Option<Label>,
    /// The revert form (an optional comment) is open on this line.
    pub reverting: Option<(String, String)>,
    /// The Reject form of an attestation (its id, the comment typed).
    pub rejecting: Option<(String, String)>,
    /// Give the form just opened (label, revert, answer) the keyboard once.
    pub focus_form: bool,
    /// The answer field of a question line: its key and the text typed.
    pub answering: Option<(String, String)>,
    /// The option picked on an expanded brief (decision id, option key).
    pub brief_choice: Option<(String, String)>,
    /// The orchestrator strip lists today's actions.
    pub strip_open: bool,
    /// The first complete lines came: from then on, nothing reorders
    /// between cut points (before, every update is a cut).
    pub ready: bool,
}

impl Home {
    /// A line by its [`Line::sel`]: a work item's, or a ghost.
    pub fn get(&self, sel: &str) -> Option<&Line> {
        self.lines.get(sel).or_else(|| self.ghosts.get(sel))
    }

    fn make_ghosts(&mut self) {
        self.ghosts = self
            .lines
            .values()
            .flat_map(|l| l.deep_under().map(|i| l.ghost_of(i)).collect::<Vec<_>>())
            .map(|g| (g.sel(), g))
            .collect();
    }

    /// Where a line shows; None: under the Round, for the next one.
    fn shown(&self, line: &Line) -> Option<Zone> {
        match line.zone {
            Zone::Round if !self.delivered.contains(&line.key) => None,
            z => Some(z),
        }
    }

    fn zone_of(&self, key: &str) -> Option<Zone> {
        ZONES.into_iter().find(|z| self.order[*z as usize].iter().any(|k| k == key))
    }

    /// Takes the latest lines; nothing already shown moves but a Now line,
    /// a line leaving the inbox, or a line that is gone.
    pub fn update(&mut self, lines: Vec<Line>, now: f64) {
        self.lines = lines.into_iter().map(|l| (l.key.clone(), l)).collect();
        self.make_ghosts();
        if !self.ready {
            self.cut(now);
            return;
        }
        for z in 0..ZONES.len() {
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
        let mut next = vec![];
        for key in keys {
            let before = self.zone_of(&key);
            let target = self.shown(&self.lines[&key]);
            if before == target && target.is_some() {
                continue;
            }
            if let Some(b) = before {
                self.order[b as usize].retain(|k| k != &key);
                // Parked by the user is not "resolved on its own".
                if b == Zone::Round && target != Some(Zone::Parked) {
                    self.resolved += 1;
                }
            }
            match target {
                Some(t) => self.order[t as usize].push(key),
                None => next.push(key),
            }
        }
        // The next round keeps its arrival order between cut points.
        self.next.retain(|k| next.contains(k));
        for k in next {
            if !self.next.contains(&k) {
                self.next.push(k);
            }
        }
        self.delivered.retain(|k| self.lines.get(k).is_some_and(|l| l.zone == Zone::Round));
    }

    /// A cut point: deliver the waiting Round, sort everything again.
    pub fn cut(&mut self, now: f64) {
        let mut all: Vec<&Line> = self.lines.values().collect();
        all.sort_by(|a, b| cmp(a, b));
        self.order = Default::default();
        self.delivered.clear();
        self.next.clear();
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
        self.next.len()
    }

    /// The lines of the next round, dimmed under the current one.
    pub fn next_round(&self) -> Vec<&Line> {
        self.next.iter().filter_map(|k| self.lines.get(k)).collect()
    }

    /// The lines of a zone, in order; In flight without its done and stale
    /// groups.
    pub fn shown_in(&self, zone: Zone) -> Vec<&Line> {
        let lines = self.order[zone as usize].iter().filter_map(|k| self.lines.get(k));
        if zone == Zone::Flight {
            group_by_project(lines.filter(|l| !l.folds() && !l.stale()).collect())
        } else {
            lines.collect()
        }
    }

    /// In flight's done rows with no suggestion: one group at its end.
    pub fn done_group(&self) -> Vec<&Line> {
        self.order[Zone::Flight as usize].iter().filter_map(|k| self.lines.get(k)).filter(|l| l.folds()).collect()
    }

    /// In flight's stale rows: one folded group at its very end.
    pub fn stale_group(&self) -> Vec<&Line> {
        self.order[Zone::Flight as usize].iter().filter_map(|k| self.lines.get(k)).filter(|l| l.stale()).collect()
    }

    /// The ghost lines of the deep items under quicker lines, in their
    /// lines' order, drawn at the end of To sit down with.
    pub fn deep_ghosts(&self) -> Vec<&Line> {
        let mut v: Vec<&Line> = self.shown_in(Zone::Now);
        v.extend(self.shown_in(Zone::Round));
        v.extend(self.next_round());
        v.into_iter().flat_map(|l| l.deep_under().filter_map(|i| self.ghosts.get(&ghost_sel(&l.key, &i.id)))).collect()
    }

    /// To sit down with as the engine counts it: its lines and the deep
    /// items under quicker ones.
    pub fn deep_count(&self) -> usize {
        self.count(Zone::Deep) + self.lines.values().map(|l| l.deep_under().count()).sum::<usize>()
    }

    /// The selectable lines, top to bottom.
    pub fn visible(&self, folded: bool) -> Vec<&Line> {
        let mut v = self.shown_in(Zone::Now);
        v.extend(self.shown_in(Zone::Round));
        v.extend(self.next_round());
        v.extend(self.shown_in(Zone::Deep));
        v.extend(self.deep_ghosts());
        if !folded {
            v.extend(self.shown_in(Zone::Flight));
            if self.done_open {
                v.extend(self.done_group());
            }
            if self.stale_open {
                v.extend(self.stale_group());
            }
        }
        if self.parked_open {
            v.extend(self.shown_in(Zone::Parked));
        }
        v
    }

    /// Now and Round as the engine counts them (for the badges).
    pub fn counts(&self) -> (usize, usize) {
        (self.count(Zone::Now), self.count(Zone::Round))
    }

    pub fn count(&self, zone: Zone) -> usize {
        self.lines.values().filter(|l| l.zone == zone).count()
    }

    /// `1`-`4`: the first line of Now, Round, To sit down with or In
    /// flight; false when that zone is empty.
    pub fn jump(&mut self, zone: Zone, folded: bool) -> bool {
        let first = match zone {
            Zone::Round => self.shown_in(Zone::Round).first().or(self.next_round().first()).map(|l| l.key.clone()),
            Zone::Deep => self.shown_in(Zone::Deep).first().or(self.deep_ghosts().first()).map(|l| l.sel()),
            Zone::Flight if folded => None,
            z => self.shown_in(z).first().map(|l| l.key.clone()),
        };
        let found = first.is_some();
        if found {
            self.selected = first;
        }
        found
    }

    /// Tab: the next line that needs the user (Now, Round, To sit down
    /// with), after the selected one, wrapping around.
    pub fn next_needing_you(&mut self, folded: bool) -> bool {
        let keys: Vec<String> = self
            .visible(folded)
            .iter()
            .filter(|l| matches!(l.zone, Zone::Now | Zone::Round | Zone::Deep))
            .map(|l| l.sel())
            .collect();
        if keys.is_empty() {
            return false;
        }
        let at = self.selected.as_ref().and_then(|s| keys.iter().position(|k| k == s));
        let next = at.map_or(0, |i| (i + 1) % keys.len());
        self.selected = Some(keys[next].clone());
        true
    }

    pub fn select_next(&mut self, delta: i32, folded: bool) {
        let keys: Vec<String> = self.visible(folded).iter().map(|l| l.sel()).collect();
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
        let mut found =
            self.selected.as_ref().and_then(|s| visible.iter().find(|l| &l.sel() == s)).map(|l| (*l).clone());
        if found.is_none()
            && let Some((key, item)) = self.selected.as_ref().and_then(|s| s.split_once('\n'))
        {
            // A ghost's item became its line's top: that line now.
            found = visible
                .iter()
                .find(|l| l.key == key && l.top.as_ref().is_some_and(|t| t.id == item))
                .map(|l| (*l).clone());
        }
        let line = found.or_else(|| visible.first().map(|l| (*l).clone()));
        self.selected = line.as_ref().map(|l| l.sel());
        line
    }
}

/// The label form: a ball and a short text (at most [`LABEL_CHARS`]).
#[derive(Debug, Clone, PartialEq)]
pub struct Label {
    pub key: String,
    pub ball: String,
    pub text: String,
}

pub const LABEL_CHARS: usize = 40;
pub const LABEL_BALLS: [&str; 3] = ["others", "agent", "stalled"];

/// `text` in at most `max` characters, keeping its start and its end:
/// `inbox-i4d-…-polish`. Branch names differ at both ends.
pub fn middle_ellipsis(text: &str, max: usize) -> String {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= max {
        return text.to_string();
    }
    if max <= 1 {
        return "…".into();
    }
    let keep = max - 1;
    let head = keep.div_ceil(2);
    let tail = keep - head;
    let mut out: String = chars[..head].iter().collect();
    out.push('…');
    out.extend(&chars[chars.len() - tail..]);
    out
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
        engine_lines(
            &rows.iter().map(|r| (r.key.clone(), r.clone())).collect(),
            &BTreeMap::new(),
            &BTreeMap::new(),
            0.0,
        )
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
        assert!(keys(&h, Zone::Flight).is_empty(), "never in In flight");
        let next: Vec<&str> = h.next_round().iter().map(|l| l.name.as_str()).collect();
        assert_eq!(next, ["a", "n"], "under the Round, for the next one, once each");
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
        r.needs_you =
            Some(lazyagent_proto::types::NeedsYou { count: 3, top: "Approval:/w/shop-cart/a1".into(), items: vec![] });
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
        let got = engine_lines(&rows, &items, &BTreeMap::new(), 0.0);
        assert_eq!(got.len(), 2, "the work item once, the orchestrator's dialog on its own line");
        assert_eq!((got[0].more, got[0].verb(), got[0].agent().as_deref()), (2, "Approve", Some("a1")));
        assert_eq!((got[1].name.as_str(), got[1].zone), ("orchestrator", Zone::Now));
    }

    fn decision(id: &str, key: &str, valid: bool) -> Decision {
        serde_json::from_value(json!({
            "id": id, "key": key, "worktree_path": "/w", "agent": "a2", "holder": "user", "replaces": null,
            "state": "open", "valid": valid, "class": if valid { "deep" } else { "quick" }, "blocking": false,
            "brief": {"question": "Retries?", "options": [{"key": "A", "label": "Exponential"}, {"key": "B", "label": "Fixed"}],
                      "recommendation": "A", "tried": "", "evidence": [], "reversible": "yes", "impact_if_wrong": ""},
            "invalid": [], "problems": [], "sha": null, "created_at": 100.0, "closed_at": null, "answer": null
        }))
        .unwrap()
    }

    #[test]
    fn deep_lines_show_at_once_and_stay_outside_the_rounds() {
        let mut h = Home::default();
        let mut rows = vec![row("a", "in_flight", None, "agent", 1.0)];
        h.update(lines(&rows), 100.0);
        h.ready = true;
        rows[0] = row("a", "inbox", Some("deep"), "you", 101.0);
        rows.push(row("b", "inbox", Some("round"), "you", 101.0));
        h.update(lines(&rows), 102.0);
        assert_eq!(keys(&h, Zone::Deep), ["a"], "no cut point needed");
        assert_eq!(h.waiting(), 1, "only the Round line waits");
        assert_eq!((h.count(Zone::Deep), h.counts()), (1, (0, 1)));
        assert_eq!(h.visible(true).len(), 2, "folding hides only the in-flight list (the next round shows)");
        h.cut(200.0);
        assert_eq!((keys(&h, Zone::Round), keys(&h, Zone::Deep)), (vec!["b".to_string()], vec!["a".to_string()]));
    }

    #[test]
    fn labels_read_text_then_fact_and_done_rows_stay_a_day() {
        let mut r = row("cart", "in_flight", None, "others", 1.0);
        r.state = "CI running".into();
        r.gesture = Some(json!({"kind": "label", "at": 1.0, "by": "user", "ball": "others", "text": "QA"}));
        let mut done = row("old", "in_flight", None, "done", 1000.0);
        done.state = "merged".into();
        let rows: BTreeMap<_, _> = [r, done].into_iter().map(|r| (r.key.clone(), r)).collect();
        let got = engine_lines(&rows, &BTreeMap::new(), &BTreeMap::new(), 1000.0 + DONE_FOR - 1.0);
        assert_eq!(got.iter().map(|l| l.state.as_str()).collect::<Vec<_>>(), ["QA · CI running", "merged"]);
        assert!(got[1].done() && !got[0].done());
        let got = engine_lines(&rows, &BTreeMap::new(), &BTreeMap::new(), 1000.0 + DONE_FOR);
        assert_eq!(got.len(), 1, "a tombstone leaves after 24 h");
    }

    #[test]
    fn a_brief_decides_its_verb_and_the_orchestrators_decision_finds_its_row() {
        let mut r = row("hook", "inbox", Some("deep"), "you", 1.0);
        r.needs_you = Some(lazyagent_proto::types::NeedsYou { count: 1, top: "Decision:d1".into(), items: vec![] });
        let item: Actionable = serde_json::from_value(json!({
            "id": "Decision:d1", "occurrence": "d1@1", "kind": "decision", "level": "deep", "title": "Decide",
            "label": "Retries?", "detail": "brief by agent a2", "state": "decide: Retries?", "work_item": "shop:hook",
            "worktree_path": "/w", "agent_id": "a2", "pr": null, "job_id": null, "exit_code": null,
            "blocks_agent": false, "priority": "medium", "since": 1.0, "park": null, "decision_id": "d1"
        }))
        .unwrap();
        let rows = BTreeMap::from([(r.key.clone(), r)]);
        let items = BTreeMap::from([(item.id.clone(), item)]);
        let mut ds = BTreeMap::from([("d1".to_string(), decision("d1", "shop:hook", true))]);
        let l = &engine_lines(&rows, &items, &ds, 200.0)[0];
        assert_eq!((l.verb(), l.zone, l.brief.as_ref().map(|d| d.id.as_str())), ("Decide", Zone::Deep, Some("d1")));
        ds.insert("d1".into(), decision("d1", "shop:hook", false));
        assert_eq!(engine_lines(&rows, &items, &ds, 200.0)[0].verb(), "Answer");
        // Not loaded yet: the item's own detail says it is a quick one.
        let mut quick = items.clone();
        quick.get_mut("Decision:d1").unwrap().detail = "open terminal".into();
        assert_eq!(engine_lines(&rows, &quick, &BTreeMap::new(), 200.0)[0].verb(), "Answer");
        // The orchestrator answered d2 an hour ago; the user's d1 is not its.
        let mut d2 = decision("d2", "shop:hook", true);
        (d2.answered_by, d2.state, d2.closed_at) = (Some("orchestrator".into()), "answered".into(), Some(3600.0));
        d2.answer = Some(json!({"choice": "B", "reason": "fixed is enough here"}));
        ds.insert("d2".into(), d2);
        let l = &engine_lines(&rows, &items, &ds, 7200.0)[0];
        assert_eq!(l.decided.as_ref().map(|d| d.id.as_str()), Some("d2"));
        assert!(engine_lines(&rows, &items, &ds, 3600.0 + DECIDED_FOR)[0].decided.is_none(), "a day later");
    }

    #[test]
    fn middle_ellipsis_keeps_both_ends() {
        let name = "inbox-i4d-home-polish-and-more #42";
        assert_eq!(middle_ellipsis(name, 100), name);
        let cut = middle_ellipsis(name, 15);
        assert_eq!(cut.chars().count(), 15);
        assert_eq!(cut, "inbox-i…ore #42");
        assert_eq!(middle_ellipsis("abc", 1), "…");
        assert_eq!(middle_ellipsis("ñandú-ñandú", 5), "ña…dú");
    }

    #[test]
    fn parked_rows_wait_apart_and_are_not_resolved_on_their_own() {
        let mut h = Home::default();
        let mut rows = vec![row("a", "inbox", Some("round"), "you", 1.0), row("b", "in_flight", None, "agent", 2.0)];
        h.update(lines(&rows), 100.0);
        h.ready = true;
        assert_eq!(keys(&h, Zone::Round), ["a"]);
        rows[0] = row("a", "parked", None, "you", 3.0);
        rows[0].needs_you =
            Some(lazyagent_proto::types::NeedsYou { count: 1, top: "Ci:/w/shop-a:1".into(), items: vec![] });
        h.update(lines(&rows), 101.0);
        assert_eq!((keys(&h, Zone::Parked), h.resolved), (vec!["a".to_string()], 0));
        assert!(h.lines["shop:a"].parked_needs_you());
        assert_eq!(h.counts(), (0, 0), "parked lines are outside the badges");
        assert_eq!(h.visible(false).len(), 1, "folded by default");
        h.parked_open = true;
        assert_eq!(h.visible(false).len(), 2);
    }

    #[test]
    fn done_rows_with_nothing_to_do_fold_into_one_group_at_the_end() {
        let mut rows = vec![
            row("a", "in_flight", None, "done", 1.0),
            row("b", "in_flight", None, "agent", 2.0),
            row("c", "in_flight", None, "done", 3.0),
        ];
        rows[2].suggestion = Some("close PR #47: its head is in develop".into());
        let mut h = Home::default();
        h.update(lines(&rows), 10.0);
        assert_eq!(keys(&h, Zone::Flight), ["b", "c"], "a done row with a suggestion stays in place");
        let done: Vec<&str> = h.done_group().iter().map(|l| l.key.as_str()).collect();
        assert_eq!(done, ["shop:a"]);
        assert_eq!(h.visible(false).len(), 2, "folded by default");
        h.done_open = true;
        assert_eq!(h.visible(false).last().map(|l| l.key.as_str()), Some("shop:a"), "at the end of In flight");
        assert!(h.visible(true).is_empty(), "In flight folded hides the group too");
    }

    #[test]
    fn in_flight_groups_by_project_keeping_the_order_inside() {
        let mut rows: Vec<StatusRow> = ["a", "b", "c", "d", "e"]
            .iter()
            .enumerate()
            .map(|(i, k)| row(k, "in_flight", None, "agent", i as f64))
            .collect();
        for (i, p) in [(0, "x"), (2, "y"), (3, "x")] {
            rows[i].project = Some(serde_json::from_value(json!({"id": p, "title": p.to_uppercase()})).unwrap());
        }
        let mut h = Home::default();
        h.update(lines(&rows), 10.0);
        // Newest first: e d c b a. No project first, then x (d, a), then y.
        assert_eq!(keys(&h, Zone::Flight), ["e", "b", "d", "a", "c"]);
        assert_eq!(h.visible(false).len(), 5);
    }

    #[test]
    fn waits_on_joins_the_state_and_only_high_and_low_get_a_marker() {
        let mut r = row("a", "in_flight", None, "agent", 1.0);
        r.state = "Edit".into();
        r.waits_on = serde_json::from_value(json!([{"url": "u", "title": "Cart", "state": "review"},
                                                     {"url": "v", "title": "Tax", "state": "open"}]))
        .unwrap();
        let l = &lines(&[r.clone()])[0];
        assert_eq!(l.state, "Edit · waits on Cart, Tax");
        r.ball = "done".into();
        r.state = "merged".into();
        assert_eq!(lines(&[r])[0].state, "merged");
        assert_eq!((priority_marker("high"), priority_marker("medium"), priority_marker("low")), ("▴", "", "▾"));
    }

    #[test]
    fn zone_jumps_and_tab_go_to_what_needs_you() {
        let mut h = Home::default();
        let rows = vec![
            row("a", "inbox", Some("now"), "you", 1.0),
            row("b", "inbox", Some("round"), "you", 2.0),
            row("c", "inbox", Some("deep"), "you", 3.0),
            row("d", "in_flight", None, "agent", 4.0),
        ];
        h.update(lines(&rows), 10.0);
        assert!(h.jump(Zone::Flight, false));
        assert_eq!(h.selected.as_deref(), Some("shop:d"));
        assert!(!h.jump(Zone::Flight, true), "folded: nothing to jump to");
        assert!(h.jump(Zone::Deep, false));
        assert_eq!(h.selected.as_deref(), Some("shop:c"));
        // Tab: c → a → b → c (In flight is skipped).
        let mut seen = vec![];
        for _ in 0..3 {
            assert!(h.next_needing_you(false));
            seen.push(h.selected.clone().unwrap());
        }
        assert_eq!(seen, ["shop:a", "shop:b", "shop:c"]);
    }

    #[test]
    fn verbs_are_imperatives_and_a_working_line_reads_its_report_not_its_tools() {
        let mut r = row("cart", "in_flight", None, "agent", 1.0);
        r.agents = serde_json::from_value(json!([{"id": "a1", "status": "running", "since": 100.0}])).unwrap();
        r.state = "Bash done".into();
        let mut l = lines(&[r.clone()])[0].clone();
        // The age is the line's own column: never twice.
        assert_eq!(l.working().as_deref(), Some("working"));
        r.note = Some(
            serde_json::from_value(json!({"text": "tests pass; opening the PR", "by": "agent:a1", "at": 700.0}))
                .unwrap(),
        );
        l = lines(&[r.clone()])[0].clone();
        assert_eq!(l.working().as_deref(), Some("working · tests pass; opening the PR"));
        r.note = Some(serde_json::from_value(json!({"text": "Pending item", "by": "notes", "at": 700.0})).unwrap());
        assert_eq!(lines(&[r.clone()])[0].working().as_deref(), Some("working · Pending item"), "else its notes' line");
        r.note = Some(serde_json::from_value(json!({"text": "released", "by": "orchestrator", "at": 700.0})).unwrap());
        assert_eq!(lines(&[r.clone()])[0].working().as_deref(), Some("working"), "not the orchestrator's note");
        r.rule = 16;
        assert!(lines(&[r])[0].working().is_none(), "a delegation says more");
        let mut nobody = row("pdf", "inbox", Some("round"), "you", 1.0);
        nobody.state = "PR has no reviewer".into();
        nobody.rule = 10;
        assert_eq!(lines(&[nobody])[0].verb(), "Request reviewer");
    }

    #[test]
    fn a_line_never_looked_at_shows_no_dot() {
        let mut r = row("a", "in_flight", None, "agent", 1.0);
        r.last_event_at = Some(50.0);
        assert!(!lines(&[r.clone()])[0].unseen(0.0), "no seen_at yet: seen now");
        r.seen_at = Some(40.0);
        assert!(lines(&[r.clone()])[0].unseen(0.0));
        r.seen_at = Some(60.0);
        assert!(!lines(&[r])[0].unseen(0.0));
    }

    #[test]
    fn ages_never_say_zero_seconds() {
        assert_eq!(
            [age(0.0), age(59.0), age(240.0), age(7200.0), age(3.0 * 86400.0)],
            ["<1m", "<1m", "4m", "2h", "3d"]
        );
    }
}
