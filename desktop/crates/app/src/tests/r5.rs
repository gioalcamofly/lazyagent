//! Attestations for nondeterministic CI checks (proposal 0014 R5, feature
//! `ci_attest`): the Confirm item shows the evidence (base runs and their
//! range, the agent's own runs, the drop against max_drop) with Accept and
//! Reject (a comment); the row's chip names the attestation; Why? lists
//! the standing attestations with Revoke; and the screenshots.

use super::*;

const ATT: &str = "att1";
const CHIP: &str = "CI red · unrelated (base also red: 2 tests; eval-suite attested by agent a1)";

fn caps() -> Vec<&'static str> {
    I4E.iter().copied().chain(["rules", "actions", "ci_tests", "ci_attest"]).collect()
}

fn home() -> (Harness<'static, App>, mpsc::Sender<Msg>) {
    let caps = caps();
    let (app, tx) = engine_home(&caps);
    (harness(app), tx)
}

fn attestation(state: &str) -> serde_json::Value {
    let base: Vec<_> = [(41, 71), (42, 75), (43, 78), (44, 73), (45, 76)]
        .iter()
        .map(|(id, p)| json!({"run_id": id, "passed": p, "total": 100, "sha": "3f2a9c1".repeat(5)}))
        .collect();
    json!({
        "id": ATT, "key": "repo:tax", "branch": "tax", "check": "eval-suite", "name": "eval-suite",
        "checks": [".github/workflows/evals.yml/eval-suite"], "sha": "9be01d4".repeat(5), "point": null,
        "compared": "", "actor": "agent", "agent_id": "a1", "at": crate::app::unix_now() - 1320.0,
        "expires_at": crate::app::unix_now() + 3600.0, "state": state, "confirm": "user", "accepted_for": true,
        "confirmed_by": if state == "accepted" { json!("user") } else { json!(null) },
        "decided_at": null, "comment": null, "ended": null,
        "evidence": {"verdict": "unrelated",
                     "reason": "pass rate within the base's range over 5 base runs; failing cases differ every run",
                     "method": "range", "base_runs": base,
                     "own_runs": [{"passed": 74, "total": 100, "where": "local"},
                                  {"passed": 75, "total": 100, "where": "local"},
                                  {"run_id": 46, "passed": 76, "total": 100}],
                     "range": {"base_min": 0.71, "base_max": 0.78, "own_min": 0.74, "own_max": 0.76,
                               "drop": 0.0, "max_drop": 0.05, "within": true}},
        "actor_text": "agent a1", "text": "base 5 runs 71-78 %, branch 3 runs (2 local) 74-76 %",
        "range_text": "drop 0 points vs max 5: within the base's range"})
}

fn confirm_item() -> serde_json::Value {
    json!({"id": format!("Attestation_confirm:/r/repo-tax:{ATT}"), "occurrence": "a@1",
           "kind": "attestation_confirm", "level": "round", "title": "Confirm attestation", "label": "#7 Feat",
           "detail": "agent a1 says eval-suite's failure is unrelated: base 5 runs 71-78 %, branch 3 runs (2 local) 74-76 %; drop 0 points vs max 5: within the base's range",
           "state": "Confirm unrelated: eval-suite", "work_item": "repo:tax", "worktree_path": "/r/repo-tax",
           "agent_id": "a1", "pr": {"number": 7, "title": "Feat", "url": "https://example.com/pull/7"},
           "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "medium",
           "since": crate::app::unix_now() - 120.0, "park": null, "run": null, "attestation": attestation("pending")})
}

/// The tax line with its Confirm item on top, in Round.
fn confirm_line(tx: &mpsc::Sender<Msg>) {
    let item = confirm_item();
    let mut row = status_row("tax", "inbox", Some("round"), "you", "Confirm unrelated: eval-suite", 120.0);
    row["needs_you"] = json!({"count": 1, "top": item["id"],
                              "items": [{"id": item["id"], "kind": item["kind"], "level": "round",
                                         "state": item["state"], "since": item["since"]}]});
    (row["rule_id"], row["verb"], row["action"]) = (json!("queue"), json!("Confirm"), json!("expand"));
    row["urls"] = json!(["https://example.com/pull/7"]);
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
}

/// A Merge line in Round (the user's gate) whose red CI is explained:
/// the chip says by what.
fn merge_line(tx: &mpsc::Sender<Msg>, key: &str, note: &str) {
    let item = json!({"id": format!("Rule:merge:repo:{key}"), "occurrence": "m@1", "kind": "rule", "level": "round",
                      "title": "Merge", "label": "#9 Cart", "detail": "", "state": "merge: approved, CI ok",
                      "work_item": format!("repo:{key}"), "worktree_path": format!("/r/repo-{key}"), "agent_id": null,
                      "pr": {"number": 9, "title": "Cart", "url": "https://example.com/pull/9"}, "job_id": null,
                      "exit_code": null, "blocks_agent": false, "priority": "medium",
                      "since": crate::app::unix_now() - 3000.0, "park": null, "run": null,
                      "rule_id": "merge", "verb": "Merge"});
    let mut row = status_row(key, "inbox", Some("round"), "you", "merge: approved, CI ok", 3000.0);
    row["needs_you"] = json!({"count": 1, "top": item["id"],
                              "items": [{"id": item["id"], "kind": "rule", "level": "round",
                                         "state": item["state"], "since": item["since"]}]});
    (row["rule_id"], row["verb"], row["action"], row["ci_note"]) =
        (json!("queue"), json!("Merge"), json!("open_pr"), json!(note));
    row["urls"] = json!(["https://example.com/pull/9"]);
    tx.send(event("actionable.created", json!({"item": item}))).unwrap();
    tx.send(event("status.updated", json!({"row": row}))).unwrap();
}

fn settle(h: &mut Harness<'static, App>) {
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
}

fn explain(p: &serde_json::Value, attestations: serde_json::Value) -> serde_json::Value {
    json!({"key": p["key"],
           "rule": {"id": "merge", "num": 17, "origin": "global", "file": "/home/u/.config/lazyagent/rules.toml",
                    "line": 12, "locked": false, "says": "Merge when CI is green or red only for reasons unrelated to the branch.",
                    "when": "pr.state == \"open\" and (ci.state in [\"green\", \"none\"] or ci.unrelated)",
                    "and_when": null, "verb": "Merge", "action": "open_pr", "item": false, "replaces": "merge"},
           "then": {"ball": "you", "level": "round", "state": "merge: approved, CI ok", "verb": "Merge",
                    "action": "open_pr", "item": false},
           "facts": [{"name": "ci.unrelated", "value": true, "unknown": false, "source": "mechanism A",
                      "freshness": "per pass"},
                     {"name": "ci.attested", "value": [".github/workflows/evals.yml/eval-suite"], "unknown": false,
                      "source": "attestations", "freshness": "per pass"}],
           "above": [], "items": [],
           "answer": "“merge: approved, CI ok”: CI is red only for reasons unrelated to the branch.",
           "attestations": attestations})
}

fn engine_answers(h: &Harness<'static, App>) -> Calls {
    fake_engine(h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
        "ci_attestation_confirm" | "ci_attestation_revoke" => Some(attestation("accepted")),
        "rules_explain" => {
            let mut ended = attestation("expired");
            (ended["id"], ended["ended"], ended["at"]) =
                (json!("att0"), json!("head moved"), json!(crate::app::unix_now() - 90000.0));
            Some(explain(p, json!([attestation("accepted"), ended])))
        }
        _ => None,
    })
}

fn expand(h: &mut Harness<'static, App>) {
    h.state_mut().state.home.selected = Some("repo:tax".into());
    h.run_steps(1);
    h.key_press(egui::Key::Enter);
    h.run_steps(3);
}

#[test]
fn the_confirm_item_shows_the_evidence_and_accept_records_it() {
    let (mut h, tx) = home();
    let calls = engine_answers(&h);
    confirm_line(&tx);
    settle(&mut h);
    let lines = h.state().state.home.shown_in(crate::home::Zone::Round);
    assert_eq!(lines.iter().map(|l| l.verb().to_string()).collect::<Vec<_>>(), ["Confirm"]);
    expand(&mut h);
    h.get_by_label("agent a1 says eval-suite's failure is not the branch's");
    h.get_by_label("“pass rate within the base's range over 5 base runs; failing cases differ every run”");
    h.get_by_label("5 runs: 71 %, 75 %, 78 %, 73 %, 76 % · range 71-78 %");
    h.get_by_label("3 runs at 9be01d4: 74 % (local), 75 % (local), 76 % · range 74-76 %");
    h.get_by_label("0 points vs max_drop 5: within the base's range");
    h.get_by_label("Accept").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "ci_attestation_confirm"), [json!({"id": ATT, "accept": true})]);
    assert!(h.state().state.toasts.iter().any(|t| t.text.starts_with("Accepted")));
}

#[test]
fn reject_asks_for_a_comment_and_sends_it() {
    let (mut h, tx) = home();
    let calls = engine_answers(&h);
    confirm_line(&tx);
    settle(&mut h);
    expand(&mut h);
    h.get_by_label("Reject…").click();
    h.run_steps(3);
    assert_eq!(h.state().state.home.rejecting, Some((ATT.into(), String::new())));
    // No comment, no Reject.
    h.get_by_label("Reject").click();
    h.run_steps(2);
    assert!(calls_of(&calls, "ci_attestation_confirm").is_empty());
    h.state_mut().state.home.rejecting = Some((ATT.into(), "case 14 fails anew: look again".into()));
    h.run_steps(2);
    h.get_by_label("Reject").click();
    h.run_steps(3);
    assert_eq!(
        calls_of(&calls, "ci_attestation_confirm"),
        [json!({"id": ATT, "accept": false, "comment": "case 14 fails anew: look again"})]
    );
    assert!(h.state().state.home.rejecting.is_none());
    // Esc closes the form unsent.
    h.state_mut().state.home.rejecting = Some((ATT.into(), "x".into()));
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    assert!(h.state().state.home.rejecting.is_none());
    assert_eq!(calls_of(&calls, "ci_attestation_confirm").len(), 1);
}

#[test]
fn an_accepted_attestation_is_the_rows_chip() {
    let (mut h, tx) = home();
    engine_answers(&h);
    merge_line(&tx, "tax", CHIP);
    settle(&mut h);
    // Too long for the line: the attestation first, the rest on hover.
    h.get_by_label("CI red · eval-suite attested by agent a1 +1");
    assert_eq!(
        crate::home_view::chip_text("CI red · unrelated (eval-suite attested by agent a3)"),
        "CI red · eval-suite attested by agent a3"
    );
    assert_eq!(
        crate::home_view::chip_text("CI red · unrelated (eval attested by agent a3)"),
        "CI red · unrelated (eval attested by agent a3)"
    );
    assert_eq!(
        crate::home_view::chip_text("CI red · unrelated (base also red: 2 tests in unit-tests and more)"),
        crate::home::middle_ellipsis("CI red · unrelated (base also red: 2 tests in unit-tests and more)", 48)
    );
}

#[test]
fn why_lists_the_standing_attestations_with_revoke() {
    let (mut h, tx) = home();
    let calls = engine_answers(&h);
    merge_line(&tx, "tax", CHIP);
    settle(&mut h);
    h.key_press(egui::Key::W);
    h.run_steps(3);
    h.get_by_label("Attestations · 1 standing");
    h.get_by_label("accepted · confirmed by you");
    h.get_by_label("base 5 runs 71-78 %, branch 3 runs (2 local) 74-76 %");
    // The ended one is folded; its state says why it ended.
    h.get_by_label("Ended · 1");
    assert!(h.query_by_label("expired: head moved").is_none());
    h.get_by_label("Revoke").click();
    h.run_steps(4);
    assert_eq!(calls_of(&calls, "ci_attestation_revoke"), [json!({"id": ATT})]);
    // Why? asks again.
    assert_eq!(calls_of(&calls, "rules_explain").len(), 2);
}

/// docs/design/desktop-screenshots/r5-*.png: the Confirm item expanded
/// with its evidence (dark 1600, light 1100), the Reject form, and Why?
/// with an accepted attestation.
#[test]
fn home_r5_screenshots() {
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
        engine_answers(&h);
        confirm_line(&tx);
        merge_line(&tx, "cart", "CI red · unrelated (eval-suite attested by agent a3)");
        settle(&mut h);
        expand(&mut h);
        let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
            Ok(image) => {
                image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap();
                true
            }
            Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
            Err(_) => false,
        };
        if !shot(&mut h, &format!("r5-confirm-{tag}")) {
            return;
        }
        if dark {
            h.state_mut().state.home.rejecting = Some((ATT.into(), "case 14 fails anew: look again".into()));
            h.run_steps(3);
            shot(&mut h, "r5-reject-dark-1600");
            h.state_mut().state.home.rejecting = None;
            h.state_mut().state.home.selected = Some("repo:cart".into());
            h.run_steps(2);
            h.key_press(egui::Key::W);
            h.run_steps(4);
            shot(&mut h, "r5-why-dark-1600");
        }
    }
}
