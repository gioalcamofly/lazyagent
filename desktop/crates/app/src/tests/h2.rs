//! H2: Home item layout proposals (docs/design/home-item-proposals.md). The
//! same neutral sample (an approval, a red CI, a review, a brief, an agent
//! working, a PR waiting on others, a hold, a delegated letter, a done row)
//! drawn in every `home_item_style`, dark and light.

use super::*;
use crate::home_view::{DetailStyle, ItemStyle};

fn caps() -> Vec<&'static str> {
    I4E.iter()
        .copied()
        .chain([
            "rules",
            "actions",
            "item_capabilities",
            "delegate_all",
            "owner_approve",
            crate::home_view::F_ROW_FIELDS,
        ])
        .collect()
}

fn pr(n: u64, ci: &str, review: &str) -> serde_json::Value {
    json!({"number": n, "state": "OPEN", "review": review, "ci": ci, "mergeable": "MERGEABLE"})
}

fn needs(items: &[&serde_json::Value]) -> serde_json::Value {
    json!({"count": items.len(), "top": items[0]["id"],
           "items": items.iter().map(|i| json!({"id": i["id"], "kind": i["kind"], "level": i["level"],
                                                  "state": i["state"], "since": i["since"]})).collect::<Vec<_>>()})
}

/// The sample, as the engine would send it.
fn sample(tx: &mpsc::Sender<Msg>, now: f64) {
    let send = |name: &str, data: serde_json::Value| tx.send(event(name, data)).unwrap();
    // Now: an agent waits on a command approval.
    let approval = ask_item("upload-resume", "approval", "now", "npm test -- --runInBand src/upload");
    let mut upload = status_row("upload-resume", "inbox", Some("now"), "you", "approve: npm test", 120.0);
    upload["needs_you"] = needs(&[&approval]);
    upload["agents"] = json!([{"id": "a1", "status": "waiting_for_approval", "since": now - 120.0}]);
    // Round: a red CI with a review behind it, on a long branch name.
    let long = LONG;
    let mut ci = json!({"id": format!("Ci:/r/repo-{long}:15"), "occurrence": "c@1", "kind": "ci", "level": "round",
        "title": "ci", "label": "#15 Retry failed charges", "detail": "", "state": "CI red: unit-tests",
        "work_item": format!("repo:{long}"), "worktree_path": format!("/r/repo-{long}"), "agent_id": null,
        "pr": {"number": 15, "title": "Retry failed charges", "url": "https://example.com/pull/15"},
        "job_id": null, "exit_code": null, "blocks_agent": false, "priority": "high", "since": now - 900.0,
        "park": null, "run": null});
    ci["delegable"] = json!(true);
    let mut review = ci.clone();
    (review["id"], review["kind"], review["state"]) =
        (json!(format!("Review:/r/repo-{long}:15")), json!("review"), json!("changes requested"));
    let mut billing = status_row(long, "inbox", Some("round"), "you", "CI red: unit-tests", 900.0);
    billing["needs_you"] = needs(&[&ci, &review]);
    billing["priority"] = json!("high");
    billing["pr"] = pr(15, "red", "CHANGES_REQUESTED");
    billing["urls"] = json!(["https://example.com/pull/15"]);
    billing["agents"] = json!([{"id": "a2", "status": "waiting", "since": now - 900.0}]);
    billing["delegable"] = json!(true);
    billing["delegates"] = json!([ci["id"], review["id"]]);
    // Round: the user's own review before a peer's (Approve applies).
    let mut own = ci.clone();
    (own["id"], own["kind"], own["state"], own["title"]) =
        (json!("Rule:owner_review:repo:invoice-pdf"), json!("rule"), json!("ready for your review"), json!("Review"));
    (own["work_item"], own["worktree_path"], own["priority"], own["since"]) =
        (json!("repo:invoice-pdf"), json!("/r/repo-invoice-pdf"), json!("medium"), json!(now - 1500.0));
    (own["rule_id"], own["verb"]) = (json!("owner_review"), json!("Review"));
    let mut invoice = status_row("invoice-pdf", "inbox", Some("round"), "you", "ready for your review", 1500.0);
    invoice["needs_you"] = needs(&[&own]);
    invoice["pr"] = pr(16, "green", "");
    invoice["urls"] = json!(["https://example.com/pull/16"]);
    (invoice["verb"], invoice["approvable"]) =
        (json!("Review"), json!({"sha": "3f2a9c1", "pr": 16, "approved": false}));
    // To sit down with: a brief.
    let b = brief_item("d1", "retry-policy", "deep", true);
    let mut policy = status_row("retry-policy", "inbox", Some("deep"), "you", "decide: retries", 600.0);
    policy["needs_you"] = needs(&[&b]);
    policy["agents"] = json!([{"id": "a2", "status": "waiting", "since": now - 600.0}]);
    // In flight: an agent at work, with its last report.
    let mut search = status_row("search-filters", "in_flight", None, "agent", "working", 240.0);
    search["agents"] = json!([{"id": "a3", "status": "running", "since": now - 1800.0}]);
    search["note"] = json!({"text": "filter chips wired; writing the tests", "by": "agent:a3", "at": now - 240.0});
    search["pr"] = pr(17, "running", "");
    // A PR waiting on a peer.
    let mut export = status_row("order-export", "in_flight", None, "others", "waiting for a peer review", 7200.0);
    export["pr"] = pr(18, "red", "REVIEW_REQUIRED");
    export["rule"] = json!(8);
    export["ci_note"] = json!("CI red · unrelated (base also red: 2 tests)");
    export["gesture"] = json!({"kind": "label", "text": "QA", "ball": "others", "at": now - 7000.0, "by": "user"});
    export["note"] =
        json!({"text": "rebased on develop; nothing else to do here", "by": "orchestrator", "at": now - 7100.0});
    // Held by the orchestrator while it checks its worker's work.
    let mut cart = status_row("cart-discount", "in_flight", None, "agent", "orchestrator · verifying tests", 600.0);
    cart["hold"] = json!({"key": "repo:cart-discount", "holder": "orchestrator", "at": now - 600.0,
                          "until": now + 3000.0, "by": "engine", "note": "checking agent a4's work", "auto": true});
    cart["last_step"] =
        json!({"text": "reported: tests pass locally", "kind": "report", "actor": "agent a4", "at": now - 660.0});
    cart["agents"] = json!([{"id": "a4", "status": "waiting", "since": now - 660.0}]);
    // A letter delegated to the branch's agent.
    let mut stock = status_row("stock-sync", "in_flight", None, "agent", "agent a5 · on it", 400.0);
    stock["pr"] = pr(19, "red", "");
    stock["agents"] = json!([{"id": "a5", "status": "running", "since": now - 400.0}]);
    stock["delegation"] = json!({"agent": "a5", "item": "Ci:/r/repo-stock-sync:19", "occurrence": "o",
                                 "fact": "CI red: lint", "at": now - 400.0, "by": "user", "phase": "on_it"});
    // Merged today, a suggestion left.
    let mut docs = status_row("docs-index", "in_flight", None, "done", "already merged into develop", 3600.0);
    docs["pr"] = pr(14, "green", "APPROVED");
    docs["suggestion"] = json!("close PR #14: its head is in develop");
    docs["rule"] = json!(1);
    for i in [&approval, &ci, &review, &own, &b] {
        send("actionable.created", json!({"item": i}));
    }
    send("decision.created", json!({"decision": brief("d1", "retry-policy", true)}));
    for r in [upload, billing, invoice, policy, search, export, cart, stock, docs] {
        send("status.updated", json!({"row": r}));
    }
}

const LONG: &str = "billing-retries-with-exponential-backoff-and-jitter";

/// What happened on a line since the user looked (status_timeline).
fn timeline(key: &str, now: f64) -> serde_json::Value {
    let e = |ago: f64, kind: &str, actor: &str, text: &str| json!({"at": now - ago, "kind": kind, "actor": actor, "text": text});
    let entries = match key.strip_prefix("repo:").unwrap_or(key) {
        "upload-resume" => vec![
            e(600.0, "commit", "agent:a1", "commit 8c1d2e0: resume uploads from the last chunk"),
            e(120.0, "ball", "engine", "agent → you: approve: npm test"),
        ],
        LONG => vec![
            e(1800.0, "commit", "agent:a2", "commit 4f2a9c1: retry failed charges with jitter"),
            e(900.0, "ball", "engine", "agent → you: CI red: unit-tests"),
            e(700.0, "review", "engine", "changes requested on PR #15"),
        ],
        "retry-policy" => vec![e(600.0, "decision", "agent:a2", "brief d1: Retries: fixed or exponential backoff?")],
        _ => vec![],
    };
    json!({"key": key, "since": now - 3600.0, "seen_at": now - 3600.0, "entries": entries})
}

/// Home with the sample in `style`, `dark` or light, at `width`.
fn home(style: ItemStyle, dark: bool, width: f32) -> Harness<'static, App> {
    home_with(style, DetailStyle::Current, dark, width, 960.0)
}

fn home_with(style: ItemStyle, detail: DetailStyle, dark: bool, width: f32, height: f32) -> Harness<'static, App> {
    let now = crate::app::unix_now();
    let caps = caps();
    let (mut app, tx) = engine_home(&caps);
    app.state.look_from = now;
    app.state.home_item_style = style;
    app.state.home_detail_style = detail;
    let mut h = Harness::builder().with_size([width, height]).build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(2);
    if dark != (h.state().state.theme.kind == ThemeKind::Dark) {
        let ctx = h.ctx.clone();
        h.state_mut().state.toggle_theme(&ctx);
        h.run_steps(2);
    }
    fake_engine(&h, |m, p| match m {
        "status_timeline" => Some(timeline(p["key"].as_str().unwrap_or(""), crate::app::unix_now())),
        _ => None,
    });
    sample(&tx, now);
    h.run_steps(3);
    h.state_mut().state.home.cut(now);
    h.state_mut().state.home.selected = Some("repo:invoice-pdf".into());
    h.run_steps(3);
    h.state_mut().state.toasts.clear();
    h.run_steps(2);
    h
}

#[test]
fn every_style_draws_the_sample() {
    for style in ItemStyle::ALL {
        let mut h = home(style, true, 1400.0);
        h.get_by_label("Round");
        assert_eq!(h.state().state.home.shown_in(crate::home::Zone::Flight).len(), 5, "{style:?}");
        h.run_steps(1);
    }
}

/// docs/design/desktop-screenshots/h2-*.png: each style, dark and light,
/// the same sample (written to the temp dir; copied by hand).
#[test]
fn h2_screenshots() {
    let out = std::env::temp_dir();
    for style in ItemStyle::ALL {
        for (dark, tag, width) in [(true, "dark", 1400.0), (false, "light", 1400.0), (true, "narrow-dark", 1000.0)] {
            let mut h = home(style, dark, width);
            match h.render() {
                Ok(image) => image.save(out.join(format!("lazyagent-desktop-h2-{}-{tag}.png", style.name()))).unwrap(),
                Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
                Err(_) => return,
            }
        }
    }
}

/// The sample's three expanded items: an approval in Now, a red CI with a
/// review behind it, a brief in To sit down with.
fn expand_three(h: &mut Harness<'static, App>) {
    let now = crate::app::unix_now();
    let keys = ["repo:upload-resume".to_string(), format!("repo:{LONG}"), "repo:retry-policy".to_string()];
    let log: lazyagent_proto::types::CiLogSummary = serde_json::from_value(json!({
        "key": keys[1], "runs": [], "text": "",
        "jobs": [{"name": "unit-tests", "step": "Run tests", "url": "", "total": 3, "lines": [
            "FAILED tests/test_retry.py::test_backoff_caps_at_30s - AssertionError: 45.0 > 30",
            "FAILED tests/test_retry.py::test_jitter_is_bounded - AssertionError",
            "2 failed, 118 passed in 9.41s"]}]}))
    .unwrap();
    let s = &mut h.state_mut().state;
    s.ci_logs.insert(keys[1].clone(), ((0, 0), Some(log)));
    for k in &keys {
        s.timelines.insert(k.clone(), serde_json::from_value(timeline(k, now)).unwrap());
    }
    s.home.selected = Some(keys[1].clone());
    s.home.expanded = Some(keys[1].clone());
    s.home.expanded_also = vec![keys[0].clone(), keys[2].clone()];
    h.run_steps(4);
}

#[test]
fn every_detail_style_draws_the_three_items() {
    for detail in DetailStyle::ALL {
        let mut h = home_with(ItemStyle::Grid, detail, true, 1400.0, 1500.0);
        expand_three(&mut h);
        // The approval's command and the brief's question, in every style.
        h.get_by_label("npm test -- --runInBand src/upload");
        assert!(h.query_all_by_label_contains("Retries: fixed or exponential backoff?").count() >= 1, "{detail:?}");
    }
}

/// docs/design/desktop-screenshots/h2d-*.png: the grid line (d) with three
/// items expanded, each detail style, dark, light and 1000 px.
#[test]
fn h2_detail_screenshots() {
    let out = std::env::temp_dir();
    for detail in DetailStyle::ALL {
        for (dark, tag, width) in [(true, "dark", 1400.0), (false, "light", 1400.0), (true, "narrow-dark", 1000.0)] {
            let mut h = home_with(ItemStyle::Grid, detail, dark, width, 1500.0);
            expand_three(&mut h);
            match h.render() {
                Ok(image) => {
                    image.save(out.join(format!("lazyagent-desktop-h2d-{}-{tag}.png", detail.name()))).unwrap()
                }
                Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
                Err(_) => return,
            }
        }
    }
}
