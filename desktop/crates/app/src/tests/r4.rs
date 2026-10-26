//! CI red but unrelated to the branch (proposal 0014 R4): the row's opt-in
//! `ci_note`, asked for only from an engine with `ci_tests`, kept across
//! `status.updated` (which carries no fields), and drawn as a chip.

use super::*;

const NOTE: &str = "CI red · unrelated (base also red: 2 tests)";

fn caps(ci_tests: bool) -> Vec<&'static str> {
    let extra: &[&str] = if ci_tests { &["rules", "actions", "ci_tests"] } else { &["rules", "actions"] };
    I4E.iter().copied().chain(extra.iter().copied()).collect()
}

fn unrelated_row() -> serde_json::Value {
    let mut row = status_row("tax", "in_flight", None, "stalled", "no agent", 600.0);
    (row["rule"], row["rule_id"], row["verb"], row["action"], row["ci_note"]) =
        (json!(12), json!("no_agent"), json!(null), json!(null), json!(NOTE));
    row
}

#[test]
fn an_unrelated_red_shows_its_chip_and_keeps_it_across_status_updated() {
    let (app, tx) = engine_home(&caps(true));
    let mut h = harness(app);
    let calls = fake_engine(&h, |_, _| None);
    h.state_mut().state.fetch_inbox();
    h.run_steps(3);
    let [params] = &calls_of(&calls, "status_list")[..] else { panic!("one status_list") };
    assert_eq!(params["fields"], json!(["rule_id", "verb", "action", "ci_note"]));
    // As status_list's answer gives it (with the fields).
    tx.send(event("status.updated", json!({"row": unrelated_row()}))).unwrap();
    h.run_steps(3);
    h.state_mut().state.home.cut(crate::app::unix_now());
    h.run_steps(2);
    h.get_by_label(NOTE);
    // status.updated has no opt-in fields: the note stays while the rule
    // and the state are the same.
    let mut bare = unrelated_row();
    for k in ["rule_id", "verb", "action", "ci_note"] {
        bare.as_object_mut().unwrap().remove(k);
    }
    tx.send(event("status.updated", json!({"row": bare}))).unwrap();
    h.run_steps(3);
    let row = &h.state().state.status_rows["repo:tax"];
    assert_eq!(row.ci_note.as_deref(), Some(NOTE));
    h.get_by_label(NOTE);
}

#[test]
fn an_engine_before_r4_is_not_asked_for_the_note() {
    let (app, _tx) = engine_home(&caps(false));
    let mut h = harness(app);
    let calls = fake_engine(&h, |_, _| None);
    h.state_mut().state.fetch_inbox();
    h.run_steps(3);
    let [params] = &calls_of(&calls, "status_list")[..] else { panic!("one status_list") };
    assert_eq!(params["fields"], json!(["rule_id", "verb", "action"]));
}
