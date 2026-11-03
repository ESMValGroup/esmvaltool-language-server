from __future__ import annotations

import copy
import os
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

# The documentation of the development versions of ESMValCore and ESMValTool
# that the language server is developed with.
# TODO: use the latest documentation once ESMValCore#3261 and ESMValTool#4638
# have been merged.
ESMVALCORE_DOCS = "https://esmvalcore--3261.org.readthedocs.build/en/3261/"
ESMVALTOOL_DOCS = "https://esmvaltool--4638.org.readthedocs.build/en/4638/"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add options to choose what links are checked against."""
    group = parser.getgroup("links", "checking links (pixi run test-links)")
    group.addoption(
        "--esmvalcore-docs",
        default=os.environ.get("ESMVALCORE_DOCS", ESMVALCORE_DOCS),
        help=(
            "URL or local build directory of the ESMValCore documentation "
            "to check links to the latest documentation against. "
            "Default: $ESMVALCORE_DOCS or %(default)s."
        ),
    )
    group.addoption(
        "--esmvaltool-docs",
        default=os.environ.get("ESMVALTOOL_DOCS", ESMVALTOOL_DOCS),
        help=(
            "URL or local build directory of the ESMValTool documentation "
            "to check links to the latest documentation against. "
            "Default: $ESMVALTOOL_DOCS or %(default)s."
        ),
    )


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
