//! R3d: the owner's approval of a PR head outside GitHub. ⇧A (and the
//! Approve button) on the user's own review line records the head shown
//! (`owner_approve`), Ctrl+Z undoes it, a line the engine does not call
//! approvable sends nothing; the key bar names ⇧A; status_list asks for
//! `approvable`; Why? shows the approval with Revoke; the timeline reads
//! "review  you approved …".

use super::*;

const HEAD: &str = "3f2a9c1e5b7d40a8c6e2f1b0d9a8c7e6f5d4c3b2";

fn caps() -> Vec<&'static str> {
    I4E.iter().copied().chain(["rules", "actions", "item_capabilities", "owner_approve"]).collect()
}

fn settle(h: &mut Harness<'static, App>) {
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
}

/// A Round line `key` decided by the user's rule `rule_id` (an item under
/// the queue), with the engine's row field `approvable`.
fn review_line(tx: &mpsc::Sender<Msg>, key: &str, rule_id: &str, verb: &str, approvable: serde_json::Value) {
    let since = crate::app::unix_now() - 600.0;
    let item = json!({"id": format!("Rule:{rule_id}:repo:{key}"), "occurrence": format!("Rule:{rule_id}:repo:{key}@1"),
                      "kind": "rule", "level": "round", "title": verb, "label": "#7 Feat", "detail": "",
                      "state": "new commits since your approval", "work_item": format!("repo:{key}"),
                      "worktree_path": format!("/r/repo-{key}"), "agent_id": null,
                      "pr": {"number": 7, "title": "Feat", "url": "https://example.com/pull/7"},
                      "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
                      "since": since, "park": null, "run": null, "class": "quick", "rule_id": rule_id,
                      "verb": verb, "delegable": false, "passable": false});
    let mut row = status_row(key, "inbox", Some("round"), "you", "new commits since your approval", 600.0);
    row["needs_you"] = json!({"count": 1, "top": item["id"],
                              "items": [{"id": item["id"], "kind": "rule", "level": "round",
                                         "state": item["state"], "since": since}]});
    row["urls"] = json!(["https://example.com/pull/7"]);
    (row["rule_id"], row["verb"], row["action"]) = (json!("queue"), json!(verb), json!("open_pr"));
    (row["delegable"], row["passable"], row["approvable"]) = (json!(false), json!(false), approvable);
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
}

fn approval(state: &str) -> serde_json::Value {
    json!({"key": "repo:fix", "branch": "fix", "pr": 7, "sha": HEAD, "at": crate::app::unix_now() - 3600.0,
           "source": "agent", "note": "approved after the retry fix", "agent_id": "a1", "has_previous": false,
           "state": state, "after": 1, "merges": 1,
           "text": "you approved 3f2a9c1 (agent): 1 commit since (1 clean merge of the base not counted)"})
}

fn engine(h: &Harness<'static, App>) -> Calls {
    fake_engine(h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": [
            {"at": crate::app::unix_now() - 120.0, "kind": "owner_approval", "actor": "user",
             "text": "you approved 3f2a9c1", "ref": HEAD}]})),
        "owner_approve" => Some(approval("head")),
        "owner_approval_revoke" => Some(json!({"revoked": true, "standing": null, "key": p["key"], "sha": HEAD})),
        "rules_explain" => Some(json!({
            "key": p["key"],
            "rule": {"id": "queue", "origin": "builtin", "file": null, "line": null, "locked": true, "says": "",
                     "when": "item.open", "and_when": null, "verb": null, "action": null, "item": false},
            "then": {"ball": "you", "level": "round", "state": "new commits since your approval"},
            "facts": [], "above": [], "items": [],
            "answer": "“new commits since your approval”: there are pushes since your approval.",
            "owner_approval": approval("counted")})),
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
        &crate::home_view::KeysCtx { gestures: true, why: true, ..Default::default() },
        Some(&line),
    )
}

fn approvable(approved: bool) -> serde_json::Value {
    json!({"sha": HEAD, "pr": 7, "approved": approved})
}

#[test]
fn shift_a_approves_the_head_shown_and_ctrl_z_undoes_it() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h);
    review_line(&tx, "fix", "owner_review_again", "Review", approvable(false));
    settle(&mut h);
    assert!(bar(&h, "fix").contains("⇧A approve"), "{}", bar(&h, "fix"));
    select(&mut h, "fix");
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::A);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "owner_approve"), [json!({"key": "repo:fix", "sha": HEAD, "source": "home"})]);
    let undo = h.state().state.undo.clone().expect("an Undo");
    assert_eq!(undo.text, "Approved fix at 3f2a9c1");
    h.key_press_modifiers(egui::Modifiers::CTRL, egui::Key::Z);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "owner_approval_revoke"), [json!({"key": "repo:fix", "sha": HEAD, "undo": true})]);
}

#[test]
fn the_approve_button_shows_only_where_the_engine_says() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h);
    review_line(&tx, "fix", "owner_review_again", "Review", approvable(false));
    review_line(&tx, "merge", "merge", "Merge", json!(null));
    review_line(&tx, "done", "owner_review_again", "Review", approvable(true));
    review_line(&tx, "peer", "waiting_for_peer", "Wait", approvable(false));
    settle(&mut h);
    // Approvable but not a Review line: no button (the action bar stays as
    // it is), ⇧A still approves.
    expand(&mut h, "peer");
    assert!(h.query_by_label("Approve  (⇧A)").is_none());
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::A);
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "owner_approve").len(), 1);
    expand(&mut h, "fix");
    h.get_by_label("Approve  (⇧A)").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "owner_approve").len(), 2);
    for key in ["merge", "done"] {
        expand(&mut h, key);
        assert!(h.query_by_label("Approve  (⇧A)").is_none(), "{key}");
        assert!(!bar(&h, key).contains("⇧A"), "{key}");
        h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::A);
        h.run_steps(2);
    }
    // Nothing more sent; each says why.
    assert_eq!(calls_of(&calls, "owner_approve").len(), 2);
    let toasts: Vec<String> = h.state().state.toasts.iter().map(|t| t.text.clone()).collect();
    assert!(toasts.iter().any(|t| t.starts_with("⇧A approves your own PR")), "{toasts:?}");
    assert!(toasts.iter().any(|t| t == "You approved #7 at 3f2a9c1 already."), "{toasts:?}");
}

#[test]
fn a_refused_approval_says_why() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    *h.state().state.conn.fake.lock().unwrap() = Some(Box::new(|method, _| match method {
        "owner_approve" => Some(Err(lazyagent_proto::ClientError::Decode("the head moved".into()))),
        _ => Some(Ok(json!({}))),
    }));
    review_line(&tx, "fix", "owner_review_again", "Review", approvable(false));
    settle(&mut h);
    select(&mut h, "fix");
    h.key_press_modifiers(egui::Modifiers::SHIFT, egui::Key::A);
    h.run_steps(3);
    let toasts: Vec<String> = h.state().state.toasts.iter().map(|t| t.text.clone()).collect();
    assert!(toasts.iter().any(|t| t.starts_with("Not approved:") && t.contains("the head moved")), "{toasts:?}");
    assert!(h.state().state.undo.is_none());
}

#[test]
fn status_list_asks_for_approvable_with_the_feature() {
    let (app, _tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h);
    h.state_mut().state.fetch_inbox();
    h.run_steps(2);
    let p = calls_of(&calls, "status_list").pop().expect("asked");
    assert_eq!(p["fields"], json!(["rule_id", "verb", "action", "delegable", "passable", "approvable"]));
}

#[test]
fn why_shows_the_approval_with_revoke_and_the_timeline_says_it() {
    let (app, tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = engine(&h);
    review_line(&tx, "fix", "owner_review_again", "Review", approvable(false));
    settle(&mut h);
    select(&mut h, "fix");
    h.key_press(egui::Key::Enter);
    h.run_steps(4);
    assert!(h.query_all_by_label_contains("review  you approved 3f2a9c1 · user").count() >= 1);
    h.key_press(egui::Key::W);
    h.run_steps(3);
    h.get_by_label("Your approval");
    assert_eq!(h.query_all_by_label_contains("approved to its agent · 1").count(), 1);
    h.get_by_label("you approved 3f2a9c1 (agent): 1 commit since (1 clean merge of the base not counted)");
    h.get_by_label("approved after the retry fix");
    h.get_by_label("Revoke").click();
    h.run_steps(4);
    assert_eq!(calls_of(&calls, "owner_approval_revoke"), [json!({"key": "repo:fix", "sha": HEAD})]);
    assert_eq!(calls_of(&calls, "rules_explain").len(), 2);
}
