from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from esmvaltool_language_server import references

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def clear_cache() -> Iterator[None]:
    references._tag_lines.cache_clear()
    yield
    references._tag_lines.cache_clear()


@pytest.mark.usefixtures("clear_cache")
def test_tag_location_without_esmvaltool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(references, "esmvaltool_directory", lambda: None)
    assert references.tag_location("authors", "doe_jane") is None
    assert references.tag_location("references", "doe21") is None


@pytest.mark.usefixtures("clear_cache")
@pytest.mark.parametrize(
    "content",
    ["authors: [\n", "- authors\n", "authors: none\n"],
)
def test_tag_location_invalid_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
) -> None:
    (tmp_path / "config-references.yml").write_text(content)
    monkeypatch.setattr(references, "esmvaltool_directory", lambda: tmp_path)
    assert references.tag_location("authors", "doe_jane") is None


@pytest.mark.usefixtures("clear_cache")
def test_tag_location_missing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(references, "esmvaltool_directory", lambda: tmp_path)
    assert references.tag_location("authors", "doe_jane") is None
