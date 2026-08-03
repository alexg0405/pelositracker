"""Contract between the advisor's suggestions and the line-profile editor.

The dashboard can load a line-scoped recommendation straight into that line's
execution profile. Three things must stay true for that to be safe, and all
three are easy to break from either side, so they are asserted rather than
assumed.
"""
import re
from pathlib import Path

from app.policy_advisor import ADVISOR_TUNABLE_FIELDS, POLICY_FIELD_CATALOG
from app.polymarket_us_trading import LINE_EXECUTION_PROFILE_FIELDS


JS = Path("app/static/index.js").read_text(encoding="utf-8")
HTML = Path("app/static/index.html").read_text(encoding="utf-8")


def _js_list(name):
    body = re.search(rf"const {name} = \[(.*?)\];", JS, re.S).group(1)
    return set(re.findall(r'"([a-z_]+)"', body))


def _js_object_keys(name):
    body = re.search(rf"const {name} = \{{(.*?)\}};", JS, re.S).group(1)
    return set(re.findall(r"^\s*([a-z_]+):", body, re.M))


def test_every_field_loaded_into_a_profile_is_one_a_profile_can_carry():
    applicable = _js_list("PROFILE_APPLICABLE_ADVISOR_FIELDS")

    assert applicable, "the profile-apply field list should not be empty"
    # A field the profile schema rejects would fail validation on save.
    assert applicable <= set(LINE_EXECUTION_PROFILE_FIELDS)
    # A field the advisor never suggests would silently never be filled.
    assert applicable <= set(ADVISOR_TUNABLE_FIELDS)


def test_no_advisor_field_is_silently_dropped_when_scoping_to_a_line():
    applicable = _js_list("PROFILE_APPLICABLE_ADVISOR_FIELDS")
    lane_wide = _js_object_keys("LANE_WIDE_ADVISOR_FIELDS")

    # Every tunable field is either copied into the profile or explicitly
    # explained as lane-wide. Nothing may fall between the two.
    assert applicable | lane_wide == set(ADVISOR_TUNABLE_FIELDS)
    assert not (applicable & lane_wide)


def test_profile_inputs_use_the_unit_the_advisor_reports():
    """A fraction must land in a percent input, and a count must not."""
    applicable = _js_list("PROFILE_APPLICABLE_ADVISOR_FIELDS")
    # Advisor values that are fractions of one rather than counts or scores.
    fractions = {
        "min_edge", "max_edge", "min_entry_price", "max_entry_price",
        "min_mlb_fraction_remaining",
    }
    for field in applicable:
        markup = re.search(
            rf'data-profile-field="{field}"[^>]*', HTML
        ).group(0)
        is_percent = "data-profile-percent" in markup
        assert is_percent == (field in fractions), (
            f"{field}: profile input percent={is_percent} but advisor "
            f"reports it as {'a fraction' if field in fractions else 'a count/score'}"
        )


def test_exit_fields_loaded_into_a_profile_are_ones_a_profile_can_carry():
    exits = _js_list("PROFILE_EXIT_FIELDS")

    assert exits, "the exit-apply field list should not be empty"
    assert exits <= set(LINE_EXECUTION_PROFILE_FIELDS)
    # Every one must be a field the advisor actually reports on.
    catalogued = {item["field"] for item in POLICY_FIELD_CATALOG}
    assert exits <= catalogued


def test_adaptive_overlay_controls_are_never_loaded_into_a_line_profile():
    """They are lane-wide; a profile cannot carry them."""
    adaptive = {
        "adaptive_exit_enabled", "adaptive_exit_profile",
        "adaptive_exit_horizon_minutes", "adaptive_exit_min_samples",
        "adaptive_exit_max_tightening", "volatility_stop_enabled",
        "stateless_stop_confirmation", "stop_confirmation_readings",
        "stop_grace_minutes", "catastrophic_stop_multiplier",
    }
    assert not (adaptive & set(LINE_EXECUTION_PROFILE_FIELDS))
    assert not (adaptive & _js_list("PROFILE_EXIT_FIELDS"))
    assert not (adaptive & _js_list("PROFILE_APPLICABLE_ADVISOR_FIELDS"))


def test_adaptive_recommendation_has_its_own_region_not_the_blocker_box():
    """An exit recommendation must not render under "entries are blocked"."""
    assert 'id="us-adaptive-recommendation"' in HTML
    # It is a sibling of the blocker box inside the execution-state panel.
    panel = re.search(
        r'<section id="us-execution-state".*?</section>', HTML, re.S
    ).group(0)
    blockers_at = panel.index('id="us-execution-blockers"')
    slot_at = panel.index('id="us-adaptive-recommendation"')
    assert slot_at > blockers_at
    assert 'id="us-adaptive-recommendation"' not in re.search(
        r'id="us-execution-blockers"[^>]*>.*?</div>', panel, re.S
    ).group(0)

    # The renderer targets that slot and does not append to the blocker box.
    render = re.search(
        r"function renderExecutionState\(status\) \{.*?\n  \}", JS, re.S
    ).group(0)
    assert 'querySelector("#us-adaptive-recommendation")' in render
    appended = re.findall(r"blockerBox\.innerHTML \+=", render)
    # Only the cold-start caveat still appends to the blocker box.
    assert len(appended) == 1
    assert "us-adaptive-recommendation" not in render.split(
        "blockerBox.innerHTML +="
    )[1].split("\n    }")[0]
