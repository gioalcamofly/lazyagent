from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from lazyagent.agent_providers import (
    CROSS_SESSION_INBOUND_VALUES,
    DEFAULT_AGENT_PROVIDER,
    normalize_provider_name,
)
from lazyagent.engine.notes import DEFAULT_SECTIONS as NOTES_SECTIONS
from lazyagent.engine.notes import MODES as NOTES_MODES

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

CONFIG_FILENAME = ".lazyagent.toml"


DEFAULT_BRANCH = "master"


@dataclass
class WorktreeConfig:
    """Custom worktree create/remove command templates."""

    create: str | None = None
    remove: str | None = None


@dataclass
class AgentConfig:
    """Configuration for agent process selection."""

    provider: str = DEFAULT_AGENT_PROVIDER
    # Claude's crossSessionInbound for spawned agents: "accept", "hold" or
    # "refuse". None leaves Claude Code's own setting alone.
    cross_session_inbound: str | None = None
    # [agents] worker_preamble: the fixed lines a worker is spawned with
    # (worker_prompt.py). None: the default text; "": none.
    worker_preamble: str | None = None


@dataclass
class OrchestratorConfig:
    """Configuration for the orchestrator agent."""

    prompt_file: str | None = None
    agent_instruction_template: str | None = None
    # Caps on spawn_agent calls made with an agent token (the orchestrator's
    # included); 0 turns a cap off.
    max_spawns_per_day: int = 30
    max_concurrent_agents: int = 8
    # Pass to orchestrator: how long a claim lasts (seconds).
    claim_ttl_s: int = 7200
    # The done and review items of agents the orchestrator spawned are held
    # for it this long first (minutes; 0: off), then go to the user.
    hold_spawned_ttl_min: int = 60
    # First responder (engine/orchestrator.py): a routed item the
    # orchestrator neither answered nor escalated goes to the user after
    # this long; the user counts as present while a client reported input
    # this recently.
    escalate_after_min: int = 10
    away_after_min: int = 15
    # Caps on the orchestrator's own answers; past one, items go to the
    # user (To sit down with) for the rest of the day.
    max_decisions_per_item_day: int = 3
    max_decisions_day: int = 20
    # Only shorten the mandate (never-alone actions are fixed in code):
    # the actions allowed while the user is away (None: every one) and the
    # outward kinds the orchestrator may do as its instructions say.
    away_allow: list[str] | None = None
    allow_outward: list[str] = field(
        default_factory=lambda: ["tracker", "github_comment", "pr_create"]
    )


@dataclass
class StatusConfig:
    """The work-item projection (``lazyagent status``, proposal 0013)."""

    # A row with nothing but a branch older than this collapses into "stale".
    stale_days: int = 14
    # No agent and no change for this long: "stalled".
    stalled_hours: int = 24
    # The ticket key in a branch name (a regex; its first group, else the
    # whole match) that links a feeder's work item to its branch; None:
    # models.TICKET_PATTERN (ABC-123).
    ticket_pattern: str | None = None
    # Open work items per feeder source (work_items_upsert); 0: no cap.
    max_open_per_source: int = 50
    # GitHub Actions runs (engine/actions.py): one conditional REST call per
    # repo this often, in seconds; 0 turns CI tracking off.
    actions_poll_s: int = 60
    # The open PRs where someone asked you for a review (engine/
    # review_requests.py): one GraphQL search per PR poll; false: off.
    review_requests: bool = True


# Below this, actions_poll_s would spend the rate limit for nothing.
MIN_ACTIONS_POLL_S = 15


@dataclass
class InboxConfig:
    """The inbox (proposal 0013, I4)."""

    # "To sit down with" (deep items): the number shown against it; never
    # enforced by refusing briefs.
    deep_max: int = 5
    # Open decision briefs per agent; a third is refused (0: no limit).
    max_briefs_per_agent: int = 2
    # Opt-in auto-delegation of stalled items (engine/autodelegate.py): the
    # kinds it acts on, to whom (owner or orchestrator), and how many
    # delegations a day.
    auto_delegate: list[str] = field(default_factory=list)
    auto_delegate_target: str = "owner"
    auto_delegate_max_per_day: int = 10
    # Failing checks auto-delegation leaves alone (check, workflow or job
    # names; fnmatch globs): a red made only of these is not delegated.
    auto_delegate_skip_checks: list[str] = field(default_factory=list)
    # The permission mode of an agent auto-delegation spawns when the
    # worktree has none on record (else that agent's mode): bypass.
    auto_delegate_skip_permissions: bool = True


@dataclass
class EngineConfig:
    """How the engine starts."""

    # Bring back, at engine start, the agents the previous engine ran when
    # it ended (the restart manifest); otherwise they are only offered.
    restore_on_start: bool = False


NOTES_MAX_BYTES_RANGE = (1024, 65536)


@dataclass
class NotesConfig:
    """Per-agent session notes (proposal 0011)."""

    enabled: bool = True
    # Who may write: manual, propose or free. Until propose mode lands (N6)
    # "propose" behaves as "manual".
    mode: str = "propose"
    max_bytes: int = 8192
    # How long an orphan (its agent ended without a stop) is kept.
    orphan_days: int = 7
    sections: list[str] = field(default_factory=lambda: list(NOTES_SECTIONS))


@dataclass
class Config:
    """Application configuration loaded from .lazyagent.toml."""

    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    notes: NotesConfig = field(default_factory=NotesConfig)
    status: StatusConfig = field(default_factory=StatusConfig)
    inbox: InboxConfig = field(default_factory=InboxConfig)
    default_branch: str = DEFAULT_BRANCH

    @property
    def has_custom_create(self) -> bool:
        return self.worktree.create is not None

    @property
    def has_custom_remove(self) -> bool:
        return self.worktree.remove is not None


def load_config(repo_root: str | Path) -> Config:
    """Load configuration from .lazyagent.toml in repo root.

    Returns default Config if file is missing or tomllib is unavailable.
    """
    config_path = Path(repo_root) / CONFIG_FILENAME
    if not config_path.exists():
        return Config()

    if tomllib is None:
        return Config()

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    wt_data = data.get("worktree", {})
    worktree_config = WorktreeConfig(
        create=wt_data.get("create"),
        remove=wt_data.get("remove"),
    )
    agent_data = data.get("agent", {})
    provider = normalize_provider_name(agent_data.get("provider"))
    cross_session_inbound = agent_data.get("cross_session_inbound")
    if cross_session_inbound not in CROSS_SESSION_INBOUND_VALUES:
        cross_session_inbound = None
    # [agents] as the plan names it; [agent] (this file's older table) too.
    agents_data = data.get("agents", {})
    worker_preamble = None
    for table in (agents_data, agent_data):
        if isinstance(table, dict) and isinstance(table.get("worker_preamble"), str):
            worker_preamble = table["worker_preamble"]
            break
    orch_data = data.get("orchestrator", {})
    orchestrator_config = OrchestratorConfig(
        prompt_file=orch_data.get("prompt_file"),
        agent_instruction_template=orch_data.get("agent_instruction_template"),
    )
    for name in ("max_spawns_per_day", "max_concurrent_agents"):
        value = orch_data.get(name)
        if type(value) is int and value >= 0:
            setattr(orchestrator_config, name, value)
    claim_ttl_s = orch_data.get("claim_ttl_s")
    if type(claim_ttl_s) is int and 60 <= claim_ttl_s <= 86400:
        orchestrator_config.claim_ttl_s = claim_ttl_s
    _first_responder_config(orchestrator_config, orch_data)
    engine_data = data.get("engine", {})
    engine_config = EngineConfig(
        restore_on_start=engine_data.get("restore_on_start") is True
    )
    default_branch = data.get("default_branch", DEFAULT_BRANCH)
    return Config(
        worktree=worktree_config,
        agent=AgentConfig(
            provider=provider,
            cross_session_inbound=cross_session_inbound,
            worker_preamble=worker_preamble,
        ),
        orchestrator=orchestrator_config,
        engine=engine_config,
        notes=_notes_config(data.get("notes", {})),
        status=_status_config(data.get("status", {})),
        inbox=_inbox_config(data.get("inbox", {})),
        default_branch=default_branch,
    )


def _first_responder_config(config: OrchestratorConfig, data: object) -> None:
    """``[orchestrator]``'s first-responder keys; a missing or invalid value
    keeps its default. The lists can only name known actions / kinds
    (engine/orchestrator.py): unknown names are dropped."""
    from lazyagent.engine.orchestrator import AWAY_ACTIONS, OUTWARD_KINDS

    if not isinstance(data, dict):
        return
    for name in ("escalate_after_min", "away_after_min"):
        value = data.get(name)
        if type(value) is int and value > 0:
            setattr(config, name, value)
    for name in ("max_decisions_per_item_day", "max_decisions_day", "hold_spawned_ttl_min"):
        value = data.get(name)
        if type(value) is int and value >= 0:
            setattr(config, name, value)
    away_allow = data.get("away_allow")
    if isinstance(away_allow, list):
        config.away_allow = [a for a in away_allow if a in AWAY_ACTIONS]
    allow_outward = data.get("allow_outward")
    if isinstance(allow_outward, list):
        config.allow_outward = [k for k in allow_outward if k in OUTWARD_KINDS]


def _inbox_config(data: object) -> InboxConfig:
    """``[inbox]``; a missing or invalid value keeps its default."""
    config = InboxConfig()
    if not isinstance(data, dict):
        return config
    deep_max = data.get("deep_max")
    if type(deep_max) is int and deep_max > 0:
        config.deep_max = deep_max
    briefs = data.get("max_briefs_per_agent")
    if type(briefs) is int and briefs >= 0:
        config.max_briefs_per_agent = briefs
    from lazyagent.engine.autodelegate import KINDS as AUTO_KINDS
    from lazyagent.engine.autodelegate import TARGETS

    auto = data.get("auto_delegate")
    if isinstance(auto, list):
        config.auto_delegate = [k for k in auto if k in AUTO_KINDS]
    target = data.get("auto_delegate_target")
    if target in TARGETS:
        config.auto_delegate_target = target
    per_day = data.get("auto_delegate_max_per_day")
    if type(per_day) is int and per_day >= 0:
        config.auto_delegate_max_per_day = per_day
    bypass = data.get("auto_delegate_skip_permissions")
    if isinstance(bypass, bool):
        config.auto_delegate_skip_permissions = bypass
    skip = data.get("auto_delegate_skip_checks")
    if isinstance(skip, list):
        config.auto_delegate_skip_checks = [
            p.strip() for p in skip if isinstance(p, str) and p.strip()
        ]
    return config


def _status_config(data: object) -> StatusConfig:
    """``[status]``; a missing or invalid value keeps its default."""
    config = StatusConfig()
    if not isinstance(data, dict):
        return config
    for name in ("stale_days", "stalled_hours"):
        value = data.get(name)
        if type(value) is int and value > 0:
            setattr(config, name, value)
    value = data.get("max_open_per_source")
    if type(value) is int and value >= 0:
        config.max_open_per_source = value
    value = data.get("actions_poll_s")
    if type(value) is int and (value == 0 or value >= MIN_ACTIONS_POLL_S):
        config.actions_poll_s = value
    if isinstance(data.get("review_requests"), bool):
        config.review_requests = data["review_requests"]
    pattern = data.get("ticket_pattern")
    if isinstance(pattern, str) and pattern:
        try:
            re.compile(pattern)
            config.ticket_pattern = pattern
        except re.error:
            log.warning("[status] ticket_pattern is not a valid regex: %r", pattern)
    return config


def _notes_config(data: object) -> NotesConfig:
    """``[notes]``; a missing or invalid value keeps its default."""
    config = NotesConfig()
    if not isinstance(data, dict):
        return config
    if isinstance(data.get("enabled"), bool):
        config.enabled = data["enabled"]
    if data.get("mode") in NOTES_MODES:
        config.mode = data["mode"]
    low, high = NOTES_MAX_BYTES_RANGE
    max_bytes = data.get("max_bytes")
    if type(max_bytes) is int and low <= max_bytes <= high:
        config.max_bytes = max_bytes
    orphan_days = data.get("orphan_days")
    if type(orphan_days) is int and orphan_days >= 0:
        config.orphan_days = orphan_days
    sections = data.get("sections")
    if (
        isinstance(sections, list)
        and sections
        and all(isinstance(s, str) and s.strip() and "\n" not in s for s in sections)
    ):
        config.sections = [s.strip() for s in sections]
    return config


def format_command(
    template: str,
    *,
    branch: str = "",
    name: str = "",
    base: str = "",
    path: str = "",
    repo: str = "",
    force: str = "",
    extra: str = "",
) -> str:
    """Expand placeholders and ~ in a command template.

    Placeholders: {branch}, {name}, {base}, {path}, {repo}, {force}, {extra}

    **Security note:** the result is sent verbatim to a shell (``bash -c``
    or pasted into a terminal). Substituted values are NOT shell-quoted —
    template authors are responsible for placement. In particular:

    - Do NOT embed placeholders inside double quotes; a value containing
      ``"`` or ``$`` will break the surrounding quoting and can execute
      arbitrary shell. Single-quote (``'{branch}'``) or use ``{branch}``
      bare and rely on the value not containing whitespace.
    - When values can come from an LLM (via the MCP ``extra`` param or
      a branch name that passed through the orchestrator), prefer
      template designs that don't depend on quoting at all (e.g. pass
      values via env vars: ``BRANCH={branch} ./create-worktree.sh``).

    This is intentional: locking values would break templates that already
    rely on shell expansion (``{path}`` interpolating ``~``, ``{extra}``
    expanding to multiple flags, etc.).
    """
    expanded = os.path.expanduser(template)
    return expanded.format(
        branch=branch,
        name=name,
        base=base,
        path=path,
        repo=repo,
        force=force,
        extra=extra,
    ).strip()


_TABLE = __import__("re").compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _toml_value(value: object) -> str:
    import json

    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(v) for v in value) + "]"
    return json.dumps(value)


def set_table_keys(text: str, table: str, values: dict[str, object]) -> str:
    """``text`` (a TOML file) with ``values`` set in ``[table]``: each key's
    line is replaced in place (a multi-line array too), a missing key goes
    after the table's last key, a missing table at the end. Every other
    line, comments included, stays as it was. Raises ValueError when the
    table is defined some other way (dotted keys, an inline table)."""
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines)
         if (m := _TABLE.match(line)) and m.group(1) == table and not line.lstrip().startswith("[[")),
        None,
    )
    if start is None:
        if tomllib is not None and table in tomllib.loads(text):
            raise ValueError(
                f"[{table}] is not a plain table in {CONFIG_FILENAME}: edit it by hand"
            )
        block = ["", f"[{table}]"] if text.strip() else [f"[{table}]"]
        block += [f"{k} = {_toml_value(v)}" for k, v in values.items()]
        return "\n".join(lines + block) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if _TABLE.match(lines[i])), len(lines))
    pending = dict(values)
    i = start + 1
    last_key = start
    while i < end:
        stripped = lines[i].strip()
        name = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if name and not stripped.startswith("#"):
            span = 1
            rest = stripped.split("=", 1)[1]
            if rest.strip().startswith("[") and rest.count("[") > rest.count("]"):
                depth = rest.count("[") - rest.count("]")
                while depth > 0 and i + span < end:
                    depth += lines[i + span].count("[") - lines[i + span].count("]")
                    span += 1
            if name in pending:
                comment = ""
                if span == 1 and "#" in rest and not rest.strip().startswith(("\"", "'", "[")):
                    comment = "  #" + rest.split("#", 1)[1]
                lines[i : i + span] = [f"{name} = {_toml_value(pending.pop(name))}{comment}"]
                end -= span - 1
                span = 1
            last_key = i + span - 1
            i += span
            continue
        i += 1
    if pending:
        lines[last_key + 1 : last_key + 1] = [f"{k} = {_toml_value(v)}" for k, v in pending.items()]
    return "\n".join(lines) + "\n"


def write_inbox_config(
    repo_root: str | Path, auto_delegate: list[str], auto_delegate_target: str,
    skip_checks: list[str] | None = None,
) -> None:
    """Write ``[inbox] auto_delegate`` / ``auto_delegate_target`` (and
    ``auto_delegate_skip_checks`` when given) in the repo's
    ``.lazyagent.toml`` (created when missing), keeping the rest of the
    file. The result is parsed back before it replaces the file."""
    from lazyagent.engine.notes import _atomic_write

    path = Path(repo_root) / CONFIG_FILENAME
    text = path.read_text() if path.exists() else ""
    keys: dict[str, object] = {
        "auto_delegate": list(auto_delegate), "auto_delegate_target": auto_delegate_target,
    }
    if skip_checks is not None:
        keys["auto_delegate_skip_checks"] = list(skip_checks)
    new = set_table_keys(text, "inbox", keys)
    if tomllib is not None:
        data = tomllib.loads(new)
        inbox = data.get("inbox", {})
        if any(inbox.get(k) != v for k, v in keys.items()):
            raise ValueError(f"could not write [inbox] in {CONFIG_FILENAME} safely")
    _atomic_write(path, new.encode())
