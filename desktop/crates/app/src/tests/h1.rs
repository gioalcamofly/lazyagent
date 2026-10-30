//! H1: a Home item opens on a click anywhere on its surface (its padding,
//! its state column, a second line, the gap between its lines); a button
//! inside it does only its own thing.

use super::*;

const KEY: &str = "repo:clicky";
const CI: &str = "Ci:/r/repo-clicky:7";

fn caps() -> Vec<&'static str> {
    I4E.iter().copied().chain(["rules", "actions", "item_capabilities", "delegate_all"]).collect()
}

/// A Round line with CI red, delegable, and a suggestion as its second line.
fn home() -> (Harness<'static, App>, Calls) {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        "ci_log_summary" => Some(json!({"jobs": [], "text": ""})),
        _ => None,
    });
    let item = json!({"id": CI, "occurrence": "c@1", "kind": "ci", "level": "round", "title": "",
                      "label": "#7 Feat", "detail": "", "state": "CI red: tests", "work_item": KEY,
                      "worktree_path": "/r/repo-clicky", "agent_id": null,
                      "pr": {"number": 7, "title": "Feat", "url": "https://example.com/pull/7"},
                      "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
                      "since": crate::app::unix_now() - 600.0, "park": null, "run": null, "class": "quick",
                      "delegable": true, "passable": true});
    let mut row = status_row("clicky", "inbox", Some("round"), "you", "CI red: tests", 600.0);
    row["needs_you"] = json!({"count": 1, "top": CI, "items": [{"id": CI, "kind": "ci", "level": "round",
                                                                 "state": "CI red: tests", "since": item["since"]}]});
    row["agents"] = json!([{"id": "a1", "status": "waiting", "since": crate::app::unix_now() - 600.0}]);
    row["urls"] = json!(["https://example.com/pull/7"]);
    (row["rule_id"], row["verb"], row["action"]) = (json!("queue"), json!("Fix CI"), json!("delegate"));
    (row["delegable"], row["passable"], row["delegates"]) = (json!(true), json!(true), json!([CI]));
    row["suggestion"] = json!("rerun the tests");
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    (h, calls)
}

fn rect(h: &Harness<'static, App>, label: &str) -> egui::Rect {
    h.get_by_label_contains(label).rect()
}

fn click_at(h: &mut Harness<'static, App>, pos: egui::Pos2) {
    h.event(egui::Event::PointerMoved(pos));
    h.run_steps(1);
    for pressed in [true, false] {
        h.event(egui::Event::PointerButton {
            pos,
            button: egui::PointerButton::Primary,
            pressed,
            modifiers: Default::default(),
        });
    }
    h.run_steps(3);
}

fn opened(h: &Harness<'static, App>) -> (Option<String>, Option<String>) {
    let home = &h.state().state.home;
    (home.selected.clone(), home.expanded.clone())
}

#[test]
fn a_click_anywhere_on_an_item_opens_it() {
    let (mut h, _) = home();
    let verb = rect(&h, "Fix CI");
    let state = h.get_by_label("· tests").rect();
    let age = rect(&h, "10m");
    let second = rect(&h, "suggestion: rerun the tests");
    let points = [
        ("left padding", egui::pos2(verb.left() - 24.0, verb.center().y)),
        ("right edge", egui::pos2(age.right() + 4.0, age.center().y)),
        ("state column", state.center()),
        ("second line", second.center()),
        ("gap between lines", egui::pos2(verb.center().x, (verb.bottom() + second.top()) / 2.0)),
        ("bottom padding", egui::pos2(verb.center().x, second.bottom() + 2.0)),
    ];
    for (what, pos) in points {
        h.state_mut().state.home.selected = None;
        h.state_mut().state.home.expanded = None;
        h.run_steps(2);
        click_at(&mut h, pos);
        assert_eq!(opened(&h), (Some(KEY.into()), Some(KEY.into())), "a click on its {what} at {pos:?}");
    }
    // A second click folds it, as the title's click always did.
    click_at(&mut h, state.center());
    assert_eq!(opened(&h), (Some(KEY.into()), None));
}

#[test]
fn the_surface_is_highlighted_and_shows_a_pointer_on_hover() {
    let (mut h, _) = home();
    let second = rect(&h, "suggestion: rerun the tests");
    h.event(egui::Event::PointerMoved(second.center()));
    h.run_steps(2);
    assert_eq!(h.output().platform_output.cursor_icon, egui::CursorIcon::PointingHand);
}

#[test]
fn a_button_inside_an_expanded_item_does_only_its_own_thing() {
    let (mut h, calls) = home();
    let verb = rect(&h, "Fix CI");
    click_at(&mut h, verb.center());
    assert_eq!(opened(&h), (Some(KEY.into()), Some(KEY.into())));
    h.get_by_label("Delegate to its agent  (d)").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "status_gesture"), [json!({"key": KEY, "kind": "delegate", "to": "agent"})]);
    assert_eq!(opened(&h).1.as_deref(), Some(KEY), "the button did not fold the item");
    // Empty space in the details stays there.
    let bar = rect(&h, "More…");
    click_at(&mut h, egui::pos2(bar.right() + 40.0, bar.center().y));
    assert_eq!(opened(&h).1.as_deref(), Some(KEY), "a click in the details does not fold the item");
    h.get_by_label("More…").click();
    h.run_steps(2);
    h.get_by_label("Why?  (w)");
    assert_eq!(opened(&h).1.as_deref(), Some(KEY), "More… did not fold the item");
}
