"""The rules catalog (proposal 0014): a closed list of facts the engine
computes (:mod:`.facts`), a small three-valued expression language over them
(:mod:`.lang`), and rules that give a row its ball, state and level and an
inbox kind its level and condition (:mod:`.catalog`).

R1 ships only the built-in catalog (``builtin.toml``, package data), which
reproduces the old derivation table exactly; user rule files come in R2.
"""

from __future__ import annotations

from functools import cache
from importlib import resources

from lazyagent.engine.rules.catalog import Catalog, CatalogError, load

__all__ = ["Catalog", "CatalogError", "builtin", "builtin_text", "load"]


def builtin_text() -> str:
    """``builtin.toml`` as shipped in the package."""
    return resources.files(__package__).joinpath("builtin.toml").read_text(encoding="utf-8")


@cache
def builtin() -> Catalog:
    """The built-in catalog, compiled once."""
    return load(builtin_text())
