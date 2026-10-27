"""Decision briefs: an agent asks the user for a choice that needs reading
(proposal 0013 "Decisions that need context", docs/design/inbox-plan.md §5b,
I4).

A worker calls ``request_decision`` with its token, for its own work item,
with the brief template: one question, 2-4 options with a recommendation,
what it tried, evidence (diff hunks, log lines, files at lines, notes
sections, URLs), reversibility and the impact if wrong, an optional
``class`` and ``blocking``. The engine checks it (:func:`check_brief`):

- text fields are capped and kept as plain text on one line (``tried`` keeps
  its line breaks);
- evidence paths must be inside the worktree (others are dropped); ``sha``
  defaults to the branch head at request time;
- fewer than 2 options, a recommendation that is not one of them, or no
  usable evidence makes it **invalid**: it is still kept, as a Quick item
  "agent <id> asks: <question>" with its terminal one key away (the
  engine's backstop, Q-B34 d).

A valid brief is a Deep item ("To sit down with") unless it says ``class:
"quick"``; a quick one is Round, or Now when ``blocking``. ``blocking``
also sorts it first in its level; while the agent has an open brief, its
plain question item (``question``) is left out: the brief stands for it.

At most ``[inbox] max_briefs_per_agent`` (2) open briefs per agent: a third
is refused with a message (a ``replaces`` brief takes its place instead).
Lifecycle: ``open`` → ``answered`` → ``resolved`` (I5); ``withdrawn`` by the
agent; ``superseded`` by a brief that ``replaces`` it; ``expired`` when its
work item closes. Every brief carries ``holder`` and ``replaces``. Briefs are
kept in the status overlay (overlay.py), closed ones for 30 days.

The orchestrator as first responder (engine/orchestrator.py, I4b) may hold a
new brief before the user sees it (``holder: "orchestrator"``: no inbox
item) and answer or escalate it; it also turns an agent's plain question
(``waiting_for_user``) into a record of its own (``source: "question"``).
An answered brief carries ``answer {choice, label?, comment?, reason?,
presence?, at, delivered_at?}``, ``answered_by`` and ``revertible``.
Auto-delegation (engine/autodelegate.py) writes a brief of its own
(``source: "auto_delegate"``) after two failed attempts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from lazyagent.engine.actionables import DECISION, Item, Snapshot
from lazyagent.engine.notes import _section_bounds
from lazyagent.engine.overlay import one_line
from lazyagent.engine.status import clip_words

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

DECISION_CREATED = "decision.created"
DECISION_RESOLVED = "decision.resolved"

STATES = ("open", "answered", "resolved", "withdrawn", "superseded", "expired")
HOLDERS = ("user", "orchestrator")
CLASSES = ("deep", "quick")
REVERSIBLE = ("yes", "partly", "no")
EVIDENCE_KINDS = ("diff", "log", "file", "notes", "url")

MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_EVIDENCE = 8
QUESTION_CHARS = 200
KEY_CHARS = 8
LABEL_CHARS = 120
CONSEQUENCE_CHARS = 200
TRIED_CHARS = 600
IMPACT_CHARS = 200
REF_CHARS = 200
LINES_CHARS = 40
URL_CHARS = 500
SHA_CHARS = 64

_LINES = re.compile(r"^\d+(-\d+)?$")
_SHA = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _text(value: Any, limit: int) -> str:
    return one_line(value, limit) if isinstance(value, str) else ""


def _block(value: Any, limit: int) -> str:
    """Plain text that keeps its line breaks (``tried``), capped."""
    if not isinstance(value, str):
        return ""
    lines = [" ".join(line.split()) for line in value.strip().splitlines()]
    text = "\n".join(line for line in lines if line)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def inside(worktree_path: str, path: Any) -> str | None:
    """``path`` relative to the worktree, or None when it leaves it."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        return None
    root = os.path.realpath(worktree_path)
    full = os.path.realpath(os.path.join(root, path.strip()))
    if full != root and not full.startswith(root + os.sep):
        return None
    return os.path.relpath(full, root)


def _evidence(
    entry: Any, worktree_path: str, head: str | None, problems: list[str]
) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or entry.get("kind") not in EVIDENCE_KINDS:
        problems.append(f"evidence of unknown kind dropped (kinds: {', '.join(EVIDENCE_KINDS)})")
        return None
    kind = entry["kind"]
    out: dict[str, Any] = {"kind": kind}
    if kind in ("diff", "file"):
        path = inside(worktree_path, entry.get("path"))
        if path is None:
            problems.append(f"{kind} evidence dropped: its path is not inside the worktree")
            return None
        out["path"] = path
        sha = entry.get("sha")
        out["sha"] = sha if isinstance(sha, str) and _SHA.match(sha) else head
        if kind == "diff" and isinstance(entry.get("hunk"), str):
            out["hunk"] = _text(entry["hunk"], REF_CHARS)
    elif kind == "log":
        ref = _text(entry.get("ref"), REF_CHARS)
        if not ref:
            problems.append("log evidence dropped: it needs a ref (e.g. job:tests)")
            return None
        out["ref"] = ref
    elif kind == "notes":
        section = _text(entry.get("section"), 80)
        if not section:
            problems.append("notes evidence dropped: it needs a section")
            return None
        out["section"] = section
    elif kind == "url":
        url = entry.get("url")
        if not isinstance(url, str) or not re.match(r"^https?://\S+$", url) or len(url) > URL_CHARS:
            problems.append("url evidence dropped: it needs an http(s) URL")
            return None
        out["url"] = url
    lines = entry.get("lines")
    if kind in ("log", "file") and isinstance(lines, str) and _LINES.match(lines.strip()):
        out["lines"] = lines.strip()[:LINES_CHARS]
    return out


def check_brief(
    params: dict[str, Any], worktree_path: str, head: str | None
) -> tuple[dict[str, Any], list[str], list[str]]:
    """``(brief, invalid, problems)``: the brief as kept (capped, plain
    text, evidence inside the worktree), why it is not a valid brief (empty
    when it is), and what was dropped or capped. Raises ValueError only
    when there is no question at all."""
    question = _text(params.get("question"), QUESTION_CHARS)
    if not question:
        raise ValueError("question is required: the one thing you need decided")
    invalid: list[str] = []
    problems: list[str] = []

    options: list[dict[str, Any]] = []
    raw = params.get("options")
    for i, option in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(option, str):
            option = {"label": option}
        if not isinstance(option, dict):
            problems.append("an option that is not an object was dropped")
            continue
        label = _text(option.get("label"), LABEL_CHARS)
        key = _text(option.get("key"), KEY_CHARS) or chr(ord("A") + i)
        if not label or any(o["key"] == key for o in options):
            problems.append(f"option {key!r} dropped: no label, or a repeated key")
            continue
        kept = {"key": key, "label": label}
        consequence = _text(option.get("consequence"), CONSEQUENCE_CHARS)
        if consequence:
            kept["consequence"] = consequence
        options.append(kept)
    if len(options) > MAX_OPTIONS:
        problems.append(f"only the first {MAX_OPTIONS} options were kept")
        options = options[:MAX_OPTIONS]
    if len(options) < MIN_OPTIONS:
        invalid.append(f"fewer than {MIN_OPTIONS} options")

    recommendation = params.get("recommendation")
    keys = {o["key"].casefold(): o["key"] for o in options}
    if isinstance(recommendation, str) and recommendation.strip().casefold() in keys:
        recommendation = keys[recommendation.strip().casefold()]
    else:
        recommendation = _text(recommendation, KEY_CHARS) or None
        invalid.append("the recommendation is not one of the options")

    evidence: list[dict[str, Any]] = []
    raw = params.get("evidence")
    for entry in raw if isinstance(raw, list) else []:
        kept = _evidence(entry, worktree_path, head, problems)
        if kept is not None:
            evidence.append(kept)
    if len(evidence) > MAX_EVIDENCE:
        problems.append(f"only the first {MAX_EVIDENCE} pieces of evidence were kept")
        evidence = evidence[:MAX_EVIDENCE]
    if not evidence:
        invalid.append("no evidence")

    reversible = params.get("reversible")
    if reversible not in REVERSIBLE:
        if reversible is not None:
            problems.append(f"reversible must be one of {', '.join(REVERSIBLE)}")
        reversible = None

    brief = {
        "question": question,
        "options": options,
        "recommendation": recommendation,
        "tried": _block(params.get("tried"), TRIED_CHARS),
        "evidence": evidence,
        "reversible": reversible,
        "impact_if_wrong": _text(params.get("impact_if_wrong"), IMPACT_CHARS),
    }
    return brief, invalid, problems


EVIDENCE_LINES = 400
EVIDENCE_CONTEXT = 3


def _span(lines: str | None, total: int) -> tuple[int, int]:
    """``(first, last)`` 1-based lines to show for ``lines`` ("120-134"),
    with a little context, at most :data:`EVIDENCE_LINES`."""
    if not lines:
        return 1, min(total, EVIDENCE_LINES)
    a, _, b = lines.partition("-")
    first, last = int(a), int(b or a)
    if last < first:
        first, last = last, first
    first = max(1, first - EVIDENCE_CONTEXT)
    last = min(total, last + EVIDENCE_CONTEXT, first + EVIDENCE_LINES - 1)
    return first, last


def _numbered(text: str, lines: str | None) -> dict[str, Any]:
    all_lines = text.splitlines()
    first, last = _span(lines, len(all_lines))
    return {"start": first, "text": "\n".join(all_lines[first - 1:last]),
            "total": len(all_lines)}


async def _git_show(path: str, sha: str | None, rel: str) -> str | None:
    args = ["git", "-C", path, "show", f"{sha}:{rel}"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 10.0)
    except (OSError, asyncio.TimeoutError):
        return None
    return out.decode(errors="replace") if proc.returncode == 0 else None


def level_of(record: dict[str, Any]) -> str:
    """Its inbox level: ``deep``, or for a quick one Now when blocking."""
    if record["class"] == "deep":
        return "deep"
    return "now" if record["blocking"] else "round"


class Decisions:
    """The engine's briefs; see the module docstring."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        engine.actionables.add_detector(self.items)
        engine.actionables.filters.append(self._briefed)

    @property
    def store(self) -> dict[str, dict[str, Any]]:
        return self._engine.overlay.decisions

    def changed(self) -> None:
        self._engine.overlay.changed()
        self._engine.status.mark_dirty()

    _changed = changed

    # --- Reads ---

    def get(self, decision_id: str) -> dict[str, Any]:
        record = self.store.get(decision_id)
        if record is None:
            raise ValueError(f"No decision {decision_id!r}")
        return dict(record)

    def list(self, *, key: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        """Briefs, newest first (``key``: one work item; ``state``: one state)."""
        if state is not None and state not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        found = [
            dict(r)
            for r in self.store.values()
            if (key is None or r["key"] == key) and (state is None or r["state"] == state)
        ]
        return sorted(reversed(found), key=lambda r: -r["created_at"])

    def open_for(self, worktree_path: str, agent_id: str) -> list[dict[str, Any]]:
        return [
            r
            for r in self.store.values()
            if r["state"] == "open"
            and (r["worktree_path"], r["agent"]) == (worktree_path, agent_id)
        ]

    async def evidence(self, decision_id: str, index: Any) -> dict[str, Any]:
        """``decision_evidence``: what one piece of evidence points at, as
        text (a file at its lines, at the brief's commit; a log's lines;
        a notes section). A diff hunk is read from ``get_diff``, a URL is
        only a link."""
        record = self.get(decision_id)
        evidence = record["brief"]["evidence"]
        if type(index) is not int or not 0 <= index < len(evidence):
            raise ValueError(f"index must be 0-{len(evidence) - 1}")
        e = evidence[index]
        out: dict[str, Any] = {"id": decision_id, "index": index, "kind": e["kind"]}
        path = record["worktree_path"]
        engine = self._engine
        if e["kind"] == "file":
            text = await _git_show(path, e.get("sha"), e["path"]) if e.get("sha") else None
            if text is None:
                rel = inside(path, e["path"])
                try:
                    with open(os.path.join(path, rel or ""), encoding="utf-8", errors="replace") as f:
                        text = f.read(4_000_000)
                    out["note"] = "as in the worktree now (its commit is not readable)"
                except OSError as err:
                    out["error"] = f"cannot read {e['path']}: {err.strerror or err}"
                    return out
            out.update(_numbered(text, e.get("lines")))
        elif e["kind"] == "log":
            ref = e["ref"]
            agent = ref.removeprefix("agent:") if ref.startswith("agent") else None
            if agent is not None:
                agent = agent if agent and agent != "agent" else record["agent"]
                try:
                    lines, _ = engine.read_agent_output(path, agent, 2000)
                except ValueError:
                    out["error"] = f"agent {agent} is not running: its output is gone"
                    return out
                out.update(_numbered("\n".join(lines), e.get("lines")))
                out["note"] = f"agent {agent}'s terminal now (line numbers count from its tail)"
            elif ref.startswith(("job:", "run:")):
                out.update(await self._ci_log(record, ref, e.get("lines")))
            elif inside(path, ref) is not None and os.path.isfile(os.path.join(path, ref)):
                with open(os.path.join(path, ref), encoding="utf-8", errors="replace") as f:
                    out.update(_numbered(f.read(4_000_000), e.get("lines")))
            else:
                out["error"] = f"{ref} is not a log the engine can read (open it from the PR's checks)"
        elif e["kind"] == "notes":
            store = engine.notes
            notes_id = engine.first_responder._notes_id(record)
            if store is None or notes_id is None or notes_id not in store:
                out["error"] = "the agent's notes are gone"
                return out
            lines = store.get(notes_id).text.splitlines()
            bounds = _section_bounds(lines, e["section"])
            if bounds is None:
                out["error"] = f"no section {e['section']!r} in the agent's notes"
                return out
            body = lines[bounds[0] + 1:bounds[1]]
            out.update(start=1, text="\n".join(body).strip("\n"), total=len(body))
        elif e["kind"] == "diff":
            out["error"] = "read the diff with get_diff (worktree_path, pr: true)"
        else:
            out["url"] = e.get("url")
        return out

    async def _ci_log(self, record: dict[str, Any], ref: str, lines: str | None) -> dict[str, Any]:
        """``job:<name>`` (a failed job of the item's red CI on its head)
        or ``run:<id>``: the tail of its failed log (engine/actions.py)."""
        from lazyagent.engine import actions

        name = ref.partition(":")[2].strip()
        try:
            if ref.startswith("run:"):
                if not name.isdigit():
                    return {"error": f"{ref}: a run is run:<id>"}
                summary = await actions.log_summary(self._engine, run_id=int(name))
                jobs = summary["jobs"]
            else:
                summary = await actions.log_summary(self._engine, key=record["key"])
                fold = name.casefold()
                jobs = [j for j in summary["jobs"] if j["name"].casefold() == fold] or [
                    j for j in summary["jobs"] if fold in j["name"].casefold()
                ]
        except ValueError as err:
            return {"error": str(err)}
        if not jobs or not any(j["lines"] for j in jobs):
            return {"error": summary.get("error")
                    or f"no failed log for {ref} in the red CI run on the head"}
        text = []
        for j in jobs:
            if len(jobs) > 1:
                text.append(f"{j['name']} · {j['step']}")
            text.extend(j["lines"])
        run = summary["runs"][0] if summary["runs"] else {}
        job = jobs[0]
        shown = sum(len(j["lines"]) for j in jobs)
        note = (f"the last {shown} of {sum(j['total'] for j in jobs)} lines of the failed log"
                + (f" ({job['name']} · {job['step']})" if len(jobs) == 1 and job["step"] else "")
                + (f", {run.get('workflow')} run {run.get('id')}" if run else ""))
        return {**_numbered("\n".join(text), lines), "note": note, "url": job["url"] or run.get("url")}

    # --- The agent's calls ---

    def request(
        self,
        params: dict[str, Any],
        *,
        key: str,
        worktree_path: str,
        agent_id: str,
        head: str | None,
        now: float,
        holder: str = "user",
        by: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Keep a brief from ``agent_id`` on ``key``; returns it and the
        message for the agent. Raises ValueError (no question, over the
        limit, a bad ``replaces``). ``by``: the orchestrator writing it for
        ``agent_id`` (a new brief the user asked it for)."""
        if holder not in HOLDERS:
            raise ValueError(f"holder must be one of {', '.join(HOLDERS)}")
        brief, invalid, problems = check_brief(params, worktree_path, head)
        replaces = params.get("replaces")
        mine = self.open_for(worktree_path, agent_id)
        old = None
        if replaces is not None:
            old = next((r for r in mine if r["id"] == replaces), None)
            if old is None:
                raise ValueError(
                    f"replaces must be one of your open briefs ({_ids(mine) or 'none'})"
                )
        limit = self._engine.config.inbox.max_briefs_per_agent
        if old is None and limit and len(mine) >= limit:
            raise ValueError(
                f"You already have {len(mine)} open briefs ({_ids(mine)}), the limit "
                f"per agent: withdraw one with decision_withdraw, replace one "
                "(replaces=<id>), or wait for the user's answer"
            )
        cls = params.get("class")
        if cls is not None and cls not in CLASSES:
            problems.append(f"class must be one of {', '.join(CLASSES)}")
            cls = None
        blocking = params.get("blocking") is True
        overlay = self._engine.overlay
        decision_id = f"d{overlay.next_decision}"
        overlay.next_decision += 1
        record = {
            "id": decision_id,
            "key": key,
            "worktree_path": worktree_path,
            "agent": agent_id,
            "holder": holder,
            "replaces": old["id"] if old is not None else None,
            "state": "open",
            "valid": not invalid,
            "class": "quick" if invalid else (cls or "deep"),
            "blocking": blocking,
            "brief": brief,
            "invalid": invalid,
            "problems": problems,
            "sha": head,
            "created_at": now,
            "closed_at": None,
            "answer": None,
        }
        self.store[decision_id] = record
        actor = by or f"agent:{agent_id}"
        # The user asked for this one (Request a new brief): theirs, not routed.
        rebrief = old is not None and old.get("rebrief", {}).get("state") == "asked"
        if old is not None:
            self.close(old, "superseded", now, actor, f"replaced by {decision_id}")
            self._engine.explainer.rebriefed(old, decision_id, actor)
        if holder == "user" and not rebrief:
            self._engine.first_responder.route(record, now)
        overlay.add(key, "decision", actor, f"asked: {brief['question']}", ref=decision_id)
        self._engine.events.emit(DECISION_CREATED, {"decision": dict(record)})
        self._changed()
        return dict(record), _message(record)

    def _new(self, **fields: Any) -> dict[str, Any]:
        overlay = self._engine.overlay
        decision_id = f"d{overlay.next_decision}"
        overlay.next_decision += 1
        record = {
            "id": decision_id,
            "holder": "user",
            "replaces": None,
            "state": "open",
            "valid": False,
            "blocking": False,
            "invalid": [],
            "problems": [],
            "sha": None,
            "closed_at": None,
            "answer": None,
            **fields,
        }
        self.store[decision_id] = record
        return record

    def question(
        self, *, key: str, worktree_path: str, agent_id: str, detail: str, now: float
    ) -> dict[str, Any]:
        """An agent's plain question (it waits in its terminal), routed to
        the orchestrator: a record of its own, answered with free text."""
        question = f"agent {agent_id} waits for an answer"
        if detail:
            question += f": {detail}"
        record = self._new(
            key=key,
            worktree_path=worktree_path,
            agent=agent_id,
            source="question",
            **{"class": "quick"},
            blocking=True,
            brief={
                "question": one_line(question, QUESTION_CHARS),
                "options": [],
                "recommendation": None,
                "tried": "",
                "evidence": [],
                "reversible": None,
                "impact_if_wrong": "",
            },
            invalid=["a question in the agent's terminal, not a brief"],
            created_at=now,
        )
        self._engine.first_responder.route(record, now)
        self._engine.overlay.add(key, "decision", f"agent:{agent_id}",
                                 f"asked: {record['brief']['question']}", ref=record["id"])
        self._engine.events.emit(DECISION_CREATED, {"decision": dict(record)})
        self._changed()
        return record

    def engine_brief(
        self, *, key: str, worktree_path: str, agent_id: str, question: str, tried: str,
        now: float, item_kind: str | None = None,
    ) -> dict[str, Any]:
        """A brief the engine writes (auto-delegation failed twice): To sit
        down with, the user's. ``item_kind``: the kind of the item it is
        about (it closes once that clears, autodelegate.py)."""
        record = self._new(
            key=key,
            worktree_path=worktree_path,
            agent=agent_id,
            source="auto_delegate",
            **{"class": "deep"},
            brief={
                "question": one_line(question, QUESTION_CHARS),
                "options": [],
                "recommendation": None,
                "tried": _block(tried, TRIED_CHARS),
                "evidence": [],
                "reversible": None,
                "impact_if_wrong": "",
            },
            invalid=["written by the engine"],
            created_at=now,
        )
        if item_kind is not None:
            record["item_kind"] = item_kind
        self._engine.overlay.add(key, "decision", "engine",
                                 f"asked: {record['brief']['question']}", ref=record["id"])
        self._engine.events.emit(DECISION_CREATED, {"decision": dict(record)})
        self._changed()
        return record

    def withdraw(self, decision_id: str, worktree_path: str, agent_id: str, now: float) -> dict[str, Any]:
        record = self.store.get(decision_id)
        if record is None or (record["worktree_path"], record["agent"]) != (worktree_path, agent_id):
            raise ValueError(f"No brief {decision_id!r} of yours")
        if record["state"] != "open":
            raise ValueError(f"Brief {decision_id} is {record['state']}: nothing to withdraw")
        self.close(record, "withdrawn", now, f"agent:{agent_id}", "withdrawn")
        return dict(record)

    # --- The engine's ---

    def expire_item(self, key: str, how: str) -> None:
        """The work item closed: its open briefs expire."""
        now = self._engine.status.clock()
        for record in list(self.store.values()):
            if record["key"] == key and record["state"] == "open":
                self.close(record, "expired", now, "engine", f"expired: {how.replace('_', ' ')}")

    def close(self, record: dict[str, Any], state: str, now: float, actor: str, text: str) -> None:
        record["state"] = state
        record["closed_at"] = now
        if state == "superseded":
            record["superseded_by"] = text.rpartition(" ")[2]
        self._engine.overlay.add(
            record["key"], "decision", actor, f"{text}: {record['brief']['question']}",
            ref=record["id"],
        )
        self._engine.events.emit(DECISION_RESOLVED, {"decision": dict(record)})
        self._changed()

    # --- The inbox ---

    def items(self, snap: Snapshot) -> Iterable[Item]:
        for record in self.store.values():
            # Held by the orchestrator: not the user's (yet).
            if record["state"] != "open" or record.get("holder") == "orchestrator":
                continue
            brief = record["brief"]
            agent = record["agent"]
            question = brief["question"]
            source = record.get("source")
            if source == "auto_delegate":
                label = question
                detail = "auto-delegation failed: see what was tried"
                state = clip_words(question)
            elif source == "question":
                label = f"agent {agent} asks"
                detail = "open terminal"
                state = "answer"
            elif record["valid"]:
                label = question
                reversible = brief["reversible"] or "reversibility not given"
                detail = (
                    f"brief by agent {agent} · {len(brief['options'])} options · {reversible}"
                )
                state = clip_words(f"decide: {question}")
            else:
                label = f"agent {agent} asks: {question}"
                detail = "open terminal"
                state = clip_words(f"asks: {question}")
            escalation = record.get("escalation")
            if escalation is not None:
                if escalation.get("suggestion"):
                    detail = f"orchestrator suggests {escalation['suggestion']}: {escalation['note']}"
                else:
                    detail = f"{escalation['note']} · {detail}"
            elif record.get("capped"):
                detail = f"{detail} · orchestrator capped: {record['capped']}"
            yield Item(
                f"Decision:{record['id']}",
                DECISION.name,
                state,
                label=label,
                detail=detail,
                work_item=record["key"],
                worktree_path=record["worktree_path"],
                agent_id=agent,
                level=level_of(record),
                blocks_agent=record["blocking"],
                decision_id=record["id"],
            )

    def _briefed(self, found: dict[str, Item]) -> None:
        """An agent with an open brief: the brief stands for its question."""
        for record in self.store.values():
            pending = record["state"] == "answered" and not (
                record.get("answer") or {}
            ).get("delivered_at")
            if record["state"] == "open" or pending:
                slot = f"{record['worktree_path']}/{record['agent']}"
                found.pop(f"Question:{slot}", None)
                # The prose question its turn ended with ("see brief d3?").
                found.pop(f"Turn_question:{slot}", None)


def _ids(records: Iterable[dict[str, Any]]) -> str:
    return ", ".join(r["id"] for r in records)


def _message(record: dict[str, Any]) -> str:
    """What the agent is told back."""
    did = record["id"]
    if not record["valid"]:
        return (
            f"Brief {did} is not a full brief ({'; '.join(record['invalid'])}): the user "
            f"sees it as a quick question \"agent {record['agent']} asks: …\" with your "
            "terminal one key away. If the choice needs reading, send a full one "
            f"(2-4 options, a recommendation among them, at least one piece of evidence) "
            f"with replaces=\"{did}\"."
        )
    where = "To sit down with" if record["class"] == "deep" else "inbox"
    text = f"Brief {did} is in the user's {where}."
    if record.get("holder") == "orchestrator":
        text = (
            f"Brief {did} went to the orchestrator first: it answers within its "
            "limits or passes it to the user."
        )
    if record["blocking"]:
        text += f" End your turn now; the answer comes as a line \"Decision {did}: …\"."
    else:
        text += (
            " Keep working on what does not depend on it; the answer comes as a line "
            f"\"Decision {did}: …\" when you are idle. Withdraw it with "
            "decision_withdraw if you no longer need it."
        )
    if record["problems"]:
        text += " Note: " + "; ".join(record["problems"]) + "."
    return text
