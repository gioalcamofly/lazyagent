//! Home fixes from the second UX review (proposal 0013, I4f): a deep item
//! under a quicker line, stale rows folded with their dirty files, a CI red
//! with no Actions log, delegating with no live agent, the Why? answer,
//! parked rows that say until when, Merge's own button first, a detached
//! head by its directory, starting the orchestrator, and the screenshots.

use super::*;

/// I4e's features, with CI tracking and the rules (R2).
fn caps() -> Vec<&'static str> {
    I4E.iter().copied().chain(["rules", "actions"]).collect()
}

fn home() -> (Harness<'static, App>, mpsc::Sender<Msg>) {
    let caps = caps();
    let (app, tx) = engine_home(&caps);
    (harness(app), tx)
}

/// An inbox item on work item `key` (`repo:<key>`).
fn item(key: &str, kind: &str, level: &str, state: &str, ago: f64) -> serde_json::Value {
    let id = format!("{}:/r/repo-{key}:7", kind[..1].to_uppercase());
    json!({"id": id, "occurrence": format!("{id}@1"), "kind": kind, "level": level, "title": kind,
           "label": "#7 Feat", "detail": "", "state": state, "work_item": format!("repo:{key}"),
           "worktree_path": format!("/r/repo-{key}"), "agent_id": null,
           "pr": {"number": 7, "title": "Feat", "url": "https://example.com/pull/7"},
           "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
           "since": crate::app::unix_now() - ago, "park": null, "run": null})
}

fn needs(items: &[&serde_json::Value]) -> serde_json::Value {
    json!({"count": items.len(), "top": items[0]["id"],
           "items": items.iter().map(|i| json!({"id": i["id"], "kind": i["kind"], "level": i["level"],
                                                  "state": i["state"], "since": i["since"]})).collect::<Vec<_>>()})
}

/// A Round line whose CI is red, with a changes requested under it.
fn red_with_review(tx: &mpsc::Sender<Msg>) {
    let ci = item("tax", "ci", "round", "CI red: e2e-suite", 600.0);
    let review = item("tax", "review", "deep", "changes requested", 3600.0);
    let mut row = status_row("tax", "inbox", Some("round"), "you", "CI red: e2e-suite", 600.0);
    row["rule"] = json!(5);
    row["needs_you"] = needs(&[&ci, &review]);
    row["urls"] = json!(["https://example.com/pull/7"]);
    tx.send(event("actionable.created", json!({"item": ci}))).unwrap();
    tx.send(event("actionable.created", json!({"item": review}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
}

fn settle(h: &mut Harness<'static, App>) {
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
}

#[test]
fn a_deep_item_under_a_round_line_shows_in_to_sit_down_with_and_names_itself() {
    let (mut h, tx) = home();
    let calls = fake_engine(&h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    red_with_review(&tx);
    settle(&mut h);
    let home = &h.state().state.home;
    assert_eq!(home.shown_in(crate::home::Zone::Round).len(), 1);
    assert_eq!(home.deep_count(), 1, "the engine counts it in deep: so does Home");
    // "+ changes requested", not "2 items".
    h.get_by_label("+ changes requested");
    assert!(h.query_by_label("2 items").is_none());
    // The ghost line in To sit down with: its verb, its state, its place.
    h.get_by_label("Address review");
    h.get_by_label("· changes requested");
    h.get_by_label("under its Fix CI in the Round");
    // A click expands the ghost itself (I4g), which lists every item.
    h.get_by_label("under its Fix CI in the Round").click();
    h.run_steps(3);
    assert_eq!(h.state().state.home.expanded.as_deref(), Some("repo:tax\nR:/r/repo-tax:7"));
    h.get_by_label("Its items");
    h.get_by_label_contains("1. Fix CI · CI red: e2e-suite · leads the line");
    h.get_by_label_contains("2. Address review · changes requested · this line");
    assert_eq!(calls_of(&calls, "status_timeline").len(), 1);
}

#[test]
fn stale_rows_fold_at_the_end_of_in_flight_with_their_dirty_files() {
    let (mut h, tx) = home();
    fake_engine(&h, |_, _| None);
    let mut old = status_row("old-experiment", "in_flight", None, "stalled", "no activity", 40.0 * 86400.0);
    (old["stale"], old["dirty"], old["rule"]) = (json!(true), json!(13), json!(11));
    let mut clean = status_row("mods", "in_flight", None, "stalled", "no activity", 30.0 * 86400.0);
    (clean["stale"], clean["dirty"], clean["rule"]) = (json!(true), json!(0), json!(11));
    let live = status_row("cart", "in_flight", None, "agent", "working", 60.0);
    for r in [old, clean, live] {
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    settle(&mut h);
    let names = home_names(&h, crate::home::Zone::Flight);
    assert_eq!(names, ["cart"], "stale rows are folded, never in the list");
    h.get_by_label("2 stale · 13 uncommitted files ▸").click();
    h.run_steps(3);
    h.get_by_label("no activity · 13 uncommitted files");
    h.get_by_label("no activity · no uncommitted files");
    let visible: Vec<String> = h.state().state.home.visible(false).iter().map(|l| l.name.clone()).collect();
    assert_eq!(visible, ["cart", "mods", "old-experiment"], "at the very end, newest first");
}

#[test]
fn a_ci_red_with_no_actions_run_says_whose_check_it_is() {
    let (mut h, tx) = home();
    let calls = fake_engine(&h, |m, p| match m {
        "ci_log_summary" => Some(json!({"key": p["key"], "runs": [], "jobs": [], "text": "",
            "checks": [{"name": "e2e-suite", "provider": "CircleCI", "url": "https://app.circleci.com/p/1"}],
            "error": "no Actions log: check e2e-suite from CircleCI"})),
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    red_with_review(&tx);
    settle(&mut h);
    h.key_press(egui::Key::Enter);
    h.run_steps(4);
    assert_eq!(calls_of(&calls, "ci_log_summary"), [json!({"key": "repo:tax"})], "asked once, with no run");
    h.get_by_label("Failed log");
    h.get_by_label("no Actions log: check e2e-suite from CircleCI");
    h.get_by_label("e2e-suite · CircleCI");
    h.get_by_label("open the check");
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "ci_log_summary").len(), 1, "once per head, not per frame");
}

#[test]
fn delegating_with_no_live_agent_restores_one_or_starts_one_first() {
    let (mut h, tx) = home();
    let calls = fake_engine(&h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        "spawn_agent" => Some(json!({"agent_id": "a1", "worktree_path": "/r/repo-tax", "session_name": "s"})),
        _ => None,
    });
    red_with_review(&tx);
    settle(&mut h);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    assert!(h.query_by_label("Delegate to its agent  (d)").is_none(), "no agent runs there");
    // d says how, never promises what cannot happen.
    h.key_press(egui::Key::D);
    h.run_steps(2);
    assert!(calls_of(&calls, "status_gesture").is_empty());
    h.get_by_label("Spawn an agent for this").click();
    h.run_steps(4);
    let spawn = calls_of(&calls, "spawn_agent");
    assert_eq!(spawn.len(), 1);
    assert_eq!(spawn[0]["worktree_path"], "/r/repo-tax");
    assert_eq!(calls_of(&calls, "status_gesture")[0]["kind"], "delegate");
    // A restorable agent there: restore it, then delegate.
    h.state_mut().state.restorable = vec![
        serde_json::from_value(json!({"id": "e1", "worktree_path": "/r/repo-tax", "agent_id": "a1", "kind": "agent",
            "provider": "claude", "label": "", "session_name": "s", "skip_permissions": false,
            "conversation_id": null, "instruction": null, "started_at": 1.0, "engine_id": null, "ended_at": 2.0}))
        .unwrap(),
    ];
    h.run_steps(2);
    h.get_by_label("Restore agent and delegate").click();
    h.run_steps(4);
    assert_eq!(calls_of(&calls, "restore_agents")[0]["ids"], json!(["e1"]));
    assert_eq!(calls_of(&calls, "status_gesture").len(), 2);
}

#[test]
fn why_answers_in_one_sentence_and_folds_the_rules_that_did_not_match() {
    let (mut h, tx) = home();
    let calls = fake_engine(&h, |m, p| match m {
        "rules_explain" => Some(json!({"key": p["key"],
            "rule": {"id": "no_activity", "num": 11, "origin": "builtin", "file": null, "line": null, "locked": false,
                     "says": "", "when": "item.idle > config.stalled_after", "and_when": null, "verb": null,
                     "action": null, "item": false, "replaces": null},
            "then": {"ball": "stalled", "level": null, "state": "no activity", "verb": null, "action": null, "item": false},
            "facts": [{"name": "item.idle", "value": 197132.43, "unknown": false, "source": "projection",
                       "freshness": "per pass", "shown": "2 d 6 h"},
                      {"name": "config.stalled_after", "value": 86400, "unknown": false, "source": "config",
                       "freshness": "per pass", "shown": "24 h"}],
            "above": [{"id": "merge", "origin": "builtin", "failed": "pr.state == \"open\"", "value": "false",
                       "said": "The PR's state is open"},
                      {"id": "tracker_qa", "origin": "builtin", "failed": "tracker.qa == \"tested\"", "value": "unknown",
                       "said": "The task's QA is tested"}],
            "items": [],
            "answer": "“no activity”: the time since the last sign of work (2 d 6 h) is over the stalled threshold (24 h)."})),
        _ => None,
    });
    tx.send(event(
        "status.updated",
        json!({"row": status_row("docs", "in_flight", None, "stalled", "no activity", 2.0 * 86400.0)}),
    ))
    .unwrap();
    settle(&mut h);
    h.key_press(egui::Key::W);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "rules_explain").len(), 1);
    h.get_by_label(
        "“no activity”: the time since the last sign of work (2 d 6 h) is over the stalled threshold (24 h).",
    );
    h.get_by_label("2 d 6 h");
    h.get_by_label("24 h");
    assert!(h.query_by_label_contains("197132").is_none(), "never raw seconds");
    // Folded, the unknown ones counted, then first once open.
    let header = h.get_by_label_contains("Rules above no_activity that did not match · 2 (1 unknown)");
    assert!(h.query_by_label("The PR's state is open").is_none());
    header.click();
    h.run_steps(2);
    let unknown = h.get_by_label("The task's QA is tested").rect().min.y;
    let falsey = h.get_by_label("The PR's state is open").rect().min.y;
    assert!(unknown < falsey, "the unknown rule comes first");
}

#[test]
fn a_merge_lines_own_button_comes_first_and_a_parked_line_says_until_and_what_needs_you() {
    let (mut h, tx) = home();
    fake_engine(&h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        _ => None,
    });
    let mut merge = item("export", "rule", "round", "merge: approved, CI green", 20.0 * 86400.0);
    (merge["rule_id"], merge["verb"]) = (json!("merge"), json!("Merge"));
    let mut row = status_row("export", "inbox", Some("round"), "you", "merge: approved, CI green", 60.0);
    row["needs_you"] = needs(&[&merge]);
    row["urls"] = json!(["https://example.com/pull/7"]);
    let conflict = item("coupons", "conflict", "round", "conflict with base", 3.0 * 3600.0);
    let mut parked = status_row("coupons", "parked", None, "you", "parked indefinitely", 3.0 * 3600.0);
    (parked["rule"], parked["holder"]) = (json!(7), json!("user"));
    parked["gesture"] = json!({"kind": "park", "at": crate::app::unix_now() - 3.0 * 3600.0, "by": "user",
                               "until": "indefinite"});
    parked["needs_you"] = needs(&[&conflict]);
    for i in [&merge, &conflict] {
        tx.send(event("actionable.created", json!({"item": i}))).unwrap();
    }
    for r in [row, parked] {
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    settle(&mut h);
    // Its age is the item's: approved 20 days ago.
    h.get_by_label("20d");
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    let first = h.get_by_label("Open PR to merge  (p)").rect();
    let terminal = h.get_by_label("Open terminal  (o)").rect();
    assert!(first.min.x < terminal.min.x && (first.min.y - terminal.min.y).abs() < 4.0, "first in its row");
    assert!(h.query_by_label("PR  (p)").is_none(), "one button for the PR");
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::Z);
    h.run_steps(3);
    h.get_by_label("parked indefinitely · needs you: conflict with base");
    h.get_by_label("3h");
}

#[test]
fn a_detached_head_reads_as_its_directory() {
    let (mut h, tx) = home();
    fake_engine(&h, |_, _| None);
    let mut r = status_row("x", "in_flight", None, "stalled", "no activity", 2.0 * 86400.0);
    (r["key"], r["branch"], r["worktree_path"]) =
        (json!("repo:@6bf0c804d69b"), json!(null), json!("/r/repo-hotfix-premerge-6bf0c80"));
    tx.send(event("status.updated", json!({"row": r}))).unwrap();
    settle(&mut h);
    assert_eq!(home_names(&h, crate::home::Zone::Flight), ["repo-hotfix-premerge-6bf0c80"]);
}

#[test]
fn the_orchestrator_not_running_has_a_start_button() {
    let (mut h, _tx) = home();
    let calls = fake_engine(&h, |_, _| None);
    h.state_mut().state.orchestrator =
        Some(serde_json::from_value(json!({"alive": false, "report": null, "queue": []})).unwrap());
    h.run_steps(2);
    h.get_by_label("not running");
    h.get_by_label("Start the orchestrator").click();
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "spawn_agent")[0]["kind"], "orchestrator");
}

/// docs/design/desktop-screenshots/i4f-*.png: Home with a deep item under
/// a red CI (its ghost line), a 20-day Merge, the approval's keys in the
/// bar, a working line with its report, a detached head, the stale fold
/// open and a parked line that says until when; then the expanded CI line
/// with no Actions log, the deny's Undo, and the Why? answer.
#[test]
fn home_i4f_screenshots() {
    let now = crate::app::unix_now();
    let caps = caps();
    let out = std::env::temp_dir();
    for (width, tag, dark) in [(1600.0, "dark-1600", true), (1100.0, "light-1100", false)] {
        let (mut app, tx) = engine_home(&caps);
        app.state.orchestrator =
            Some(serde_json::from_value(json!({"alive": false, "report": null, "queue": []})).unwrap());
        app.state.look_from = now;
        let mut h = Harness::builder().with_size([width, 1000.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app);
        h.run_steps(2);
        if dark != (h.state().state.theme.kind == ThemeKind::Dark) {
            let ctx = h.ctx.clone();
            h.state_mut().state.toggle_theme(&ctx);
            h.run_steps(2);
        }
        fake_engine(&h, |m, p| match m {
            "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
            "ci_log_summary" => Some(json!({"key": p["key"], "runs": [], "jobs": [], "text": "",
                "checks": [{"name": "e2e-suite", "provider": "CircleCI", "url": "https://app.circleci.com/p/1"}],
                "error": "no Actions log: check e2e-suite from CircleCI"})),
            "rules_explain" => Some(json!({"key": p["key"],
                "rule": {"id": "queue", "num": 6, "origin": "builtin", "file": null, "line": null, "locked": true,
                         "says": "The work item's open inbox items, top first.", "when": "items.count > 0",
                         "and_when": null, "verb": null, "action": null, "item": false, "replaces": null},
                "then": {"ball": "you", "level": "round", "state": "CI red: e2e-suite", "verb": null, "action": null,
                         "item": false},
                "facts": [{"name": "items.count", "value": 2, "unknown": false, "source": "ActionableQueue",
                           "freshness": "per pass", "shown": "2"}],
                "above": [{"id": "merged", "origin": "builtin", "failed": "pr.state == \"merged\"", "value": "false",
                           "said": "The PR's state is merged"},
                          {"id": "tracker_done", "origin": "builtin", "failed": "tracker.status == \"done\"",
                           "value": "unknown", "said": "The task's status is done"}],
                "items": [{"id": "C:/r/repo-tax:7", "kind": "ci", "level": "round", "state": "CI red: e2e-suite"},
                          {"id": "R:/r/repo-tax:7", "kind": "review", "level": "deep", "state": "changes requested"}],
                "answer": "“CI red: e2e-suite” leads: it is the first of the open inbox items; also open: changes requested."})),
            _ => None,
        });
        red_with_review(&tx);
        let approval = ask_item("pdf", "approval", "now", "rm -rf build/ && npm run build -- --prod");
        let mut approve = status_row("pdf", "inbox", Some("now"), "you", "approve: rm -rf build/", 120.0);
        approve["needs_you"] = json!({"count": 1, "top": approval["id"]});
        approve["agents"] = json!([{"id": "a1", "status": "waiting_for_approval", "since": now - 120.0}]);
        let mut merge = item("export", "rule", "round", "merge: approved, CI green", 20.0 * 86400.0);
        (merge["rule_id"], merge["verb"]) = (json!("merge"), json!("Merge"));
        let mut merge_row = status_row("export", "inbox", Some("round"), "you", "merge: approved, CI green", 60.0);
        merge_row["needs_you"] = needs(&[&merge]);
        merge_row["urls"] = json!(["https://example.com/pull/7"]);
        let mut working = status_row("stock-sync", "in_flight", None, "agent", "working", 720.0);
        working["agents"] = json!([{"id": "a4", "status": "running", "since": now - 720.0}]);
        working["note"] = json!({"text": "tests pass; opening the PR", "by": "agent:a4", "at": now - 60.0});
        let mut detached = status_row("x", "in_flight", None, "stalled", "no activity", 2.0 * 86400.0);
        (detached["key"], detached["branch"], detached["worktree_path"], detached["rule"]) =
            (json!("repo:@6bf0c804d69b"), json!(null), json!("/r/repo-hotfix-premerge-6bf0c80"), json!(11));
        let mut stale = status_row("old-experiment", "in_flight", None, "stalled", "no activity", 40.0 * 86400.0);
        (stale["stale"], stale["dirty"], stale["rule"]) = (json!(true), json!(13), json!(11));
        let mut stale2 = status_row("old-mods", "in_flight", None, "stalled", "no activity", 30.0 * 86400.0);
        (stale2["stale"], stale2["dirty"], stale2["rule"]) = (json!(true), json!(0), json!(11));
        let conflict = item("coupons", "conflict", "round", "conflict with base", 3.0 * 3600.0);
        let mut parked = status_row("coupons", "parked", None, "you", "parked indefinitely", 3.0 * 3600.0);
        (parked["rule"], parked["holder"]) = (json!(7), json!("user"));
        parked["gesture"] = json!({"kind": "park", "at": now - 3.0 * 3600.0, "by": "user", "until": "indefinite"});
        parked["needs_you"] = needs(&[&conflict]);
        for i in [&approval, &merge, &conflict] {
            tx.send(event("actionable.created", json!({"item": i}))).unwrap();
        }
        for r in [approve, merge_row, working, detached, stale, stale2, parked] {
            tx.send(event("status.updated", json!({"row": r}))).unwrap();
        }
        settle(&mut h);
        {
            let s = &mut h.state_mut().state;
            s.home.stale_open = true;
            s.home.parked_open = true;
            s.home.selected = Some("repo:pdf".into());
        }
        h.run_steps(4);
        h.get_by_label("+ changes requested");
        h.get_by_label_contains("a approve · ⇧D deny");
        let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
            Ok(image) => {
                image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
                true
            }
            Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
            Err(_) => false,
        };
        if !shot(&mut h, &format!("i4f-home-{tag}")) {
            return;
        }
        // The CI line expanded: its items, whose red it is, no false promise.
        h.state_mut().state.home.selected = Some("repo:tax".into());
        h.run_steps(1);
        h.key_press(egui::Key::Enter);
        h.run_steps(5);
        h.get_by_label("no Actions log: check e2e-suite from CircleCI");
        h.get_by_label("Spawn an agent for this");
        shot(&mut h, &format!("i4f-ci-expanded-{tag}"));
        if dark {
            // The deny waits for its Undo.
            h.key_press(egui::Key::Escape);
            h.state_mut().state.home.selected = Some("repo:pdf".into());
            h.run_steps(2);
            h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::D);
            h.run_steps(3);
            h.get_by_label_contains("Denying pdf");
            shot(&mut h, "i4f-deny-undo-dark-1600");
            h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Z);
            h.run_steps(2);
            // Why?, for the user.
            h.state_mut().state.home.selected = Some("repo:tax".into());
            h.run_steps(1);
            h.key_press(egui::Key::W);
            h.run_steps(2);
            h.state_mut().state.toasts.clear();
            h.run_steps(2);
            h.get_by_label_contains("“CI red: e2e-suite” leads");
            shot(&mut h, "i4f-why-dark-1600");
        }
    }
}
