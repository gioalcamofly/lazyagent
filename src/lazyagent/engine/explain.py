"""Explain this and Request a new brief (proposal 0013 "Decisions that need
context", docs/design/inbox-plan.md §5b, I5).

The user, from the decision view, asks for an explanation of a brief
(``decision_explain_request {id, question?, restore?}``) or of any other
inbox item (``{key, ...}``), or for a new brief instead of one
(``decision_rebrief_request {id, note?}``). The engine picks who answers,
first match:

1. **the owner agent is alive**: it gets a fixed prompt through the first
   responder's mailbox (when its turn is over, never mid-turn);
2. **the owner is not alive but restorable**: nothing is done unless the
   request says ``restore: true`` (the view's "Restore agent and explain");
   then the engine restores it (its notes come back with it) and sends the
   same prompt. Never automatic: it starts a process;
3. **the orchestrator is running**: the same prompt, with pointers to the
   item's notes, diff, CI and timeline;
4. **none**: refused (the view disables the button and offers Open terminal
   and Pass to orchestrator).

Only on the user's click; there is no fallback to a bare model call.

The answer to Explain is ``decision_explain {id | key, text}``, accepted
from the owner's token or the orchestrator's (recorded as ``by``), only
while a request is open, and capped at :data:`MAX_LINES` lines. The answer
to a rebrief is a new ``request_decision`` with ``replaces: <id>``; the old
brief closes as ``superseded`` (decisions.py). The orchestrator may write
that brief for the owner's work item when the rebrief went to it.

A request is ``{target, id, key, kind, route, to, agent, worktree_path,
question?, note?, state: asked | answered, asked_at, sent_at?, text?, by?,
answered_at?, truncated?}``; ``decision.explain`` carries it on each change.
A brief's last request is also kept on its record (``explain`` / ``rebrief``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from lazyagent.engine.overlay import one_line
from lazyagent.engine.registry import ORCHESTRATOR_KEY

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine
    from lazyagent.engine.manifest import Entry

log = logging.getLogger(__name__)

DECISION_EXPLAIN = "decision.explain"

KINDS = ("explain", "rebrief")
ROUTES = ("owner", "restore", "orchestrator")
MAX_LINES = 10
LINE_CHARS = 300
QUESTION_CHARS = 300
NOTE_CHARS = 300
# Requests (and their answers) are kept this long.
KEEP = 7 * 86400.0


def cap_lines(text: str) -> tuple[str, bool]:
    """``text`` as at most :data:`MAX_LINES` non-empty plain lines, each
    capped; True when something was cut."""
    lines = [one_line(line, LINE_CHARS) for line in text.strip().splitlines()]
    lines = [line for line in lines if line]
    cut = len(lines) > MAX_LINES or any(len(line) >= LINE_CHARS for line in lines)
    return "\n".join(lines[:MAX_LINES]), cut


class Explainer:
    """The engine's Explain and rebrief requests; see the module docstring."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.requests: dict[str, dict[str, Any]] = {}

    @property
    def _clock(self) -> Any:
        return self._engine.status.clock

    # --- Who answers ---

    def _owner(self, path: str, agent: str | None) -> str | None:
        """The live owner agent's id in ``path``."""
        engine = self._engine
        live = [a for a in engine.agent_ids(path)
                if getattr(engine.get_session(path, a), "running", False)]
        if agent is not None:
            return agent if agent in live else None
        return live[0] if live else None

    def _restorable(self, path: str, agent: str | None) -> Entry | None:
        engine = self._engine
        if engine.manifest is None:
            return None
        for entry in engine._offerable():
            if entry.worktree_path == path and (agent is None or entry.agent_id == agent):
                return entry
        return None

    def route(self, path: str | None, agent: str | None) -> tuple[str | None, Any]:
        """``(route, what)``: ``owner`` and the agent id, ``restore`` and the
        restorable entry, ``orchestrator``, or ``(None, None)``."""
        if path is not None:
            owner = self._owner(path, agent)
            if owner is not None:
                return "owner", owner
            entry = self._restorable(path, agent)
            if entry is not None:
                return "restore", entry
        if self._engine.first_responder.alive():
            return "orchestrator", None
        return None, None

    # --- The user's requests ---

    def _target(self, params: dict[str, Any], kind: str) -> dict[str, Any]:
        """What the request is about: a brief (``id``) or, for Explain, a
        work item (``key``)."""
        engine = self._engine
        decision_id = params.get("id")
        if decision_id is not None:
            if not isinstance(decision_id, str) or not decision_id:
                raise ValueError("id must be a decision id (e.g. d3)")
            record = engine.decisions.store.get(decision_id)
            if record is None:
                raise ValueError(f"No decision {decision_id!r}")
            if kind == "rebrief":
                if record["state"] != "open":
                    raise ValueError(f"Decision {decision_id} is {record['state']}: no new brief")
                if record.get("source") is not None:
                    raise ValueError(f"Decision {decision_id} is not an agent's brief")
            return {
                "target": decision_id, "id": decision_id, "key": record["key"],
                "worktree_path": record["worktree_path"], "agent": record["agent"],
                "record": record,
            }
        if kind == "rebrief":
            raise ValueError("id is required: the brief to replace")
        key = params.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("id or key is required: a decision id, or a work item key")
        path = engine.status.path_of(key)
        if path is None and engine.status.row(key) is None:
            raise ValueError(f"No work item {key!r}")
        agent = None
        if path is not None:
            items = engine.actionables.list()["items"]
            mine = [i for i in items if i.get("work_item") == key and i.get("agent_id")]
            agent = mine[0]["agent_id"] if mine else None
            if agent is not None and agent not in engine.agent_ids(path) \
                    and self._restorable(path, agent) is None:
                agent = None
        return {"target": f"key:{key}", "id": None, "key": key, "worktree_path": path,
                "agent": agent, "record": None}

    async def request(self, params: dict[str, Any], kind: str) -> dict[str, Any]:
        """``decision_explain_request`` / ``decision_rebrief_request``."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        engine = self._engine
        target = self._target(params, kind)
        question = params.get("question")
        if question is not None and not isinstance(question, str):
            raise ValueError("question must be a string")
        note = params.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError("note must be a string")
        restore = params.get("restore", False)
        if type(restore) is not bool:
            raise ValueError("restore must be a boolean")
        path, agent = target["worktree_path"], target["agent"]
        route, what = self.route(path, agent)
        if route is None:
            raise ValueError(
                "Nobody can explain it: its agent is not running and cannot be restored, "
                "and no orchestrator runs. Open its terminal, or pass it to the orchestrator."
            )
        if route == "restore":
            if not restore:
                raise ValueError(
                    f"Its agent {what.agent_id} is not running but can be restored: ask again "
                    "with restore: true (Restore agent and explain). Never automatic."
                )
            result = await engine.restore_agents([what.id])
            if not result["restored"]:
                why = "; ".join(f["reason"] for f in result["failed"]) or "unknown"
                raise ValueError(f"Could not restore agent {what.agent_id}: {why}")
            agent = result["restored"][0]["agent_id"]
        elif route == "owner":
            agent = what
        now = self._clock()
        req: dict[str, Any] = {
            "target": target["target"],
            "id": target["id"],
            "key": target["key"],
            "kind": kind,
            "route": route,
            "to": "orchestrator" if route == "orchestrator" else f"agent:{agent}",
            "agent": agent if route != "orchestrator" else target["agent"],
            "worktree_path": path,
            "state": "asked",
            "asked_at": now,
        }
        if question and question.strip():
            req["question"] = one_line(question, QUESTION_CHARS)
        if note and note.strip():
            req["note"] = one_line(note, NOTE_CHARS)
        slot = (ORCHESTRATOR_KEY, "") if route == "orchestrator" else (path, agent)
        tag = f"{kind}:{target['target']}"
        mailbox = engine.first_responder.mailbox
        mailbox.drop(tag)

        def sent() -> None:
            req["sent_at"] = self._clock()
            self._changed(req)

        mailbox.post(slot, self.prompt(req, target["record"]), tag=tag, on_sent=sent)
        self.requests[f"{kind}:{target['target']}"] = req
        what_text = "explain" if kind == "explain" else "a new brief"
        engine.overlay.add(target["key"], "decision", "user",
                           f"asked {req['to']} for {what_text}", ref=target["id"])
        self._changed(req)
        mailbox.flush()
        return dict(req)

    def prompt(self, req: dict[str, Any], record: dict[str, Any] | None) -> str:
        """The fixed prompt (one line)."""
        if record is not None:
            what = f"brief {record['id']} (\"{record['brief']['question']}\")"
            reply = f'decision_explain {{id: "{record["id"]}", text}}'
        else:
            row = self._engine.status.row(req["key"]) or {}
            state = row.get("state") or ""
            what = f"item {req['key']}" + (f" ({state})" if state else "")
            reply = f'decision_explain {{key: "{req["key"]}", text}}'
        if req["kind"] == "explain":
            text = f"Explain {what} for the user in at most {MAX_LINES} lines; reply with {reply}."
            if req.get("question"):
                text += f" Their question: {req['question']}"
        else:
            text = (
                f"The user asks for a new brief instead of {what}"
                + (f": {req['note']}" if req.get("note") else "")
                + f". Send a new request_decision with replaces: \"{record['id']}\""  # type: ignore[index]
                " (the old one closes as superseded)."
            )
        if req["route"] == "orchestrator":
            where = req["worktree_path"] or "its worktree"
            owner = f"agent {req['agent']}" if req.get("agent") else "its agent"
            text += (
                f" {owner} is not running: read the item's notes (notes_read for {where}), "
                f"diff, CI and timeline (status_list, decision_get) first."
            )
            if req["kind"] == "rebrief":
                text += " Write the brief with your token; it goes to the user."
        return text

    # --- The answers ---

    def open_request(self, kind: str, target: str) -> dict[str, Any] | None:
        req = self.requests.get(f"{kind}:{target}")
        return req if req is not None and req["state"] == "asked" else None

    def explain(self, params: dict[str, Any], role: str, slot: tuple[str, str] | None) -> dict[str, Any]:
        """``decision_explain {id | key, text}`` from the owner's token or
        the orchestrator's."""
        decision_id, key = params.get("id"), params.get("key")
        if isinstance(decision_id, str) and decision_id:
            target = decision_id
        elif isinstance(key, str) and key:
            target = f"key:{key}"
        else:
            raise ValueError("id or key is required: what the user asked you to explain")
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required: the explanation, at most 10 lines")
        if slot is None:
            raise ValueError("decision_explain needs your token (the owner agent's or the orchestrator's)")
        req = self.open_request("explain", target)
        if req is None:
            raise ValueError(
                f"Nobody asked for an explanation of {target.removeprefix('key:')} "
                "(or it was answered): explain only when the user asks"
            )
        if slot[0] == ORCHESTRATOR_KEY:
            by = "orchestrator"
        elif (slot[0], slot[1]) == (req["worktree_path"], req["agent"]):
            by = f"agent:{slot[1]}"
        else:
            raise ValueError("Only the item's own agent or the orchestrator explains it")
        kept, cut = cap_lines(text)
        now = self._clock()
        req.update(state="answered", text=kept, by=by, answered_at=now, truncated=cut)
        self._engine.overlay.add(req["key"], "decision", by, "explained", ref=req["id"])
        self._changed(req)
        result = dict(req)
        if cut:
            result["message"] = f"Kept the first {MAX_LINES} lines (each at most {LINE_CHARS} characters)."
        return result

    def rebriefed(self, old: dict[str, Any], new_id: str, actor: str) -> None:
        """A brief that ``replaces`` ``old`` arrived: its rebrief request is
        answered."""
        req = self.open_request("rebrief", old["id"])
        if req is None:
            return
        req.update(state="answered", by=actor, answered_at=self._clock(), replaced_by=new_id)
        self._changed(req)

    def rebrief_for_orchestrator(self, decision_id: Any) -> dict[str, Any] | None:
        """The brief the orchestrator was asked to write a new one for."""
        if not isinstance(decision_id, str):
            return None
        req = self.open_request("rebrief", decision_id)
        if req is None or req["route"] != "orchestrator":
            return None
        return self._engine.decisions.store.get(decision_id)

    # --- Bookkeeping ---

    def _changed(self, req: dict[str, Any]) -> None:
        engine = self._engine
        if req["id"] is not None:
            record = engine.decisions.store.get(req["id"])
            if record is not None:
                record[req["kind"]] = {k: v for k, v in req.items() if k != "target"}
                engine.overlay.changed()
        engine.events.emit(DECISION_EXPLAIN, {"explain": dict(req)})

    def prune(self, now: float) -> None:
        for name, req in list(self.requests.items()):
            if now - req["asked_at"] > KEEP:
                del self.requests[name]

    def list(self, *, key: str | None = None) -> list[dict[str, Any]]:
        return [dict(r) for r in self.requests.values() if key is None or r["key"] == key]
