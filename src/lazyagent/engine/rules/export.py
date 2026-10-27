"""The catalog as data (``rules_catalog``), the rules table as Markdown, and
the ``--why`` and ``rules show`` text. The "How lazyagent works" page
(``lazyagent rules doc``) is doc.py's."""

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


def _kind_state(k: Any) -> str:
    from lazyagent.engine.rules.doc import template

    if k.state is None:
        return ""
    return k.state.value if k.state.expr is None else template(k.state.expr.source)


def catalog_payload(catalog: Catalog, check: dict[str, Any]) -> dict[str, Any]:
    from lazyagent.engine.rules.doc import annotate, concepts

    functions = {**FUNCTIONS, **ROW_FUNCTIONS}
    return annotate({
        "facts": facts_payload(),
        "functions": [{"name": f.name, "doc": f.doc} for f in functions.values()],
        "rules": [describe(r) | {"position": i, "then": _out(r)}
                  for i, r in enumerate(catalog.rules)],
        "kinds": [
            {"id": k.id, "level": k.level, "class": "deep" if k.level == "deep" else "quick",
             "title": k.title, "rank": k.rank, "blocks_agent": k.blocks_agent,
             "detector": k.detector, "says": k.says, "verb": k.verb, "action": k.action,
             "delegable": k.can_delegate, "passable": k.can_pass,
             "when": None if k.when is None else " ".join(k.when.source.split()),
             "state_text": _kind_state(k)}
            for k in catalog.kinds
        ],
        "actions": [{"id": a, "key": key, "label": label} for a, (key, label) in ACTIONS.items()],
        "verbs": list(VERBS),
        # The page's hand-written part (concepts.toml): intros, zones, the
        # ball, the colours, the keys.
        "concepts": concepts(),
        # The rule files' [review] tables, merged.
        "review": {"not_human": sorted(catalog.not_human),
                   "generated_paths": list(catalog.generated_paths)},
        # [ci.check] and [ci_unrelated], merged (R4).
        "ci": catalog.ci.to_dict(),
        "files": check["files"],
        "errors": check["errors"],
        "warnings": check["warnings"],
    })


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
    if result.get("answer"):
        out.append(f"  {result['answer']}")
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
    approval = result.get("owner_approval")
    if approval:
        out.append(f"  owner  {approval.get('text') or 'you approved ' + approval['sha'][:7]}")
        out.append(f"         revoke: lazyagent pr approve {result['key']} --revoke")
    ci = result.get("ci")
    if ci:
        out.append("  CI     " + ("unrelated: " + ci["summary"] if ci["unrelated"] is True
                                  else "related" if ci["unrelated"] is False
                                  else "related (some test data unknown)"))
        for c in ci["checks"] + ci["others"]:
            verdict = "unrelated" if c["unrelated"] else "related"
            new = c.get("new") or []
            named = (" (new: " + ", ".join(t.split("::", 1)[-1] for t in new[:5])
                     + (f", +{len(new) - 5} more" if len(new) > 5 else "") + ")") if new else ""
            out.append(f"         {c.get('job') or c['name']}: {verdict}: {c['detail']}{named}")
    item = result.get("item_rule")
    if item:
        out.append(f"  item   from rule {item['id']}  ({_where(item)})")
        out.append(f"         when {item['when']}" + (f" and {item['and_when']}"
                                                       if item.get("and_when") else ""))
        _facts(out, "         facts it read", item["facts"], indent="           ")
        item_misses = [a for a in item.get("above", []) if a["value"] != "false" or a.get("file")]
        for a in item_misses:
            where = f"  ({_where(a)})" if a.get("file") else ""
            out.append(f"         above it: {a['id']:<22} {a['value']}: {a['failed']}{where}")
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
