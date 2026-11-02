"""Describe the tags of ESMValTool, such as authors and references."""

from __future__ import annotations

import re

from pybtex.database import BibliographyData, Entry, parse_string
from pybtex.exceptions import PybtexError
from pybtex.richtext import Text

# Characters that have a meaning in Markdown text.
_MARKDOWN = re.compile(r"([\\`*_\[\]<>])")

# References with more authors are shown as "First et al.".
_MAX_AUTHORS = 2

# A DOI, possibly as a URL or with a prefix, e.g. doi:10.1000/xyz.
_DOI = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:)?\s*(10\.\S+)", re.IGNORECASE
)


def _escape(text: str) -> str:
    """Escape text so it is shown as it is in Markdown."""
    return _MARKDOWN.sub(r"\\\1", text)


def _latex_to_text(text: str) -> str:
    """Convert LaTeX, e.g. braces and accents, to plain text."""
    plain = Text.from_latex(text).render_as("text")
    return " ".join(plain.split())


def _entry(name: str, bibtex: str) -> Entry | None:
    """Parse the entry of a reference from its bibtex file."""
    try:
        data: BibliographyData = parse_string(bibtex, "bibtex")
    except PybtexError:
        return None
    entries = data.entries
    if name in entries:
        return entries[name]
    return next(iter(entries.values()), None)


def _authors(entry: Entry) -> str | None:
    """Describe the authors as e.g. "Doe", "Doe and Roe", or "Doe et al."."""
    persons = entry.persons.get("author", [])
    names = [_latex_to_text(" ".join(person.last_names)) for person in persons]
    names = [name for name in names if name]
    if not names:
        return None
    if len(names) > _MAX_AUTHORS:
        return f"{names[0]} et al."
    return " and ".join(names)


def _link(entry: Entry) -> str | None:
    """Get the URL of the DOI of a reference, or its URL."""
    if match := _DOI.fullmatch(entry.fields.get("doi", "").strip()):
        return f"https://doi.org/{match.group(1)}"
    url = entry.fields.get("url", "").strip()
    return url or None


def reference_summary(name: str, bibtex: str) -> str | None:
    """Summarize a reference as its first author, year, and title.

    Parameters
    ----------
    name:
        The name of the reference, which is also the key of its entry.
    bibtex:
        The content of the bibtex file of the reference.

    Returns
    -------
    :
        Markdown text, linked to the DOI or URL of the reference, or
        :obj:`None` if the bibtex cannot be read or has no title.
    """
    entry = _entry(name, bibtex)
    if entry is None:
        return None
    title = _latex_to_text(entry.fields.get("title", "")).rstrip(".")
    if not title:
        return None
    authors = _authors(entry)
    year = entry.fields.get("year", "").strip()
    if authors:
        text = (
            f"{authors} ({year}): {title}" if year else f"{authors}: {title}"
        )
    else:
        text = f"{title} ({year})" if year else title
    text = _escape(text)
    link = _link(entry)
    return f"[{text}]({link})" if link else text


def _person_name(name: str) -> str:
    """Write a name like "Doe, Jane" as "Jane Doe"."""
    last, comma, first = name.partition(",")
    return f"{first.strip()} {last.strip()}" if comma else name.strip()


def _orcid_link(orcid: str) -> str:
    """Link to an ORCID, given as a URL or as an identifier."""
    identifier = orcid.strip().rstrip("/").rsplit("/", 1)[-1]
    return f"[ORCID {identifier}](https://orcid.org/{identifier})"


def author_summary(author: dict[str, object]) -> str:
    """Describe an author with links to their ORCID and GitHub profile.

    Parameters
    ----------
    author:
        The definition of the author in ``config-references.yml``, with
        keys such as ``name``, ``institute``, ``orcid``, and ``github``.

    Returns
    -------
    :
        Markdown text.
    """
    fields = {
        key: str(value).strip()
        for key, value in author.items()
        if isinstance(value, str | int) and str(value).strip()
    }
    name = _escape(_person_name(fields.get("name", "")))
    heading = f"**{name}**" if name else ""
    if institute := fields.get("institute"):
        heading = ", ".join(
            part for part in (heading, _escape(institute)) if part
        )
    links = []
    if orcid := fields.get("orcid"):
        links.append(_orcid_link(orcid))
    if github := fields.get("github"):
        handle = github.lstrip("@")
        links.append(f"[@{_escape(handle)}](https://github.com/{handle})")
    if email := fields.get("email"):
        links.append(f"[{_escape(email)}](mailto:{email})")
    return "\n\n".join(part for part in (heading, " · ".join(links)) if part)
