"""
Automated accessibility coverage for streamlit_app.py (TRSET-4).

Three basic WCAG requirements, guarded so future UI changes cannot silently
regress them:

  * 1.4.3 Contrast (Minimum) -- pure-Python unit tests computing the WCAG
    contrast ratio for the foreground/background text pairs the app actually
    renders. Tokens are read from their real sources (.streamlit/config.toml
    and streamlit_app._BRAND_CSS), not hardcoded, so the ratios re-derive if a
    token changes -- a genuine regression guard, not a restatement of a literal.
  * 2.1 Keyboard Accessible -- rendered-tree smoke checks (via the same
    streamlit.testing.v1.AppTest harness the other suites use) that interactive
    widgets carry accessible labels and the app's unsafe_allow_html blocks add
    no positive tabindex / keyboard trap.
  * 1.3 Adaptable -- rendered-tree smoke checks that inputs have non-empty
    labels, emitted <img>s retain alt text (guarding TRSET-3), and severity
    information is conveyed as text, not color alone.

Scope is the app's OWN color tokens and the elements its own code emits, not
Streamlit's generated DOM (a full browser/axe-core audit is a separate ticket).
"""

import os
import re
import sys

import pytest

pytest.importorskip("streamlit")

if sys.version_info >= (3, 11):
    import tomllib  # noqa: E402
else:  # pragma: no cover - env pins 3.12; kept honest rather than silently broken
    import tomli as tomllib  # type: ignore  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

sys.path.insert(0, "post_processing")
import streamlit_app  # noqa: E402
from tidepool_data_science_simulator.projects.risk.gui_runner import (  # noqa: E402
    RunResult,
    RiskDirRunResult,
)
from severity_model import (  # noqa: E402
    SeverityAssessment,
    StageResult,
    CatastrophicFinding,
)

# WCAG 1.4.3 pass thresholds.
NORMAL_TEXT_MIN = 4.5   # < 18.66px bold / < 24px
LARGE_TEXT_MIN = 3.0    # >= 18.66px bold / >= 24px


# ---------------------------------------------------------------------------
# WCAG 1.4.3 contrast helper (pure, no dependency) + token readers
# ---------------------------------------------------------------------------

def _linearize(channel: float) -> float:
    """Convert one sRGB channel (0..1) to its linear-light value per WCAG."""
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a #RRGGBB color."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG contrast ratio (>= 1.0) between two #RRGGBB colors, order-independent."""
    lums = sorted((_relative_luminance(fg), _relative_luminance(bg)))
    return (lums[1] + 0.05) / (lums[0] + 0.05)


def _theme_tokens() -> dict:
    """The app's real theme tokens, read from .streamlit/config.toml."""
    config_path = os.path.join(os.path.dirname(__file__), "..", ".streamlit", "config.toml")
    with open(config_path, "rb") as fh:
        return tomllib.load(fh)["theme"]


def _css_override_text_color() -> str:
    """The single text color the app forces via _BRAND_CSS's input-value override."""
    matches = re.findall(r"color:\s*(#[0-9A-Fa-f]{6})", streamlit_app._BRAND_CSS)
    assert len(matches) == 1, (
        f"expected exactly one color override in _BRAND_CSS, found {matches}; "
        "update this test if the override set changed"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# AppTest rendered-tree helpers
# ---------------------------------------------------------------------------

_INTERACTIVE_ATTRS = (
    "selectbox", "multiselect", "radio", "checkbox", "button",
    "text_input", "number_input", "text_area", "slider", "date_input",
)


def _interactive_widgets(at):
    widgets = []
    for attr in _INTERACTIVE_ATTRS:
        widgets.extend(getattr(at, attr))
    return widgets


def _assert_all_widgets_labeled(at):
    widgets = _interactive_widgets(at)
    assert widgets, "expected the app to render at least one interactive widget"
    for w in widgets:
        assert w.label and w.label.strip(), f"widget {w!r} has an empty accessible label"


def _assert_no_positive_tabindex(at):
    for m in at.markdown:
        for value in re.findall(r'tabindex\s*=\s*["\']?(-?\d+)', m.value):
            assert int(value) <= 0, f"positive tabindex found in emitted markup: {m.value[:120]!r}"


def _assert_all_images_have_alt(at):
    for m in at.markdown:
        for tag in re.findall(r"<img[^>]*>", m.value):
            alt = re.search(r'alt="([^"]*)"', tag)
            assert alt and alt.group(1).strip(), f"<img> without non-empty alt text: {tag!r}"


def _make_assessment_with_catastrophic():
    stage = StageResult(
        stage="pre", harm_type="Hypoglycemia", severity="3", tir="70.0", tbr="10.0", tar="5.0",
        lbgi_score_avg=3, dka_score_avg=1, hyperglycemia_score=0, n_sims=4,
        lbgi_value_avg="2.5", dka_index_value_avg="21.91",
    )
    return SeverityAssessment(
        simulation_id="TLR-CAT", subdirectory_name="TLR-CAT", timestamp="2026-07-21T00:00:00",
        profile_count=4, stages={"pre": stage, "no_loop": stage, "post": stage},
        catastrophic_findings=[
            CatastrophicFinding(sim_id="s1", stage="pre", condition="extended_low", updated_severity=5)
        ],
    )


# ---------------------------------------------------------------------------
# WCAG 1.4.3 -- contrast (unit)
# ---------------------------------------------------------------------------

def test_contrast_helper_matches_known_reference_ratio():
    # Black on white is the canonical 21:1 anchor -- guards the helper itself so a
    # broken formula can't make the token assertions below pass vacuously.
    assert round(contrast_ratio("#000000", "#FFFFFF"), 1) == 21.0


def test_theme_text_on_background_meets_normal_text_contrast():
    theme = _theme_tokens()
    ratio = contrast_ratio(theme["textColor"], theme["backgroundColor"])
    assert ratio >= NORMAL_TEXT_MIN, (
        f"textColor {theme['textColor']} on backgroundColor {theme['backgroundColor']} "
        f"is {ratio:.2f}:1, below the {NORMAL_TEXT_MIN}:1 normal-text minimum"
    )


def test_css_override_text_on_secondary_background_meets_normal_text_contrast():
    theme = _theme_tokens()
    fg = _css_override_text_color()
    ratio = contrast_ratio(fg, theme["secondaryBackgroundColor"])
    assert ratio >= NORMAL_TEXT_MIN, (
        f"CSS-override text {fg} on secondaryBackgroundColor {theme['secondaryBackgroundColor']} "
        f"is {ratio:.2f}:1, below the {NORMAL_TEXT_MIN}:1 normal-text minimum"
    )


def test_primary_color_on_white_is_a_documented_known_finding_not_a_gate():
    # brand primaryColor (#627CFF ~3.6:1 on white) is BELOW the 4.5:1 normal-text
    # minimum but is excluded from the pass/fail gate per TRSET-4 adjudication
    # (brand color; remediation is a separate ticket -- Out-of-Scope). This test
    # documents the known state and flags loudly if the value ever shifts out of
    # its known band, so the adjudication can be revisited -- it is not a failure.
    theme = _theme_tokens()
    ratio = contrast_ratio(theme["primaryColor"], theme["backgroundColor"])
    assert LARGE_TEXT_MIN <= ratio < NORMAL_TEXT_MIN, (
        f"primaryColor {theme['primaryColor']} on white is now {ratio:.2f}:1 -- outside the "
        f"known ~3.6:1 band [{LARGE_TEXT_MIN}, {NORMAL_TEXT_MIN}); re-adjudicate the brand-color finding"
    )


# ---------------------------------------------------------------------------
# WCAG 2.1 (Keyboard) + 1.3 (Adaptable) -- rendered smoke
# ---------------------------------------------------------------------------

def test_all_interactive_widgets_have_accessible_labels():
    # Serves both 2.1 (a keyboard user needs a programmatic name for each control)
    # and 1.3 (label is programmatically associated), so it is asserted once.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception
    _assert_all_widgets_labeled(at)


def test_unsafe_html_blocks_introduce_no_keyboard_trap():
    # The app's only unsafe_allow_html output is _BRAND_CSS and the logo <img>;
    # neither may inject a positive tabindex that would break natural tab order.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception
    _assert_no_positive_tabindex(at)


def test_all_emitted_images_carry_alt_text():
    # WCAG 1.3: non-text content the app emits has a text alternative. Generalizes
    # the TRSET-3 logo-alt guard to any <img> the app adds later.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception
    _assert_all_images_have_alt(at)
    logo_imgs = [
        m for m in at.markdown
        if "<img" in m.value and f'alt="{streamlit_app.LOGO_ALT_TEXT}"' in m.value
    ]
    assert len(logo_imgs) == 1, "expected exactly one logo <img> with the sanctioned alt text"


def test_severity_information_is_conveyed_as_text_not_color_alone():
    # WCAG 1.3: a catastrophic finding's meaning is carried by text -- the heading
    # label and the severity value in the table -- not by color alone.
    fake_result = RunResult(
        save_dir="/tmp/fake",
        risk_dir_results=[RiskDirRunResult("TLR-CAT", _make_assessment_with_catastrophic(), [])],
        cancelled=False,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = fake_result
    at.run()
    assert not at.exception

    assert any("catastrophic findings" in m.value.lower() for m in at.markdown), (
        "catastrophic findings must be announced in text, not conveyed by color alone"
    )
    stage_df = at.dataframe[0].value
    assert "Severity" in stage_df.columns
    assert stage_df["Severity"].iloc[0] == "3", "severity must render as a text value, not a color swatch"


# ---------------------------------------------------------------------------
# Feature gate -- end-to-end integration accessibility check
# ---------------------------------------------------------------------------

def test_integration_full_app_run_is_accessible():
    # TRSET-4 Feature gate: one end-to-end AppTest run against the REAL config
    # library (mirroring test_integration_full_app_run_renders_header_and_logo in
    # test_streamlit_app.py), asserting the three accessibility markers at the
    # full-system rendered-tree boundary -- not against a mocked result.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception

    # 2.1 + 1.3: every interactive control the real render emits is labeled.
    _assert_all_widgets_labeled(at)
    # 1.3: the logo image carries alt text.
    _assert_all_images_have_alt(at)
    # 2.1: no keyboard trap injected by the app's unsafe_allow_html.
    _assert_no_positive_tabindex(at)
