"""lazyagent prompts: the skill, the pointer block and skill lines, on a fake
HOME (never the real ~/.claude, ~/.codex or ~/.gemini)."""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest

from lazyagent import prompts
from lazyagent.prompts import ChangedError, Env
from lazyagent.prompts import cli as prompts_cli

ORIGINAL = "# My instructions\n\nBe brief.\n"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "fakehome"
    h.mkdir()
    return h


def make_env(home: Path, *, version="0.8.0", repo=None, which=lambda b: None, **environ) -> Env:
    environ.setdefault("XDG_STATE_HOME", str(home / ".state"))
    return Env(home, environ, repo, version, which)


def claude(home: Path, text: str | None = ORIGINAL, skills=("planner",)) -> Path:
    d = home / ".claude"
    d.mkdir(exist_ok=True)
    if text is not None:
        (d / "CLAUDE.md").write_text(text)
    for s in skills:
        (d / "skills" / s).mkdir(parents=True, exist_ok=True)
        (d / "skills" / s / "SKILL.md").write_text(f"---\nname: {s}\n---\nDo {s} things.\n")
    return d


def install_all(env: Env, **kw) -> list[dict]:
    out = []
    for c in prompts.plan(env, "install", **kw):
        if c.action not in ("none", "blocked"):
            out.append(prompts.apply(env, c.id, "install", c.base_sha, c.new_sha))
    return out


def uninstall_all(env: Env, **kw) -> list[dict]:
    out = []
    for c in prompts.plan(env, "uninstall", **kw):
        if c.action not in ("none", "blocked"):
            out.append(prompts.apply(env, c.id, "uninstall", c.base_sha, c.new_sha, force=c.needs_force))
    return out


def run_cli(env: Env, *argv: str, answers=(), interactive=True) -> tuple[int, str]:
    stream = io.StringIO()
    replies = iter(answers)
    code = prompts_cli.main(list(argv), env=env, ask=lambda _q: next(replies),
                            interactive=interactive, stream=stream)
    return code, stream.getvalue()


# --- Targets ------------------------------------------------------------------


@pytest.mark.parametrize("present", [(), ("claude",), ("codex",), ("gemini",),
                                     ("claude", "codex", "gemini"), ("codex", "gemini")])
def test_each_target_missing_or_present(home, present):
    for name in present:
        (home / prompts.TARGETS[name].dir_name).mkdir()
    env = make_env(home)
    assert prompts.resolve_targets(env, None) == list(present)
    changes = prompts.plan(env, "install")
    assert {c.target for c in changes} == set(present)
    status = {t["name"]: t for t in prompts.status(env)["targets"]}
    assert set(status) == {"claude", "codex", "gemini"}
    for name, t in status.items():
        assert t["detected"] == (name in present)
        assert t["state"] == "absent"
    if not present:
        code, _ = run_cli(env, "install", "--yes")
        assert code == 2, "no CLI found: nothing to do, and it says so"
    for name in present:
        install_all(env, targets=[name])
    if present:
        assert all(t["state"] == ("installed" if n in present else "absent") for n, t in
                   ((t["name"], t) for t in prompts.status(env)["targets"]))
    for name in ("claude", "codex", "gemini"):
        spec = prompts.TARGETS[name]
        assert (home / spec.dir_name / spec.instructions).exists() == (name in present)
        assert (home / spec.dir_name / "skills" / "lazyagent" / "SKILL.md").exists() == (name in present)


def test_a_moved_config_dir_and_the_binary_on_path(home, tmp_path):
    moved = tmp_path / "codex-home"
    moved.mkdir()
    env = make_env(home, CODEX_HOME=str(moved), which=lambda b: f"/usr/bin/{b}" if b == "codex" else None)
    info = prompts.target_info(env, "codex")
    assert info["detected"] and info["binary"] == "/usr/bin/codex"
    install_all(env)
    assert (moved / "AGENTS.md").exists()
    assert (moved / "skills" / "lazyagent" / "SKILL.md").exists()
    assert str(moved / "skills" / "lazyagent" / "SKILL.md") in (moved / "AGENTS.md").read_text()
    with pytest.raises(ValueError, match="unknown target"):
        prompts.resolve_targets(env, "cursor")


# --- Install, update, uninstall --------------------------------------------------


def test_install_writes_the_skill_the_pointer_and_a_skill_line_around_your_text(home):
    claude(home)
    env = make_env(home)
    changes = prompts.plan(env, "install", skills=["planner"])
    assert [(c.kind, c.action) for c in changes] == [
        ("skill", "create"), ("pointer", "update"), ("skill_line", "update")]
    install_all(env, skills=["planner"])
    text = (home / ".claude" / "CLAUDE.md").read_text()
    assert text.startswith(ORIGINAL + "\n<!-- lazyagent:begin v0.8.0 h=")
    assert text.endswith("<!-- lazyagent:end -->\n")
    assert "~/.claude/skills/lazyagent/SKILL.md" in text
    skill = (home / ".claude" / "skills" / "lazyagent" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: lazyagent\n")
    assert "<!-- lazyagent:generated v0.8.0 h=" in skill
    line = (home / ".claude" / "skills" / "planner" / "SKILL.md").read_text()
    assert line.startswith("---\nname: planner\n---\nDo planner things.\n\n<!-- lazyagent:begin")
    # Installed: nothing more to do.
    assert all(c.action == "none" for c in prompts.plan(env, "install"))
    assert prompts.status(env, "claude")["targets"][0]["state"] == "installed"
    assert prompts.notice(env) is None


def test_update_across_versions_replaces_only_the_block(home, monkeypatch):
    claude(home)
    real = prompts._template

    def old_template(name):
        return (real(name).replace("follow the lazyagent skill", "read the lazyagent skill")
                .replace("# Working inside lazyagent", "# Inside lazyagent"))

    monkeypatch.setattr(prompts, "_template", old_template)
    old = make_env(home, version="0.7.0")
    install_all(old)
    # The user writes after the block.
    md = home / ".claude" / "CLAUDE.md"
    md.write_text(md.read_text() + "\n## Later notes\nMore.\n")
    monkeypatch.setattr(prompts, "_template", real)
    new = make_env(home, version="0.8.0")
    states = {c.kind: c for c in prompts.plan(new, "install")}
    assert (states["pointer"].state, states["pointer"].version) == ("outdated", "0.7.0")
    assert states["skill"].state == "outdated"
    assert prompts.status(new, "claude")["targets"][0]["state"] == "outdated"
    notice = prompts.notice(new)
    assert notice["text"] == "Your lazyagent prompts are outdated (v0.7.0 → v0.8.0): lazyagent prompts install"
    install_all(new)
    text = md.read_text()
    assert text.startswith(ORIGINAL + "\n<!-- lazyagent:begin v0.8.0 ")
    assert text.endswith("<!-- lazyagent:end -->\n\n## Later notes\nMore.\n")
    assert "read the lazyagent skill" not in text
    assert prompts.notice(new) is None


def test_a_new_version_that_renders_the_same_text_is_no_news(home):
    claude(home)
    install_all(make_env(home, version="0.7.0"))
    env = make_env(home, version="0.8.0")
    assert all(c.state == "installed" and c.action == "none" for c in prompts.plan(env, "install"))
    assert prompts.notice(env) is None


def test_uninstall_restores_your_files_and_removes_what_it_created(home):
    claude(home)
    env = make_env(home)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    install_all(env, skills=["planner"])
    # Uninstall finds the skill line without being told the skill.
    plan = prompts.plan(env, "uninstall")
    assert [(c.kind, c.action) for c in plan] == [
        ("skill", "remove"), ("pointer", "update"), ("skill_line", "update")]
    uninstall_all(env)
    after = {p: p.read_bytes() for p in home.rglob("*") if p.is_file() and ".state" not in p.parts}
    assert after == before
    assert not (home / ".claude" / "skills" / "lazyagent").exists()
    # Backups of every file that existed, and a log line per write.
    log = [json.loads(x) for x in (env.backups_dir() / "log.jsonl").read_text().splitlines()]
    assert len(log) == 6
    assert [e["existed"] for e in log] == [False, True, True, True, True, True]
    first_pointer = next(e for e in log if e["id"] == "claude:global:pointer")
    assert Path(first_pointer["backup"]).read_text() == ORIGINAL
    assert all(c.action == "none" for c in prompts.plan(env, "uninstall"))


def test_a_file_install_created_is_removed_on_uninstall(home):
    (home / ".gemini").mkdir()
    env = make_env(home)
    install_all(env)
    assert (home / ".gemini" / "GEMINI.md").read_text().startswith("<!-- lazyagent:begin")
    uninstall_all(env)
    assert not (home / ".gemini" / "GEMINI.md").exists()
    assert list((home / ".gemini").iterdir()) == []


def test_atomic_write_keeps_the_file_mode(home):
    d = claude(home)
    os.chmod(d / "CLAUDE.md", 0o600)
    env = make_env(home)
    install_all(env)
    assert (d / "CLAUDE.md").stat().st_mode & 0o777 == 0o600
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


# --- Hand edits ----------------------------------------------------------------------


def test_a_hand_edited_block_is_shown_and_needs_force(home):
    claude(home)
    env = make_env(home)
    install_all(env)
    md = home / ".claude" / "CLAUDE.md"
    md.write_text(md.read_text().replace("status reports", "status reports (my edit)"))
    c = prompts.change_for(env, "claude:global:pointer", "install")
    assert (c.state, c.action, c.needs_force) == ("modified", "update", True)
    assert "-When lazyagent" in c.diff() and "(my edit)" in c.diff()
    assert prompts.status(env, "claude")["targets"][0]["state"] == "modified"
    with pytest.raises(ValueError, match="edited by hand"):
        prompts.apply(env, c.id, "install", c.base_sha, c.new_sha)
    # --yes skips it without --force; the file is untouched.
    code, out = run_cli(env, "install", "--yes")
    assert code == 0 and "skipped: edited by hand" in out
    assert "(my edit)" in md.read_text()
    code, out = run_cli(env, "install", "--yes", "--force")
    assert code == 0 and "(my edit)" not in md.read_text()
    assert md.read_text().startswith(ORIGINAL)


def test_broken_markers_are_never_touched(home):
    claude(home, ORIGINAL + "<!-- lazyagent:begin v0.7.0 h=abc -->\nhalf a block\n")
    env = make_env(home)
    c = prompts.change_for(env, "claude:global:pointer", "install")
    assert (c.state, c.action) == ("modified", "blocked")
    assert "do not pair up" in c.note
    with pytest.raises(ValueError, match="pair up"):
        prompts.apply(env, c.id, "install", c.base_sha, c.new_sha, force=True)


def test_a_skill_file_lazyagent_did_not_write(home):
    d = claude(home)
    (d / "skills" / "lazyagent").mkdir(parents=True)
    (d / "skills" / "lazyagent" / "SKILL.md").write_text("my own lazyagent notes\n")
    env = make_env(home)
    c = prompts.change_for(env, "claude:global:skill", "install")
    assert (c.state, c.note, c.needs_force) == ("modified", "not written by lazyagent", True)
    u = prompts.change_for(env, "claude:global:skill", "uninstall")
    assert u.action == "blocked", "uninstall never deletes a file it did not write"


def test_a_hand_edited_skill_file(home):
    claude(home)
    env = make_env(home)
    install_all(env)
    f = home / ".claude" / "skills" / "lazyagent" / "SKILL.md"
    f.write_text(f.read_text() + "\nmy addition\n")
    c = prompts.change_for(env, "claude:global:skill", "install")
    assert (c.state, c.note) == ("modified", "edited by hand")


# --- Preview, approval, symlinks ------------------------------------------------------


def test_a_file_changed_since_the_preview_is_refused(home):
    claude(home)
    env = make_env(home)
    c = prompts.change_for(env, "claude:global:pointer", "install")
    md = home / ".claude" / "CLAUDE.md"
    md.write_text(ORIGINAL + "a line written meanwhile\n")
    with pytest.raises(ChangedError, match="changed since the preview"):
        prompts.apply(env, c.id, "install", c.base_sha, c.new_sha)
    assert md.read_text() == ORIGINAL + "a line written meanwhile\n"
    # A different rendering (lazyagent was upgraded meanwhile) is refused too.
    c = prompts.change_for(env, "claude:global:pointer", "install")
    with pytest.raises(ChangedError, match="what lazyagent would write"):
        prompts.apply(make_env(home, version="0.9.0"), c.id, "install", c.base_sha, c.new_sha)


def test_the_cli_refuses_a_file_changed_while_you_read_its_diff(home):
    claude(home, skills=())
    env = make_env(home)
    md = home / ".claude" / "CLAUDE.md"

    def answers():
        yield "y"  # the skill
        md.write_text("edited elsewhere\n")
        yield "y"  # the pointer, changed meanwhile

    replies = answers()
    stream = io.StringIO()
    code = prompts_cli.main(["install"], env=env, ask=lambda _q: next(replies),
                            interactive=True, stream=stream)
    out = stream.getvalue()
    assert code == 1
    assert "refused: ~/.claude/CLAUDE.md changed since the preview" in out
    assert md.read_text() == "edited elsewhere\n"
    assert "1 written, 0 skipped, 1 refused" in out


def test_a_symlink_out_of_the_target_dir_is_said_and_needs_approval(home, tmp_path):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    (dotfiles / "CLAUDE.md").write_text(ORIGINAL)
    d = claude(home, text=None, skills=())
    (d / "CLAUDE.md").symlink_to(dotfiles / "CLAUDE.md")
    env = make_env(home)
    c = prompts.change_for(env, "claude:global:pointer", "install")
    assert c.outside == str(dotfiles / "CLAUDE.md")
    with pytest.raises(ValueError, match="resolves outside its dir"):
        prompts.apply(env, c.id, "install", c.base_sha, c.new_sha)
    code, out = run_cli(env, "install", "--yes")
    assert "it resolves outside its dir (a symlink): the write goes to" in out
    assert "skipped: it resolves outside its dir (pass --follow-symlinks)" in out
    assert (dotfiles / "CLAUDE.md").read_text() == ORIGINAL
    code, out = run_cli(env, "install", "--yes", "--follow-symlinks")
    assert code == 0
    assert (d / "CLAUDE.md").is_symlink(), "the link stays; its target is written"
    assert "<!-- lazyagent:begin" in (dotfiles / "CLAUDE.md").read_text()
    # Interactive: the warning is shown before the question.
    uninstall_all_follow = prompts.change_for(env, "claude:global:pointer", "uninstall")
    assert uninstall_all_follow.outside


def test_a_symlink_inside_the_target_dir_is_fine(home):
    d = claude(home, text=None, skills=())
    (d / "real.md").write_text(ORIGINAL)
    (d / "CLAUDE.md").symlink_to(d / "real.md")
    env = make_env(home)
    c = prompts.change_for(env, "claude:global:pointer", "install")
    assert c.outside is None
    prompts.apply(env, c.id, "install", c.base_sha, c.new_sha)
    assert "lazyagent:begin" in (d / "real.md").read_text()


def test_a_symlinked_skill_dir_of_yours(home, tmp_path):
    d = claude(home, skills=())
    elsewhere = tmp_path / "shared-skills" / "planner"
    elsewhere.mkdir(parents=True)
    (elsewhere / "SKILL.md").write_text("---\nname: planner\n---\n")
    (d / "skills").mkdir(exist_ok=True)
    (d / "skills" / "planner").symlink_to(elsewhere)
    env = make_env(home)
    c = prompts.change_for(env, "claude:global:skill_line:planner", "install")
    assert c.outside == str(elsewhere / "SKILL.md")


# --- Scope repo, skills -----------------------------------------------------------------


def test_scope_repo_writes_the_repos_files_only(home, tmp_path):
    claude(home)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Repo rules\n")
    env = make_env(home, repo=repo)
    install_all(env, targets=["claude", "codex"], scope="repo")
    assert (home / ".claude" / "CLAUDE.md").read_text() == ORIGINAL
    assert "`.claude/skills/lazyagent/SKILL.md`" not in (repo / "CLAUDE.md").read_text()
    assert ".claude/skills/lazyagent/SKILL.md" in (repo / "CLAUDE.md").read_text()
    assert (repo / ".claude" / "skills" / "lazyagent" / "SKILL.md").exists()
    assert (repo / "AGENTS.md").read_text().startswith("# Repo rules\n\n<!-- lazyagent:begin")
    assert (repo / ".codex" / "skills" / "lazyagent" / "SKILL.md").exists()
    with pytest.raises(ValueError, match="scope repo needs a repo"):
        prompts.plan(make_env(home), "install", scope="repo")


def test_skills_that_do_not_exist_are_an_error(home):
    claude(home)
    env = make_env(home)
    assert prompts.available_skills(env, "claude") == ["planner"]
    with pytest.raises(ValueError, match="no skill named nope"):
        prompts.plan(env, "install", skills=["nope"])
    with pytest.raises(ValueError, match="bad skill name"):
        prompts.plan(env, "install", skills=["../x"])
    code, _ = run_cli(env, "install", "--skills", "nope", "--yes")
    assert code == 2


# --- CLI ------------------------------------------------------------------------------------


def test_cli_dry_run_and_diff_write_nothing(home):
    claude(home)
    env = make_env(home)
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    code, out = run_cli(env, "install", "--dry-run", "--skills", "planner", interactive=False)
    assert code == 0
    assert "+++ ~/.claude/skills/lazyagent/SKILL.md" in out
    assert "Dry run: 3 file(s) would change; nothing written." in out
    code, out = run_cli(env, "diff")
    assert code == 0 and "+++ ~/.claude/CLAUDE.md" in out
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_cli_needs_a_terminal_or_a_flag(home):
    claude(home)
    env = make_env(home)
    code, _ = run_cli(env, "install", interactive=False)
    assert code == 2
    assert not (home / ".claude" / "skills" / "lazyagent").exists()


def test_cli_asks_file_by_file(home):
    claude(home)
    env = make_env(home)
    code, out = run_cli(env, "install", "--skills", "planner", answers=["y", "n", "q"])
    assert code == 0
    assert "[1/3]" in out and "[2/3]" in out and "[3/3]" in out
    assert "Stopped; nothing more written." in out
    assert (home / ".claude" / "skills" / "lazyagent" / "SKILL.md").exists()
    assert (home / ".claude" / "CLAUDE.md").read_text() == ORIGINAL
    assert "lazyagent" not in (home / ".claude" / "skills" / "planner" / "SKILL.md").read_text()


def test_cli_status_and_uninstall(home):
    claude(home)
    env = make_env(home)
    run_cli(env, "install", "--yes")
    code, out = run_cli(env, "status")
    assert code == 0
    assert re.search(r"^claude\s+installed\s+Claude Code \(found", out, re.MULTILINE)
    assert re.search(r"^codex\s+absent", out, re.MULTILINE)
    code, out = run_cli(env, "status", "--json")
    assert json.loads(out)["targets"][0]["state"] == "installed"
    code, out = run_cli(env, "uninstall", "--yes")
    assert code == 0 and "deleted ~/.claude/skills/lazyagent/SKILL.md" in out
    assert (home / ".claude" / "CLAUDE.md").read_text() == ORIGINAL
    code, out = run_cli(env, "uninstall", "--yes")
    assert "Nothing to change: nothing installed." in out


def test_the_lazyagent_command_dispatches_prompts(monkeypatch, capsys):
    from lazyagent import cli

    with pytest.raises(SystemExit) as e:
        cli.main(["prompts"])
    assert e.value.code == 2
    assert "install,status,uninstall,diff" in capsys.readouterr().out


# --- The text ----------------------------------------------------------------------------------


def test_the_skill_names_only_real_tools_and_agrees_with_the_preamble():
    from lazyagent import mcp_server
    from lazyagent.worker_prompt import DEFAULT_WORKER_PREAMBLE

    skill = prompts.render_skill("0.8.0")
    tools = {"status_report", "request_decision", "decision_withdraw", "notes_read", "notes_append",
             "notes_replace_section", "status_list", "orchestrator_hold", "orchestrator_release",
             "decision_escalate", "rules_explain", "rules_catalog", "list_worktrees"}
    for tool in tools:
        assert f"`{tool}" in skill or f"{tool}(" in skill, tool
        assert callable(getattr(mcp_server, tool)), tool
    # What the preamble tells spawned agents, the skill says the same way.
    for phrase in ("ball=\"agent\"", "until", "needs_user=true", "outward=",
                   "Decision <id>: <choice> ...", "blocking=true"):
        assert phrase in skill, phrase
    assert "ball='agent'" in DEFAULT_WORKER_PREAMBLE and "Decision <id>: <choice> ..." in DEFAULT_WORKER_PREAMBLE
    assert len(skill.splitlines()) < 140, "keep it tight"


def test_templates_ship_in_the_package():
    names = {p.name for p in (Path(prompts.__file__).parent / "templates").iterdir()}
    assert names == {"skill.md", "pointer.md", "skill_line.md"}


# --- Engine methods ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_methods_plan_and_apply_and_apply_is_the_users(home, monkeypatch):
    from lazyagent.engine.methods import EngineMethods
    from tests.test_ipc import _make_engine

    claude(home)
    env = make_env(home)
    monkeypatch.setattr(EngineMethods, "_prompts_env", lambda self: env)
    methods = EngineMethods(_make_engine())
    status = await methods.prompts_status({})
    assert status["notice"] is None and status["targets"][0]["skills"] == ["planner"]
    plan = await methods.prompts_plan({"action": "install", "skills": ["planner"]})
    first = plan["changes"][0]
    with pytest.raises(ValueError, match="Unknown caller token"):
        await methods.prompts_apply({"id": first["id"], "action": "install", "base_sha": first["base_sha"],
                                     "new_sha": first["new_sha"], "caller": "tok"})
    with pytest.raises(ValueError, match="base_sha is required"):
        await methods.prompts_apply({"id": first["id"], "action": "install"})
    for c in plan["changes"]:
        out = await methods.prompts_apply({"id": c["id"], "action": "install", "base_sha": c["base_sha"],
                                           "new_sha": c["new_sha"]})
        assert out["written"]
    status = await methods.prompts_status({"targets": ["claude"]})
    assert status["targets"][0]["state"] == "installed"


def test_lazyagent_status_ends_with_the_outdated_line(monkeypatch, capsys, tmp_path):
    from lazyagent.engine import cli as engine_cli

    home = Path(os.environ["HOME"])  # the test's own (conftest)
    home.mkdir()
    claude(home)
    real = prompts._template
    monkeypatch.setattr(prompts, "_template", lambda n: real(n).replace("follow", "read"))
    install_all(make_env(home, version="0.7.0", XDG_STATE_HOME=os.environ["XDG_STATE_HOME"]))
    monkeypatch.setattr(prompts, "_template", real)
    engine_cli._prompts_notice(str(tmp_path))
    out = capsys.readouterr().out
    assert f"Your lazyagent prompts are outdated (v0.7.0 → v{prompts.__version__})" in out
    install_all(Env.process())
    engine_cli._prompts_notice(str(tmp_path))
    assert capsys.readouterr().out == ""


def test_the_i4h_brief_guidance_makes_an_older_skill_outdated_at_the_same_version(home, monkeypatch):
    """The skill now says: a choice for the user is a brief, then end the
    turn. A skill installed before that reads outdated, with no version
    bump: the state compares the text."""
    claude(home)
    real = prompts._template
    assert "write a brief, then end your turn" in real("skill.md")

    def before(name):
        text = real(name)
        if name != "skill.md":
            return text
        start = text.index("tries failed, this branch")
        end = text.index("Only a quick yes/no stays in the conversation.\n") + len(
            "Only a quick yes/no stays in the conversation.\n")
        return text[:start] + ("tries failed. A short yes/no or A/B question goes in the "
                               "conversation instead.\n") + text[end:]

    monkeypatch.setattr(prompts, "_template", before)
    env = make_env(home)
    install_all(env)
    monkeypatch.setattr(prompts, "_template", real)
    states = {c.kind: c.state for c in prompts.plan(env, "install")}
    assert states["skill"] == "outdated"
    assert prompts.status(env, "claude")["targets"][0]["state"] == "outdated"
