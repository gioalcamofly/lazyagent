//! I4h: an agent's question in prose at the end of its turn leads its line
//! (verb Answer, the question as the text, Enter opens the terminal), and
//! Delegate / Pass only where an agent or the orchestrator can act: not on
//! the owner's Review or Merge, nor on the question; on CI red, yes. And
//! the screenshots.

use super::*;

const QUESTION: &str = "Should I fix the flaky parser test in this PR, or open a separate task?";

fn caps() -> Vec<&'static str> {
    I4E.iter().copied().chain(["rules", "actions", "item_capabilities"]).collect()
}

fn settle(h: &mut Harness<'static, App>) {
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
}

fn item(key: &str, kind: &str, fields: serde_json::Value) -> serde_json::Value {
    let mut item = json!({"id": format!("X:{key}"), "occurrence": format!("X:{key}@1"), "kind": kind,
                          "level": "round", "title": "", "label": "#7 Feat", "detail": "", "state": "",
                          "work_item": format!("repo:{key}"), "worktree_path": format!("/r/repo-{key}"),
                          "agent_id": "a1", "pr": {"number": 7, "title": "Feat", "url": "https://example.com/pull/7"},
                          "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
                          "since": crate::app::unix_now() - 600.0, "park": null, "run": null, "class": "quick"});
    for (k, v) in fields.as_object().unwrap() {
        item[k] = v.clone();
    }
    item
}

/// A line whose top item is `item`, with an idle agent a1 and the engine's
/// row fields (verb, action, delegable, passable).
fn line(tx: &mpsc::Sender<Msg>, key: &str, item: &serde_json::Value, verb: &str, action: &str, passable: bool) {
    let mut row = status_row(key, "inbox", Some("round"), "you", item["state"].as_str().unwrap(), 600.0);
    row["needs_you"] = json!({"count": 1, "top": item["id"],
                              "items": [{"id": item["id"], "kind": item["kind"], "level": "round",
                                         "state": item["state"], "since": item["since"]}]});
    row["agents"] = json!([{"id": "a1", "status": "waiting", "since": crate::app::unix_now() - 600.0}]);
    row["urls"] = json!(["https://example.com/pull/7"]);
    (row["rule_id"], row["verb"], row["action"]) = (json!("queue"), json!(verb), json!(action));
    (row["delegable"], row["passable"]) = (item["delegable"].clone(), json!(passable));
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
}

fn asks(tx: &mpsc::Sender<Msg>, key: &str) {
    let it = item(
        key,
        "turn_question",
        json!({
        "id": format!("Turn_question:/r/repo-{key}/a1"), "title": "Asks you", "label": QUESTION,
        "detail": "Agent a1 asks · answer in its terminal", "state": "answer: Should I fix the…",
        "delegable": false, "passable": false}),
    );
    line(tx, key, &it, "Answer", "open_terminal", false);
}

/// The owner's own review again after new commits: a user rule's item.
fn owner_review(tx: &mpsc::Sender<Msg>, key: &str) {
    let it = item(
        key,
        "rule",
        json!({
        "id": format!("Rule:owner_review_again:repo:{key}"), "title": "Review",
        "state": "new commits since your approval", "rule_id": "owner_review_again", "verb": "Review",
        "agent_id": null, "delegable": false, "passable": false}),
    );
    line(tx, key, &it, "Review", "open_pr", false);
}

fn merge(tx: &mpsc::Sender<Msg>, key: &str) {
    let it = item(
        key,
        "rule",
        json!({
        "id": format!("Rule:merge:repo:{key}"), "title": "Merge", "state": "merge: approved, CI green",
        "rule_id": "merge", "verb": "Merge", "agent_id": null, "delegable": false, "passable": false}),
    );
    line(tx, key, &it, "Merge", "open_pr", false);
}

fn ci_red(tx: &mpsc::Sender<Msg>, key: &str) {
    let it = item(
        key,
        "ci",
        json!({
        "id": format!("Ci:/r/repo-{key}:7"), "title": "CI failing", "detail": "tests",
        "state": "CI red: tests", "agent_id": null, "delegable": true, "passable": true}),
    );
    line(tx, key, &it, "Fix CI", "delegate", true);
}

fn orchestrator_info() -> lazyagent_proto::types::Agent {
    serde_json::from_value(agent("", "Orchestrator", "waiting", "so")).unwrap()
}

fn engine(h: &Harness<'static, App>) -> Calls {
    fake_engine(h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        "ci_log_summary" => Some(json!({"jobs": [], "text": ""})),
        _ => None,
    })
}

fn select(h: &mut Harness<'static, App>, key: &str) {
    h.state_mut().state.home.selected = Some(format!("repo:{key}"));
    h.run_steps(1);
}

fn expand(h: &mut Harness<'static, App>, key: &str) {
    h.state_mut().state.home.expanded = Some(format!("repo:{key}"));
    select(h, key);
    h.run_steps(3);
}

fn bar(h: &Harness<'static, App>, key: &str) -> String {
    let line = h.state().state.home.lines.get(&format!("repo:{key}")).cloned().unwrap();
    crate::home_view::line_keys(
        &crate::home_view::KeysCtx { delegation: true, gestures: true, pass: true, ..Default::default() },
        Some(&line),
    )
}

#[test]
fn a_question_at_the_end_of_a_turn_is_an_answer_line_and_enter_opens_the_terminal() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h);
    asks(&tx, "parser");
    settle(&mut h);
    let line = h.state().state.home.lines.get("repo:parser").cloned().unwrap();
    assert_eq!(line.verb(), "Answer");
    assert_eq!(line.ask_text().as_deref(), Some(QUESTION));
    // The line's text is the question itself, not five words of it.
    assert_eq!(line.state, QUESTION);
    assert!(!line.delegable() && !line.passable());
    h.get_by_label("Answer");
    let keys = bar(&h, "parser");
    assert!(keys.starts_with("Enter open · o terminal"), "{keys}");
    assert!(!keys.contains("delegate") && !keys.contains("pass"), "{keys}");
    // Enter: the agent's terminal, where the answer goes.
    select(&mut h, "parser");
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    assert_eq!(h.state().state.view, crate::app::View::Worktrees);
    assert_eq!(h.state().state.selected.as_deref(), Some("/r/repo-parser"));
    // d and ⇧O: nothing sent.
    h.state_mut().state.view = crate::app::View::Home;
    h.run_steps(2);
    select(&mut h, "parser");
    h.key_press(egui::Key::D);
    h.run_steps(2);
    assert!(h.state().state.toasts.iter().any(|t| t.text.contains("answered, not delegated")));
    assert!(calls_of(&calls, "status_gesture").is_empty());
    // Expanded: the question in full, the terminal, no Delegate, no Pass.
    expand(&mut h, "parser");
    h.get_by_label("Open terminal  (o)");
    // On the line and in full under it.
    assert!(h.query_all_by_label_contains(QUESTION).count() >= 2);
    assert!(h.query_by_label("Delegate to its agent  (d)").is_none());
    assert!(h.query_by_label("Pass to orchestrator  (⇧O)").is_none());
}

#[test]
fn review_and_merge_offer_neither_delegate_nor_pass_ci_red_offers_both() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h);
    h.state_mut().state.store.orchestrator = vec![orchestrator_info()];
    owner_review(&tx, "review");
    merge(&tx, "merge");
    ci_red(&tx, "tests");
    settle(&mut h);
    for key in ["review", "merge"] {
        expand(&mut h, key);
        assert!(h.query_by_label("Delegate to its agent  (d)").is_none(), "{key}");
        assert!(h.query_by_label("Spawn an agent for this").is_none(), "{key}");
        assert!(h.query_by_label("Pass to orchestrator  (⇧O)").is_none(), "{key}");
        let keys = bar(&h, key);
        assert!(!keys.contains("delegate") && !keys.contains("pass"), "{key}: {keys}");
        h.key_press(egui::Key::D);
        h.run_steps(2);
        h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::O);
        h.run_steps(2);
    }
    assert!(calls_of(&calls, "status_gesture").is_empty(), "nothing delegated or passed");
    let toasts: Vec<String> = h.state().state.toasts.iter().map(|t| t.text.clone()).collect();
    assert!(toasts.iter().any(|t| t == "Review is yours to do: an agent cannot do it for you."), "{toasts:?}");
    assert!(toasts.iter().any(|t| t == "Merge is yours: the orchestrator cannot act on it."), "{toasts:?}");

    expand(&mut h, "tests");
    h.get_by_label("Delegate to its agent  (d)");
    h.get_by_label("Pass to orchestrator  (⇧O)");
    assert!(bar(&h, "tests").contains("d delegate"));
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::O);
    h.run_steps(3);
    assert_eq!(
        calls_of(&calls, "status_gesture"),
        [json!({"key": "repo:tests", "kind": "delegate", "to": "orchestrator"})]
    );
}

#[test]
fn an_older_engine_without_the_fields_keeps_the_kind_list() {
    let (app, tx) = engine_home(&I4E.iter().copied().chain(["rules", "actions"]).collect::<Vec<_>>());
    let mut h = harness(app);
    engine(&h);
    let mut it = item("tests", "ci", json!({"id": "Ci:/r/repo-tests:7", "state": "CI red: tests", "agent_id": null}));
    it.as_object_mut().unwrap().remove("delegable");
    line(&tx, "tests", &it, "Fix CI", "delegate", true);
    settle(&mut h);
    let l = h.state().state.home.lines.get("repo:tests").cloned().unwrap();
    assert!(l.delegable() && l.passable());
    expand(&mut h, "tests");
    h.get_by_label("Delegate to its agent  (d)");
}

#[test]
fn status_list_asks_for_the_capabilities_with_the_feature() {
    let (app, _tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h);
    h.state_mut().state.fetch_inbox();
    h.run_steps(2);
    let p = calls_of(&calls, "status_list").pop().expect("asked");
    assert_eq!(p["fields"], json!(["rule_id", "verb", "action", "delegable", "passable"]));
}

/// docs/design/desktop-screenshots/i4h-*.png: the Answer line expanded
/// above a Review and a CI red line (dark 1600, light 1100), and the
/// Review and CI red lines expanded.
#[test]
fn home_i4h_screenshots() {
    let caps = caps();
    let out = std::env::temp_dir();
    for (width, tag, dark) in [(1600.0, "dark-1600", true), (1100.0, "light-1100", false)] {
        let (mut app, tx) = engine_home(&caps);
        app.state.look_from = crate::app::unix_now();
        let mut h = Harness::builder().with_size([width, 900.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app);
        h.run_steps(2);
        if dark != (h.state().state.theme.kind == ThemeKind::Dark) {
            let ctx = h.ctx.clone();
            h.state_mut().state.toggle_theme(&ctx);
            h.run_steps(2);
        }
        engine(&h);
        h.state_mut().state.store.orchestrator = vec![orchestrator_info()];
        asks(&tx, "parser");
        owner_review(&tx, "export");
        ci_red(&tx, "tests");
        settle(&mut h);
        let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
            Ok(image) => {
                image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
                true
            }
            Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
            Err(_) => false,
        };
        expand(&mut h, "parser");
        if !shot(&mut h, &format!("i4h-answer-{tag}")) {
            return;
        }
        if dark {
            expand(&mut h, "export");
            shot(&mut h, "i4h-review-dark-1600");
            expand(&mut h, "tests");
            shot(&mut h, "i4h-ci-dark-1600");
        }
    }
}
