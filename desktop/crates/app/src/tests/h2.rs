//! H2: Home item layout proposals (docs/design/home-item-proposals.md). The
//! same neutral sample (an approval, a red CI, a review, a brief, an agent
//! working, a PR waiting on others, a hold, a delegated letter, a done row)
//! drawn in every `home_item_style`, dark and light.

use super::*;
use crate::home_view::ItemStyle;

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
    let long = "billing-retries-with-exponential-backoff-and-jitter";
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

/// Home with the sample in `style`, `dark` or light, at `width`.
fn home(style: ItemStyle, dark: bool, width: f32) -> Harness<'static, App> {
    let now = crate::app::unix_now();
    let caps = caps();
    let (mut app, tx) = engine_home(&caps);
    app.state.look_from = now;
    app.state.home_item_style = style;
    let mut h = Harness::builder().with_size([width, 960.0]).build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    h.run_steps(2);
    if dark != (h.state().state.theme.kind == ThemeKind::Dark) {
        let ctx = h.ctx.clone();
        h.state_mut().state.toggle_theme(&ctx);
        h.run_steps(2);
    }
    fake_engine(&h, |m, p| match m {
        "status_timeline" => Some(json!({"key": p["key"], "since": null, "seen_at": null, "entries": []})),
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
