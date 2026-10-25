"""The catalog as data (``rules_catalog``) and as Markdown (``lazyagent
rules doc``). R6 builds the "How lazyagent works" page on these."""

from __future__ import annotations

from typing import Any

from lazyagent.engine.rules.catalog import ACTIONS, VERBS, Catalog, Rule, describe
from lazyagent.engine.rules.facts import FACTS, FUNCTIONS, ROW_FUNCTIONS


def _out(rule: Rule) -> dict[str, Any]:
    then: dict[str, Any] = {}
    for name, out in rule.outs.items():
        if out.expr is None:
            then[name] = out.value
        else:
            then[f"{name}_expr"] = " ".join(out.expr.source.split())
    if rule.inherit:
        then["inherit"] = list(rule.inherit)
    return then


def facts_payload() -> list[dict[str, Any]]:
    return [
        {
            "name": f.name,
            "type": str(f.type),
            "values": list(f.values),
            "phrase": f.phrase,
            "source": f.source,
            "freshness": f.freshness,
            "cost": f.cost,
            "scopes": [s for s, g in (("row", f.row), ("item", f.item)) if g is not None],
        }
        for f in FACTS.values()
    ]


def catalog_payload(catalog: Catalog, check: dict[str, Any]) -> dict[str, Any]:
    functions = {**FUNCTIONS, **ROW_FUNCTIONS}
    return {
        "facts": facts_payload(),
        "functions": [{"name": f.name, "doc": f.doc} for f in functions.values()],
        "rules": [describe(r) | {"position": i, "then": _out(r)}
                  for i, r in enumerate(catalog.rules)],
        "kinds": [
            {"id": k.id, "level": k.level, "class": "deep" if k.level == "deep" else "quick",
             "title": k.title, "rank": k.rank, "blocks_agent": k.blocks_agent,
             "detector": k.detector, "says": k.says, "verb": k.verb, "action": k.action,
             "when": None if k.when is None else " ".join(k.when.source.split())}
            for k in catalog.kinds
        ],
        "actions": [{"id": a, "key": key, "label": label} for a, (key, label) in ACTIONS.items()],
        "verbs": list(VERBS),
        # The page's hand-written concepts text comes with R6 (concepts.toml).
        "concepts": [],
        "files": check["files"],
        "errors": check["errors"],
        "warnings": check["warnings"],
    }


def _cell(text: Any) -> str:
    return "" if text in (None, "") else str(text).replace("|", "\\|")


def markdown(catalog: Catalog, *, title: str = "lazyagent rules") -> str:
    """The catalog as Markdown: the row rules in order, the inbox kinds,
    the facts and the actions."""
    lines = [f"# {title}", "",
             "Row rules: first match wins, in this order. An unknown fact never matches.", "",
             "| # | id | from | when | ball | level | state | verb |",
             "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(catalog.rules, 1):
        then = _out(r)
        where = r.origin + (" (locked)" if r.locked else "")
        if r.file:
            where += f" {r.file}:{r.line}"
        when = " ".join(r.when.source.split())
        if r.and_when is not None:
            when = f"({when}) and ({' '.join(r.and_when.source.split())})"
        state = then.get("state") or then.get("state_expr") or (
            "(below)" if "state" in r.inherit else "")
        ball = then.get("ball") or then.get("ball_expr") or (
            "(below)" if "ball" in r.inherit else "")
        level = then.get("level") or then.get("level_expr") or ""
        verb = (r.verb or "") + (" · item" if r.item else "")
        lines.append(f"| {i} | `{r.id}` | {_cell(where)} | `{_cell(when)}` | {_cell(ball)} | "
                     f"{_cell(level)} | {_cell(state)} | {_cell(verb)} |")
    lines += ["", "## Inbox kinds", "", "| id | level | title | verb | when |",
              "|---|---|---|---|---|"]
    for k in catalog.kinds:
        when = " ".join(k.when.source.split()) if k.when is not None else f"({k.detector})"
        lines.append(f"| `{k.id}` | {k.level} | {_cell(k.title)} | {_cell(k.verb)} | "
                     f"`{_cell(when)}` |")
    lines += ["", "## Facts", "", "| fact | type | is | source | cost |", "|---|---|---|---|---|"]
    for f in FACTS.values():
        kind = str(f.type) + (f" ({', '.join(f.values)})" if f.values else "")
        lines.append(f"| `{f.name}` | {_cell(kind)} | {_cell(f.phrase)} | {_cell(f.source)} | "
                     f"{f.cost} |")
    lines += ["", "## Actions", "", "| action | key | does |", "|---|---|---|"]
    for a, (key, label) in ACTIONS.items():
        lines.append(f"| `{a}` | {key} | {_cell(label)} |")
    return "\n".join(lines) + "\n"


def _where(rule: dict[str, Any]) -> str:
    """``your global rules.toml, line 12`` · ``built-in``."""
    origin = rule.get("origin")
    if origin == "builtin" or not rule.get("file"):
        return "built-in" + (", locked" if rule.get("locked") else "")
    line = f", line {rule['line']}" if rule.get("line") else ""
    return f"your {origin} rules.toml ({rule['file']}{line})"


def _value(fact: dict[str, Any]) -> str:
    if fact["unknown"]:
        return "unknown"
    value = fact["value"]
    if isinstance(value, list):
        return "[" + ", ".join(map(str, value)) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value) if isinstance(value, str) else str(value)


def format_explain(result: dict[str, Any]) -> str:
    """``rules_explain`` as plain text (``lazyagent status --why``)."""
    rule, then = result.get("rule"), result.get("then") or {}
    out = [f"{result['key']}"]
    if rule is None:
        out.append("  no rule matched (stalled: no rule matched)")
        return "\n".join(out)
    out.append(f"  rule   {rule['id']}  ({_where(rule)})")
    if rule.get("replaces"):
        out.append(f"         replaces the {_where(rule['replaces'] | {'locked': False})} one")
    if rule.get("says"):
        out.append(f"  says   {rule['says']}")
    out.append(f"  when   {rule['when']}" + (f"\n         and {rule['and_when']}"
                                              if rule.get("and_when") else ""))
    shown = [f"ball {then.get('ball')}"]
    if then.get("level"):
        shown.append(f"level {then['level']}")
    shown.append(f"state {then.get('state')!r}")
    if then.get("verb"):
        shown.append(f"verb {then['verb']}")
    out.append("  then   " + " · ".join(shown))
    if result.get("inherited_from"):
        out.append(f"         (inherited from {result['inherited_from']})")
    if result.get("items"):
        out.append("  items  " + "; ".join(
            f"{i['id']} ({i['level']}: {i['state']})" for i in result["items"]))
    _facts(out, "  facts it read", result["facts"])
    item = result.get("item_rule")
    if item:
        out.append(f"  item   from rule {item['id']}  ({_where(item)})")
        out.append(f"         when {item['when']}" + (f" and {item['and_when']}"
                                                       if item.get("and_when") else ""))
        _facts(out, "         facts it read", item["facts"], indent="           ")
    misses = [a for a in result["above"] if a["value"] != "false" or a.get("file")]
    if result["above"]:
        out.append(f"  rules above that did not match ({len(result['above'])}; "
                   "unknown and your rules shown)")
        for a in misses:
            where = f"  ({_where(a)})" if a.get("file") else ""
            out.append(f"    {a['id']:<24} {a['value']}: {a['failed']}{where}")
            if a.get("facts"):
                _facts(out, None, a["facts"], indent="        ")
    return "\n".join(out)


def _facts(out: list[str], title: str | None, facts: list[dict[str, Any]],
           indent: str = "    ") -> None:
    if not facts:
        return
    if title:
        out.append(title)
    width = max(len(f["name"]) for f in facts)
    for f in facts:
        out.append(f"{indent}{f['name']:<{width}} = {_value(f):<24} {f['source']}")


def format_rules(payload: dict[str, Any]) -> str:
    """``lazyagent rules show``: the effective rules in order."""
    out = []
    for r in payload["rules"]:
        tag = "locked" if r["locked"] else r["origin"]
        where = f"  {r['file']}:{r['line']}" if r.get("file") else ""
        when = r["when"] + (f" and ({r['and_when']})" if r.get("and_when") else "")
        if len(when) > 70:
            when = when[:69] + "…"
        extra = " [item]" if r["item"] else ""
        out.append(f"{r['position'] + 1:>3} {r['id']:<24} {tag:<7} {when}{extra}{where}")
    for p in payload["errors"]:
        line = f":{p['line']}" if p.get("line") else ""
        out.append(f"error: {p.get('file') or 'rules'}{line}: {p['message']} ({p['fallback']})")
    return "\n".join(out)
