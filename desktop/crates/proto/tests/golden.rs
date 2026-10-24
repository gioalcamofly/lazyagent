//! Keeps this client in sync with the Python side: the golden fixtures of
//! `tests/protocol/fixtures/ipc/` decode into our types without losing a
//! field, and the methods and events we use exist in the v1 schemas.

use std::path::PathBuf;

use lazyagent_proto::types::*;
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

fn fixtures() -> Vec<Value> {
    let dir = repo().join("tests/protocol/fixtures/ipc");
    let mut out: Vec<Value> = std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .map(|e| serde_json::from_str(&std::fs::read_to_string(e.unwrap().path()).unwrap()).unwrap())
        .collect();
    out.sort_by_key(|f| f["case"].as_str().unwrap().to_string());
    assert!(out.len() >= 30, "expected the golden fixtures, found {}", out.len());
    out
}

fn schema(name: &str) -> Value {
    let path = repo().join("src/lazyagent/protocol/schema/v1").join(name);
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// Every key of `original` (recursively) is in `ours`: decoding and
/// re-encoding kept every field the server sends.
fn assert_keeps_fields(case: &str, original: &Value, ours: &Value, at: &str) {
    match (original, ours) {
        (Value::Object(a), Value::Object(b)) => {
            for (k, v) in a {
                let Some(w) = b.get(k) else { panic!("{case}: field {at}.{k} lost by the Rust type") };
                assert_keeps_fields(case, v, w, &format!("{at}.{k}"));
            }
        }
        (Value::Array(a), Value::Array(b)) => {
            assert_eq!(a.len(), b.len(), "{case}: {at}");
            for (i, (v, w)) in a.iter().zip(b).enumerate() {
                assert_keeps_fields(case, v, w, &format!("{at}[{i}]"));
            }
        }
        (Value::Number(a), Value::Number(b)) => assert_eq!(a.as_f64(), b.as_f64(), "{case}: {at}"),
        (a, b) => assert_eq!(a, b, "{case}: {at}"),
    }
}

fn round_trip<T: DeserializeOwned + Serialize>(case: &str, result: &Value) {
    let typed: T = serde_json::from_value(result.clone()).unwrap_or_else(|e| panic!("{case}: {e}"));
    assert_keeps_fields(case, result, &serde_json::to_value(&typed).unwrap(), "result");
}

#[test]
fn every_success_fixture_of_a_method_we_use_decodes_losslessly() {
    let mut checked = 0;
    for f in fixtures() {
        let case = f["case"].as_str().unwrap();
        let Some(result) = f["response"].get("result") else { continue };
        match f["method"].as_str().unwrap() {
            "list_worktrees" => round_trip::<Vec<Worktree>>(case, result),
            "list_agents" => round_trip::<ListAgentsResult>(case, result),
            "spawn_agent" => round_trip::<SpawnResult>(case, result),
            "stop_agent" => round_trip::<StopResult>(case, result),
            "create_worktree" => round_trip::<CreateWorktreeResult>(case, result),
            "remove_worktree" => round_trip::<RemoveWorktreeResult>(case, result),
            "send_agent_input" => round_trip::<SendInputResult>(case, result),
            "read_agent_output" => round_trip::<ReadOutputResult>(case, result),
            "list_restorable" => round_trip::<RestorableList>(case, result),
            "restore_agents" => round_trip::<Restored>(case, result),
            "dismiss_restorable" => round_trip::<DismissResult>(case, result),
            // Not used by the desktop app (MCP-only).
            "get_agent_status" | "list_events" => continue,
            "notes_get" => round_trip::<Notes>(case, result),
            "notes_put" | "notes_append" | "notes_replace_section" | "notes_adopt" => {
                round_trip::<NotesWritten>(case, result)
            }
            "notes_list" => round_trip::<NotesList>(case, result),
            "notes_delete" => round_trip::<NotesDeleted>(case, result),
            "review_replies" => round_trip::<ReviewReplies>(case, result),
            // Its result is not read (the app only needs it to succeed);
            // review_reply is the agents' (MCP).
            "review_register" | "review_reply" => continue,
            "status_list" => round_trip::<StatusList>(case, result),
            "actionables_list" => round_trip::<ActionablesList>(case, result),
            "actionables_get" | "actionables_mark" => round_trip::<ActionableResult>(case, result),
            "status_gesture" => round_trip::<StatusGestureResult>(case, result),
            // The orchestrator's (MCP).
            "orchestrator_release" | "orchestrator_hold" => continue,
            // Agents' (MCP).
            "status_report" | "request_decision" | "decision_withdraw" => continue,
            "status_seen" => round_trip::<StatusSeen>(case, result),
            "status_timeline" => round_trip::<StatusTimeline>(case, result),
            "decision_get" => round_trip::<DecisionResult>(case, result),
            "decisions_list" => round_trip::<DecisionsList>(case, result),
            "decision_revert" => round_trip::<DecisionResult>(case, result),
            "inbox_config_get" | "inbox_config_set" => round_trip::<InboxConfig>(case, result),
            "presence_report" | "presence_set" => round_trip::<PresenceResult>(case, result),
            // The user answers from the decision view (I5).
            "decision_answer" => round_trip::<DecisionResult>(case, result),
            "decision_explain_request" | "decision_rebrief_request" => {
                round_trip::<DecisionExplainResult>(case, result)
            }
            "decision_evidence" => round_trip::<DecisionEvidence>(case, result),
            // The expanded CI line's failed log (I7).
            "ci_log_summary" => round_trip::<CiLogSummary>(case, result),
            // The orchestrator's and the agents' (MCP).
            "decision_escalate" | "decision_explain" => continue,
            // External feeders' (MCP, a planner or a script).
            "work_items_upsert" | "work_items_close" => continue,
            other => panic!("{case}: a fixture for {other}, which this test does not know: decide"),
        }
        checked += 1;
    }
    assert!(checked >= 34, "only {checked} fixtures checked");
}

#[test]
fn error_fixtures_decode_as_rpc_errors() {
    for f in fixtures() {
        if let Some(err) = f["response"].get("error") {
            let e: lazyagent_proto::RpcError = serde_json::from_value(err.clone()).unwrap();
            assert!(!e.code.is_empty(), "{}", f["case"]);
        }
    }
}

/// Methods the desktop app calls.
const METHODS_USED: &[&str] = &[
    "subscribe",
    "get_sidebar_state",
    "set_interest",
    "get_config",
    "attach_stream",
    "detach_stream",
    "claim_size",
    "resize",
    "open_shell",
    "spawn_agent",
    "stop_agent",
    "create_worktree",
    "remove_worktree",
    "refresh_worktrees",
    "refresh_git_status",
    "get_pr_info",
    "get_diff",
    "send_agent_input",
    "read_agent_output",
    "list_agents",
    "list_restorable",
    "restore_agents",
    "dismiss_restorable",
    "notes_get",
    "notes_put",
    "review_register",
    "review_replies",
    "status_list",
    "actionables_list",
    "actionables_mark",
    "status_gesture",
    "status_seen",
    "status_timeline",
    "decisions_list",
    "decision_get",
    "presence_report",
    "presence_set",
    "decision_revert",
    "inbox_config_get",
    "inbox_config_set",
    "decision_answer",
    "decision_explain_request",
    "decision_rebrief_request",
    "decision_evidence",
    "ci_log_summary",
];

/// Events in events.json that the desktop app deliberately ignores.
const IGNORED_EVENTS: &[&str] = &[
    "ui.command",
    "ui.view_changed",
    // The rows carry the claim (status.updated); the desktop needs no more.
    "orchestrator.claim",
    "orchestrator.released",
    "orchestrator.lapsed",
    "orchestrator.hold",
];

#[test]
fn methods_we_call_are_in_the_schema() {
    let defs = schema("methods.json")["$defs"].clone();
    for m in METHODS_USED {
        assert!(defs.get(format!("{m}.params")).is_some(), "{m} is not in methods.json");
        assert!(defs.get(format!("{m}.result")).is_some(), "{m} has no result schema");
    }
    assert!(schema("handshake.json")["$defs"].get("hello.params").is_some());
}

#[test]
fn every_schema_event_is_handled_or_ignored_on_purpose() {
    let defs = schema("events.json")["$defs"].as_object().unwrap().clone();
    // Event definitions are the dotted names; the rest are shared shapes.
    let events: Vec<&String> = defs.keys().filter(|k| k.contains('.')).collect();
    for e in &events {
        assert!(
            KNOWN_EVENTS.contains(&e.as_str()) || IGNORED_EVENTS.contains(&e.as_str()),
            "new event {e} in events.json: handle it in EngineEvent or add it to IGNORED_EVENTS"
        );
    }
    for k in KNOWN_EVENTS {
        assert!(defs.contains_key(*k), "{k} is not in events.json");
    }
}

#[test]
fn required_fields_of_our_result_types_match_the_schema() {
    // A result built from only the schema's required fields must decode:
    // if the schema makes a field optional that we require, this fails.
    let methods = schema("methods.json")["$defs"].clone();
    let required = |def: &str| -> Vec<String> {
        methods[def]["required"]
            .as_array()
            .map(|a| a.iter().map(|v| v.as_str().unwrap().to_string()).collect())
            .unwrap_or_default()
    };
    let ours_required = |v: Value| -> Vec<String> {
        // Serialise then keep the keys our type always writes (no skip_serializing_if).
        v.as_object().unwrap().iter().filter(|(_, v)| !v.is_null()).map(|(k, _)| k.clone()).collect()
    };
    let attach = AttachResult {
        stream_id: 1,
        session_id: "s".into(),
        size: Size { rows: 1, cols: 1 },
        mode: "tail".into(),
        offset: 0,
        snapshot: None,
    };
    let mut ours = ours_required(serde_json::to_value(attach).unwrap());
    let mut schema_req = required("attach_stream.result");
    ours.sort();
    schema_req.sort();
    assert_eq!(ours, schema_req);
    for (def, value) in [
        ("size_owner", serde_json::to_value(SizeOwner { session_id: "s".into(), rows: 1, cols: 1, owner: true })),
        (
            "open_shell.result",
            serde_json::to_value(OpenShellResult {
                worktree_path: "/".into(),
                session_id: "s".into(),
                size: Size { rows: 1, cols: 1 },
            }),
        ),
        (
            "get_config.result",
            serde_json::to_value(ConfigView {
                agent_provider: "claude".into(),
                default_branch: "main".into(),
                has_custom_create: false,
                has_custom_remove: false,
            }),
        ),
    ] {
        let mut ours = ours_required(value.unwrap());
        let mut theirs = required(def);
        ours.sort();
        theirs.sort();
        assert_eq!(ours, theirs, "{def}");
    }
}
