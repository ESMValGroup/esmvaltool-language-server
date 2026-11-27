from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import pytest

import esmvaltool_language_server
from esmvaltool_language_server import __main__

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bool]]:
    """Do not start the server, and restore the log level."""
    started: list[bool] = []
    monkeypatch.setattr(
        __main__.server, "start_io", lambda: started.append(True)
    )
    logger = logging.getLogger(esmvaltool_language_server.__name__)
    level = logger.level
    yield started
    logger.setLevel(level)


@pytest.mark.parametrize(
    ("args", "level"),
    [([], logging.INFO), (["--log-level", "debug"], logging.DEBUG)],
)
def test_log_level(
    monkeypatch: pytest.MonkeyPatch,
    started: list[bool],
    args: list[str],
    level: int,
) -> None:
    monkeypatch.setattr(sys, "argv", ["esmvaltool-language-server", *args])
    __main__.main()
    assert started == [True]
    logger = logging.getLogger(esmvaltool_language_server.__name__)
    assert logger.level == level
