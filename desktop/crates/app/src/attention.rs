//! The attention queue (ui-plan §3b; the web page's attention.js): who
//! needs the user, most urgent first. An item lives while its condition
//! holds (an agent's status, an open PR's state, an unexpected exit, a
//! failed job) and clears itself when the condition does. Dismissing an
//! occurrence or snoozing an item is the app's own (kept in its state file).
//!
//! Only for engines without the `status` / `actionables` features: Home's
//! fallback (home.rs `fallback_lines`). A newer engine keeps the queue.

use std::collections::{BTreeMap, HashMap, HashSet};

use lazyagent_proto::types::{Agent, PrInfo};

use crate::store::{self, ORCHESTRATOR_KEY, Store};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Kind {
    Approval,
    Question,
    Hanged,
    Failed,
    Exited,
    Ci,
    Review,
    Conflict,
    Done,
    Job,
}

impl Kind {
    pub fn rank(self) -> u8 {
        match self {
            Kind::Approval => 1,
            Kind::Question => 2,
            Kind::Hanged => 3,
            Kind::Failed | Kind::Exited => 4,
            Kind::Ci | Kind::Review | Kind::Conflict => 5,
            Kind::Done => 6,
            Kind::Job => 7,
        }
    }

    pub fn title(self) -> &'static str {
        match self {
            Kind::Approval => "Needs approval",
            Kind::Question => "Question",
            Kind::Hanged => "Hanged?",
            Kind::Failed => "Failed",
            Kind::Exited => "Exited",
            Kind::Ci => "CI failing",
            Kind::Review => "Changes requested",
            Kind::Conflict => "Merge conflict",
            Kind::Done => "Your turn",
            Kind::Job => "Job failed",
        }
    }

    /// The notification setting this kind falls under (web: per kind).
    pub fn setting(self) -> &'static str {
        match self {
            Kind::Approval => "approval",
            Kind::Question => "question",
            Kind::Hanged => "hanged",
            Kind::Failed | Kind::Exited => "failed",
            Kind::Done => "done",
            Kind::Ci | Kind::Review | Kind::Conflict => "ci",
            Kind::Job => "job",
        }
    }
}

/// The notification kinds and their labels, for the settings.
pub const SETTINGS: &[(&str, &str)] = &[
    ("approval", "Needs approval"),
    ("question", "Asks a question"),
    ("hanged", "Looks hanged"),
    ("failed", "Failed or exited"),
    ("done", "Finished its turn"),
    ("ci", "CI failing, changes requested, conflict"),
    ("job", "A job failed"),
];

pub fn kind_of_status(status: &str) -> Option<Kind> {
    Some(match status {
        "waiting_for_approval" => Kind::Approval,
        "waiting_for_user" => Kind::Question,
        "possibly_hanged" => Kind::Hanged,
        "failed" => Kind::Failed,
        // A Claude agent that finished its turn reports "waiting".
        "completed" | "waiting" => Kind::Done,
        _ => return None,
    })
}

/// An agent that exited on its own, as the app saw it.
#[derive(Debug, Clone, PartialEq)]
pub struct Exit {
    pub key: String,
    pub agent_id: String,
    pub label: String,
    pub exit_code: Option<i32>,
    /// Unix seconds.
    pub at: f64,
}

/// A finished job, as far as the queue cares.
#[derive(Debug, Clone, PartialEq)]
pub struct JobEnd {
    pub id: String,
    pub title: String,
    pub exit_code: Option<i32>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Item {
    pub id: String,
    pub kind: Kind,
    /// Worktree path or the orchestrator key; empty for a job.
    pub key: String,
    pub agent_id: Option<String>,
    pub label: String,
    pub detail: String,
    pub pr: Option<PrInfo>,
    pub exit: Option<Exit>,
    pub job: Option<String>,
    /// First seen (Unix seconds).
    pub since: f64,
    /// `id@since`: dismissing hides this occurrence, not a later one.
    pub occurrence: String,
}

/// What the user did with items (persisted).
#[derive(Debug, Clone, Default, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct Marks {
    /// occurrence → when dismissed
    #[serde(default)]
    pub dismissed: BTreeMap<String, f64>,
    /// item id → snoozed until (Unix seconds)
    #[serde(default)]
    pub snoozed: BTreeMap<String, f64>,
}

impl Marks {
    /// Drops marks that can no longer matter (a week old).
    pub fn prune(&mut self, now: f64) {
        self.dismissed.retain(|_, at| now - *at < 7.0 * 86400.0);
        self.snoozed.retain(|_, until| *until > now);
    }
}

fn ci_failed(pr: &PrInfo) -> Vec<&str> {
    pr.checks.iter().filter(|c| c.conclusion.eq_ignore_ascii_case("FAILURE")).map(|c| c.name.as_str()).collect()
}

/// Every item, most urgent first (rank, then newest). `since` holds when
/// each condition was first seen; it is updated (set on first sight,
/// dropped once the condition clears).
pub fn items(
    store: &Store,
    exits: &[Exit],
    jobs: &[JobEnd],
    since: &mut HashMap<String, f64>,
    marks: &Marks,
    now: f64,
) -> Vec<Item> {
    let mut out = vec![];
    let mut seen = HashSet::new();
    let mut add = |mut item: Item| {
        seen.insert(item.id.clone());
        let at = *since.entry(item.id.clone()).or_insert(now);
        item.since = at;
        item.occurrence = format!("{}@{at}", item.id);
        if marks.dismissed.contains_key(&item.occurrence) || marks.snoozed.get(&item.id).is_some_and(|u| *u > now) {
            return;
        }
        out.push(item);
    };
    let blank = |id: String, kind: Kind, key: &str| Item {
        id,
        kind,
        key: key.to_string(),
        agent_id: None,
        label: String::new(),
        detail: String::new(),
        pr: None,
        exit: None,
        job: None,
        since: now,
        occurrence: String::new(),
    };
    let mut agents = |key: &str, list: &[Agent]| {
        for a in list {
            let Some(kind) = kind_of_status(&a.status) else { continue };
            let mut item = blank(format!("{kind:?}:{key}/{}", a.agent_id), kind, key);
            item.agent_id = Some(a.agent_id.clone());
            item.label = store::agent_label(a);
            item.detail = if a.detail.is_empty() { a.status.clone() } else { a.detail.clone() };
            add(item);
        }
    };
    agents(ORCHESTRATOR_KEY, &store.orchestrator);
    for w in &store.worktrees {
        agents(&w.path, &w.agents);
    }
    for w in &store.worktrees {
        let Some(Some(pr)) = store.pr_info.get(&w.path) else { continue };
        if !pr.state.eq_ignore_ascii_case("OPEN") {
            continue;
        }
        let mut pr_item = |kind: Kind, detail: String| {
            let mut item = blank(format!("{kind:?}:{}:{}", w.path, pr.number), kind, &w.path);
            item.label = format!("#{} {}", pr.number, pr.title);
            item.detail = detail;
            item.pr = Some(pr.clone());
            add(item);
        };
        if store::ci_summary(pr) == Some("fail") {
            pr_item(Kind::Ci, ci_failed(pr).join(", "));
        }
        if pr.review_decision.as_deref().is_some_and(|r| r.eq_ignore_ascii_case("CHANGES_REQUESTED")) {
            pr_item(Kind::Review, "a reviewer asked for changes".into());
        }
        if pr.mergeable.as_deref().is_some_and(|m| m.eq_ignore_ascii_case("CONFLICTING")) {
            pr_item(Kind::Conflict, "conflicts with the base branch".into());
        }
    }
    for e in exits {
        let mut item = blank(format!("Exited:{}/{}:{}", e.key, e.agent_id, e.at), Kind::Exited, &e.key);
        item.agent_id = Some(e.agent_id.clone());
        item.label = e.label.clone();
        item.detail = match e.exit_code {
            Some(c) => format!("exited with code {c}"),
            None => "exited".into(),
        };
        item.exit = Some(e.clone());
        add(item);
    }
    for j in jobs {
        if j.exit_code == Some(0) {
            continue;
        }
        let mut item = blank(format!("Job:{}", j.id), Kind::Job, "");
        item.label = j.title.clone();
        item.detail = match j.exit_code {
            Some(c) => format!("exit {c}"),
            None => "killed".into(),
        };
        item.job = Some(j.id.clone());
        add(item);
    }
    since.retain(|id, _| seen.contains(id));
    out.sort_by(|a, b| a.kind.rank().cmp(&b.kind.rank()).then(b.since.total_cmp(&a.since)));
    out
}

/// The prompt that sends a failing PR's checks to an agent (ui-plan §4d).
pub fn ci_failure_prompt(pr: &PrInfo) -> String {
    let lines: Vec<String> = pr
        .checks
        .iter()
        .filter(|c| c.conclusion.eq_ignore_ascii_case("FAILURE"))
        .map(|c| match c.details_url.as_deref().filter(|u| !u.is_empty()) {
            Some(url) => format!("- {}: {url}", c.name),
            None => format!("- {}", c.name),
        })
        .collect();
    format!(
        "CI is failing on PR #{} ({}). Failed checks:\n{}\nPlease look into the failures and fix them.",
        pr.number,
        pr.title,
        lines.join("\n")
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use lazyagent_proto::types::SidebarState;
    use serde_json::json;

    fn store(agents: serde_json::Value, pr: serde_json::Value) -> Store {
        let mut s = Store::default();
        let state: SidebarState = serde_json::from_value(json!({
            "worktrees": [{"path": "/r", "branch": "main", "head": "h", "is_main": true, "is_bare": false,
                           "agent_status": "", "agents": agents}],
            "orchestrator": {"agents": []}, "pr_info": {"/r": pr}, "git_statuses": {}, "view": null, "cursor": 0
        }))
        .unwrap();
        s.seed(state);
        s
    }

    fn agent(id: &str, status: &str) -> serde_json::Value {
        json!({"agent_id": id, "label": "", "session_name": "", "status": status, "confidence": "high", "detail": ""})
    }

    #[test]
    fn ranks_then_newest_first() {
        let s = store(
            json!([
                agent("a1", "completed"),
                agent("a2", "waiting_for_user"),
                agent("a3", "waiting_for_approval"),
                agent("a4", "running")
            ]),
            json!(null),
        );
        let mut since = HashMap::new();
        let got = items(&s, &[], &[], &mut since, &Marks::default(), 100.0);
        let kinds: Vec<Kind> = got.iter().map(|i| i.kind).collect();
        assert_eq!(kinds, [Kind::Approval, Kind::Question, Kind::Done]);
        // Newer first within a rank.
        let s2 = store(json!([agent("a1", "completed"), agent("a5", "waiting")]), json!(null));
        let mut since2 = HashMap::from([("Done:/r/a1".to_string(), 10.0)]);
        let ids: Vec<String> =
            items(&s2, &[], &[], &mut since2, &Marks::default(), 50.0).into_iter().map(|i| i.id).collect();
        assert_eq!(ids, ["Done:/r/a5", "Done:/r/a1"]);
    }

    #[test]
    fn items_clear_themselves_and_dismiss_hides_one_occurrence() {
        let mut since = HashMap::new();
        let waiting = store(json!([agent("a1", "waiting_for_user")]), json!(null));
        let first = items(&waiting, &[], &[], &mut since, &Marks::default(), 1.0);
        assert_eq!(first.len(), 1);
        let mut marks = Marks::default();
        marks.dismissed.insert(first[0].occurrence.clone(), 2.0);
        assert!(items(&waiting, &[], &[], &mut since, &marks, 3.0).is_empty());
        // It clears (running), then asks again later: a new occurrence shows.
        let running = store(json!([agent("a1", "running")]), json!(null));
        assert!(items(&running, &[], &[], &mut since, &marks, 4.0).is_empty());
        assert!(since.is_empty(), "first-seen times are dropped with their condition");
        assert_eq!(items(&waiting, &[], &[], &mut since, &marks, 5.0).len(), 1);
        // Snoozed until 100.
        marks.snoozed.insert("Question:/r/a1".into(), 100.0);
        assert!(items(&waiting, &[], &[], &mut since, &marks, 50.0).is_empty());
        assert_eq!(items(&waiting, &[], &[], &mut since, &marks, 150.0).len(), 1);
    }

    #[test]
    fn open_prs_exits_and_jobs() {
        let pr = json!({"number": 7, "title": "Login", "state": "OPEN", "review_decision": "CHANGES_REQUESTED",
                        "mergeable": "CONFLICTING", "checks": [
                            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
                            {"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE", "details_url": "https://ci/1"}]});
        let s = store(json!([]), pr.clone());
        let exits =
            [Exit { key: "/r".into(), agent_id: "a1".into(), label: "Agent a1".into(), exit_code: Some(2), at: 5.0 }];
        let jobs = [
            JobEnd { id: "j1".into(), title: "Create x".into(), exit_code: Some(1) },
            JobEnd { id: "j2".into(), title: "ok".into(), exit_code: Some(0) },
        ];
        let got = items(&s, &exits, &jobs, &mut HashMap::new(), &Marks::default(), 10.0);
        let kinds: Vec<Kind> = got.iter().map(|i| i.kind).collect();
        assert_eq!(kinds.len(), 5);
        assert_eq!(kinds[0], Kind::Exited);
        assert!(kinds.contains(&Kind::Ci) && kinds.contains(&Kind::Review) && kinds.contains(&Kind::Conflict));
        assert_eq!(kinds[4], Kind::Job);
        let ci = got.iter().find(|i| i.kind == Kind::Ci).unwrap();
        assert_eq!(ci.detail, "tests");
        assert_eq!(
            ci_failure_prompt(ci.pr.as_ref().unwrap()),
            "CI is failing on PR #7 (Login). Failed checks:\n- tests: https://ci/1\nPlease look into the failures and fix them."
        );
        // A merged PR asks for nothing.
        let merged = store(json!([]), json!({"number": 7, "title": "x", "state": "MERGED", "checks": []}));
        assert!(items(&merged, &[], &[], &mut HashMap::new(), &Marks::default(), 1.0).is_empty());
    }

    #[test]
    fn marks_prune_old_entries() {
        let mut m = Marks::default();
        m.dismissed.insert("a@1".into(), 0.0);
        m.dismissed.insert("b@1".into(), 1_000_000.0);
        m.snoozed.insert("x".into(), 5.0);
        m.prune(1_000_100.0);
        assert_eq!(m.dismissed.len(), 1);
        assert!(m.snoozed.is_empty());
    }
}
