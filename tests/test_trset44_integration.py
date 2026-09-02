"""The tool's name agrees across every site that renders it (TRSET-44).

Before TRSET-44 three different names for one tool rendered within a
screenful: ``st.title`` and the start page said "Estimation", ``DISCLAIMER_TEXT``
said "Evaluation". "Evaluation" is correct -- it is what TRSET expands to
(Tidepool Risk Severity Evaluation Tool), confirmed against the QMS
documentation on 2026-09-01.

This suite exists because TRSET-34 showed the eyeball check is the one that
misses. It does not compare the strings by reading them; it *extracts* the
name from each site with one regex and compares the captures, so a future edit
to any single site fails here rather than shipping a fresh split.

Two things are deliberately NOT asserted equal:

  * "Loop" -- a title-only qualifier. ``PAGE_TITLE`` and ``st.title`` name this
    GUI ("Tidepool Loop Risk Severity Evaluation Tool"); ``DISCLAIMER_TEXT`` and
    ``PURPOSE_TEXT`` name the underlying tool, and ``PURPOSE_TEXT``'s "(TRSET)"
    makes its occurrence the acronym's expansion, which has no "Loop" in it.
    So the invariant is on the canonical core, with "Loop" optional.
  * The ``set_page_config`` page title, which ``AppTest`` does not expose. It is
    covered by the source-level test below instead.
"""

import re

import pytest

pytest.importorskip("streamlit")

import start_page  # noqa: E402
import streamlit_app  # noqa: E402

from conftest import make_app_test  # noqa: E402

RENDER_TIMEOUT_SECONDS = 60

# The one spelling of the name, and the only one this repo may render.
CANONICAL_NAME = "Risk Severity Evaluation Tool"

# Matches any naming of this tool -- right or wrong -- so a regression is a
# failed comparison with a readable diff, not a no-match. The middle word is
# captured loosely on purpose: "Estimation" has to be *caught*, not skipped.
_NAME_RE = re.compile(r"Tidepool (?:Loop )?Risk Severity \w+ Tool")


def _names_in(text):
    """Every tool-name occurrence in ``text``, with the "Loop" qualifier dropped.

    Dropping "Loop" is what makes the four sites comparable at all; see the
    module docstring for why it is not part of the invariant.
    """
    return [m.replace("Tidepool Loop ", "Tidepool ") for m in _NAME_RE.findall(text)]


def _acknowledged_app():
    """The app past the TRSET-34 start-page gate, so ``st.title`` renders.

    Through the shared factory, not a local flag write -- the gate is bypassed
    in exactly one place repo-wide.
    """
    return make_app_test(default_timeout=RENDER_TIMEOUT_SECONDS)


def test_the_three_named_sites_agree_on_the_tools_name():
    """AC 3: ``st.title``, ``DISCLAIMER_TEXT`` and the purpose statement match.

    Compared by extraction, not by reading -- the captures themselves are the
    assertion.
    """
    at = _acknowledged_app()
    at.run()
    assert not at.exception

    rendered_title = next(t.value for t in at.title)

    sites = {
        "st.title": _names_in(rendered_title),
        "DISCLAIMER_TEXT": _names_in(streamlit_app.DISCLAIMER_TEXT),
        "PURPOSE_TEXT": _names_in(start_page.PURPOSE_TEXT),
        "start_page.PAGE_TITLE": _names_in(start_page.PAGE_TITLE),
    }

    for site, names in sites.items():
        assert names, f"{site} no longer names the tool at all"

    expected = f"Tidepool {CANONICAL_NAME}"
    assert sites == {site: [expected] * len(names) for site, names in sites.items()}


def test_set_page_config_title_matches():
    """The browser-tab title too -- ``AppTest`` cannot see it, so read the source.

    Source-level rather than behavioural because ``set_page_config`` is a no-op
    under ``AppTest``. A grep is the honest tool here; pretending otherwise
    would be a test that passes without checking anything.
    """
    source = open(streamlit_app.__file__).read()
    page_titles = re.findall(r'page_title="([^"]*)"', source)
    assert page_titles, "set_page_config no longer sets a page_title"
    assert _names_in("".join(page_titles)) == [f"Tidepool {CANONICAL_NAME}"]


def test_no_site_renders_the_superseded_estimation_spelling():
    """AC 1: "Estimation" is gone from everything a user can see."""
    at = _acknowledged_app()
    at.run()
    assert not at.exception

    rendered = [t.value for t in at.title] + [m.value for m in at.markdown]
    rendered += [s.value for s in at.subheader]
    offenders = [text for text in rendered if "Estimation" in text]
    assert not offenders, f"superseded naming still rendered: {offenders}"

    for name, value in (
        ("DISCLAIMER_TEXT", streamlit_app.DISCLAIMER_TEXT),
        ("PURPOSE_TEXT", start_page.PURPOSE_TEXT),
        ("PAGE_TITLE", start_page.PAGE_TITLE),
    ):
        assert "Estimation" not in value, f"{name} still says Estimation"
