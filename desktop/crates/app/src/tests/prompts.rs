//! "Prompts…" (Agents menu): the targets and their files, the review file
//! by file (approve sends the plan's shas, skip sends nothing), a refusal
//! from the engine, the outdated notice on the menu bar, and screenshots.

use super::*;
use lazyagent_proto::ClientError;
use lazyagent_proto::envelope::RpcError;

/// Frames for the modal to settle after its content changed size (it
/// re-centres from the previous frame's size, and a click aims at that).
const SETTLE: usize = 6;

fn caps() -> Vec<&'static str> {
    I4D.iter().copied().chain([crate::prompts_view::FEATURE]).collect()
}

fn status(notice: Option<&str>) -> serde_json::Value {
    json!({"version": "0.8.0", "scope": "global", "notice": notice.map(|t| json!({"ids": ["claude:global:pointer"], "version": "0.8.0", "text": t})),
           "targets": [
        {"name": "claude", "title": "Claude Code", "dir": "/home/dev/.claude", "detected": true, "binary": "/usr/bin/claude",
         "reason": "~/.claude exists; `claude` on PATH", "state": "outdated", "note": "", "skills": ["planner", "release"],
         "files": [
            {"id": "claude:global:skill", "kind": "skill", "skill": null, "path": "/home/dev/.claude/skills/lazyagent/SKILL.md",
             "display": "~/.claude/skills/lazyagent/SKILL.md", "state": "outdated", "version": "0.7.0", "outside": null, "note": ""},
            {"id": "claude:global:pointer", "kind": "pointer", "skill": null, "path": "/home/dev/.claude/CLAUDE.md",
             "display": "~/.claude/CLAUDE.md", "state": "modified", "version": "0.7.0", "outside": "/home/dev/dotfiles/CLAUDE.md",
             "note": "the block was edited by hand"}]},
        {"name": "codex", "title": "Codex", "dir": "/home/dev/.codex", "detected": false, "binary": null,
         "reason": "no ~/.codex; `codex` not on PATH", "state": "absent", "note": "", "skills": [], "files": []},
        {"name": "gemini", "title": "Gemini CLI", "dir": "/home/dev/.gemini", "detected": true, "binary": null,
         "reason": "~/.gemini exists; `gemini` not on PATH", "state": "absent", "note": "", "skills": [], "files": []}]})
}

const POINTER_DIFF: &str = "--- ~/.claude/CLAUDE.md\n+++ ~/.claude/CLAUDE.md\n@@ -3,6 +3,6 @@\n Be brief.\n \n-<!-- lazyagent:begin v0.7.0 h=84795642b455 -->\n+<!-- lazyagent:begin v0.8.0 h=50cdf75ed1ed -->\n ## lazyagent\n \n-When lazyagent's MCP tools are available, read the lazyagent skill (my edit).\n+When lazyagent's MCP tools are available (server `lazyagent_app` or `lazyagent`), follow the lazyagent skill for status reports, decision briefs, session notes and working with an orchestrator: ~/.claude/skills/lazyagent/SKILL.md\n <!-- lazyagent:end -->\n";

fn plan() -> serde_json::Value {
    json!({"version": "0.8.0", "action": "install", "changes": [
        {"id": "claude:global:skill", "target": "claude", "scope": "global", "kind": "skill", "skill": null,
         "path": "/home/dev/.claude/skills/lazyagent/SKILL.md", "real_path": "/home/dev/.claude/skills/lazyagent/SKILL.md",
         "display": "~/.claude/skills/lazyagent/SKILL.md", "state": "installed", "action": "none", "outside": null,
         "version": "0.8.0", "note": "", "needs_force": false, "base_sha": "aaa", "new_sha": "aaa", "diff": ""},
        {"id": "claude:global:pointer", "target": "claude", "scope": "global", "kind": "pointer", "skill": null,
         "path": "/home/dev/.claude/CLAUDE.md", "real_path": "/home/dev/dotfiles/CLAUDE.md", "display": "~/.claude/CLAUDE.md",
         "state": "modified", "action": "update", "outside": "/home/dev/dotfiles/CLAUDE.md", "version": "0.7.0",
         "note": "the block was edited by hand", "needs_force": true, "base_sha": "b1", "new_sha": "b2", "diff": POINTER_DIFF},
        {"id": "gemini:global:skill", "target": "gemini", "scope": "global", "kind": "skill", "skill": null,
         "path": "/home/dev/.gemini/skills/lazyagent/SKILL.md", "real_path": "/home/dev/.gemini/skills/lazyagent/SKILL.md",
         "display": "~/.gemini/skills/lazyagent/SKILL.md", "state": "absent", "action": "create", "outside": null,
         "version": null, "note": "", "needs_force": false, "base_sha": "absent", "new_sha": "c2",
         "diff": "--- /dev/null\n+++ ~/.gemini/skills/lazyagent/SKILL.md\n@@ -0,0 +1,3 @@\n+---\n+name: lazyagent\n+---\n"}]})
}

/// The engine: status, plan, and prompts_apply answered by `apply`.
fn prompts_engine(
    h: &Harness<'static, App>,
    mut apply: impl FnMut(&serde_json::Value) -> Result<serde_json::Value, String> + Send + 'static,
) -> Calls {
    let calls: Calls = Default::default();
    let seen = calls.clone();
    *h.state().state.conn.fake.lock().unwrap() = Some(Box::new(move |method, params| {
        seen.lock().unwrap().push((method.to_string(), params.clone()));
        Some(match method {
            "prompts_status" => Ok(status(None)),
            "prompts_plan" => Ok(plan()),
            "prompts_apply" => apply(params)
                .map_err(|message| ClientError::Rpc(RpcError { code: "VALIDATION_ERROR".into(), message, data: None })),
            _ => Ok(json!({})),
        })
    }));
    calls
}

fn open_from_the_menu(h: &mut Harness<'static, App>) {
    h.get_by_label("Agents").click();
    h.run_steps(2);
    h.get_by_label("Prompts…").click();
    h.run_steps(3);
}

#[test]
fn the_agents_menu_opens_the_prompts_page_with_each_targets_files() {
    let (app, _tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = prompts_engine(&h, |_| Ok(json!({})));
    h.run_steps(2);
    open_from_the_menu(&mut h);
    assert!(h.state().state.prompts.open);
    assert_eq!(calls_of(&calls, "prompts_status"), vec![json!({"scope": "global"})]);
    for label in [
        "Agent prompts",
        "Claude Code",
        "Codex",
        "Gemini CLI",
        "~/.claude/skills/lazyagent/SKILL.md  v0.7.0",
        "found: ~/.claude exists; `claude` on PATH",
        "not found: no ~/.codex; `codex` not on PATH",
        "Review install…",
        "Review uninstall…",
    ] {
        h.get_by_label(label);
    }
    h.get_by_label_contains("symlink to /home/dev/dotfiles/CLAUDE.md");
    // The repo's files instead: asked again with scope repo.
    h.get_by_label_contains("This repo's files").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "prompts_status").last().unwrap(), &json!({"scope": "repo"}));
    h.key_press(egui::Key::Escape);
    h.run_steps(2);
    assert!(!h.state().state.prompts.open);
    assert!(calls_of(&calls, "prompts_apply").is_empty(), "nothing written by opening or closing");
}

#[test]
fn without_the_feature_the_menu_item_is_disabled() {
    let (app, _tx) = engine_home(I4D);
    let mut h = harness(app);
    let calls = prompts_engine(&h, |_| Ok(json!({})));
    h.run_steps(2);
    h.get_by_label("Agents").click();
    h.run_steps(2);
    h.get_by_label("Prompts…").click();
    h.run_steps(2);
    assert!(!h.state().state.prompts.open);
    h.state_mut().state.fetch_prompts_notice();
    h.run_steps(2);
    assert!(calls.lock().unwrap().is_empty(), "an older engine is never asked");
}

#[test]
fn the_review_approves_and_skips_file_by_file_with_the_plans_shas() {
    let (app, _tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = prompts_engine(&h, |p| Ok(json!({"id": p["id"], "written": true})));
    h.run_steps(2);
    open_from_the_menu(&mut h);
    h.get_by_label("planner").click();
    h.run_steps(2);
    h.get_by_label("Review install…").click();
    h.run_steps(SETTLE);
    assert_eq!(
        calls_of(&calls, "prompts_plan"),
        vec![json!({"action": "install", "scope": "global", "skills": ["planner"]})]
    );
    // The installed file is left out: two to review.
    h.get_by_label("Install · file 1 of 2");
    h.get_by_label("update · pointer · now modified (v0.7.0)");
    h.get_by_label_contains("It resolves outside its dir (a symlink): approving writes /home/dev/dotfiles/CLAUDE.md");
    h.get_by_label_contains("the block was edited by hand: approving replaces your edits");
    h.get_by_label("-When lazyagent's MCP tools are available, read the lazyagent skill (my edit).");
    h.get_by_label("Approve: replace my edits").click();
    h.run_steps(SETTLE);
    assert_eq!(
        calls_of(&calls, "prompts_apply"),
        vec![json!({"id": "claude:global:pointer", "action": "install", "base_sha": "b1", "new_sha": "b2",
                    "follow_symlinks": true, "force": true})]
    );
    h.get_by_label("Install · file 2 of 2");
    h.get_by_label("+name: lazyagent");
    h.get_by_label("Skip").click();
    h.run_steps(SETTLE);
    assert_eq!(calls_of(&calls, "prompts_apply").len(), 1, "a skipped file is never sent");
    h.get_by_label("1 written, 1 skipped, 0 refused");
    h.get_by_label("Back to the list").click();
    h.run_steps(3);
    assert!(h.state().state.prompts.review.is_none());
    assert_eq!(calls_of(&calls, "prompts_status").len(), 2, "the list is read again");
}

#[test]
fn a_file_changed_since_the_preview_is_refused_and_can_be_previewed_again() {
    let (app, _tx) = engine_home(&caps());
    let mut h = harness(app);
    let calls = prompts_engine(&h, |_| Err("~/.claude/CLAUDE.md changed since the preview: preview it again".into()));
    h.run_steps(2);
    open_from_the_menu(&mut h);
    h.get_by_label("Review install…").click();
    h.run_steps(SETTLE);
    h.get_by_label("Approve: replace my edits").click();
    h.run_steps(SETTLE);
    h.get_by_label("Refused: ~/.claude/CLAUDE.md changed since the preview: preview it again");
    h.get_by_label("Install · file 1 of 2");
    h.get_by_label("Preview again").click();
    h.run_steps(3);
    assert_eq!(calls_of(&calls, "prompts_plan").len(), 2);
    let r = h.state().state.prompts.review.as_ref().unwrap();
    assert_eq!((r.at, r.outcomes.iter().flatten().count()), (0, 0), "a fresh review");
}

#[test]
fn the_outdated_notice_shows_on_the_menu_bar_and_opens_the_page() {
    let (app, _tx) = engine_home(&caps());
    let mut h = harness(app);
    let text = "Your lazyagent prompts are outdated (v0.7.0 → v0.8.0): lazyagent prompts install";
    let calls = fake_engine(&h, move |m, _| (m == "prompts_status").then(|| status(Some(text))));
    h.run_steps(2);
    assert!(h.query_by_label("Prompts outdated").is_none());
    h.state_mut().state.fetch_prompts_notice();
    h.run_steps(3);
    assert_eq!(h.state().state.prompts.notice.as_deref(), Some(text));
    h.get_by_label("Prompts outdated").click();
    h.run_steps(3);
    assert!(h.state().state.prompts.open);
    assert!(h.state().state.prompts.notice.is_none(), "seen: it goes until the next connection");
    assert_eq!(calls_of(&calls, "prompts_status").len(), 2);
}

/// The list and a file's diff, for docs/guides/agent-prompts.md.
#[test]
fn prompts_screenshots() {
    let (app, _tx) = engine_home(&caps());
    let mut h =
        Harness::builder().with_size([1400.0, 880.0]).wgpu().build_ui_state(|ui, app: &mut App| app.draw(ui), app);
    prompts_engine(&h, |p| Ok(json!({"id": p["id"], "written": true})));
    h.run_steps(2);
    h.state_mut().state.open_prompts();
    h.run_steps(4);
    let out = std::env::temp_dir();
    let shot = |h: &mut Harness<'static, App>, name: &str| match h.render() {
        Ok(image) => image.save(out.join(format!("lazyagent-desktop-{name}.png"))).unwrap(),
        Err(e) if std::env::var("LAZYAGENT_REQUIRE_GPU").as_deref() == Ok("1") => panic!("{e}"),
        Err(_) => {}
    };
    shot(&mut h, "prompts-list");
    h.state_mut().state.prompts_review("install");
    h.run_steps(4);
    shot(&mut h, "prompts-diff");
}
