from __future__ import annotations

import pytest

from esmvaltool_language_server.tags import author_summary, reference_summary


def _bibtex(fields: str, key: str = "doe21") -> str:
    return f"@article{{{key},\n{fields}\n}}\n"


@pytest.mark.parametrize(
    ("authors", "expected"),
    [
        ("Doe, Jane", "Doe"),
        ("Doe, Jane and Roe, Richard", "Doe and Roe"),
        ("Doe, Jane and Roe, Richard and Poe, Edgar", "Doe et al."),
        ("Jane Doe and others", "Doe and others"),
    ],
)
def test_reference_authors(authors: str, expected: str) -> None:
    bibtex = _bibtex(f"author = {{{authors}}},\ntitle = {{T}},\nyear = 2021")
    assert reference_summary("doe21", bibtex) == f"{expected} (2021): T"


@pytest.mark.parametrize(
    ("doi", "url"),
    [
        ("10.1000/xyz", "https://doi.org/10.1000/xyz"),
        ("https://doi.org/10.1000/xyz", "https://doi.org/10.1000/xyz"),
        ("doi:10.1000/xyz", "https://doi.org/10.1000/xyz"),
    ],
)
def test_reference_doi(doi: str, url: str) -> None:
    bibtex = _bibtex(f"author = {{Doe, J.}},\ntitle = {{T}},\ndoi = {{{doi}}}")
    assert reference_summary("doe21", bibtex) == f"[Doe: T]({url})"


def test_reference_url() -> None:
    bibtex = _bibtex("title = {T},\nurl = {https://example.org}")
    assert reference_summary("doe21", bibtex) == "[T](https://example.org)"


def test_reference_latex_title() -> None:
    bibtex = _bibtex(
        'title = {The {CMIP6} [data] of {\\"O}land.},\nyear = 2020'
    )
    assert reference_summary("doe21", bibtex) == (
        "The CMIP6 \\[data\\] of Öland (2020)"
    )


def test_reference_entry_with_name() -> None:
    bibtex = _bibtex("title = {Other}", key="other") + _bibtex("title = {T}")
    assert reference_summary("doe21", bibtex) == "T"


@pytest.mark.parametrize(
    "bibtex",
    [
        "@article{doe21,\n  title = {T\n",
        "@misc{doe21,\n  author = {},\n  title = {}\n}\n",
        "",
    ],
)
def test_reference_unreadable(bibtex: str) -> None:
    assert reference_summary("doe21", bibtex) is None


def test_author_summary() -> None:
    summary = author_summary(
        {
            "name": "Doe, Jane",
            "institute": "Example Institute",
            "email": "jane@example.org",
            "orcid": "https://orcid.org/0000-0001-2345-6789",
            "github": "janedoe",
        },
    )
    assert summary == (
        "**Jane Doe**, Example Institute\n\n"
        "[@janedoe](https://github.com/janedoe)"
        " · [jane@example.org](mailto:jane@example.org)"
        " · [ORCID 0000-0001-2345-6789](https://orcid.org/0000-0001-2345-6789)"
    )


def test_author_summary_missing_fields() -> None:
    summary = author_summary({"name": "Doe_Jane", "orcid": None})
    assert summary == "**Doe\\_Jane**"


def test_author_summary_orcid_identifier() -> None:
    summary = author_summary({"orcid": "0000-0001-2345-6789"})
    assert summary == (
        "[ORCID 0000-0001-2345-6789](https://orcid.org/0000-0001-2345-6789)"
    )
