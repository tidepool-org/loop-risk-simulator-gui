"""End-to-end integration test for the TRSET-34 start page (Feature gate).

Drives the real ``streamlit_app.py`` through ``AppTest`` against the real config
library -- no mocks in the exercised path, and deliberately NOT through the
shared ``make_app_test`` factory, which exists to *bypass* the gate for every
other suite. This is the one place the unacknowledged first render is real.

Three phases, matching the approved plan:

  1. Unacknowledged first render -- the start page is what the user gets, the
     tool is not reachable, and the text on screen is the module's constants
     rather than a copy of them.
  2. The click -- a real ``Got it`` press advances to the tool with the shared
     chrome intact.
  3. The gate stays down -- an unrelated widget interaction on a later rerun
     does not bring the start page back. This is the phase that matters: the
     failure mode it targets is ``_init_session_state()`` re-seeding the
     acknowledgement flag to ``False`` on every rerun.
"""

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

import start_page  # noqa: E402
import streamlit_app  # noqa: E402

# The start page renders no simulation, so it needs nothing like the run
# budgets the other integration suites carry -- this is the ordinary app-render
# timeout the smoke suite uses.
RENDER_TIMEOUT_SECONDS = 60


def _markdown_values(at):
    return [m.value for m in at.markdown]


def _unacknowledged_app():
    """The app as a brand-new session sees it: the gate untouched."""
    return AppTest.from_file("streamlit_app.py", default_timeout=RENDER_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Phase 1 -- unacknowledged first render
# ---------------------------------------------------------------------------

def test_phase1_first_render_shows_the_start_page_and_not_the_tool():
    at = _unacknowledged_app()
    at.run()
    assert not at.exception

    # The page itself, in order: title, then the two section headings.
    assert [t.value for t in at.title] == [start_page.PAGE_TITLE]
    assert [s.value for s in at.subheader] == [
        start_page.PURPOSE_HEADING,
        start_page.AI_DISCLOSURE_HEADING,
    ]

    # Verbatim text, asserted against the module constants -- never a literal
    # copied into the test, which would only prove the copy matches itself.
    values = _markdown_values(at)
    assert start_page.PURPOSE_TEXT in values
    assert start_page.AI_DISCLOSURE_PARAGRAPH_1 in values
    assert start_page.AI_DISCLOSURE_PARAGRAPH_2 in values

    # Shared chrome above the gate: brand styling, exactly one role="alert"
    # banner, and the logo -- the start page is inside the app, not beside it.
    assert streamlit_app._BRAND_CSS.strip() in values
    assert sum(1 for v in values if 'role="alert"' in v) == 1
    assert sum(1 for v in values if f'alt="{streamlit_app.LOGO_ALT_TEXT}"' in v) == 1

    # ...and the acknowledge button, which is the only control on the page.
    assert [b.label for b in at.button] == [start_page.ACKNOWLEDGE_LABEL]

    # The tool is not reachable: none of the main page's controls exist.
    assert not at.radio, "the config-source radio must not render behind the gate"
    assert not at.selectbox, "the library selector must not render behind the gate"
    assert not at.dataframe, "no results pane may render behind the gate"
    assert at.session_state[streamlit_app.START_PAGE_ACK_KEY] is False


def test_phase1_the_start_page_title_still_matches_the_main_page_title():
    # start_page restates the app title rather than importing it (importing
    # streamlit_app would be circular, and an st.Page-adopted module has to stand
    # alone). This is the guard that keeps the two copies in step.
    at = _unacknowledged_app()
    at.run()
    start_title = at.title[0].value

    at.button[0].click().run()
    at.run()
    assert not at.exception
    assert at.title[0].value == start_title


def test_phase1_the_start_page_emits_no_raw_html_of_its_own():
    """AC 10: the page uses st.title/subheader/markdown/button only.

    Asserted by difference: the only HTML in the unacknowledged render is the
    shared chrome (brand CSS, disclaimer banner, logo), all of which comes from
    streamlit_app, not from start_page.
    """
    at = _unacknowledged_app()
    at.run()
    assert not at.exception

    chrome = ("<style>", 'role="alert"', "<img")
    page_html = [
        v for v in _markdown_values(at)
        if "<" in v and not any(marker in v for marker in chrome)
    ]
    assert page_html == [], f"start page emitted raw HTML of its own: {page_html!r}"


# ---------------------------------------------------------------------------
# Phase 2 -- the click
# ---------------------------------------------------------------------------

def test_phase2_clicking_got_it_advances_to_the_tool():
    at = _unacknowledged_app()
    at.run()

    # A real click on the real button, then the explicit run() that renders the
    # page the gate's st.rerun() asked for.
    at.button[0].click().run()
    at.run()
    assert not at.exception

    # The tool is here.
    assert at.session_state[streamlit_app.START_PAGE_ACK_KEY] is True
    assert at.radio(key="config_source") is not None
    assert [b.label for b in at.button] == ["Run Tool"]

    # ...and the start page is gone.
    values = _markdown_values(at)
    assert start_page.PURPOSE_TEXT not in values
    assert start_page.AI_DISCLOSURE_PARAGRAPH_1 not in values
    assert start_page.AI_DISCLOSURE_PARAGRAPH_2 not in values
    assert start_page.PURPOSE_HEADING not in [s.value for s in at.subheader]

    # The shared chrome is unchanged across the transition -- still exactly one
    # banner (not one per page) and still the logo.
    assert streamlit_app._BRAND_CSS.strip() in values
    assert sum(1 for v in values if 'role="alert"' in v) == 1
    assert sum(1 for v in values if f'alt="{streamlit_app.LOGO_ALT_TEXT}"' in v) == 1


def test_phase2_no_control_returns_to_the_start_page():
    # AC 9. Once through, nothing on the page offers a way back -- the only
    # start-page-labeled control was the one just consumed.
    at = _unacknowledged_app()
    at.run()
    at.button[0].click().run()
    at.run()
    assert not at.exception

    assert start_page.ACKNOWLEDGE_LABEL not in [b.label for b in at.button]
    assert start_page.PAGE_TITLE not in [s.value for s in at.subheader]


# ---------------------------------------------------------------------------
# Phase 3 -- the gate stays down
# ---------------------------------------------------------------------------

def test_phase3_an_unrelated_interaction_does_not_bring_the_start_page_back():
    """The regression this feature is most likely to grow.

    ``_init_session_state()`` runs on every rerun, above the gate. If the
    acknowledgement flag were seeded unconditionally there (or re-seeded
    anywhere else), the first widget the user touched after clicking through
    would throw them back to the start page.
    """
    at = _unacknowledged_app()
    at.run()
    at.button[0].click().run()
    at.run()
    assert not at.exception

    # An interaction that has nothing to do with the gate, forcing a full rerun
    # back through _init_session_state().
    at.radio(key="config_source").set_value(streamlit_app.SOURCE_CONFIGURE).run()
    assert not at.exception

    assert at.session_state[streamlit_app.START_PAGE_ACK_KEY] is True
    values = _markdown_values(at)
    assert start_page.PURPOSE_TEXT not in values
    assert start_page.AI_DISCLOSURE_PARAGRAPH_1 not in values
    assert start_page.ACKNOWLEDGE_LABEL not in [b.label for b in at.button]
    # The editor the interaction asked for actually rendered, so this is a real
    # rerun of the tool and not a vacuous pass.
    assert at.radio(key="config_source").value == streamlit_app.SOURCE_CONFIGURE


def test_phase3_the_gate_survives_repeated_reruns():
    # AC 6: any number of reruns, not just the one interaction above.
    at = _unacknowledged_app()
    at.run()
    at.button[0].click().run()
    for _ in range(3):
        at.run()
        assert not at.exception
        assert at.session_state[streamlit_app.START_PAGE_ACK_KEY] is True
        assert start_page.PURPOSE_TEXT not in _markdown_values(at)


def test_phase3_acknowledgement_is_per_session_only():
    """AC 7: a new session starts over.

    A fresh ``AppTest`` is a fresh session, exactly like a browser refresh --
    nothing is written outside session state, so the gate is back up.
    """
    first = _unacknowledged_app()
    first.run()
    first.button[0].click().run()
    first.run()
    assert first.session_state[streamlit_app.START_PAGE_ACK_KEY] is True

    second = _unacknowledged_app()
    second.run()
    assert not second.exception
    assert second.session_state[streamlit_app.START_PAGE_ACK_KEY] is False
    assert start_page.PURPOSE_TEXT in _markdown_values(second)
