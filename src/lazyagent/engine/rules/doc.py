""""How lazyagent works" (proposal 0014, R6): the hand-written concepts
(``concepts.toml``), the English sentence of a rule's condition, the zone a
rule leads to, and the page as Markdown (``lazyagent rules doc``).

The desktop draws the same page from ``rules_catalog`` (its ``concepts``,
and the ``sentence``/``template``/``zone`` of every rule and kind), so the
two never drift: ``docs/guides/how-lazyagent-works.md`` and the desktop's
build-time copy of the catalog are generated here and checked by tests."""

from __future__ import annotations

import json
import tomllib
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from lazyagent.engine.rules.facts import FACTS
from lazyagent.engine.rules.lang import Node, parse

GENERATED = ("overview", "zones", "line", "ball", "states", "kinds", "keys", "rules", "facts")
COLOURS = ("yellow", "orange", "purple", "cyan", "blue", "grey")
KEY_PLACES = ("home", "decision", "page", "why")
ZONES = ("now", "round", "deep", "flight", "parked")
# The doc and the desktop's copy, relative to the repo root.
DOC_PATH = "docs/guides/how-lazyagent-works.md"
COPY_PATH = "desktop/crates/app/assets/rules_catalog.json"
SCREENSHOTS = "desktop/crates/app/assets/how"
# From docs/guides/ to the screenshots.
_SHOT_LINK = "../../" + SCREENSHOTS


# ----------------------------------------------------------------------
# concepts.toml
# ----------------------------------------------------------------------


@lru_cache(maxsize=1)
def concepts() -> dict[str, Any]:
    """The concepts, checked: section ids unique, ``generated`` and colours
    known, every key's place known."""
    text = resources.files("lazyagent.engine.rules").joinpath("concepts.toml").read_text("utf-8")
    data = tomllib.loads(text)
    if data.get("version") != 1:
        raise ValueError("concepts.toml: version must be 1")
    sections = []
    for s in data.get("section", []):
        if s.get("generated") not in (None, *GENERATED):
            raise ValueError(f"concepts.toml: section {s['id']}: unknown generated {s['generated']!r}")
        sections.append({"id": s["id"], "title": s["title"], "intro": s.get("intro", ""),
                         "generated": s.get("generated"), "screenshot": s.get("screenshot"),
                         "caption": s.get("caption", ""), "example": s.get("example", "").strip("\n"),
                         "cards": [{"title": c["title"], "text": c["text"]} for c in s.get("cards", [])]})
    if len({s["id"] for s in sections}) != len(sections):
        raise ValueError("concepts.toml: duplicate section id")
    for group in ("zone", "ball", "colour"):
        for z in data.get(group, []):
            colour = z.get("colour", z["id"] if group == "colour" else None)
            if colour not in COLOURS:
                raise ValueError(f"concepts.toml: {group} {z['id']}: unknown colour {colour!r}")
    if [z["id"] for z in data.get("zone", [])] != list(ZONES):
        raise ValueError(f"concepts.toml: the zones are {', '.join(ZONES)}, in that order")
    for k in data.get("key", []):
        if k["where"] not in KEY_PLACES:
            raise ValueError(f"concepts.toml: key {k['keys']}: unknown where {k['where']!r}")
    line = data.get("line", {})
    return {
        "sections": sections,
        "zones": [{"id": z["id"], "name": z["name"], "colour": z["colour"], "text": z["text"]}
                  for z in data.get("zone", [])],
        "balls": [{"id": b["id"], "colour": b["colour"], "text": b["text"]} for b in data.get("ball", [])],
        "line": {"example": list(line.get("example", [])),
                 "parts": [{"part": p["part"], "text": p["text"]} for p in line.get("parts", [])]},
        "colours": [{"id": c["id"], "text": c["text"]} for c in data.get("colour", [])],
        "keys": [{"keys": k["keys"], "where": k["where"], "does": k["does"]} for k in data.get("key", [])],
    }


# ----------------------------------------------------------------------
# Sentences and templates
# ----------------------------------------------------------------------


def _phrase(name: str) -> str:
    fact = FACTS.get(name)
    if fact is None:
        return name
    # "the first failing check (empty: none)" reads as "the first failing check".
    return fact.phrase.split(" (")[0]


def _lit(n: Node) -> str:
    if n.op == "list":
        values = [_lit(a) for a in n.args]
        return ", ".join(values[:-1]) + " or " + values[-1] if len(values) > 1 else "".join(values)
    if isinstance(n.value, bool):
        return "true" if n.value else "false"
    if isinstance(n.value, str):
        return n.value.replace("_", " ") if n.value else "empty"
    if isinstance(n.value, (int, float)) and n.type is not None and n.type.kind == "duration":
        return _duration(n.value)
    return str(n.value)


def _duration(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def _value(n: Node) -> str:
    if n.op == "fact":
        return _phrase(n.value)
    if n.op in ("lit", "list"):
        return _lit(n)
    return template_of(n)


_NUM = {">": "more than", ">=": "at least", "<": "fewer than", "<=": "at most"}


def _cmp(n: Node) -> str:
    left, right = n.args
    op = n.value
    subject = _value(left)
    if right.op == "lit" and right.value == "" and op in ("==", "!="):
        # "the first failing check" → "there is a / no first failing check";
        # "the agent's report note" → "… is (not) empty".
        if subject.startswith("the ") and "'s " not in subject:
            return f"there is {'no' if op == '==' else 'a'} {subject.removeprefix('the ')}"
        return f"{subject} is {'empty' if op == '==' else 'not empty'}"
    if right.op == "lit" and right.value == 0 and right.type is not None and right.type.kind == "num":
        if op == ">":
            return f"there are {subject.removeprefix('the ')}"
        if op == "==":
            return f"there are no {subject.removeprefix('the ')}"
    if op == "==":
        return f"{subject} is {_value(right)}"
    if op == "!=":
        return f"{subject} is not {_value(right)}"
    if op == "in":
        if left.op == "lit":
            return f"{_value(right)} include {_value(left)}"
        return f"{subject} is {_value(right)}"
    if op == "not in":
        if left.op == "lit":
            return f"{_value(right)} do not include {_value(left)}"
        return f"{subject} is not {_value(right)}"
    if op == "subset":
        return f"{subject} are all in {_value(right)}"
    if left.op == "fact" and FACTS.get(left.value) is not None and str(FACTS[left.value].type) == "duration":
        word = {">": "over", ">=": "at least", "<": "under", "<=": "at most"}[op]
        return f"{subject} is {word} {_value(right)}"
    return f"{subject} is {_NUM.get(op, op)} {_value(right)}"


_NEGATIONS = ((" is on", " is off"), (" is a ", " is no "), (" is ", " is not "), (" are ", " are not "))
_VERBS = ((" holds ", " does not hold "), (" failed", " did not fail"), (" passed ", " did not pass "),
          (" reported ", " did not report "), (" says ", " does not say "))


def negate(clause: str) -> str:
    """A boolean fact's phrase, negated: "the inbox queue is on" → "the
    inbox queue is off", "there is a PR" → "there is no PR"."""
    for old, new in _NEGATIONS:
        if old in clause:
            return clause.replace(old, new, 1)
    for article in ("a ", "an "):
        if clause.startswith(article):
            # "an open inbox item blocks an agent" → "no open inbox item …"
            return "no " + clause.removeprefix(article)
    for old, new in _VERBS:
        if old in clause:
            return clause.replace(old, new, 1)
    return f"not {clause}"


def _sentence(n: Node) -> str:
    if n.op == "and":
        return " and ".join(_sentence(a) for a in n.args)
    if n.op == "or":
        text = " or ".join(_sentence(a) for a in n.args)
        return f"({text})" if n.paren else text
    if n.op == "not":
        inner = n.args[0]
        if inner.op == "fact":
            return negate(_phrase(inner.value))
        return f"not ({_sentence(inner)})"
    if n.op == "cmp":
        return _cmp(n)
    if n.op == "fact":
        return _phrase(n.value)
    if n.op == "call" and n.value in ("known", "unknown", "empty") and n.args:
        word = {"known": "is known", "unknown": "is unknown", "empty": "is empty"}[n.value]
        return f"{_value(n.args[0])} {word}"
    if n.op == "lit" and n.value is True:
        return "always"
    return template_of(n)


def sentence(when: str, and_when: str | None = None) -> str:
    """A condition in English, from the facts' phrases:
    ``pr.state == "open" and ci.state in ["green", "none"]`` → "the PR's
    state is open and CI on the PR is green or none"."""
    text = _sentence(parse(when))
    if and_when:
        text += " and " + _sentence(parse(and_when))
    return text[:1].upper() + text[1:]


def template_of(n: Node) -> str:
    """An output expression as text with its holes: ``with("CI red",
    ci.first_failing)`` → "CI red: ‹the first failing check›"."""
    if n.op == "lit":
        return str(n.value)
    if n.op == "fact":
        return f"‹{_phrase(n.value)}›"
    if n.op == "+":
        parts = []
        for a in n.args:
            text = template_of(a)
            if a.op == "?":
                options = [t for t in text.split(" / ") if t]
                text = "(" + " / ".join(options) + ")"
            parts.append(text)
        return "".join(parts)
    if n.op == "?":
        _, yes, no = n.args
        options = []
        for part in (yes, no):
            for t in template_of(part).split(" / "):
                if t not in options:
                    options.append(t)
        return " / ".join(options)
    if n.op == "call":
        name, args = n.value, n.args
        if name == "with" and len(args) == 2:
            return f"{template_of(args[0])}: {template_of(args[1])}"
        if name == "clip" and args:
            return template_of(args[0])
        if name == "park_text":
            return "parked until ‹when› / parked: ‹reason›"
        if name == "waiting_text":
            return "waiting: ‹reason› / waiting (‹board›)"
        if name == "agents.detail_of":
            return "‹the agent's detail›"
    return f"‹{' '.join(str(n.value or n.op).split())}›"


def template(expr: str) -> str:
    return template_of(parse(expr))


# ----------------------------------------------------------------------
# Where a rule leads
# ----------------------------------------------------------------------


def rule_zone(rule: dict[str, Any]) -> str:
    """now · round · deep · flight · parked · done · inbox (the queue: the
    top item's level)."""
    then = rule["then"]
    if then.get("ball") == "done":
        return "done"
    if rule["id"] == "park":
        return "parked"
    if "level_expr" in then:
        return "inbox"
    if then.get("level") in ("now", "round", "deep"):
        return then["level"]
    return "flight"


def queue_off(rule: dict[str, Any]) -> bool:
    """Rules for an engine with the inbox queue off (the queue's items say
    the same with it on)."""
    return " ".join(rule["when"].split()).startswith("not queue.enabled") or (
        "and not queue.enabled" in rule["when"])


def state_text(rule: dict[str, Any]) -> str:
    then = rule["then"]
    if then.get("state"):
        return then["state"]
    if then.get("state_expr"):
        return template(then["state_expr"])
    if "state" in then.get("inherit", []):
        return "(the state below)"
    return ""


def ball_text(rule: dict[str, Any]) -> str:
    then = rule["then"]
    if then.get("ball"):
        return then["ball"]
    if then.get("ball_expr"):
        return template(then["ball_expr"])
    if "ball" in then.get("inherit", []):
        return "(the ball below)"
    return ""


def annotate(payload: dict[str, Any]) -> dict[str, Any]:
    """Adds ``sentence``, ``zone``, ``state_text``, ``ball_text`` and
    ``queue_off`` to every rule, and ``zone``, ``sentence``, ``state_text``
    to every kind (the page reads them, the desktop draws them)."""
    for r in payload["rules"]:
        try:
            r["sentence"] = sentence(r["when"], r.get("and_when"))
        except Exception:  # noqa: BLE001 - a user rule's odd shape: show its source
            r["sentence"] = r["when"]
        r["zone"] = rule_zone(r)
        r["state_text"] = state_text(r)
        r["ball_text"] = ball_text(r)
        r["queue_off"] = queue_off(r)
    for k in payload["kinds"]:
        k["zone"] = k["level"]
        k["sentence"] = sentence(k["when"]) if k.get("when") else ""
    return payload


# ----------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------


def _cell(text: Any) -> str:
    return "" if text in (None, "") else str(text).replace("|", "\\|").replace("\n", " ")


def _key(text: str) -> str:
    return text.replace("Shift+", "⇧")


def _zone_name(zone: str, payload: dict[str, Any]) -> str:
    names = {z["id"]: z["name"] for z in payload["concepts"]["zones"]}
    return names.get(zone) or {"done": "Done", "inbox": "Inbox (the top item decides)"}[zone]


def _actions(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {a["id"]: a for a in payload["actions"]}


def _action_cell(action: str | None, payload: dict[str, Any]) -> str:
    a = _actions(payload).get(action or "")
    return f"`{_key(a['key'])}` {_cell(a['label'])}" if a else ""


def _states(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    kinds = payload["kinds"]
    rules = [r for r in payload["rules"] if not r["queue_off"]]
    for zone in (*ZONES[:3], "inbox", "flight", "parked", "done"):
        zk = [k for k in kinds if k["zone"] == zone]
        zr = [r for r in rules if r["zone"] == zone]
        if not zk and not zr:
            continue
        out += [f"### {_zone_name(zone, payload)}", "",
                "| verb | state | when | key |", "|---|---|---|---|"]
        for k in zk:
            state = k.get("state_text") or k["title"]
            when = k["sentence"] or f"({k['detector']})"
            out.append(f"| **{_cell(k['verb'])}** | {_cell(state)} | {_cell(when)} | "
                       f"{_action_cell(k['action'], payload)} |")
        for r in zr:
            verb = f"**{_cell(r['verb'])}**" if r.get("verb") else _cell(r["ball_text"])
            out.append(f"| {verb} | {_cell(r['state_text'])} | {_cell(r['sentence'])} | "
                       f"{_action_cell(r.get('action'), payload)} |")
        out.append("")
    out += ["### All rules, in order", "",
            "First match wins. *queue off*: rules for an engine without the inbox queue.", "",
            "| # | rule | ball | state | when |", "|---|---|---|---|---|"]
    for r in payload["rules"]:
        tag = " (locked)" if r["locked"] else ""
        tag += " *queue off*" if r["queue_off"] else ""
        out.append(f"| {r['position'] + 1} | `{r['id']}`{tag} | {_cell(r['ball_text'])} | "
                   f"{_cell(r['state_text'])} | {_cell(r['sentence'])} |")
    return out + [""]


def _kinds(payload: dict[str, Any]) -> list[str]:
    out = ["| verb | item | level | class | blocks an agent | key |", "|---|---|---|---|---|---|"]
    for k in payload["kinds"]:
        out.append(f"| **{_cell(k['verb'])}** | {_cell(k['title'])} | {k['level']} | {k['class']} | "
                   f"{'yes' if k['blocks_agent'] else ''} | {_action_cell(k['action'], payload)} |")
    return out + [""]


def _keys(payload: dict[str, Any]) -> list[str]:
    names = {"home": "Home", "decision": "The decision view", "page": "This page", "why": "The Why? view"}
    out: list[str] = []
    for place in KEY_PLACES:
        keys = [k for k in payload["concepts"]["keys"] if k["where"] == place]
        out += [f"### {names[place]}", "", "| key | does |", "|---|---|"]
        out += [f"| `{k['keys']}` | {_cell(k['does'])} |" for k in keys]
        out.append("")
    out += ["### Actions", "", "| action | key | does |", "|---|---|---|"]
    out += [f"| `{a['id']}` | `{_key(a['key'])}` | {_cell(a['label'])} |" for a in payload["actions"]]
    return out + [""]


def _rules(payload: dict[str, Any]) -> list[str]:
    mine = [r for r in payload["rules"] if r["origin"] != "builtin"]
    out: list[str] = []
    for f in payload["files"]:
        state = "absent" if not f.get("exists") else str(f.get("applied", "")).replace("_", " ")
        out.append(f"- `{f['path']}` ({f['scope']}): {state}, {f.get('rules', 0)} rules")
    if not mine:
        out.append("- No rules of yours: the built-ins apply.")
    for r in mine:
        replaces = " · replaces the built-in" if r.get("replaces") else ""
        out.append(f"- `{r['id']}` ({r['origin']}, {r.get('file')}:{r.get('line')}){replaces}: "
                   f"{r['sentence']} → {r['state_text'] or r['ball_text']}")
    for p in payload["errors"]:
        out.append(f"- error: {p.get('file') or 'rules'}:{p.get('line') or ''} {p['message']} ({p['fallback']})")
    for p in payload["warnings"]:
        out.append(f"- warning: {p.get('file') or 'rules'}:{p.get('line') or ''} {p['message']}")
    return out + [""]


def _facts(payload: dict[str, Any]) -> list[str]:
    out = ["| fact | is | type | source | fresh | cost |", "|---|---|---|---|---|---|"]
    for f in payload["facts"]:
        kind = f["type"] + (f" ({', '.join(f['values'])})" if f["values"] else "")
        out.append(f"| `{f['name']}` | {_cell(f['phrase'])} | {_cell(kind)} | {_cell(f['source'])} | "
                   f"{_cell(f['freshness'])} | {f['cost']} |")
    return out + [""]


def _generated(part: str, payload: dict[str, Any]) -> list[str]:
    c = payload["concepts"]
    if part == "overview":
        return ["```", "you ⇄ desktop (Home) ⇄ engine ⇄ agents", "            ⇅", "       orchestrator",
                "```", ""]
    if part == "zones":
        out = ["| zone | colour | what |", "|---|---|---|"]
        out += [f"| **{z['name']}** | {z['colour']} | {_cell(z['text'])} |" for z in c["zones"]]
        out += ["", "Colours:", ""]
        out += [f"- **{col['id']}**: {col['text']}" for col in c["colours"]]
        return out + [""]
    if part == "line":
        out = ["```", "  ".join(c["line"]["example"]), "```", ""]
        out += [f"- `{p['part']}` {p['text']}" for p in c["line"]["parts"]]
        return out + [""]
    if part == "ball":
        out = ["| ball | colour | means |", "|---|---|---|"]
        out += [f"| **{b['id']}** | {b['colour']} | {_cell(b['text'])} |" for b in c["balls"]]
        return out + [""]
    return {"states": _states, "kinds": _kinds, "keys": _keys, "rules": _rules,
            "facts": _facts}[part](payload)


def page_markdown(payload: dict[str, Any]) -> str:
    """The "How lazyagent works" page as Markdown, from an annotated
    ``rules_catalog`` payload."""
    lines = ["# How lazyagent works", "",
             "<!-- Generated by `lazyagent rules doc --write` from the rules catalog and "
             "src/lazyagent/engine/rules/concepts.toml. Do not edit by hand. -->", "",
             "The same page is in the desktop app: Help → How lazyagent works (`F1`); `?` on Home opens it "
             "at Home.", ""]
    lines += [f"- [{s['title']}](#{_anchor(s['title'])})" for s in payload["concepts"]["sections"]]
    lines.append("")
    for s in payload["concepts"]["sections"]:
        lines += [f"## {s['title']}", "", s["intro"], ""]
        if s["example"]:
            lines += ["```toml", s["example"], "```", ""]
        for card in s["cards"]:
            lines.append(f"- **{card['title']}**: {card['text']}")
        if s["cards"]:
            lines.append("")
        if s["screenshot"]:
            lines += [f"![{s['caption']}]({_SHOT_LINK}/{s['screenshot']}.png)", "", f"*{s['caption']}*", ""]
        if s["generated"]:
            lines += _generated(s["generated"], payload)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _anchor(title: str) -> str:
    keep = "".join(ch for ch in title.lower() if ch.isalnum() or ch in " -")
    return keep.replace(" ", "-")


# ----------------------------------------------------------------------
# The built-in page and the desktop's copy
# ----------------------------------------------------------------------


def builtin_payload() -> dict[str, Any]:
    """``rules_catalog`` for the built-ins alone (no files): what the doc
    and the desktop's copy are made of."""
    from lazyagent.engine.rules import builtin
    from lazyagent.engine.rules.export import catalog_payload

    return catalog_payload(builtin(), {"files": [], "errors": [], "warnings": []})


def copy_json(payload: dict[str, Any] | None = None) -> str:
    return json.dumps(payload or builtin_payload(), indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def write(root: Path) -> list[Path]:
    """Writes the doc and the desktop's copy under the checkout ``root``."""
    payload = builtin_payload()
    out = []
    for rel, text in ((DOC_PATH, page_markdown(payload)), (COPY_PATH, copy_json(payload))):
        path = root / rel
        if not path.parent.is_dir():
            raise FileNotFoundError(f"{path.parent} is not there: run it in a lazyagent checkout")
        path.write_text(text, encoding="utf-8")
        out.append(path)
    return out
