"""``lazyagent prompts``: install lazyagent's guidance into your agent prompts.

- ``status``: per target (claude, codex, gemini), whether it is there and
  each file's state: installed, outdated, modified or absent.
- ``diff``: what ``install`` (or, with ``--uninstall``, ``uninstall``) would
  change, as unified diffs. Writes nothing.
- ``install`` / ``uninstall``: one file at a time, each diff shown and
  approved (y / n / q) before it is written; ``--dry-run`` only shows them,
  ``--yes`` approves them all (a hand-edited block only with ``--force``, a
  symlink out of its dir only with ``--follow-symlinks``).

Targets default to the CLIs whose config dir exists; ``--scope repo``
writes the repo's own ``CLAUDE.md`` / ``AGENTS.md`` / ``GEMINI.md`` and skills
instead of your global ones.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from lazyagent import prompts
from lazyagent.prompts import Change, ChangedError, Env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazyagent prompts",
        description="Install lazyagent's usage guidance (a lazyagent skill and a short pointer "
        "in your global instructions file) into your agent CLIs' prompts. Nothing is written "
        "without your approval of each file's diff.",
    )
    commands = parser.add_subparsers(dest="command", metavar="{install,status,uninstall,diff}")

    def command(name: str, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help, description=help)
        sub.add_argument("--target", default=None,
                         help="claude, codex, gemini, a comma list, or all "
                         "(default: the CLIs whose config dir exists)")
        sub.add_argument("--scope", choices=prompts.SCOPES, default="global",
                         help="global (default: your ~/.claude etc.) or repo (the repo's own "
                         "CLAUDE.md / AGENTS.md / GEMINI.md and skills dir)")
        sub.add_argument("--repo", default=None,
                         help="The repo for --scope repo (default: auto-detect from cwd)")
        return sub

    status = command("status", "Each target's files: installed, outdated, modified or absent")
    status.add_argument("--json", action="store_true", help="Print the status as JSON")
    diff = command("diff", "Show what install would change (writes nothing)")
    diff.add_argument("--skills", default="", help="Also add a line to these skills of yours (a,b)")
    diff.add_argument("--uninstall", action="store_true", help="Show what uninstall would change")
    for name, help in (("install", "Install or update, approving each file's diff"),
                       ("uninstall", "Remove lazyagent's blocks and skill, approving each file")):
        sub = command(name, help)
        if name == "install":
            sub.add_argument("--skills", default="",
                             help="Also add a one-line pointer to these skills of yours (a,b)")
        sub.add_argument("--dry-run", action="store_true", help="Show the diffs; write nothing")
        sub.add_argument("--yes", action="store_true", help="Approve every file without asking")
        sub.add_argument("--force", action="store_true",
                         help="With --yes: also replace or remove blocks edited by hand")
        sub.add_argument("--follow-symlinks", action="store_true",
                         help="With --yes: also write files that resolve outside their dir")
    return parser


class _Out:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.color = stream.isatty()

    def line(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def diff(self, text: str) -> None:
        for line in text.splitlines():
            if self.color and line.startswith(("+++", "---")):
                line = f"\033[1m{line}\033[0m"
            elif self.color and line.startswith("+"):
                line = f"\033[32m{line}\033[0m"
            elif self.color and line.startswith("-"):
                line = f"\033[31m{line}\033[0m"
            elif self.color and line.startswith("@@"):
                line = f"\033[36m{line}\033[0m"
            self.line(line)


def _env(args: argparse.Namespace) -> Env:
    repo = args.repo
    if args.scope == "repo" and repo is None:
        from lazyagent.engine import launcher
        from lazyagent.worktree_manager import WorktreeManagerError

        try:
            repo = launcher.resolve_repo_root(None)
        except WorktreeManagerError as e:
            raise ValueError(f"--scope repo: {e}") from None
    return Env.process(Path(repo).resolve() if repo else None)


def _targets(args: argparse.Namespace, env: Env) -> list[str]:
    names = prompts.resolve_targets(env, args.target)
    if not names:
        raise ValueError("no agent CLI found (no ~/.claude, ~/.codex or ~/.gemini): "
                         "pass --target claude|codex|gemini")
    return names


def _skills(args: argparse.Namespace) -> list[str]:
    return [s for s in (getattr(args, "skills", "") or "").split(",") if s.strip()]


def _header(c: Change) -> str:
    was = f" (v{c.version})" if c.version else ""
    return f"{c.display}: {c.action} · {c.kind.replace('_', ' ')} · now {c.state}{was}"


def _warnings(c: Change) -> list[str]:
    out = []
    if c.outside:
        out.append(f"! it resolves outside its dir (a symlink): the write goes to {c.outside}")
    if c.needs_force:
        out.append(f"! {c.note or 'edited by hand'}: approving replaces your edits (a backup is kept)")
    return out


def cmd_status(args: argparse.Namespace, env: Env, out: _Out) -> int:
    result = prompts.status(env, args.target or "all", args.scope)
    if args.json:
        out.line(json.dumps(result))
        return 0
    out.line(f"lazyagent {result['version']} · scope {args.scope}")
    for t in result["targets"]:
        found = "found" if t["detected"] else "not found"
        out.line(f"{t['name']:7} {t['state']:9} {t['title']} ({found}: {t['reason']})")
        for f in t["files"]:
            was = f" v{f['version']}" if f["version"] else ""
            extra = f" · {f['note']}" if f["note"] else ""
            if f["outside"]:
                extra += f" · symlink to {f['outside']}"
            out.line(f"          {f['state']:9} {f['display']}{was}{extra}")
        if t["note"]:
            out.line(f"          {t['note']}")
    notice = prompts.notice(env)
    if notice:
        out.line(notice["text"])
    return 0


def cmd_diff(args: argparse.Namespace, env: Env, out: _Out) -> int:
    action = "uninstall" if args.uninstall else "install"
    changes = prompts.plan(env, action, _targets(args, env), args.scope, _skills(args))
    pending = [c for c in changes if c.action != "none"]
    for c in pending:
        out.line(_header(c))
        for w in _warnings(c):
            out.line(w)
        if c.action == "blocked":
            out.line(f"! not touched: {c.note}")
        else:
            out.diff(c.diff())
        out.line()
    if not pending:
        out.line("Nothing to change.")
    return 0


def cmd_apply(
    args: argparse.Namespace,
    env: Env,
    out: _Out,
    ask: Callable[[str], str],
    interactive: bool,
) -> int:
    action = args.command
    changes = prompts.plan(env, action, _targets(args, env), args.scope, _skills(args))
    pending = [c for c in changes if c.action != "none"]
    if not pending:
        out.line("Nothing to change: " + ("already installed." if action == "install" else "nothing installed."))
        return 0
    if not (args.dry_run or args.yes or interactive):
        raise ValueError("approving each file needs a terminal: run it in one, or pass "
                         "--dry-run (show) or --yes (approve all)")
    stamp = prompts.new_stamp()
    wrote: list[Change] = []
    skipped = failed = 0
    for n, c in enumerate(pending, 1):
        out.line(f"[{n}/{len(pending)}] {_header(c)}")
        for w in _warnings(c):
            out.line(w)
        if c.action == "blocked":
            out.line(f"! not touched: {c.note}")
            skipped += 1
            out.line()
            continue
        out.diff(c.diff())
        if args.dry_run:
            out.line()
            continue
        follow, force = bool(c.outside), c.needs_force
        if args.yes:
            if c.outside and not args.follow_symlinks:
                out.line("skipped: it resolves outside its dir (pass --follow-symlinks)")
                skipped += 1
                out.line()
                continue
            if c.needs_force and not args.force:
                out.line("skipped: edited by hand (pass --force to replace it)")
                skipped += 1
                out.line()
                continue
        else:
            verb = "Delete" if c.action == "remove" else "Write"
            answer = ask(f"{verb} {c.display}? [y]es / [n]o, skip / [q]uit: ").strip().lower()
            if answer in ("q", "quit"):
                out.line("Stopped; nothing more written.")
                break
            if answer not in ("y", "yes"):
                skipped += 1
                out.line("skipped")
                out.line()
                continue
        try:
            result = prompts.apply(env, c.id, action, c.base_sha, c.new_sha,
                                   follow_symlinks=follow, force=force, stamp=stamp)
        except ChangedError as e:
            out.line(f"refused: {e}")
            failed += 1
        except (ValueError, OSError) as e:
            out.line(f"failed: {e}")
            failed += 1
        else:
            wrote.append(c)
            backup = f" (backup: {result['backup']})" if result.get("backup") else ""
            out.line(("deleted" if c.action == "remove" else "wrote") + f" {c.display}{backup}")
        out.line()
    if args.dry_run:
        out.line(f"Dry run: {len(pending)} file(s) would change; nothing written.")
        return 0
    out.line(f"{len(wrote)} written, {skipped} skipped, {failed} refused. "
             f"Backups and a log: {env.backups_dir()}")
    return 1 if failed else 0


def main(
    argv: list[str] | None = None,
    *,
    env: Env | None = None,
    ask: Callable[[str], str] = input,
    interactive: bool | None = None,
    stream: TextIO | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    out = _Out(stream or sys.stdout)
    try:
        env = env or _env(args)
        if args.command == "status":
            return cmd_status(args, env, out)
        if args.command == "diff":
            return cmd_diff(args, env, out)
        tty = sys.stdin.isatty() and sys.stdout.isatty() if interactive is None else interactive
        return cmd_apply(args, env, out, ask, tty)
    except (ValueError, OSError) as e:
        print(f"lazyagent prompts: {e}", file=sys.stderr, flush=True)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nlazyagent prompts: stopped", file=sys.stderr, flush=True)
        return 130
