//! Deep items reachable from Home (proposal 0013, I4g): a ghost line in To
//! sit down with is a keyboard stop and a click target that acts on its own
//! item (a brief answered there), a brief under a CI top is answered in the
//! expanded line, the selection survives a refresh, the engine's own brief
//! is answered in words, the skip list says why no agent went; and the
//! screenshots.

use super::*;

const GHOST: &str = "repo:tax\nDecision:d1";

fn caps() -> Vec<&'static str> {
    I4E.iter().copied().chain(["rules", "actions"]).collect()
}

fn home() -> (Harness<'static, App>, mpsc::Sender<Msg>) {
    let caps = caps();
    let (app, tx) = engine_home(&caps);
    (harness(app), tx)
}

fn ci_item(key: &str) -> serde_json::Value {
    json!({"id": format!("C:/r/repo-{key}:7"), "occurrence": "c@1", "kind": "ci", "level": "round", "title": "ci",
           "label": "#7 Feat", "detail": "", "state": "CI red: eval-suite", "work_item": format!("repo:{key}"),
           "worktree_path": format!("/r/repo-{key}"), "agent_id": null,
           "pr": {"number": 7, "title": "Feat", "url": "https://example.com/pull/7"},
           "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
           "since": crate::app::unix_now() - 300.0, "park": null, "run": null})
}

fn needs(items: &[&serde_json::Value]) -> serde_json::Value {
    json!({"count": items.len(), "top": items[0]["id"],
           "items": items.iter().map(|i| json!({"id": i["id"], "kind": i["kind"], "level": i["level"],
                                                  "state": i["state"], "since": i["since"]})).collect::<Vec<_>>()})
}

/// A Round line whose CI is red, a brief `d1` (`level`) under it.
fn red_with_brief(tx: &mpsc::Sender<Msg>, level: &str, d: serde_json::Value) -> serde_json::Value {
    let ci = ci_item("tax");
    let b = brief_item("d1", "tax", level, true);
    let mut row = status_row("tax", "inbox", Some("round"), "you", "CI red: eval-suite", 300.0);
    row["needs_you"] = needs(&[&ci, &b]);
    row["urls"] = json!(["https://example.com/pull/7"]);
    tx.send(event("actionable.created", json!({"item": ci}))).unwrap();
    tx.send(event("actionable.created", json!({"item": b}))).unwrap();
    tx.send(event("decision.created", json!({"decision": d}))).unwrap();
    tx.send(event("status.updated", json!({"row": row.clone()}))).unwrap();
    row
}

fn settle(h: &mut Harness<'static, App>) {
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
}

fn engine_answers(h: &Harness<'static, App>) -> Calls {
    fake_engine(h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        "decision_answer" => {
            let mut d = brief("d1", "tax", true);
            d["state"] = json!("answered");
            Some(json!({"decision": d}))
        }
        _ => None,
    })
}

fn selected(h: &Harness<'static, App>) -> Option<String> {
    h.state().state.home.selected.clone()
}

#[test]
fn a_ghost_is_a_keyboard_stop_and_its_brief_is_answered_there() {
    let (mut h, tx) = home();
    let calls = engine_answers(&h);
    red_with_brief(&tx, "deep", brief("d1", "tax", true));
    settle(&mut h);
    assert_eq!(h.state().state.home.deep_count(), 1);
    // j from the Round line: the ghost, not the line again.
    h.state_mut().state.home.selected = Some("repo:tax".into());
    h.run_steps(1);
    h.key_press(egui::Key::J);
    h.run_steps(2);
    assert_eq!(selected(&h).as_deref(), Some(GHOST));
    h.key_press(egui::Key::K);
    h.run_steps(2);
    assert_eq!(selected(&h).as_deref(), Some("repo:tax"));
    // 3 jumps to To sit down with: its only line is the ghost. Tab cycles.
    h.key_press(egui::Key::Num3);
    h.run_steps(2);
    assert_eq!(selected(&h).as_deref(), Some(GHOST));
    h.key_press(egui::Key::Tab);
    h.run_steps(2);
    assert_eq!(selected(&h).as_deref(), Some("repo:tax"));
    h.key_press(egui::Key::Tab);
    h.run_steps(2);
    assert_eq!(selected(&h).as_deref(), Some(GHOST));
    // Enter expands the ghost on its brief (not the CI, not the view).
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    let s = &h.state().state;
    assert!(s.decision.is_none(), "Enter expands a ghost; v opens the view");
    assert_eq!(s.home.expanded.as_deref(), Some(GHOST));
    h.get_by_label_contains("A  Exponential, cap 30 s");
    h.get_by_label_contains("2. Decide · decide: retries: fixed or exponential? · this line");
    assert!(h.query_by_label("Failed log").is_none(), "the CI's details stay on its line");
    h.key_press(egui::Key::Num2);
    h.run_steps(2);
    assert_eq!(h.state().state.home.brief_choice, Some(("d1".into(), "B".into())));
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Enter);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_answer"), [json!({"id": "d1", "choice": "B", "comment": ""})]);
    // v on the ghost opens the decision view on its brief.
    h.state_mut().state.home.selected = Some(GHOST.into());
    h.run_steps(1);
    h.key_press(egui::Key::V);
    h.run_steps(2);
    assert_eq!(h.state().state.decision.as_ref().map(|v| v.id.as_str()), Some("d1"));
}

#[test]
fn a_click_on_a_ghost_selects_and_expands_it() {
    let (mut h, tx) = home();
    engine_answers(&h);
    red_with_brief(&tx, "deep", brief("d1", "tax", true));
    settle(&mut h);
    // Its question, not the CI's state; where its line is.
    h.get_by_label("under its Fix CI in the Round").click();
    h.run_steps(3);
    assert_eq!(selected(&h).as_deref(), Some(GHOST));
    assert_eq!(h.state().state.home.expanded.as_deref(), Some(GHOST));
    h.get_by_label_contains("B  Fixed 2 s, 5 tries");
    // Its own label takes the click too.
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    assert!(h.state().state.home.expanded.is_none());
    h.get_by_label("· Retries: fixed or exponential backoff?").click();
    h.run_steps(3);
    assert_eq!(h.state().state.home.expanded.as_deref(), Some(GHOST));
}

#[test]
fn a_brief_under_a_ci_top_is_answered_in_the_expanded_line() {
    let (mut h, tx) = home();
    let calls = engine_answers(&h);
    // A quick brief: no ghost line, answered on its line all the same.
    red_with_brief(&tx, "round", brief("d1", "tax", true));
    settle(&mut h);
    assert_eq!(h.state().state.home.deep_count(), 0);
    h.state_mut().state.home.selected = Some("repo:tax".into());
    h.run_steps(1);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    h.get_by_label("Its items");
    h.get_by_label("Answer here").click();
    h.run_steps(3);
    h.get_by_label_contains("A  Exponential, cap 30 s");
    h.key_press(egui::Key::Num1);
    h.run_steps(2);
    assert_eq!(h.state().state.home.brief_choice, Some(("d1".into(), "A".into())));
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Enter);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "decision_answer"), [json!({"id": "d1", "choice": "A", "comment": ""})]);
    // v opens the view on the brief opened in the items, not on the CI.
    h.key_press(egui::Key::V);
    h.run_steps(2);
    assert_eq!(h.state().state.decision.as_ref().map(|v| v.id.as_str()), Some("d1"));
}

#[test]
fn the_ghost_stays_selected_across_a_refresh() {
    let (mut h, tx) = home();
    engine_answers(&h);
    let row = red_with_brief(&tx, "deep", brief("d1", "tax", true));
    settle(&mut h);
    h.key_press(egui::Key::Num3);
    h.run_steps(2);
    h.key_press(egui::Key::Enter);
    h.run_steps(2);
    // The row again, a new line above it, and a cut: still the ghost.
    let other = status_row("cart", "inbox", Some("round"), "you", "conflict with base", 900.0);
    tx.send(event("status.updated", json!({"row": other}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    settle(&mut h);
    assert_eq!(selected(&h).as_deref(), Some(GHOST));
    assert_eq!(h.state().state.home.expanded.as_deref(), Some(GHOST));
    let line = h.state_mut().state.home.current(false);
    assert_eq!(line.and_then(|l| l.ghost).map(|g| g.item).as_deref(), Some("Decision:d1"));
}

#[test]
fn the_engines_own_brief_is_answered_in_words_from_its_ghost() {
    let (mut h, tx) = home();
    let calls = engine_answers(&h);
    let mut d = brief("d1", "tax", false);
    d["source"] = json!("auto_delegate");
    d["agent"] = json!("a1");
    d["brief"]["question"] = json!("Auto-delegation failed twice: CI red: eval-suite on tax");
    d["brief"]["options"] = json!([]);
    d["brief"]["tried"] = json!("10:02 sent to agent a1 · no commit was made · its last report: \"flaky, cannot fix\"");
    d["invalid"] = json!(["written by the engine"]);
    red_with_brief(&tx, "deep", d);
    settle(&mut h);
    h.key_press(egui::Key::Num3);
    h.run_steps(2);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    assert!(matches!(h.state().state.view, crate::app::View::Home), "not the agent's terminal: answered here");
    assert_eq!(h.state().state.home.expanded.as_deref(), Some(GHOST));
    h.get_by_label("Auto-delegation failed twice: CI red: eval-suite on tax");
    h.get_by_label_contains("no commit was made");
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Enter);
    h.run_steps(2);
    assert!(calls_of(&calls, "decision_answer").is_empty(), "nothing typed yet");
    h.state_mut().state.home.brief_choice = Some(("d1".into(), "leave eval-suite, fix the rest".into()));
    h.run_steps(1);
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Enter);
    h.run_steps(3);
    assert_eq!(
        calls_of(&calls, "decision_answer"),
        [json!({"id": "d1", "choice": "leave eval-suite, fix the rest", "comment": ""})]
    );
}

#[test]
fn a_skipped_auto_delegation_says_why_on_the_expanded_line() {
    let (mut h, tx) = home();
    engine_answers(&h);
    let ci = ci_item("tax");
    let mut row = status_row("tax", "inbox", Some("round"), "you", "CI red: eval-suite", 300.0);
    row["needs_you"] = needs(&[&ci]);
    row["auto_delegate"] = json!("auto-delegation skipped: eval-suite is on the skip list");
    tx.send(event("actionable.created", json!({"item": ci}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    settle(&mut h);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    h.get_by_label("auto-delegation skipped: eval-suite is on the skip list");
}

/// docs/design/desktop-screenshots/i4g-*.png: To sit down with with a
/// ghost brief selected, the ghost expanded on its brief, the Round line
/// expanded with a brief answered in place, and the engine's own brief.
#[test]
fn home_i4g_screenshots() {
    let now = crate::app::unix_now();
    let caps = caps();
    let out = std::env::temp_dir();
    for (width, tag, dark) in [(1600.0, "dark-1600", true), (1100.0, "light-1100", false)] {
        let (mut app, tx) = engine_home(&caps);
        app.state.look_from = now;
        let mut h = Harness::builder().with_size([width, 900.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app);
        h.run_steps(2);
        if dark != (h.state().state.theme.kind == ThemeKind::Dark) {
            let ctx = h.ctx.clone();
            h.state_mut().state.toggle_theme(&ctx);
            h.run_steps(2);
        }
        engine_answers(&h);
        red_with_brief(&tx, "deep", brief("d1", "tax", true));
        // A second red line whose engine brief sits under it.
        let mut ci = ci_item("cart");
        ci["state"] = json!("CI red: label-gate");
        let mut b = brief_item("d2", "cart", "deep", false);
        b["state"] = json!("auto-delegation failed twice");
        (b["label"], b["detail"]) = (
            json!("Auto-delegation failed twice: CI red: label-gate on cart"),
            json!("auto-delegation failed: see what was tried"),
        );
        let mut d = brief("d2", "cart", false);
        d["source"] = json!("auto_delegate");
        d["brief"]["question"] = json!("Auto-delegation failed twice: CI red: label-gate on cart");
        d["brief"]["options"] = json!([]);
        (d["brief"]["evidence"], d["brief"]["reversible"], d["brief"]["recommendation"]) =
            (json!([]), json!(null), json!(null));
        d["brief"]["tried"] = json!(
            "10:02 sent to agent a1 · no commit was made\n10:40 sent to agent a1 · it committed (head now 4f2a9c1) · its last report: \"unit fixed; label-gate needs a label\""
        );
        let mut row = status_row("cart", "inbox", Some("round"), "you", "CI red: label-gate", 900.0);
        row["needs_you"] = needs(&[&ci, &b]);
        let skip = ci_item("pdf");
        let mut skipped = status_row("pdf", "inbox", Some("round"), "you", "CI red: eval-suite", 1200.0);
        skipped["needs_you"] = needs(&[&skip]);
        skipped["auto_delegate"] = json!("auto-delegation skipped: eval-suite is on the skip list");
        for i in [&ci, &b, &skip] {
            tx.send(event("actionable.created", json!({"item": i}))).unwrap();
        }
        tx.send(event("decision.created", json!({"decision": d}))).unwrap();
        for r in [row, skipped] {
            tx.send(event("status.updated", json!({"row": r}))).unwrap();
        }
        settle(&mut h);
        h.state_mut().state.home.selected = Some(GHOST.into());
        h.run_steps(3);
        let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
            Ok(image) => {
                image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
                true
            }
            Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
            Err(_) => false,
        };
        if !shot(&mut h, &format!("i4g-home-{tag}")) {
            return;
        }
        h.key_press(egui::Key::Enter);
        h.run_steps(3);
        h.key_press(egui::Key::Num2);
        h.run_steps(3);
        shot(&mut h, &format!("i4g-ghost-expanded-{tag}"));
        if dark {
            h.key_press(egui::Key::Escape);
            h.state_mut().state.home.selected = Some("repo:cart\nDecision:d2".into());
            h.run_steps(2);
            h.key_press(egui::Key::Enter);
            h.run_steps(3);
            h.state_mut().state.home.brief_choice = Some(("d2".into(), "add the label, then re-run".into()));
            h.run_steps(3);
            shot(&mut h, "i4g-engine-brief-dark-1600");
            h.key_press(egui::Key::Escape);
            h.state_mut().state.home.selected = Some("repo:tax".into());
            h.run_steps(2);
            h.key_press(egui::Key::Enter);
            h.run_steps(3);
            h.get_by_label("Answer here").click();
            h.run_steps(4);
            shot(&mut h, "i4g-answer-here-dark-1600");
            h.key_press(egui::Key::Escape);
            h.key_press(egui::Key::Escape);
            h.state_mut().state.home.selected = Some("repo:pdf".into());
            h.run_steps(2);
            h.key_press(egui::Key::Enter);
            h.run_steps(3);
            shot(&mut h, "i4g-skipped-dark-1600");
        }
    }
}
