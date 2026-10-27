//! I4i: Delegate all (one letter with every delegable item), one item
//! alone from its own line, a held letter with Send anyway, Why? and Notes
//! only under More…, a repeated timeline line counted.

use super::*;

fn caps() -> Vec<&'static str> {
    I4E.iter().copied().chain(["rules", "actions", "item_capabilities", "delegate_all"]).collect()
}

fn settle(h: &mut Harness<'static, App>) {
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
}

fn item(id: &str, kind: &str, state: &str) -> serde_json::Value {
    json!({"id": id, "occurrence": format!("{id}@1"), "kind": kind, "level": "round", "title": "",
           "label": "#7 Feat", "detail": "", "state": state, "work_item": "repo:feat",
           "worktree_path": "/r/repo-feat", "agent_id": null,
           "pr": {"number": 7, "title": "Feat", "url": "https://example.com/pull/7"},
           "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
           "since": crate::app::unix_now() - 600.0, "park": null, "run": null, "class": "quick",
           "delegable": true, "passable": true})
}

const CI: &str = "Ci:/r/repo-feat:7";
const CONFLICT: &str = "Conflict:/r/repo-feat:7";

/// repo:feat with a conflict and CI red, both delegable, agent a1 idle;
/// `delegation`: the row's.
fn feat(tx: &mpsc::Sender<Msg>, delegation: serde_json::Value) {
    let items = [item(CONFLICT, "conflict", "conflict with base"), item(CI, "ci", "CI red: tests")];
    let mut row = status_row("feat", "inbox", Some("round"), "you", "conflict with base", 600.0);
    row["needs_you"] = json!({"count": 2, "top": CONFLICT, "items": items.iter().map(|i| json!({
        "id": i["id"], "kind": i["kind"], "level": "round", "state": i["state"], "since": i["since"]})).collect::<Vec<_>>()});
    row["agents"] = json!([{"id": "a1", "status": "waiting", "since": crate::app::unix_now() - 600.0}]);
    row["urls"] = json!(["https://example.com/pull/7"]);
    (row["rule_id"], row["verb"], row["action"]) = (json!("queue"), json!("Resolve"), json!("delegate"));
    (row["delegable"], row["passable"], row["delegates"]) = (json!(true), json!(true), json!([CONFLICT, CI]));
    row["delegation"] = delegation;
    for i in items {
        tx.send(event("actionable.created", json!({"item": i}))).unwrap();
    }
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
}

fn engine(h: &Harness<'static, App>, entries: serde_json::Value) -> Calls {
    fake_engine(h, move |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": entries.clone()})),
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        "ci_log_summary" => Some(json!({"jobs": [], "text": ""})),
        _ => None,
    })
}

fn expand(h: &mut Harness<'static, App>) {
    h.state_mut().state.home.expanded = Some("repo:feat".into());
    h.state_mut().state.home.selected = Some("repo:feat".into());
    h.run_steps(4);
}

#[test]
fn delegate_all_sends_every_delegable_item_and_one_goes_alone_from_its_line() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h, json!([]));
    feat(&tx, json!(null));
    settle(&mut h);
    let line = h.state().state.home.lines.get("repo:feat").cloned().unwrap();
    assert_eq!(line.delegate_label(), "Delegate all (2)");
    expand(&mut h);
    h.get_by_label("Delegate all (2)  (d)").click();
    h.run_steps(3);
    assert_eq!(
        calls_of(&calls, "status_gesture"),
        [json!({"key": "repo:feat", "kind": "delegate", "to": "agent"})],
        "no item: every one the row's delegates names"
    );
    // One alone, from its own line under "Its items".
    let alone = h.query_all_by_label("delegate this one").count();
    assert_eq!(alone, 2, "one per delegable item");
    h.query_all_by_label("delegate this one").nth(1).unwrap().click();
    h.run_steps(3);
    assert_eq!(
        calls_of(&calls, "status_gesture").pop().unwrap(),
        json!({"key": "repo:feat", "kind": "delegate", "to": "agent", "item": CI})
    );
}

#[test]
fn one_delegable_item_keeps_the_plain_label() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    engine(&h, json!([]));
    feat(&tx, json!(null));
    settle(&mut h);
    let mut line = h.state().state.home.lines.get("repo:feat").cloned().unwrap();
    line.row.as_mut().unwrap().delegates = Some(vec![CI.into()]);
    assert_eq!(line.delegate_label(), "Delegate to its agent");
    line.row.as_mut().unwrap().delegates = None;
    assert_eq!(line.delegate_label(), "Delegate to its agent", "an older engine");
}

#[test]
fn why_and_notes_live_under_more_and_their_keys_still_show() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    engine(&h, json!([]));
    feat(&tx, json!(null));
    settle(&mut h);
    expand(&mut h);
    h.get_by_label("Open terminal  (o)");
    assert!(h.query_by_label("Why?  (w)").is_none(), "not in the bar");
    assert!(h.query_by_label("Notes  (n)").is_none(), "not in the bar");
    h.get_by_label("More…").click();
    h.run_steps(2);
    h.get_by_label("Why?  (w)");
    h.get_by_label("Notes  (n)");
    let line = h.state().state.home.lines.get("repo:feat").cloned().unwrap();
    let keys = crate::home_view::line_keys(
        &crate::home_view::KeysCtx { delegation: true, gestures: true, pass: true, why: true, ..Default::default() },
        Some(&line),
    );
    let all = crate::home_view::hint_keys(true, true, false, false, false);
    assert!(keys.contains("n notes") || all.contains("n notes"), "{keys} / {all}");
    assert!(keys.contains("w why") || all.contains("w why"), "{keys} / {all}");
}

#[test]
fn the_bar_keeps_primary_verbs_and_sends_the_rest_under_more() {
    let (shown, more) = crate::home_view::bar_texts(&[
        ("Open terminal", false),
        ("Notes", true),
        ("Delegate all (2)", false),
        ("Dismiss", false),
        ("Park until…", false),
        ("Pass to orchestrator", false),
        ("Approve", false),
        ("PR", false),
        ("Why?", true),
    ]);
    assert_eq!(
        shown,
        ["Open terminal", "Delegate all (2)", "Dismiss", "Park until…", "Pass to orchestrator", "Approve"]
    );
    assert_eq!(more, ["PR", "Notes", "Why?"]);
    assert!(shown.len() <= crate::home_view::PRIMARY_BUTTONS);
}

fn held(since: f64) -> serde_json::Value {
    json!({"agent": "a1", "item": CONFLICT, "occurrence": format!("{CONFLICT}@1"), "kind": "conflict",
           "fact": "conflict with base", "at": since, "by": "user", "phase": "on_it", "sent_at": null,
           "ran": false, "state": "", "held": "waiting for a1 to be free (possibly hanged)",
           "held_since": since, "items": [{"id": CONFLICT, "kind": "conflict", "fact": "conflict with base",
                                           "sent_at": null}]})
}

#[test]
fn a_held_letter_says_why_and_offers_send_anyway_after_a_minute() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h, json!([]));
    let now = crate::app::unix_now();
    feat(&tx, held(now - 10.0));
    settle(&mut h);
    expand(&mut h);
    h.get_by_label_contains("waiting for a1 to be free (possibly hanged)");
    assert!(h.query_by_label("Send anyway").is_none(), "not before a minute");
    feat(&tx, held(now - 120.0));
    settle(&mut h);
    expand(&mut h);
    h.get_by_label("waiting for a1 to be free (possibly hanged) · 2m");
    h.get_by_label("Send anyway").click();
    h.run_steps(3);
    assert_eq!(
        calls_of(&calls, "status_gesture"),
        [json!({"key": "repo:feat", "kind": "delegate", "to": "agent", "send_now": true})]
    );
    // Not a new top-level button.
    assert!(h.query_by_label("Send anyway  (s)").is_none());
}

#[test]
fn hold_text_is_empty_once_sent_or_returned() {
    let mut d: lazyagent_proto::types::Delegation = serde_json::from_value(held(100.0)).unwrap();
    assert!(crate::home_view::send_anyway(&d, 200.0));
    assert!(!crate::home_view::send_anyway(&d, 130.0));
    d.held.clear();
    assert_eq!(crate::home_view::hold_text(&d, 200.0), None);
}

#[test]
fn a_repeated_delegate_line_is_counted_once() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let at = crate::app::unix_now() - 120.0;
    engine(
        &h,
        json!([{"at": at, "kind": "delegate", "actor": "user", "text": "delegated to agent a1: conflict with base",
                       "count": 3}]),
    );
    feat(&tx, json!(null));
    settle(&mut h);
    h.state_mut().state.home.selected = Some("repo:feat".into());
    h.key_press(egui::Key::Enter);
    h.run_steps(4);
    h.get_by_label_contains("delegated to agent a1: conflict with base ×3");
}

#[test]
fn the_engines_brief_says_where_its_answer_goes() {
    let d: lazyagent_proto::types::Decision = serde_json::from_value(json!({
        "id": "d9", "worktree_path": "/r/repo-feat", "agent": "a5", "key": "repo:feat", "state": "open",
        "holder": "user", "replaces": null, "class": "deep", "blocking": false, "invalid": [], "problems": [],
        "sha": null, "closed_at": null, "valid": false, "source": "auto_delegate", "created_at": 1.0,
        "brief": {"question": "Auto-delegation failed twice: conflict with base on feat", "options": [],
                  "recommendation": null, "tried": "", "evidence": [], "reversible": null, "impact_if_wrong": ""}
    }))
    .unwrap();
    assert_eq!(crate::home_view::engine_answer_text(&d), "sends this to agent a5");
}
