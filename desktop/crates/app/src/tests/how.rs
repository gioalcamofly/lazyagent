//! "How lazyagent works" (R6): the page and its navigation (Help menu, F1,
//! `?` on Home), what it draws from the engine's catalog or the built-in
//! copy, the keys filter, the Why? view (`w`, the expanded line's button),
//! and the screenshots the page embeds (regenerated here, checked stale).

use super::*;
use crate::app::View;
use crate::how_it_works;

/// The engine features of I4e, with rules (R2) on.
fn with_rules() -> Vec<&'static str> {
    I4E.iter().copied().chain(["rules"]).collect()
}

/// A tall window: the whole page is laid out, so every row is in the tree.
fn tall(app: App) -> Harness<'static, App> {
    Harness::builder().with_size([1400.0, 9000.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app)
}

/// The built-in copy with the merge rule's state text changed, as an
/// engine with that change would send it, and a rule of the user's.
fn changed_catalog() -> serde_json::Value {
    let mut c: serde_json::Value = serde_json::from_str(how_it_works::COPY).unwrap();
    for r in c["rules"].as_array_mut().unwrap() {
        if r["id"] == "merge" {
            r["state_text"] = json!("merge: QA passed");
        }
    }
    let mut mine = c["rules"][15].clone();
    (mine["id"], mine["origin"], mine["file"], mine["line"]) =
        (json!("qa_gate"), json!("global"), json!("/home/dev/.config/lazyagent/rules.toml"), json!(3));
    mine["sentence"] = json!("The task's status is QA");
    c["rules"].as_array_mut().unwrap().insert(5, mine);
    c["errors"] = json!([{"scope": "global", "file": "/home/dev/.config/lazyagent/rules.toml", "line": 12,
                          "rule": "bad", "message": "rule bad: when: no fact 'tracker.qa'",
                          "fallback": "skipped; the built-in is in force"}]);
    c
}

#[test]
fn question_mark_on_home_opens_the_page_at_home_and_esc_goes_back() {
    let (app, _tx) = engine_home(I4D);
    let mut h = harness(app);
    h.run_steps(2);
    h.key_press(egui::Key::Questionmark);
    h.run_steps(3);
    let s = &h.state().state;
    assert_eq!(s.view, View::Guide, "a page, not a dialog");
    assert!(s.dialog.is_none());
    assert_eq!((s.guide.current.as_str(), s.guide.back), (how_it_works::HOME_SECTION, View::Home));
    h.get_by_label("How lazyagent works");
    h.get_by_label("←  Back  (Esc)");
    h.get_by_label("built-in rules (copy)");
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    assert_eq!(h.state().state.view, View::Home);
}

#[test]
fn the_help_menu_and_f1_open_the_page_from_anywhere() {
    let (app, _tx) = engine_home(I4D);
    let mut h = harness(app);
    h.run_steps(2);
    h.get_by_label("Help").click();
    h.run_steps(2);
    h.get_by_label("How lazyagent works  (F1)").click();
    h.run_steps(3);
    assert_eq!((h.state().state.view, h.state().state.guide.current.as_str()), (View::Guide, "overview"));
    h.get_by_label("←  Back  (Esc)").click();
    h.run_steps(2);
    assert_eq!(h.state().state.view, View::Home);
    // From the worktrees, F1; Esc goes back there.
    h.state_mut().state.leave_home();
    h.run_steps(2);
    h.key_press(egui::Key::F1);
    h.run_steps(3);
    assert_eq!((h.state().state.view, h.state().state.guide.back), (View::Guide, View::Worktrees));
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    assert_eq!(h.state().state.view, View::Worktrees);
    // The navigation column jumps to a section.
    h.key_press(egui::Key::F1);
    h.run_steps(3);
    // (The column's entry comes first; the section's heading after.)
    h.get_all_by_label("Every state").next().unwrap().click();
    h.run_steps(2);
    assert_eq!(h.state().state.guide.current, "states");
}

#[test]
fn without_the_rules_feature_the_page_draws_the_built_in_copy() {
    let (app, _tx) = engine_home(I4D);
    let mut h = tall(app);
    let calls = fake_engine(&h, |_, _| None);
    h.run_steps(2);
    h.state_mut().state.open_guide("overview");
    h.run_steps(4);
    assert!(calls_of(&calls, "rules_catalog").is_empty(), "an older engine is not asked");
    // Concepts (cards, zones, the ball), generated states, items and facts.
    for label in [
        "Engine",
        "Orchestrator",
        "To sit down with  ·  3",
        "Parked  ·  1",
        "stalled",
        "Merge",
        "merge: approved, CI green",
        "Request reviewer",
        "PR has no reviewer",
        "Address review",
        "pr.review",
        "No rules of yours: the built-ins apply.",
    ] {
        assert!(h.query_all_by_label(label).count() >= 1, "the page lacks {label:?}");
    }
    assert!(
        h.query_all_by_label_contains("The PR's state is open and the PR's review decision is approved").count() >= 1
    );
    // The annotated line and the colour legend.
    h.get_by_label("changed since you looked");
    h.get_by_label("You: the ball is yours. Nothing else is orange.");
}

#[test]
fn the_page_draws_the_engines_catalog_with_your_rules() {
    let (app, _tx) = engine_home(&with_rules());
    let mut h = tall(app);
    let calls = fake_engine(&h, |m, _| (m == "rules_catalog").then(changed_catalog));
    h.run_steps(2);
    h.state_mut().state.open_guide("states");
    h.run_steps(4);
    assert_eq!(calls_of(&calls, "rules_catalog").len(), 1);
    h.get_by_label("rules from the engine");
    // A built-in rule's state text changed in the engine: the page follows.
    assert!(h.query_all_by_label("merge: QA passed").count() >= 1);
    assert!(h.query_by_label("merge: approved, CI green").is_none());
    h.get_by_label("qa_gate");
    h.get_by_label("global · /home/dev/.config/lazyagent/rules.toml:3");
    h.get_by_label_contains("error · /home/dev/.config/lazyagent/rules.toml:12: rule bad");
    // The concepts and keys stay this build's.
    assert!(h.query_all_by_label("How it fits together").count() >= 1);
}

#[test]
fn the_keys_table_filters() {
    let (app, _tx) = engine_home(I4D);
    let mut h = tall(app);
    h.run_steps(2);
    h.state_mut().state.open_guide("keys");
    h.run_steps(3);
    h.get_by_label("Next / previous line");
    assert!(h.query_all_by_label("Why is this line here?").count() >= 1);
    h.state_mut().state.guide.key_filter = "park".into();
    h.run_steps(3);
    h.get_by_label("Show / hide Parked");
    assert!(h.query_by_label("Next / previous line").is_none(), "filtered out");
    h.state_mut().state.guide.key_filter = "zzz-nothing".into();
    h.run_steps(3);
    h.get_by_label("No key matches.");
}

/// A Merge line (the queue shows the `merge` rule's item), with the
/// engine's `rules_explain` from the golden fixture.
fn merge_home() -> (Harness<'static, App>, mpsc::Sender<Msg>, Calls) {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../tests/protocol/fixtures/ipc/rules_explain__merge.json");
    let fixture: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let explained = fixture["response"]["result"].clone();
    let (app, tx) = engine_home(&with_rules());
    let mut h = harness(app);
    let calls = fake_engine(&h, move |m, p| match m {
        "rules_explain" => {
            let mut e = explained.clone();
            e["key"] = p["key"].clone();
            Some(e)
        }
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    h.run_steps(2);
    let item = json!({"id": "Rule:merge:repo:docs", "occurrence": "Rule:merge:repo:docs@1", "kind": "rule",
                      "level": "round", "title": "Merge", "label": "#14 Index the docs", "detail": "",
                      "state": "merge: approved, CI green", "work_item": "repo:docs", "worktree_path": "/r/repo-docs",
                      "agent_id": null, "pr": {"number": 14, "title": "Index the docs", "url": "https://example.com/pull/14"},
                      "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
                      "since": crate::app::unix_now() - 60.0, "park": null, "rule_id": "merge", "verb": "Merge"});
    let mut row = inbox_row("docs", "round", "Rule:merge:repo:docs", "merge: approved, CI green");
    (row["rule"], row["rule_id"], row["verb"], row["action"]) =
        (json!(2), json!("queue"), json!("Merge"), json!("open_pr"));
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    (h, tx, calls)
}

#[test]
fn w_on_a_merge_line_shows_the_rule_the_facts_and_the_rules_above_that_failed() {
    let (mut h, _tx, calls) = merge_home();
    h.get_by_label("Merge");
    h.key_press(egui::Key::W);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "rules_explain"), [json!({"key": "repo:docs"})]);
    h.get_by_label("Why is this line here?");
    // The rule: its id, where it comes from, what it says and when.
    h.get_by_label("Its inbox item comes from");
    h.get_by_label("merge");
    assert!(h.query_all_by_label("built-in").count() >= 1);
    h.get_by_label("Approved, CI green and mergeable: merging is yours, on the PR.");
    h.get_by_label_contains("when: The PR's state is open and the PR's review decision is approved");
    // The facts it saw.
    for (fact, value) in [("pr.review", "approved"), ("ci.state", "green"), ("pr.mergeable", "mergeable")] {
        h.get_by_label(fact);
        assert!(h.query_all_by_label(value).count() >= 1, "{fact} = {value}");
    }
    // The rules above it that did not match, and why (in English).
    // Folded: most questions never need them.
    h.get_by_label_contains("Rules above merge that did not match · ").click();
    h.run_steps(2);
    h.get_by_label("approve");
    assert!(h.query_all_by_label("The inbox queue is off").count() >= 1);
    // And how the line got there: the locked queue rule, its own rules above.
    h.get_by_label("queue");
    h.get_by_label("built-in, locked");
    h.get_by_label_contains("Rules above queue that did not match · 5").click();
    h.run_steps(2);
    h.get_by_label("The PR's state is merged");
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    assert!(h.state().state.why.is_none(), "back to Home");
    h.get_by_label("Merge");
}

#[test]
fn the_expanded_line_has_a_why_button_and_older_engines_say_why_not() {
    let (mut h, _tx, calls) = merge_home();
    h.state_mut().state.home.expanded = Some("repo:docs".into());
    h.run_steps(3);
    h.get_by_label("Why?  (w)").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "rules_explain").len(), 1);
    assert!(h.state().state.why.is_some());
    // g w is still the worktrees.
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    h.key_press(egui::Key::G);
    h.run_steps(1);
    h.key_press(egui::Key::W);
    h.run_steps(2);
    assert_eq!(h.state().state.view, View::Worktrees);
    assert_eq!(calls_of(&calls, "rules_explain").len(), 1);
    // Without the rules feature: a toast, no request.
    let line = h.state().state.home.lines.get("repo:docs").cloned().unwrap();
    h.state_mut().state.capabilities = Some(I4E.iter().map(|c| c.to_string()).collect());
    h.state_mut().state.open_why(&line);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "rules_explain").len(), 1);
    assert!(h.state().state.toasts.iter().any(|t| t.text.contains("cannot say why")));
}

#[test]
fn rows_keep_the_engines_verb_across_status_updated() {
    let (mut h, tx, _calls) = merge_home();
    // A user rule's verb on a row with no item: the engine's word, not the table's.
    let mut row = status_row("qa", "inbox", Some("round"), "you", "QA passed", 60.0);
    (row["rule"], row["rule_id"], row["verb"]) = (json!(0), json!("qa_gate"), json!("Ship"));
    tx.send(event("status.updated", json!({"row": row.clone()}))).unwrap();
    h.run_steps(2);
    // The event carries no fields: the last ones stay while rule and state do.
    for k in ["rule_id", "verb", "action"] {
        row.as_object_mut().unwrap().remove(k);
    }
    row["changed_at"] = json!(crate::app::unix_now() - 30.0);
    tx.send(event("status.updated", json!({"row": row.clone()}))).unwrap();
    h.run_steps(2);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    let verb = |h: &Harness<'static, App>| h.state().state.home.lines.get("repo:qa").map(|l| l.verb().to_string());
    assert_eq!(verb(&h).as_deref(), Some("Ship"));
    row["state"] = json!("something else");
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.run_steps(2);
    assert_eq!(verb(&h).as_deref(), Some("Look"), "a new state: the fallback until status_list says");
}

// --- The screenshots the page embeds ----------------------------------------
//
// Fixed fake data (a generic "shop" repo), rendered offscreen (wgpu; lavapipe
// in CI) and cropped. `LAZYAGENT_UPDATE_SCREENSHOTS=1 cargo test
// how_it_works_screenshots` writes them to assets/how/ (and the page's own
// shots to docs/design/desktop-screenshots/r6-*.png); otherwise the test
// fails when a committed one no longer matches what the app draws.

const UPDATE: &str = "LAZYAGENT_UPDATE_SCREENSHOTS";
/// Pixels whose channels differ by more than this count as changed …
const PIXEL_TOLERANCE: u8 = 48;
/// … and more than this many changed pixels make a screenshot stale.
const STALE_PIXELS: usize = 150;

fn assets() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("assets/how")
}

/// Home on the fake "shop" repo: Now (an approval, a question), the Round
/// (CI red, a Merge, one more for the next round), To sit down with (a
/// brief), In flight (working, held, passed, delegated, stalled) and a
/// parked line; the orchestrator with its report, queue and actions.
fn shop(width: f32, height: f32) -> (Harness<'static, App>, mpsc::Sender<Msg>) {
    let now = crate::app::unix_now();
    let w = |b: &str| format!("/home/dev/shop-{b}");
    let (tx, rx) = mpsc::channel();
    let state_dir = tempfile::tempdir().unwrap().keep();
    let env = Env::process().with("XDG_STATE_HOME", Some(state_dir.to_str().unwrap()));
    let conn = Conn::new("/home/dev/shop".into(), env, tx.clone(), egui::Context::default());
    let mut state = State::new(conn, FontSet::bundled_only(), Some(ThemeKind::Dark));
    state.capabilities = Some(with_rules().iter().map(|c| c.to_string()).collect());
    state.look_from = now - 1800.0;
    state.presence =
        Some(lazyagent_proto::types::Presence { mode: Some("auto".into()), effective: "present".into(), since: None });
    state.status_counts.auto_actions = Some(2);
    state.status_counts.auto_reverted = Some(0);
    state.status_counts.not_started = 2;
    state.status_counts.deep_max = 5;
    state.orchestrator = Some(
        serde_json::from_value(json!({"alive": true,
            "report": {"note": "verifying search-filters: running the unit tests", "at": now - 120.0},
            "queue": [{"key": "shop:search-filters", "kind": "hold", "note": "verifying tests", "until": now + 3000.0,
                       "by": "orchestrator", "auto": false},
                      {"key": "shop:cart-discount", "kind": "pass", "note": null, "until": now + 6600.0, "by": "user",
                       "auto": false}]}))
        .unwrap(),
    );
    state.orchestrator_actions = Some(
        serde_json::from_value(json!([
            {"key": "shop:billing-retries", "at": now - 300.0, "kind": "decision", "action": "answer_quick",
             "text": "decided: retries → B", "reason": "fixed is enough here", "mandate": "present",
             "decision_id": "d9", "revertible": true},
            {"key": "shop:stock-sync", "at": now - 1500.0, "kind": "action", "action": "ci_rerun",
             "text": "re-ran CI", "reason": null, "mandate": "present", "decision_id": null, "revertible": false}]))
        .unwrap(),
    );
    let branches = [
        "cart-discount",
        "billing-retries",
        "search-filters",
        "invoice-pdf",
        "order-export",
        "stock-sync",
        "docs-index",
        "tax-rules",
        "refunds",
        "retry-backoff",
    ];
    let trees: Vec<_> = std::iter::once(worktree("/home/dev/shop", "main", vec![]))
        .chain(
            branches.iter().map(|b| worktree(&w(b), b, vec![agent("a1", "Implementer", "running", &format!("s-{b}"))])),
        )
        .collect();
    let seed: SidebarState = serde_json::from_value(json!({
        "worktrees": trees, "orchestrator": {"agents": [agent("", "Orchestrator", "waiting", "so")]},
        "pr_info": {}, "git_statuses": {}, "view": null, "cursor": 1
    }))
    .unwrap();
    tx.send(Msg::Link(Link::Connected)).unwrap();
    tx.send(Msg::Seed(Box::new(seed), None)).unwrap();
    let row = |b: &str, zone: &str, level: Option<&str>, ball: &str, st: &str, ago: f64| {
        let mut r = status_row(b, zone, level, ball, st, ago);
        r["key"] = json!(format!("shop:{b}"));
        r["repo"] = json!("shop");
        r["worktree_path"] = json!(w(b));
        (r["seen_at"], r["last_event_at"]) = (json!(now - 1800.0), json!(now - ago.min(3600.0) * 0.5));
        r
    };
    let item = |b: &str, kind: &str, detail: &str| {
        let mut i = ask_item(b, kind, "now", detail);
        let id = format!("{}:{}/a1", if kind == "approval" { "Approval" } else { "Question" }, w(b));
        (i["id"], i["work_item"], i["worktree_path"]) = (json!(id), json!(format!("shop:{b}")), json!(w(b)));
        i
    };
    let approval = item("invoice-pdf", "approval", "rm -rf build/ && npm run build -- --prod && npm test");
    let mut approve = row("invoice-pdf", "inbox", Some("now"), "you", "approve: rm -rf build/", 120.0);
    approve["needs_you"] = json!({"count": 1, "top": approval["id"]});
    approve["agents"] = json!([{"id": "a1", "status": "waiting_for_approval", "since": now - 120.0}]);
    let question = item("refunds", "question", "Refund the shipping fee too, or only the items?");
    let mut ask = row("refunds", "inbox", Some("now"), "you", "answer: refund shipping?", 300.0);
    ask["needs_you"] = json!({"count": 1, "top": question["id"]});
    let ci_item = json!({"id": "CiFailing:shop:tax-rules", "occurrence": "CiFailing:shop:tax-rules@1", "kind": "ci",
        "level": "round", "title": "CI failing", "label": "#21 Tax rules per region", "detail": "tests",
        "state": "CI red: tests", "work_item": "shop:tax-rules", "worktree_path": w("tax-rules"), "agent_id": null,
        "pr": {"number": 21, "title": "Tax rules per region", "url": "https://example.com/shop/pull/21"},
        "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium", "since": now - 900.0,
        "park": null});
    let mut red = row("tax-rules", "inbox", Some("round"), "you", "CI red: tests", 900.0);
    (red["rule"], red["needs_you"]) = (json!(2), json!({"count": 1, "top": "CiFailing:shop:tax-rules"}));
    let merge_item = json!({"id": "Rule:merge:shop:docs-index", "occurrence": "Rule:merge:shop:docs-index@1",
        "kind": "rule", "level": "round", "title": "Merge", "label": "#14 Index the docs", "detail": "",
        "state": "merge: approved, CI green", "work_item": "shop:docs-index", "worktree_path": w("docs-index"),
        "agent_id": null, "pr": {"number": 14, "title": "Index the docs", "url": "https://example.com/shop/pull/14"},
        "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium", "since": now - 1200.0,
        "park": null, "rule_id": "merge", "verb": "Merge"});
    let mut merge = row("docs-index", "inbox", Some("round"), "you", "merge: approved, CI green", 1200.0);
    merge["needs_you"] = json!({"count": 1, "top": "Rule:merge:shop:docs-index"});
    (merge["rule"], merge["rule_id"], merge["verb"]) = (json!(2), json!("queue"), json!("Merge"));
    let mut nobody = row("order-export", "inbox", Some("round"), "you", "PR has no reviewer", 2400.0);
    nobody["rule"] = json!(10);
    let mut d = brief("d1", "retry-backoff", true);
    (d["key"], d["worktree_path"]) = (json!("shop:retry-backoff"), json!(w("retry-backoff")));
    let mut d_item = brief_item("d1", "retry-backoff", "deep", true);
    (d_item["work_item"], d_item["worktree_path"]) = (json!("shop:retry-backoff"), json!(w("retry-backoff")));
    let mut deep = row("retry-backoff", "inbox", Some("deep"), "you", "decide: retries: fixed or exponential?", 600.0);
    (deep["needs_you"], deep["class"]) = (json!({"count": 1, "top": "Decision:d1"}), json!("deep"));
    let mut working = row("stock-sync", "in_flight", None, "agent", "working", 700.0);
    working["agents"] = json!([{"id": "a4", "status": "running", "since": now - 720.0}]);
    working["note"] = json!({"text": "tests pass; opening the PR", "by": "agent:a4", "at": now - 60.0});
    let mut held = row("search-filters", "in_flight", None, "agent", "orchestrator · verifying tests", 600.0);
    (held["rule"], held["holder"]) = (json!(15), json!("orchestrator"));
    held["hold"] = json!({"key": "shop:search-filters", "holder": "orchestrator", "at": now - 600.0,
                          "until": now + 3000.0, "by": "orchestrator", "note": "verifying tests", "auto": false});
    let mut passed = row("cart-discount", "in_flight", None, "agent", "orchestrator · looking", 600.0);
    (passed["rule"], passed["holder"]) = (json!(13), json!("orchestrator"));
    passed["claim"] = json!({"key": "shop:cart-discount", "holder": "orchestrator", "at": now - 600.0,
                             "until": now + 6600.0, "by": "user", "allowed": ["answer_quick", "ci_rerun"]});
    let mut delegated =
        row("billing-retries", "in_flight", None, "agent", "agent a2 · fixing conflict with base", 400.0);
    (delegated["rule"], delegated["holder"]) = (json!(16), json!("agent"));
    let mut stalled = row("order-sync", "in_flight", None, "stalled", "no activity", 3.0 * 86400.0);
    stalled["rule"] = json!(11);
    let mut parked = row("cart-coupons", "parked", None, "you", "parked until something changes", 900.0);
    parked["rule"] = json!(7);
    parked["gesture"] = json!({"kind": "park", "at": now - 900.0, "by": "user", "until": "change"});
    parked["needs_you"] = json!({"count": 1, "top": "Question:/home/dev/shop-cart-coupons/a5"});
    let mut h = Harness::builder()
        .with_size([width, height])
        .wgpu()
        .build_ui_state(|ui, app: &mut App| app.draw(ui), App::new(state, rx, None));
    h.run_steps(2);
    let dd = d.clone();
    let explained = {
        let path =
            concat!(env!("CARGO_MANIFEST_DIR"), "/../../../tests/protocol/fixtures/ipc/rules_explain__merge.json");
        let f: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        f["response"]["result"].clone()
    };
    fake_engine(&h, move |m, p| match m {
        "status_timeline" if p["key"] == "shop:invoice-pdf" => {
            Some(json!({"key": p["key"], "since": null, "seen_at": null,
            "entries": [{"at": now - 900.0, "kind": "commit", "actor": "engine", "text": "pdf: embed the fonts"},
                        {"at": now - 120.0, "kind": "agent", "actor": "agent:a1", "text": "asked to approve a command"}]}))
        }
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": [
            {"at": now - 3000.0, "kind": "commit", "actor": "engine", "text": "retry: exponential backoff"},
            {"at": now - 600.0, "kind": "decision", "actor": "agent:a2",
             "text": "asked: Retries: fixed or exponential backoff?", "ref": "d1"}]})),
        "decision_get" => Some(json!({"decision": dd.clone()})),
        "rules_catalog" => Some(serde_json::from_str(how_it_works::COPY).unwrap()),
        "rules_explain" => {
            let mut e = explained.clone();
            e["key"] = p["key"].clone();
            Some(e)
        }
        "get_diff" => {
            Some(json!({"worktree_path": w("retry-backoff"), "diff": "diff --git a/src/retry.py b/src/retry.py\n\
--- a/src/retry.py\n+++ b/src/retry.py\n@@ -40,7 +40,12 @@ def retry(call):\n     for n in range(TRIES):\n         try:\n\
             return call()\n         except Timeout:\n-            sleep(2)\n+            delay = min(30, 2 ** n)\n\
+            sleep(delay + jitter())\n     raise GaveUp(TRIES)\n", "truncated": false}))
        }
        _ => None,
    });
    let events = vec![
        event("actionable.created", json!({"item": approval})),
        event("actionable.created", json!({"item": question})),
        event("actionable.created", json!({"item": merge_item})),
        event("actionable.created", json!({"item": ci_item})),
        event("decision.created", json!({"decision": d})),
        event("actionable.created", json!({"item": d_item})),
    ];
    for e in events {
        tx.send(e).unwrap();
    }
    for r in [approve, ask, red, merge, deep, working, held, passed, delegated, stalled, parked] {
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    h.run_steps(3);
    h.state_mut().state.inbox_loaded = 3;
    h.state_mut().state.inbox_gen += 1;
    h.run_steps(2);
    h.state_mut().state.home.cut(now);
    tx.send(event("status.updated", json!({"row": nobody}))).unwrap();
    h.run_steps(2);
    h.state_mut().state.home.selected = Some("shop:invoice-pdf".into());
    h.run_steps(3);
    h.state_mut().state.toasts.clear();
    h.run_steps(2);
    (h, tx)
}

/// The harness's image, or None without a GPU adapter (fails instead when
/// LAZYAGENT_REQUIRE_GPU=1, as in CI).
fn render(h: &mut Harness<'static, App>) -> Option<image::RgbaImage> {
    h.run_steps(3);
    match h.render() {
        Ok(img) => Some(img),
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(e) => {
            eprintln!("skipped: no wgpu adapter ({e})");
            None
        }
    }
}

/// `img` cropped to `rect` (logical points; the harness draws at 1:1).
fn crop(img: &image::RgbaImage, rect: egui::Rect) -> image::RgbaImage {
    let x = rect.min.x.max(0.0) as u32;
    let y = rect.min.y.max(0.0) as u32;
    let w = (rect.width() as u32).min(img.width() - x);
    let hgt = (rect.height() as u32).min(img.height() - y);
    image::imageops::crop_imm(img, x, y, w, hgt).to_image()
}

/// Smallest PNG the image crate writes.
fn png(img: &image::RgbaImage) -> Vec<u8> {
    use image::ImageEncoder;
    use image::codecs::png::{CompressionType, FilterType, PngEncoder};
    let mut out = vec![];
    PngEncoder::new_with_quality(&mut out, CompressionType::Best, FilterType::Adaptive)
        .write_image(img.as_raw(), img.width(), img.height(), image::ExtendedColorType::Rgba8)
        .unwrap();
    out
}

/// Pixels that differ beyond the tolerance (or every pixel when the sizes
/// differ).
fn changed_pixels(a: &image::RgbaImage, b: &image::RgbaImage) -> usize {
    if a.dimensions() != b.dimensions() {
        return (a.width() * a.height()).max(b.width() * b.height()) as usize;
    }
    a.pixels()
        .zip(b.pixels())
        .filter(|(p, q)| p.0.iter().zip(q.0.iter()).any(|(x, y)| x.abs_diff(*y) > PIXEL_TOLERANCE))
        .count()
}

/// Writes (when updating) or checks one screenshot; a stale one is kept
/// next to the temp dir's copy for a look.
fn check(name: &str, img: &image::RgbaImage, stale: &mut Vec<String>) {
    let path = assets().join(format!("{name}.png"));
    let bytes = png(img);
    if std::env::var(UPDATE).as_deref() == Ok("1") {
        std::fs::write(&path, &bytes).unwrap();
        println!("wrote {} ({} KB)", path.display(), bytes.len() / 1024);
        return;
    }
    let committed = image::open(&path).map(|i| i.to_rgba8());
    let changed = committed.as_ref().map(|c| changed_pixels(c, img)).unwrap_or(usize::MAX);
    if changed > STALE_PIXELS {
        let fresh = std::env::temp_dir().join(format!("lazyagent-how-{name}.png"));
        std::fs::write(&fresh, &bytes).unwrap();
        stale.push(format!("{name}: {changed} pixels changed (new: {})", fresh.display()));
    }
}

#[test]
fn how_it_works_screenshots_are_current() {
    const W: f32 = 1180.0;
    // The sidebar's width: the shots show Home, not the worktree list.
    const LEFT: f32 = 292.0;
    let mut stale = vec![];
    // Home with its zones.
    let (mut h, _tx) = shop(W, 760.0);
    let Some(img) = render(&mut h) else { return };
    check("home", &crop(&img, egui::Rect::from_min_max(egui::pos2(LEFT, 24.0), egui::pos2(W, 760.0))), &mut stale);
    // The approval, expanded.
    h.state_mut().state.home.expanded = Some("shop:invoice-pdf".into());
    let img = render(&mut h).unwrap();
    let ask = h.get_by_label("rm -rf build/ && npm run build -- --prod && npm test").rect();
    let buttons = h.get_by_label("Why?  (w)").rect();
    let rect = egui::Rect::from_min_max(egui::pos2(LEFT, ask.min.y - 44.0), egui::pos2(W, buttons.max.y + 8.0));
    check("expanded", &crop(&img, rect), &mut stale);
    // The park menu, on the same line.
    h.state_mut().state.home.parking = Some("shop:invoice-pdf".into());
    let img = render(&mut h).unwrap();
    let hint = h.get_by_label("1-5 choose · w whole work item · Esc close").rect();
    let rect = egui::Rect::from_min_max(egui::pos2(LEFT, hint.min.y - 76.0), egui::pos2(W, hint.max.y + 8.0));
    check("park", &crop(&img, rect), &mut stale);
    // The orchestrator strip, open.
    let s = &mut h.state_mut().state;
    (s.home.parking, s.home.expanded, s.home.strip_open) = (None, None, true);
    let img = render(&mut h).unwrap();
    let last = h.get_by_label_contains("re-ran CI").rect();
    let rect = egui::Rect::from_min_max(egui::pos2(LEFT, 64.0), egui::pos2(W, last.max.y + 10.0));
    check("strip", &crop(&img, rect), &mut stale);
    // The decision view.
    h.state_mut().state.home.strip_open = false;
    h.state_mut().state.open_decision("d1", "shop:retry-backoff");
    h.run_steps(5);
    h.state_mut().state.toasts.clear();
    let img = render(&mut h).unwrap();
    check("decision", &crop(&img, egui::Rect::from_min_max(egui::pos2(LEFT, 58.0), egui::pos2(W, 600.0))), &mut stale);
    assert!(
        stale.is_empty(),
        "the page's screenshots are stale: run `{UPDATE}=1 cargo test how_it_works_screenshots` and commit \
         crates/app/assets/how/\n{}",
        stale.join("\n")
    );
}

/// The page itself, for docs/design/desktop-screenshots/r6-*.png (written
/// when updating, else to the temp dir): the top of the page 1100 and 1600
/// wide, dark and light; its Home section, a states table, and the Why?
/// view on the Merge line.
#[test]
fn how_it_works_page_screenshots() {
    let out = if std::env::var(UPDATE).as_deref() == Ok("1") {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../docs/design/desktop-screenshots")
    } else {
        std::env::temp_dir()
    };
    let save = |img: image::RgbaImage, name: String| std::fs::write(out.join(name), png(&img)).unwrap();
    for (width, tag) in [(1100.0, "1100"), (1600.0, "1600")] {
        let (mut h, _tx) = shop(width, 900.0);
        let ctx = h.ctx.clone();
        h.state_mut().state.open_guide("overview");
        let Some(img) = render(&mut h) else { return };
        save(img, format!("r6-page-dark-{tag}.png"));
        if tag == "1600" {
            h.state_mut().state.open_guide("home");
            save(render(&mut h).unwrap(), format!("r6-page-home-dark-{tag}.png"));
        }
        h.state_mut().state.toggle_theme(&ctx);
        h.state_mut().state.open_guide("overview");
        save(render(&mut h).unwrap(), format!("r6-page-light-{tag}.png"));
        if tag == "1100" {
            h.state_mut().state.open_guide("states");
            save(render(&mut h).unwrap(), format!("r6-page-states-light-{tag}.png"));
        }
        // Back on Home: Why? on the Merge line.
        h.key_press(egui::Key::Escape);
        h.run_steps(2);
        let line = h.state().state.home.lines.get("shop:docs-index").cloned().unwrap();
        h.state_mut().state.open_why(&line);
        if tag == "1100" {
            save(render(&mut h).unwrap(), format!("r6-why-light-{tag}.png"));
        } else {
            h.state_mut().state.toggle_theme(&ctx);
            save(render(&mut h).unwrap(), format!("r6-why-dark-{tag}.png"));
        }
    }
}
