from __future__ import annotations

import copy
import warnings
from functools import lru_cache
from typing import TYPE_CHECKING

import pytest
from esmvalcore.config import CFG, Config
from lsprotocol import types

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

CURSOR = "|"


@lru_cache
def _default_config() -> Config:
    """Create a configuration object with the default values."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Do not instantiate `Config` objects directly",
            category=UserWarning,
        )
        cfg = Config()
    cfg.load_from_dirs([])
    return cfg


@pytest.fixture(autouse=True)
def ignore_user_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Use the default configuration instead of the user's configuration."""
    cfg = copy.deepcopy(_default_config())
    monkeypatch.setattr(CFG, "_mapping", cfg._mapping)
    # For servers that the tests start in a new process.
    config_dir = tmp_path / "esmvaltool_config"
    config_dir.mkdir()
    monkeypatch.setenv("ESMVALTOOL_CONFIG_DIR", str(config_dir))


def _split_cursor(text: str) -> tuple[str, types.Position]:
    before, _, after = text.partition(CURSOR)
    lines = before.split("\n")
    position = types.Position(line=len(lines) - 1, character=len(lines[-1]))
    return before + after, position


@pytest.fixture
def split_cursor() -> Callable[[str], tuple[str, types.Position]]:
    """Remove the cursor, marked by `|`, from a text and get its position."""
    return _split_cursor
