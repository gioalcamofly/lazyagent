"""The desktop's Settings page: settings_get / settings_set write [agents]
permission_mode in the global config.toml or the repo's .lazyagent.toml,
keep the rest of the file, and apply it without a restart."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lazyagent.config import global_config_path, load_config, unset_table_key, write_setting
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from tests.engine.fakes import add_session, make_engine, worktree

pytestmark = pytest.mark.asyncio

KEY = "agents.permission_mode"

REPO_TOML = """\
# Team settings
default_branch = "main"

[agents]
# how agents start here
permission_mode = "plan"  # reviewed
worker_preamble = "Hi"

[inbox]
deep_max = 7
"""


def _methods(root):
    engine = make_engine([worktree(str(root))], repo_root=str(root))
    engine.worktree_service.repo_root = str(root)
    engine._config_loader = load_config
    engine.load_config()
    return engine, EngineMethods(engine)


def _setting(result):
    (setting,) = result["settings"]
    assert setting["key"] == KEY
    return setting


async def test_nothing_set_is_the_default(tmp_path):
    _, methods = _methods(tmp_path)
    result = await methods.settings_get({})
    assert result["repo"] == tmp_path.name
    assert result["files"] == {
        "global": str(global_config_path()), "repo": str(tmp_path / ".lazyagent.toml"),
    }
    assert _setting(result) == {
        "key": KEY, "value": "cli", "source": "default", "global": None, "repo": None,
        "default": "cli",
        "choices": ["cli", "bypass", "acceptEdits", "plan", "dontAsk", "auto", "default"],
    }


async def test_the_global_file_is_created_and_applied_at_once(tmp_path):
    engine, methods = _methods(tmp_path)
    assert not global_config_path().exists()
    result = await methods.settings_set({"key": KEY, "scope": "global", "value": "bypass"})
    assert global_config_path().read_text() == '[agents]\npermission_mode = "bypass"\n'
    assert _setting(result) | {"choices": []} == {
        "key": KEY, "value": "bypass", "source": "global", "global": "bypass", "repo": None,
        "default": "cli", "choices": [],
    }
    assert engine.config.agent.permission_mode == "bypass", "no restart"
    assert engine.get_config().permission_mode == "bypass"
    assert not (tmp_path / ".lazyagent.toml").exists(), "the repo's file untouched"


async def test_the_repo_value_wins_and_the_rest_of_the_file_stays(tmp_path):
    (tmp_path / ".lazyagent.toml").write_text(REPO_TOML)
    global_config_path().parent.mkdir(parents=True)
    global_config_path().write_text("# mine\n[agents]\npermission_mode = \"acceptEdits\"\n")
    engine, methods = _methods(tmp_path)
    assert _setting(await methods.settings_get({}))["source"] == "repo"

    result = await methods.settings_set({"key": KEY, "scope": "repo", "value": "dontAsk"})
    setting = _setting(result)
    assert (setting["value"], setting["source"]) == ("dontAsk", "repo")
    assert setting["global"] == "acceptEdits"
    assert (tmp_path / ".lazyagent.toml").read_text() == REPO_TOML.replace(
        'permission_mode = "plan"  # reviewed', 'permission_mode = "dontAsk"  # reviewed'
    )
    assert engine.config.agent.permission_mode == "dontAsk"
    config = load_config(tmp_path)
    assert (config.default_branch, config.inbox.deep_max, config.agent.worker_preamble) == (
        "main", 7, "Hi"
    )

    # Setting the global one: the repo's still wins.
    result = await methods.settings_set({"key": KEY, "scope": "global", "value": "auto"})
    assert global_config_path().read_text() == "# mine\n[agents]\npermission_mode = \"auto\"\n"
    assert (_setting(result)["value"], _setting(result)["source"]) == ("dontAsk", "repo")

    # Removing the repo's: the global one applies.
    result = await methods.settings_set({"key": KEY, "scope": "repo", "value": None})
    assert (_setting(result)["value"], _setting(result)["source"]) == ("auto", "global")
    text = (tmp_path / ".lazyagent.toml").read_text()
    assert "permission_mode" not in text
    assert "# how agents start here" in text and 'worker_preamble = "Hi"' in text
    assert engine.config.agent.permission_mode == "auto"


async def test_invalid_values_keys_and_scopes_are_refused(tmp_path):
    (tmp_path / ".lazyagent.toml").write_text(REPO_TOML)
    _, methods = _methods(tmp_path)
    with pytest.raises(ValueError, match="Invalid agents.permission_mode: 'yolo'"):
        await methods.settings_set({"key": KEY, "scope": "repo", "value": "yolo"})
    with pytest.raises(ValueError, match="Unknown setting"):
        await methods.settings_set({"key": "agents.provider", "scope": "repo", "value": "cli"})
    with pytest.raises(ValueError, match="Unknown setting"):
        await methods.settings_set({"key": ["x"], "scope": "repo", "value": "cli"})
    with pytest.raises(ValueError, match="scope must be one of"):
        await methods.settings_set({"key": KEY, "scope": "team", "value": "cli"})
    with pytest.raises(ValueError, match="value is required"):
        await methods.settings_set({"key": KEY, "scope": "repo"})
    assert (tmp_path / ".lazyagent.toml").read_text() == REPO_TOML, "untouched"
    assert not global_config_path().exists()


async def test_an_invalid_value_in_a_file_counts_as_not_set(tmp_path):
    (tmp_path / ".lazyagent.toml").write_text('[agents]\npermission_mode = "yolo"\n')
    _, methods = _methods(tmp_path)
    setting = _setting(await methods.settings_get({}))
    assert (setting["repo"], setting["value"], setting["source"]) == (None, "cli", "default")


async def test_settings_set_is_the_users(tmp_path):
    engine, methods = _methods(tmp_path)
    session = MagicMock()
    session.worktree_path, session.agent_id, session.running = ORCHESTRATOR_KEY, "", True
    add_session(engine, session)
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    with pytest.raises(ValueError, match="the user's"):
        await methods.settings_set(
            {"key": KEY, "scope": "global", "value": "bypass", "caller": "tok-orch"}
        )
    assert not global_config_path().exists()


async def test_unset_table_key_leaves_the_rest():
    text = '[agents]\nx = [\n  "a",\n]\npermission_mode = "plan"\n\n[inbox]\npermission_mode = 1\n'
    assert unset_table_key(text, "agents", "x") == (
        '[agents]\npermission_mode = "plan"\n\n[inbox]\npermission_mode = 1\n'
    )
    assert unset_table_key(text, "agents", "permission_mode") == (
        '[agents]\nx = [\n  "a",\n]\n\n[inbox]\npermission_mode = 1\n'
    )
    assert unset_table_key(text, "nope", "x") == text
    assert unset_table_key(text, "agents", "nope") == text


async def test_removing_what_is_not_there_writes_nothing(tmp_path):
    write_setting(tmp_path, KEY, None, "global")
    assert not global_config_path().exists()


async def test_an_inline_comment_survives_a_replaced_string():
    from lazyagent.config import set_table_keys

    text = '[agents]\npermission_mode = "a#b"  # mine\nx = "#"\n'
    assert set_table_keys(text, "agents", {"permission_mode": "plan", "x": "y"}) == (
        '[agents]\npermission_mode = "plan"  # mine\nx = "y"\n'
    )
