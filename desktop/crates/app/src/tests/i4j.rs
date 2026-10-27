//! I4j: the opt-in row fields survive a row update (the live sequence:
//! status_list with fields, then status.updated without them, lost Approve
//! and "Delegate all"); a gesture's reply asks for them too; a refused
//! approval (the head moved) fetches the rows again; what to review on
//! "new commits since your approval" with the compare link; Delegate on a
//! row with no live agent restores or spawns it in the engine's one step.

use super::*;

const HEAD: &str = "3f2a9c1e5b7d40a8c6e2f1b0d9a8c7e6f5d4c3b2";
const NEXT: &str = "9b8a7c6d5e4f30211a2b3c4d5e6f708192a3b4c5";
const CI: &str = "Ci:/r/repo-feat:7";
const CONFLICT: &str = "Conflict:/r/repo-feat:7";

fn caps(new: bool) -> Vec<&'static str> {
    let mut caps: Vec<&str> =
        I4E.iter().copied().chain(["rules", "actions", "item_capabilities", "delegate_all", "owner_approve"]).collect();
    if new {
        caps.extend([crate::home_view::F_ROW_FIELDS, crate::home_view::F_DELEGATE_START]);
    }
    caps
}

fn settle(h: &mut Harness<'static, App>) {
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
}

fn item(key: &str, id: &str, kind: &str, state: &str, delegable: bool) -> serde_json::Value {
    json!({"id": id, "occurrence": format!("{id}@1"), "kind": kind, "level": "round", "title": "Review",
           "label": "#7 Feat", "detail": "", "state": state, "work_item": format!("repo:{key}"),
           "worktree_path": format!("/r/repo-{key}"), "agent_id": null,
           "pr": {"number": 7, "title": "Feat", "url": "https://github.com/o/r/pull/7"},
           "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
           "since": crate::app::unix_now() - 600.0, "park": null, "run": null, "class": "quick",
           "delegable": delegable, "passable": true})
}

/// A row with its items; `fields`: the opt-in ones status_list gives.
fn row(
    key: &str,
    items: &[serde_json::Value],
    agents: serde_json::Value,
    fields: serde_json::Value,
) -> serde_json::Value {
    let mut row = status_row(key, "inbox", Some("round"), "you", items[0]["state"].as_str().unwrap(), 600.0);
    row["needs_you"] = json!({"count": items.len(), "top": items[0]["id"], "items": items.iter().map(|i| json!({
        "id": i["id"], "kind": i["kind"], "level": "round", "state": i["state"], "since": i["since"]})).collect::<Vec<_>>()});
    row["agents"] = agents;
    row["urls"] = json!(["https://github.com/o/r/pull/7"]);
    for (k, v) in fields.as_object().unwrap() {
        row[k] = v.clone();
    }
    row
}

fn review_items() -> Vec<serde_json::Value> {
    let mut i = item("fix", "Rule:owner_review_again:repo:fix", "rule", "new commits since your approval", false);
    (i["rule_id"], i["verb"]) = (json!("owner_review_again"), json!("Review"));
    vec![i]
}

fn review_fields(sha: &str, review_commits: serde_json::Value) -> serde_json::Value {
    json!({"rule_id": "queue", "verb": "Review", "action": "open_pr", "delegable": false, "passable": false,
           "delegates": [], "approvable": {"sha": sha, "pr": 7, "approved": false}, "review_commits": review_commits})
}

fn feat_items() -> Vec<serde_json::Value> {
    vec![item("feat", CONFLICT, "conflict", "conflict with base", true), item("feat", CI, "ci", "CI red: tests", true)]
}

fn feat_fields() -> serde_json::Value {
    json!({"rule_id": "queue", "verb": "Resolve", "action": "delegate", "delegable": true, "passable": true,
           "delegates": [CONFLICT, CI], "approvable": null, "review_commits": null})
}

fn idle_agent() -> serde_json::Value {
    json!([{"id": "a1", "status": "waiting", "since": crate::app::unix_now() - 600.0}])
}

fn send_items(tx: &mpsc::Sender<Msg>, items: &[serde_json::Value]) {
    for i in items {
        tx.send(event("actionable.created", json!({"item": i}))).unwrap();
    }
}

fn list(rows: &[serde_json::Value]) -> serde_json::Value {
    json!({"rows": rows, "generated_at": 0.0,
           "counts": {"now": 0, "round": rows.len(), "in_flight": 0, "done": 0, "stale": 0, "not_started": 0}})
}

fn line(h: &Harness<'static, App>, key: &str) -> crate::home::Line {
    h.state().state.home.lines.get(&format!("repo:{key}")).cloned().unwrap()
}

fn expand(h: &mut Harness<'static, App>, key: &str) {
    h.state_mut().state.home.expanded = Some(format!("repo:{key}"));
    h.state_mut().state.home.selected = Some(format!("repo:{key}"));
    h.run_steps(4);
}

fn rc(since: &str, from: &str, count: u32, more: u32) -> serde_json::Value {
    json!({"since": since, "from": from, "head": NEXT, "count": count, "base_merges": 1,
           "commits": [
               {"sha": NEXT, "subject": "Retry the upload once", "author": "Ana", "date": "2026-10-27T09:12:00+00:00",
                "base_merge": null},
               {"sha": "7c6d5e4f30211a2b3c4d5e6f708192a3b4c59b8a", "subject": "Merge branch 'main' into fix",
                "author": "Ana", "date": "2026-10-27T09:00:00+00:00", "base_merge": "clean base merge"}],
           "more": more, "compare_url": format!("https://github.com/o/r/compare/{from}...{NEXT}")})
}

/// The live sequence, on both engines: status_list answers with the fields,
/// then a row update comes without them (an older engine) or with them.
fn live_sequence(new: bool) {
    let (app, tx) = engine_home(&caps(new));
    let mut h = harness(app);
    let rows = [
        row("fix", &review_items(), json!([]), review_fields(HEAD, json!(null))),
        row("feat", &feat_items(), idle_agent(), feat_fields()),
    ];
    let answer = list(&rows);
    fake_engine(&h, move |m, _| (m == "status_list").then(|| answer.clone()));
    send_items(&tx, &review_items());
    send_items(&tx, &feat_items());
    h.state_mut().state.fetch_inbox();
    settle(&mut h);
    assert_eq!(line(&h, "feat").delegate_label(), "Delegate all (2)");
    // A pass: the same rows, the fields only when the engine sends them.
    for (key, items, agents, fields) in [
        ("fix", review_items(), json!([]), review_fields(HEAD, json!(null))),
        ("feat", feat_items(), idle_agent(), feat_fields()),
    ] {
        let mut r = row(key, &items, agents, if new { fields } else { json!({}) });
        r["fingerprint"] = json!("g");
        tx.send(event("status.updated", json!({"row": r}))).unwrap();
    }
    settle(&mut h);
    assert_eq!(line(&h, "fix").approvable().map(|a| a.sha.as_str()), Some(HEAD));
    assert_eq!(line(&h, "feat").delegate_label(), "Delegate all (2)");
    expand(&mut h, "fix");
    h.get_by_label("Approve  (⇧A)");
    expand(&mut h, "feat");
    h.get_by_label("Delegate all (2)  (d)");
}

#[test]
fn an_older_engines_row_update_keeps_every_opt_in_field() {
    live_sequence(false);
}

#[test]
fn a_row_update_with_the_fields_keeps_approve_and_delegate_all() {
    live_sequence(true);
}

#[test]
fn status_list_and_a_gestures_reply_ask_for_the_same_fields() {
    let (app, tx) = engine_home(&caps(true));
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        // The reply's row with its fields: Delegate all still there.
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": {"kind": "label", "ball": "others",
            "text": "QA", "at": 1.0, "by": "user"}, "row": row("feat", &feat_items(), idle_agent(), feat_fields())})),
        _ => None,
    });
    send_items(&tx, &feat_items());
    tx.send(event("status.updated", json!({"row": row("feat", &feat_items(), idle_agent(), feat_fields())}))).unwrap();
    settle(&mut h);
    h.state_mut().state.fetch_inbox();
    h.run_steps(2);
    let fields = calls_of(&calls, "status_list").pop().unwrap()["fields"].clone();
    assert!(fields.as_array().unwrap().contains(&json!("review_commits")), "{fields}");
    let l = line(&h, "feat");
    h.state_mut().state.label_line(crate::home::Label { key: l.key.clone(), ball: "others".into(), text: "QA".into() });
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "status_gesture").pop().unwrap()["fields"], fields);
    assert_eq!(line(&h, "feat").delegate_label(), "Delegate all (2)");
}

#[test]
fn a_refused_approval_fetches_the_rows_and_the_new_head_is_the_one_approved() {
    let (app, tx) = engine_home(&caps(true));
    let mut h = harness(app);
    let calls: Calls = Default::default();
    let seen = calls.clone();
    let moved = row("fix", &review_items(), json!([]), review_fields(NEXT, json!(null)));
    *h.state().state.conn.fake.lock().unwrap() = Some(Box::new(move |method, params| {
        seen.lock().unwrap().push((method.to_string(), params.clone()));
        match method {
            "owner_approve" if params["sha"] == HEAD => {
                Some(Err(lazyagent_proto::ClientError::Decode("The PR's head is 9b8a7c6 now".into())))
            }
            "status_list" => Some(Ok(list(std::slice::from_ref(&moved)))),
            _ => Some(Ok(json!({}))),
        }
    }));
    send_items(&tx, &review_items());
    tx.send(event(
        "status.updated",
        json!({"row": row("fix", &review_items(), json!([]), review_fields(HEAD, json!(null)))}),
    ))
    .unwrap();
    settle(&mut h);
    let before = calls_of(&calls, "status_list").len();
    h.state_mut().state.home.selected = Some("repo:fix".into());
    h.run_steps(1);
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::A);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "status_list").len(), before + 1, "fetched again after the refusal");
    settle(&mut h);
    assert_eq!(line(&h, "fix").approvable().map(|a| a.sha.as_str()), Some(NEXT));
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::A);
    h.run_steps(3);
    let shas: Vec<serde_json::Value> = calls_of(&calls, "owner_approve").iter().map(|p| p["sha"].clone()).collect();
    assert_eq!(shas, [json!(HEAD), json!(NEXT)]);
}

#[test]
fn the_expanded_review_line_lists_what_to_review_and_links_the_compare() {
    let (app, tx) = engine_home(&caps(true));
    let mut h = harness(app);
    fake_engine(&h, |_, _| None);
    send_items(&tx, &review_items());
    tx.send(event(
        "status.updated",
        json!({"row": row("fix", &review_items(), json!([]),
        review_fields(NEXT, rc("approval", HEAD, 1, 14)))}),
    ))
    .unwrap();
    settle(&mut h);
    expand(&mut h, "fix");
    h.get_by_label("To review: 1 commit since your approval (3f2a9c1) · 1 base merge not counted");
    h.get_by_label("9b8a7c6  Retry the upload once · Ana · 2026-10-27");
    h.get_by_label("7c6d5e4  Merge branch 'main' into fix · base merge, not counted");
    h.get_by_label("and 14 more");
    // No new button in the bar: a link inside the line.
    assert!(h.query_by_label("Compare on GitHub  (c)").is_none());
    h.get_by_label("Compare on GitHub").click();
    let opened = (0..3).find_map(|_| {
        h.step();
        h.output().platform_output.commands.iter().find_map(|c| match c {
            egui::OutputCommand::OpenUrl(u) => Some(u.url.clone()),
            _ => None,
        })
    });
    assert_eq!(opened, Some(format!("https://github.com/o/r/compare/{HEAD}...{NEXT}")));
}

#[test]
fn since_the_pr_was_opened_compares_from_its_base() {
    let (app, tx) = engine_home(&caps(true));
    let mut h = harness(app);
    fake_engine(&h, |_, _| None);
    send_items(&tx, &review_items());
    tx.send(event(
        "status.updated",
        json!({"row": row("fix", &review_items(), json!([]),
        review_fields(NEXT, rc("opened", "main", 3, 0)))}),
    ))
    .unwrap();
    settle(&mut h);
    let l = line(&h, "fix");
    let r = l.review_commits().expect("listed");
    assert_eq!(
        crate::home_view::review_heading(r),
        "To review: since the PR was opened: 3 commits · 1 base merge not counted"
    );
    assert_eq!(r.compare_url.as_deref(), Some(format!("https://github.com/o/r/compare/main...{NEXT}").as_str()));
    // Not on a line where Approve does not apply (a teammate's PR).
    let mut other = l.clone();
    other.row.as_mut().unwrap().approvable = None;
    assert!(other.review_commits().is_none());
}

/// repo:feat with its items and no live agent; a restorable one when `restorable`.
fn no_agent(h: &mut Harness<'static, App>, tx: &mpsc::Sender<Msg>, restorable: bool) {
    send_items(tx, &feat_items());
    tx.send(event("status.updated", json!({"row": row("feat", &feat_items(), json!([]), feat_fields())}))).unwrap();
    if restorable {
        h.state_mut().state.restorable = vec![
            serde_json::from_value(
                json!({"id": "e1", "worktree_path": "/r/repo-feat", "agent_id": "a1", "kind": "agent",
                "provider": "claude", "label": "", "session_name": "s", "skip_permissions": false,
                "conversation_id": null, "instruction": null, "started_at": 1.0, "engine_id": null, "ended_at": 2.0}),
            )
            .unwrap(),
        ];
    }
    settle(h);
    expand(h, "feat");
}

#[test]
fn delegate_through_a_restore_is_one_engine_step_with_every_item() {
    let (app, tx) = engine_home(&caps(true));
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        _ => None,
    });
    no_agent(&mut h, &tx, true);
    h.get_by_label("Restore agent and delegate").click();
    h.run_steps(3);
    assert!(calls_of(&calls, "restore_agents").is_empty(), "the engine restores it");
    let [g] = calls_of(&calls, "status_gesture").try_into().unwrap();
    assert_eq!((&g["kind"], &g["to"], &g["restore"]), (&json!("delegate"), &json!("agent"), &json!("e1")));
    assert!(g.get("item").is_none(), "Delegate all: every item the row names");
    assert_eq!(h.state().state.undo.as_ref().map(|u| u.text.as_str()), Some("Delegated feat to its agent"));
}

#[test]
fn delegate_through_a_spawn_is_one_engine_step() {
    let (app, tx) = engine_home(&caps(true));
    let mut h = harness(app);
    let calls = fake_engine(&h, |m, p| match m {
        "status_gesture" => Some(json!({"key": p["key"], "claim": null, "gesture": null, "row": null})),
        _ => None,
    });
    no_agent(&mut h, &tx, false);
    h.get_by_label("Spawn an agent for this").click();
    h.run_steps(3);
    assert!(calls_of(&calls, "spawn_agent").is_empty(), "the engine spawns it");
    let [g] = calls_of(&calls, "status_gesture").try_into().unwrap();
    assert_eq!(g["spawn"]["default_permission_mode"], "cli", "no mode: the configured one applies");
    assert!(g.get("restore").is_none());
}
