"""The GUI's start page (TRSET-34).

A page-shaped unit: the verbatim disclosure text as module-level constants plus
a zero-argument, returns-nothing ``render()``. Nothing here knows about the
session gate that currently keeps the tool unreachable until the page is
acknowledged -- that gate lives in ``streamlit_app.main()`` and reads this
page's button through ``ACKNOWLEDGE_BUTTON_KEY``. Keeping the flag-setting out
of ``render()`` is what lets the forthcoming ``st.navigation`` migration adopt
this module as ``st.Page(start_page.render, ...)`` unchanged, discarding only
the gate.

The page emits no raw HTML (st.title / st.subheader / st.markdown / st.button
only), so it adds nothing to the WCAG contrast or tabindex surfaces the
TRSET-4 gates guard, and it introduces no new color tokens.
"""

import streamlit as st

# Restates the app title rather than importing it: this module must not import
# streamlit_app (that would be circular, and a page adopted by st.Page has to
# stand on its own). The integration test asserts the two stay in step.
PAGE_TITLE = "Tidepool Loop Risk Severity Estimation Tool"

PURPOSE_HEADING = "Purpose"
AI_DISCLOSURE_HEADING = "AI Code Disclosure"
ACKNOWLEDGE_LABEL = "Got it"

# The key st.button writes its clicked state to. The gate in main() reads it;
# see the module docstring for why the page itself does not act on the click.
ACKNOWLEDGE_BUTTON_KEY = "start_page_acknowledge_clicked"

# The three text blocks below are reproduced verbatim from TRSET-34. They are
# constants (the DISCLAIMER_TEXT precedent) so the tests assert the rendered
# text against the source of truth rather than a copy-pasted literal. Do not
# copy-edit, reflow, or normalize the tool's name in them.
PURPOSE_TEXT = (
    "This app is designed for risk exploration rather than formal risk "
    "assessment. It uses the Tidepool Risk Severity Estimation Tool (TRSET) "
    "without modification behind the scenes, so the outputs are from the "
    "native TRSET tool. However, the native TRSET tool supports more flexible "
    "configurations for scenarios. As with the native TRSET tool, this app is "
    "not medical software and should not be used to make insulin dosing "
    "decisions."
)

AI_DISCLOSURE_PARAGRAPH_1 = (
    "This app was written with the assistance of Claude AI. The AI did not "
    "design the various models (metabolism, Loop algorithm, etc.) that work "
    "together to form TRSET. TRSET is used unmodified and generates all "
    "outputs. AI applies to the user interface wrapper around it, including "
    "the scenario inputs and how the results from TRSET are displayed."
)

AI_DISCLOSURE_PARAGRAPH_2 = (
    "Every change followed the same process. A human defined what the change "
    "needed to do, reviewed the existing code, and worked through the edge "
    "cases and open questions before any code was written. The plan was "
    "written down and approved before implementation, and the AI worked only "
    "within that approved scope. A human then reviewed all resulting code and "
    "verified its behavior against what the change was supposed to accomplish "
    "before accepting it."
)


def render():
    """Render the start page. Takes nothing, returns nothing.

    The caller is responsible for the shared chrome above it (page config,
    brand CSS, disclaimer banner, logo) and for whatever the acknowledge
    button should do -- read via ACKNOWLEDGE_BUTTON_KEY.
    """
    st.title(PAGE_TITLE)
    st.subheader(PURPOSE_HEADING)
    st.markdown(PURPOSE_TEXT)
    st.subheader(AI_DISCLOSURE_HEADING)
    st.markdown(AI_DISCLOSURE_PARAGRAPH_1)
    st.markdown(AI_DISCLOSURE_PARAGRAPH_2)
    st.button(ACKNOWLEDGE_LABEL, key=ACKNOWLEDGE_BUTTON_KEY)
