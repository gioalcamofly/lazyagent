//! Review requests in Home: a teammate's PR that asks the user for a
//! review is a Round line with no worktree (author, title, `p` opens the
//! PR), its gestures work, it is never delegated, and the screenshots.

use super::*;

/// I4e's features, with the rules (R2) and review requests.
fn caps() -> Vec<&'static str> {
    I4E.iter().copied().chain(["rules", "actions", "review_requests"]).collect()
}

const ITEM: &str = "Review_request:#12:1800000000";
const URL: &str = "https://github.com/o/r/pull/12";

fn request_item(ago: f64) -> serde_json::Value {
    json!({"id": ITEM, "occurrence": format!("{ITEM}@1"), "kind": "review_request", "level": "round",
           "title": "Review requested", "label": "#12 Fix the CSV export", "detail": "ana asked you for a review",
           "state": "review requested by ana", "work_item": "repo:ana-csv-export", "worktree_path": null,
           "agent_id": null, "pr": {"number": 12, "title": "Fix the CSV export", "url": URL},
           "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
           "since": crate::app::unix_now() - ago, "park": null, "run": null})
}

/// The engine's row for it: no worktree, the request, the PR's number.
fn request_row(item: &serde_json::Value, ago: f64) -> serde_json::Value {
    let mut row = status_row("ana-csv-export", "inbox", Some("round"), "you", "review requested by ana", ago);
    row["worktree_path"] = json!(null);
    row["title"] = json!("Fix the CSV export");
    row["rule"] = json!(2);
    row["urls"] = json!([URL]);
    row["pr"] = json!({"number": 12, "state": "OPEN", "review": "REVIEW_REQUIRED", "ci": "none", "mergeable": ""});
    row["needs_you"] = json!({"count": 1, "top": item["id"],
        "items": [{"id": item["id"], "kind": "review_request", "level": "round",
                   "state": "review requested by ana", "since": item["since"]}]});
    row["review_request"] = json!({"number": 12, "title": "Fix the CSV export", "url": URL, "author": "ana",
                                   "requested_at": crate::app::unix_now() - ago, "team": null});
    row
}

fn settle(h: &mut Harness<'static, App>) {
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
}

fn engine(h: &Harness<'static, App>) -> Calls {
    fake_engine(h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        "actionables_mark" => Some(json!({"item": request_item(3.0 * 3600.0)})),
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        _ => None,
    })
}

#[test]
fn a_review_request_is_a_round_line_with_its_author_and_title_and_p_opens_the_pr() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h);
    let item = request_item(3.0 * 3600.0);
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": request_row(&item, 3.0 * 3600.0)}))).unwrap();
    settle(&mut h);
    assert_eq!(home_names(&h, crate::home::Zone::Round), ["ana-csv-export #12"]);
    let line = h.state().state.home.lines.get("repo:ana-csv-export").cloned().unwrap();
    assert_eq!(line.verb(), "Review");
    assert_eq!(line.state, "review requested by ana · Fix the CSV export");
    assert!(line.place.is_none(), "no worktree to open");
    h.get_by_label("Review");
    h.get_by_label_contains("review requested by ana · Fix the CSV export");
    // The key bar: the PR first, no terminal, no delegation.
    let bar = crate::home_view::line_keys(
        &crate::home_view::KeysCtx { delegation: true, gestures: true, ..Default::default() },
        Some(&line),
    );
    assert!(bar.starts_with("p PR · Enter expand"), "{bar}");
    assert!(!bar.contains("terminal") && !bar.contains("delegate"), "{bar}");
    // p opens the PR.
    h.key_press(egui::Key::P);
    h.run_steps(1);
    assert!(h.state().state.url_to_open.is_none(), "taken by the frame, opened");
    // o has nothing to open; d delegates nothing.
    h.key_press(egui::Key::O);
    h.run_steps(2);
    assert!(h.state().state.toasts.iter().any(|t| t.text.contains("no worktree")));
    h.key_press(egui::Key::D);
    h.run_steps(2);
    assert!(calls_of(&calls, "status_gesture").is_empty());
    assert!(h.state().state.toasts.iter().any(|t| t.text.contains("review it there")));
    assert!(h.state().state.home.expanded.is_none());
    // Expanded: who asked, when, and the PR's own button first.
    h.key_press(egui::Key::Enter);
    h.run_steps(4);
    h.get_by_label("Open PR to review  (p)");
    h.get_by_label("#12 Fix the CSV export · ana asked you for a review");
    h.get_by_label("review requested 3h ago · it goes once you review it on the PR");
    assert!(h.query_by_label("Open terminal  (o)").is_none());
    assert!(h.query_by_label("Delegate to its agent  (d)").is_none());
    assert!(h.query_by_label("Spawn an agent for this").is_none());
    assert!(h.query_by_label("PR  (p)").is_none(), "one PR button");
}

#[test]
fn its_gestures_work_dismiss_park_and_label() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h);
    let item = request_item(600.0);
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": request_row(&item, 600.0)}))).unwrap();
    settle(&mut h);
    // z: park the item until tomorrow.
    h.key_press(egui::Key::Z);
    h.run_steps(2);
    h.get_by_label("Park this item until");
    h.key_press(egui::Key::Num2);
    h.run_steps(2);
    assert_eq!(calls_of(&calls, "actionables_mark"), [json!({"id": ITEM, "action": "park", "until": "tomorrow"})]);
    // x: dismiss this occurrence.
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    settle(&mut h);
    h.key_press(egui::Key::X);
    h.run_steps(2);
    let marks = calls_of(&calls, "actionables_mark");
    assert_eq!(marks.len(), 2);
    assert_eq!(marks[1]["action"], json!("dismiss"));
    assert_eq!(marks[1]["id"], json!(ITEM));
    // Set aside: the row stays, in flight, and l labels it.
    let mut aside = status_row("ana-csv-export", "in_flight", None, "stalled", "review requested · set aside", 600.0);
    (aside["worktree_path"], aside["rule"], aside["urls"]) = (json!(null), json!(10), json!([URL]));
    tx.send(event(
        "actionable.resolved",
        json!({"id": ITEM, "occurrence": format!("{ITEM}@1"), "reason": "dismissed"}),
    ))
    .unwrap();
    tx.send(event("status.updated", json!({"row": aside}))).unwrap();
    settle(&mut h);
    assert_eq!(home_names(&h, crate::home::Zone::Flight), ["ana-csv-export"]);
    h.get_by_label_contains("review requested · set aside");
    h.state_mut().state.home.selected = Some("repo:ana-csv-export".into());
    h.run_steps(1);
    h.key_press(egui::Key::L);
    h.run_steps(2);
    h.get_by_label("Ball with");
    h.event(egui::Event::Text("after lunch".into()));
    h.run_steps(1);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
    assert_eq!(
        calls_of(&calls, "status_gesture"),
        [json!({"key": "repo:ana-csv-export", "kind": "label", "ball": "others", "text": "after lunch"})]
    );
}

/// docs/design/desktop-screenshots/review-requests-*.png: Home with a
/// review request in the Round next to the user's own lines, and the line
/// expanded (offscreen; skipped without a GPU unless LAZYAGENT_REQUIRE_GPU).
#[test]
fn home_review_requests_screenshots() {
    let out = std::env::temp_dir();
    for (width, tag, dark) in [(1600.0, "dark-1600", true), (1100.0, "light-1100", false)] {
        let (mut app, tx) = engine_home(&caps());
        app.state.orchestrator =
            Some(serde_json::from_value(json!({"alive": false, "report": null, "queue": []})).unwrap());
        app.state.look_from = crate::app::unix_now();
        let mut h = Harness::builder().with_size([width, 800.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app);
        h.run_steps(2);
        if dark != (h.state().state.theme.kind == ThemeKind::Dark) {
            let ctx = h.ctx.clone();
            h.state_mut().state.toggle_theme(&ctx);
            h.run_steps(2);
        }
        engine(&h);
        let item = request_item(3.0 * 3600.0);
        let mut team_item = request_item(26.0 * 3600.0);
        let team_id = "Review_request:#15:1799900000";
        (team_item["id"], team_item["occurrence"], team_item["work_item"]) =
            (json!(team_id), json!(format!("{team_id}@1")), json!("repo:bo-retry-webhooks"));
        (team_item["state"], team_item["label"], team_item["detail"]) = (
            json!("review requested by bo"),
            json!("#15 Retry failed webhooks"),
            json!("bo asked team platform for a review"),
        );
        team_item["pr"] =
            json!({"number": 15, "title": "Retry failed webhooks", "url": "https://github.com/o/r/pull/15"});
        let mut team_row = request_row(&team_item, 26.0 * 3600.0);
        (team_row["key"], team_row["branch"], team_row["title"], team_row["state"]) = (
            json!("repo:bo-retry-webhooks"),
            json!("bo-retry-webhooks"),
            json!("Retry failed webhooks"),
            json!("review requested by bo"),
        );
        team_row["urls"] = json!(["https://github.com/o/r/pull/15"]);
        team_row["pr"]["number"] = json!(15);
        team_row["review_request"] = json!({"number": 15, "title": "Retry failed webhooks",
            "url": "https://github.com/o/r/pull/15", "author": "bo",
            "requested_at": crate::app::unix_now() - 26.0 * 3600.0, "team": "platform"});
        team_row["priority"] = json!("high");
        let mut merge = item_rule("export", 2.0 * 3600.0);
        merge["work_item"] = json!("repo:export");
        let mut merge_row = status_row("export", "inbox", Some("round"), "you", "merge: approved, CI green", 7200.0);
        merge_row["needs_you"] = json!({"count": 1, "top": merge["id"]});
        merge_row["urls"] = json!(["https://github.com/o/r/pull/7"]);
        let mut working = status_row("stock-sync", "in_flight", None, "agent", "working", 720.0);
        working["agents"] = json!([{"id": "a4", "status": "running", "since": crate::app::unix_now() - 720.0}]);
        let mut theirs = status_row("cy-dark-mode", "in_flight", None, "others", "cy's PR · approved", 5.0 * 3600.0);
        theirs["rule"] = json!(10);
        for i in [&item, &team_item, &merge] {
            tx.send(event("actionable.created", json!({"item": i}))).unwrap();
        }
        for r in [request_row(&item, 3.0 * 3600.0), team_row, merge_row, working, theirs] {
            tx.send(event("status.updated", json!({"row": r}))).unwrap();
        }
        settle(&mut h);
        h.state_mut().state.home.selected = Some("repo:ana-csv-export".into());
        h.run_steps(3);
        let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
            Ok(image) => {
                image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
                true
            }
            Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
            Err(_) => false,
        };
        if !shot(&mut h, &format!("review-requests-home-{tag}")) {
            return;
        }
        h.key_press(egui::Key::Enter);
        h.run_steps(5);
        h.get_by_label("Open PR to review  (p)");
        shot(&mut h, &format!("review-requests-expanded-{tag}"));
    }
}

/// The built-in Merge's item on `key`.
fn item_rule(key: &str, ago: f64) -> serde_json::Value {
    let id = format!("Rule:merge:repo:{key}");
    json!({"id": id, "occurrence": format!("{id}@1"), "kind": "rule", "level": "round", "title": "Merge",
           "label": "#7 Export", "detail": "", "state": "merge: approved, CI green", "work_item": format!("repo:{key}"),
           "worktree_path": format!("/r/repo-{key}"), "agent_id": null,
           "pr": {"number": 7, "title": "Export", "url": "https://github.com/o/r/pull/7"},
           "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
           "since": crate::app::unix_now() - ago, "park": null, "run": null, "rule_id": "merge", "verb": "Merge"})
}
