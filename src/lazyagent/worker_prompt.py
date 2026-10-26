"""The worker preamble: a few fixed lines every agent lazyagent spawns gets,
so it knows when to report, when to write a decision brief and to keep its
notes (proposal 0013 "How agents learn the tools", docs/design/inbox-plan.md
§7a b).

Claude gets it with ``--append-system-prompt``; a provider with no system
prompt flag (Codex, Gemini) gets it prepended to the instruction, and none
when it starts with no instruction (it would become the first prompt).
``.lazyagent.toml`` ``[agents] worker_preamble`` replaces the text; ``""``
turns it off. The orchestrator has its own prompt (orchestrator_prompt.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lazyagent.agent_providers import AgentProvider
    from lazyagent.config import Config

DEFAULT_WORKER_PREAMBLE = """\
You were started by lazyagent in a git worktree; the user follows many agents \
from one inbox, through the lazyagent_app MCP tools.
- Keep your session notes current (notes_append: Goal, Decisions, Doubts, \
Pending). They come back to you after a compaction or a restore.
- A choice that needs reading (two designs, a trade-off, a risky step): call \
request_decision with one question, 2-4 options and your recommendation, what \
you tried, evidence (diff hunks, log lines, files at lines, notes sections, \
URLs), reversibility and the impact if wrong. Set blocking=true only if you \
cannot go on without it, then end your turn.
- A short question (yes/no, A/B): ask it in the conversation as usual.
- When the ball moves (you wait on CI, a reviewer or QA; or the user must act), \
call status_report with a one-line note; needs_user=true only when the user \
must act.
- If you end your turn while work continues in the background, call \
status_report with ball='agent', a note and until.
- After an action outside the repo (an issue-tracker comment, a PR, a PR \
comment), report it: status_report(outward={kind, ref, text}).
- An answer arrives as "Decision <id>: <choice> ...": follow it. Withdraw a \
brief you no longer need with decision_withdraw.
- CI red in a check lazyagent calls nondeterministic (an eval suite whose pass \
rate varies): never edit the test to pass. Read its recent runs on the base, \
run it on your head (if your instructions define a slot or lock for running \
this suite, request it before each run and release it after), then fix a real \
regression or file ci_attest with the evidence.
"""


def compose_worker_preamble(config: Config) -> str | None:
    """The preamble text, or None when ``worker_preamble = ""`` turns it off."""
    text = config.agent.worker_preamble
    if text is None:
        return DEFAULT_WORKER_PREAMBLE
    return text if text.strip() else None


def worker_system_prompt(
    config: Config, provider: AgentProvider, instruction: str | None
) -> str | None:
    """What a worker is spawned with as its system prompt: the preamble,
    except for a provider that would take it as its first prompt (no system
    prompt flag and no instruction to prepend it to)."""
    preamble = compose_worker_preamble(config)
    if preamble is None:
        return None
    if provider.system_prompt_flag is None and not instruction:
        return None
    return preamble
