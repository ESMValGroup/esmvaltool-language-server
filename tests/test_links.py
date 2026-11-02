"""Check that the links in the hover text resolve.

These tests need internet access, so they only run with ``-m network``, e.g.
with ``pixi run test-links``. Links to the documentation of ESMValCore and
ESMValTool are checked against the documentation given by the
``--esmvalcore-docs`` and ``--esmvaltool-docs`` options, so links to parts
that are not released yet can be checked.
"""

from __future__ import annotations

import functools
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urldefrag, urlsplit

import pytest
import requests
import yaml
from esmvalcore.config import CFG
from lsprotocol import types

from esmvaltool_language_server import references, vocabulary
from esmvaltool_language_server.hover import hover
from esmvaltool_language_server.schema import recipe_schema

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

pytestmark = pytest.mark.network

# Links in Markdown, which may contain balanced parentheses and backslash
# escapes, and URLs that editors show as links.
_MARKDOWN_LINK = re.compile(r"\]\((https?://(?:[^()\s]|\([^()\s]*\))+)\)")
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_URL = re.compile(r"(?<![(<])https?://[^\s)>\]`]+")
_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)

# Links to these documentation versions are checked against the
# documentation given by the options.
_DOCS = {
    "esmvalcore_docs": re.compile(
        r"https://docs\.esmvaltool\.org/projects/ESMValCore/en/latest/",
    ),
    "esmvaltool_docs": re.compile(r"https://docs\.esmvaltool\.org/en/latest/"),
}

_TIMEOUT = 30
_WORKERS = 8
_HTTP_ERROR = 400
_TOO_MANY_REQUESTS = 429
# Websites that refuse requests from scripts, so links to them are not
# checked.
_SKIPPED_HOSTS = {"wcrp-cmip.org"}


def _links(text: str) -> set[str]:
    """Find the links in Markdown text, except those in code blocks."""
    text = _CODE_BLOCK.sub("", text)
    return {
        _ESCAPE.sub(r"\1", url) for url in _MARKDOWN_LINK.findall(text)
    } | {url.rstrip(".,;:") for url in _URL.findall(text)}


def _descriptions(schema: object) -> Iterator[str]:
    """Find the descriptions in a JSON Schema."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in {"description", "markdownDescription"} and isinstance(
                value, str
            ):
                yield value
            else:
                yield from _descriptions(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from _descriptions(item)


def _hover_links(text: str) -> set[str]:
    """Find the links in the hover text of each value in a recipe."""
    recipe = yaml.safe_load(text)
    schema = recipe_schema()
    links = set()
    for number, line in enumerate(text.splitlines()):
        # Hover over the last value on the line.
        matches = list(re.finditer(r"(?:- |: )(\S)", line))
        if not matches:
            continue
        position = types.Position(line=number, character=matches[-1].start(1))
        result = hover(text, position, recipe, schema)
        if result is not None and isinstance(
            result.contents, types.MarkupContent
        ):
            links |= _links(result.contents.value)
    return links


def _items(section: str, names: Iterable[str], indent: int) -> str:
    """Write a list of names as YAML."""
    lines = [f"{section}:", *(f"  - {name}" for name in sorted(names))]
    return "\n".join(" " * indent + line for line in lines)


def _tags_recipe(directory: Path) -> str:
    """Write a recipe that uses all tags and references of ESMValTool."""
    tags = yaml.safe_load(
        (directory / "config-references.yml").read_text(encoding="utf-8"),
    )
    bibtex = [
        path.stem for path in (directory / "references").glob("*.bibtex")
    ]
    return "\n".join(
        [
            "documentation:",
            _items("authors", tags.get("authors", {}), 2),
            _items("projects", tags.get("projects", {}), 2),
            _items("references", bibtex, 2),
            "diagnostics:",
            "  diag:",
            _items("themes", tags.get("themes", {}), 4),
            _items("realms", tags.get("realms", {}), 4),
            "",
        ],
    )


def _vocabulary_recipe(project: str, vocabulary: dict) -> str:
    """Write a recipe that uses all models and experiments of a project."""
    lines = ["datasets:"]
    lines.extend(
        f"  - dataset: {name}" for name in vocabulary.get("source_id", {})
    )
    for name in vocabulary.get("experiment_id", {}):
        lines.extend(["  - dataset: x", f"    exp: {name}"])
    lines.extend(
        [
            "diagnostics:",
            "  diag:",
            "    variables:",
            "      tas:",
            f"        project: {project}",
            "",
        ],
    )
    return "\n".join(lines)


def _vocabulary_recipes() -> Iterator[str]:
    """Write a recipe for each controlled vocabulary of a project."""
    for project in CFG["projects"]:
        if content := vocabulary.controlled_vocabulary(project):
            yield _vocabulary_recipe(project, content)


def _projects_recipe() -> str:
    """Write a recipe that uses all projects."""
    lines = ["datasets:"]
    for project in CFG["projects"]:
        lines.extend(["  - dataset: x", f"    project: {project}"])
    return "\n".join([*lines, ""])


@functools.cache
def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "esmvaltool-language-server link check"
    return session


@functools.cache
def _get(url: str) -> tuple[int, str]:
    """Get the status and content of a web page, once."""
    try:
        response = _session().get(url, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        return 0, str(exc)
    return response.status_code, response.text


def _has_anchor(html: str, anchor: str) -> bool:
    return re.search(rf'\b(?:id|name)="{re.escape(anchor)}"', html) is not None


def _check_doi(doi: str) -> str | None:
    """Check that a DOI is registered, without visiting its target.

    Publishers often refuse requests from scripts, so the DOI is looked up in
    the handle system instead.
    """
    status, text = _get(f"https://doi.org/api/handles/{doi}")
    if status == _TOO_MANY_REQUESTS:
        return None
    if status >= _HTTP_ERROR or json.loads(text).get("responseCode") != 1:
        return f"DOI not found (status {status})"
    return None


def _check_local(base: Path, path: str, anchor: str) -> str | None:
    """Check a link to a local build of the documentation."""
    file = base / path
    if not path or path.endswith("/"):
        file /= "index.html"
    if not file.is_file():
        return f"{file} does not exist"
    html = file.read_text(encoding="utf-8")
    if anchor and not _has_anchor(html, anchor):
        return f"#{anchor} not found in {file}"
    return None


def _check(url: str, docs: dict[str, str]) -> str | None:
    """Check a link, returning the problem or None if it resolves."""
    for option, pattern in _DOCS.items():
        if match := pattern.match(url):
            base = docs[option]
            rest, anchor = urldefrag(url[match.end() :])
            if not urlsplit(base).scheme:
                return _check_local(Path(base), rest, anchor)
            url = f"{base}{url[match.end() :]}"
    if urlsplit(url).netloc in {"doi.org", "dx.doi.org"}:
        return _check_doi(urlsplit(url).path.lstrip("/"))
    page, anchor = urldefrag(url)
    status, html = _get(page)
    if status == _TOO_MANY_REQUESTS:
        return None
    if status == 0 or status >= _HTTP_ERROR:
        return f"status {status}" if status else html
    if anchor and not _has_anchor(html, anchor):
        return f"#{anchor} not found in {page}"
    return None


def _assert_resolve(links: set[str], docs: dict[str, str]) -> None:
    links = {
        url for url in links if urlsplit(url).netloc not in _SKIPPED_HOSTS
    }
    with ThreadPoolExecutor(_WORKERS) as executor:
        problems = dict(
            zip(
                sorted(links),
                executor.map(lambda url: _check(url, docs), sorted(links)),
                strict=True,
            ),
        )
    broken = {url: problem for url, problem in problems.items() if problem}
    assert not broken, "\n".join(
        f"{url}: {problem}" for url, problem in broken.items()
    )


@pytest.fixture
def esmvaltool_dir(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Use the ESMValTool given by the option, or the one that is installed."""
    option = request.config.getoption("esmvaltool_dir")
    directory = Path(option) if option else references.esmvaltool_directory()
    if directory is None:
        pytest.skip("ESMValTool is not installed, use --esmvaltool-dir")
    monkeypatch.setattr(references, "esmvaltool_directory", lambda: directory)
    return directory


@pytest.fixture
def docs(request: pytest.FixtureRequest) -> dict[str, str]:
    """Get the documentation to check links to the documentation against."""
    return {option: str(request.config.getoption(option)) for option in _DOCS}


def test_schema_links(docs: dict[str, str]) -> None:
    links = {
        link
        for text in _descriptions(recipe_schema().schema)
        for link in _links(text)
    }
    assert links
    _assert_resolve(links, docs)


def test_tag_links(docs: dict[str, str], esmvaltool_dir: Path) -> None:
    references.tags.cache_clear()
    try:
        links = _hover_links(_tags_recipe(esmvaltool_dir))
    finally:
        references.tags.cache_clear()
    _assert_resolve(links, docs)


def test_vocabulary_links(docs: dict[str, str]) -> None:
    links = set()
    for text in [*_vocabulary_recipes(), _projects_recipe()]:
        links |= _hover_links(text)
    _assert_resolve(links, docs)
