"""Apply evidence-backed line execution profiles to a lane's saved policy.

The dashboard's config API requires a browser session, so profile changes
normally travel through the UI. This tool writes the same saved-policy
payload directly to a lane database, using the exact save shape the server
writes (`_save_policy`): the full normalized policy plus a fresh execution
control token. A running server notices the rotated token on its next cycle
or status read (`_refresh_policy_authority`), adopts the saved policy, and
defensively disarms its latches — the architecture's designed behavior for
"a policy saved by another local server."

Lane consequences of applying while the server runs:

- dry lane: entries and simulated exits never consult the arm latches, so
  automation continues seamlessly under the new profiles;
- live lane: the armed latch and protective exits drop until the operator
  re-arms, so a running live lane should be updated through the UI or with
  the server stopped.

Only ``line_execution_profiles`` is changed. Every other saved field is
preserved exactly, including automation state, lane mode, and the lane-wide
reversal window under A/B measurement.

The embedded recommended payloads come from the 2026-08-02 settlement
grading (1,030 graded positions, 72 events, both lanes; see
docs/mlb-line-profile-optimization-2026-08-02.md). They include per-line
``reversal_confirmation_readings``, which servers running code older than
2026-08-02 cannot parse — writing such a payload under a running old server
would break its policy reads. The tool therefore refuses new-field overrides
while a server is listening unless ``--strip-new-fields`` degrades the
payload to the legacy-safe subset.

Preview by default; nothing is written without ``--apply``.
"""
from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.polymarket_us_trading import (
    _CONTROL_TOKEN_KEY,
    SUPPORTED_ENTRY_MARKET_TYPES,
    TradingPolicy,
    TradingPolicyError,
)

LANE_DATABASES = {
    "live": Path("polymarket-us-trading.db"),
    "dry": Path("workstation-data/polymarket-us-dry-run.db"),
}
DEFAULT_SERVER_PORT = 8775

# Profile override fields the schema learned after 2026-08-02, newest last.
# A server process started from older code raises on payloads carrying
# fields it does not know, which would break every policy read on that
# runtime; each field is only provably safe once the stored payload already
# carries it (the running server must have parsed it to be serving).
RUNTIME_NEW_PROFILE_FIELDS = frozenset({
    "reversal_confirmation_readings",   # 2026-08-02
    "reversal_confirmation_seconds",    # 2026-08-03, wall-clock floor
})

# Guards-off (2026-08-03, operator-directed): on moneyline and spread every
# price-triggered exit measured — reversals at any window, stops at any
# width — lost to settlement once full price paths (lifetime marks joined
# with the 30-minute post-exit recovery window) were graded: guards-off was
# positive on all eight measured lane-nights while the actual book was
# negative on nearly all of them. stop_loss 0.95 and readings 10 are the
# loosest values the validator allows; the per-position stake is the real
# risk bound. Profit targets stay (0.40/0.30) — they trade a little upside
# for night-to-night steadiness. Totals joined guards-off once the side
# split landed: the guards' protective record was pure Over contamination
# (reversal-sold Unders won 76%, +$197 giveback; stopped Unders won 50%,
# +$275) — the Under-only side gate is the real totals protection now.
# Entry gates (bands, edge windows, side gate) carry the edge:
# ML 0.45 price cliff, ML max_edge 0.10 red-flag cap, totals Unders-only
# in the cheap high-conviction pocket.
# 2026-08-04 derivation (settled data through the Aug 3 slate; the lock
# readout graded 16 of 19 decay sales as settlement winners, +$170.51 left):
# live takes only CI-validated changes; dry probes one regime step further.
RECOMMENDED_PROFILES: dict[str, list[dict[str, Any]]] = {
    "live": [
        # ML floor 0.10: the 10-15c pocket graded +403.8%/$1 with the CI
        # excluding zero (n=10, [+97, +773]) and 15-25c +245.9% (n=34).
        # First-sight entry stays (the 2-reading rule graded +142.7%/$1
        # left behind on ML); decay -0.30 is the operator's 2026-08-03
        # setting, untested but directionally confirmed by the readout.
        {"market_type": "moneyline", "game_stage": "all", "enabled": True,
         "overrides": {"min_entry_price": 0.10, "max_entry_price": 0.45,
                       "min_edge": 0.03, "max_edge": 0.10,
                       "entry_confirmation_readings": 1,
                       "exit_edge": -0.30,
                       "stop_loss": 0.95, "profit_target": 0.40,
                       "reversal_confirmation_readings": 10,
                       "reversal_confirmation_seconds": 300.0}},
        # Spread adopts agreement >= 55: within kept-side dogs the sub-55
        # band's CI includes zero ([-10, +81]) while 55-70 is validated
        # (+75.2%, [+16, +132]) - gating at 70 would discard proven volume.
        # Concentrated size caps stay.
        {"market_type": "spread", "game_stage": "all", "enabled": True,
         "overrides": {"max_position_usd": 1.0,
                       "max_profile_open_positions": 2,
                       "max_profile_exposure_usd": 5.0,
                       "min_entry_price": 0.23, "max_entry_price": 0.45,
                       "min_edge": 0.03, "min_source_agreement": 55.0,
                       "exit_edge": -0.15,
                       "stop_loss": 0.95, "profit_target": 0.30,
                       "reversal_confirmation_readings": 10,
                       "reversal_confirmation_seconds": 300.0}},
        {"market_type": "spread", "game_stage": "late", "enabled": True,
         "overrides": {}},
        # Totals ceiling stays 0.38 on the money lane: the 38-45c Unders
        # band leans positive (+51.9%) but its CI includes zero
        # ([-9.4, +93.9]) - not validated, so the safe lane holds while
        # dry keeps measuring at 0.45.
        {"market_type": "total", "game_stage": "all", "enabled": True,
         "overrides": {"min_entry_price": 0.15, "max_entry_price": 0.38,
                       "min_edge": 0.10, "max_position_usd": 1.0,
                       "profit_target": 0.30, "exit_edge": -0.20,
                       "stop_loss": 0.95,
                       "reversal_confirmation_readings": 10,
                       "reversal_confirmation_seconds": 300.0}},
    ],
    "dry": [
        # The lab runs one regime step past live everywhere: floor 0.06
        # (the validator requires strictly above the 5c hard bound; this
        # collects the thin 6-10c band), min_edge 0.01 (every dry-clean
        # kept-side edge band graded positive with CIs excluding zero;
        # the reversal trigger stays -0.03 because it floors at
        # -max(0.03, min_edge)), and decay -0.50 (the -0.03 locks sold
        # 84% eventual winners; -0.30 has no data yet, so dry probes the
        # next step while live holds -0.30).
        {"market_type": "moneyline", "game_stage": "all", "enabled": True,
         "overrides": {"min_entry_price": 0.06, "max_entry_price": 0.45,
                       "min_edge": 0.01, "max_edge": 0.10,
                       "exit_edge": -0.50,
                       "stop_loss": 0.95, "profit_target": 0.40,
                       "reversal_confirmation_readings": 10,
                       "reversal_confirmation_seconds": 300.0}},
        # The agreement>=70 experiment answered (gradient validated, gate
        # adopted by live at 55); dry reopens fully so the sub-55 control
        # band keeps accruing evidence.
        {"market_type": "spread", "game_stage": "all", "enabled": True,
         "overrides": {"min_entry_price": 0.15, "max_entry_price": 0.45,
                       "min_edge": 0.01, "exit_edge": -0.30,
                       "stop_loss": 0.95, "profit_target": 0.30,
                       "reversal_confirmation_readings": 10,
                       "reversal_confirmation_seconds": 300.0}},
        # Unders: ceiling 0.45 under continued measurement, conviction
        # floor relaxed to 0.03 (every Unders edge band graded positive).
        {"market_type": "total", "game_stage": "all", "enabled": True,
         "overrides": {"min_entry_price": 0.15, "max_entry_price": 0.45,
                       "min_edge": 0.03, "profit_target": 0.60,
                       "exit_edge": -0.35,
                       "stop_loss": 0.95,
                       "reversal_confirmation_readings": 10,
                       "reversal_confirmation_seconds": 300.0}},
    ],
}


def server_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def new_field_overrides(profiles: list[dict[str, Any]]) -> set[str]:
    used: set[str] = set()
    for profile in profiles:
        used |= set(profile.get("overrides") or {}) & RUNTIME_NEW_PROFILE_FIELDS
    return used


def proven_runtime_fields(current_payload: dict[str, Any]) -> set[str]:
    """New fields the listening runtime has demonstrably parsed.

    A server process loads the stored payload at startup and adopts every
    later save, so any new field already present in the stored profiles has
    been parsed by the running code. Fields absent from the stored payload
    stay unproven — a runtime that parses one new field may still predate a
    newer one.
    """
    return new_field_overrides(
        list(current_payload.get("line_execution_profiles") or [])
    )


def strip_new_fields(
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stripped = []
    for profile in profiles:
        overrides = {
            field: value
            for field, value in (profile.get("overrides") or {}).items()
            if field not in RUNTIME_NEW_PROFILE_FIELDS
        }
        stripped.append({**profile, "overrides": overrides})
    return stripped


def load_saved_payload(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(
        f"file:{database.expanduser().resolve().as_posix()}?mode=ro", uri=True
    )
    try:
        row = connection.execute(
            "SELECT payload FROM live_trading_config WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit(f"{database}: no saved trading policy")
    payload = json.loads(row[0])
    if not isinstance(payload, dict):
        raise SystemExit(f"{database}: saved policy payload is not an object")
    return payload


def merged_policy(
    current_payload: dict[str, Any],
    profiles: list[dict[str, Any]],
    lane_overrides: dict[str, Any] | None = None,
) -> TradingPolicy:
    return TradingPolicy.from_mapping({
        **current_payload,
        **(lane_overrides or {}),
        "line_execution_profiles": profiles,
    })


def apply_profiles(
    database: Path,
    profiles: list[dict[str, Any]],
    *,
    lane_overrides: dict[str, Any] | None = None,
    reason: str = "external_profile_apply",
) -> str:
    """Write the merged policy the way the server saves it, with a session
    boundary, and return the fresh control token the runtime will adopt."""
    policy = merged_policy(
        load_saved_payload(database), profiles, lane_overrides
    )
    token = uuid4().hex
    stored = json.dumps(
        {**asdict(policy), _CONTROL_TOKEN_KEY: token},
        sort_keys=True,
        separators=(",", ":"),
    )
    policy_json = json.dumps(
        asdict(policy), sort_keys=True, separators=(",", ":")
    )
    now = time.time()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        with connection:
            connection.execute(
                """INSERT INTO live_trading_config(singleton,payload,updated_ts)
                   VALUES (1,?,?) ON CONFLICT(singleton) DO UPDATE SET
                   payload=excluded.payload,updated_ts=excluded.updated_ts""",
                (stored, now),
            )
            connection.execute(
                "UPDATE trading_policy_sessions SET ended_ts=? "
                "WHERE ended_ts IS NULL",
                (now,),
            )
            connection.execute(
                """INSERT INTO trading_policy_sessions
                   (id,started_ts,ended_ts,mode,reason,policy_json)
                   VALUES (?,?,NULL,?,?,?)""",
                (str(uuid4()), now, policy.execution_mode, reason, policy_json),
            )
    finally:
        connection.close()
    return token


def describe_profiles(profiles: tuple[dict[str, Any], ...] | list) -> str:
    if not profiles:
        return "    (none — global fallback only)"
    lines = []
    for profile in sorted(
        profiles, key=lambda p: (p["market_type"], p["game_stage"])
    ):
        state = "on " if profile.get("enabled", True) else "OFF"
        overrides = profile.get("overrides") or {}
        detail = (
            "  ".join(f"{k}={v}" for k, v in sorted(overrides.items()))
            or "(inherits every lane value)"
        )
        lines.append(
            f"    {profile['market_type']:10s}/{profile['game_stage']:7s}"
            f" {state}  {detail}"
        )
    return "\n".join(lines)


def describe_resolution(policy: TradingPolicy) -> str:
    stages = (("all", None), ("early", 0.75), ("middle", 0.375), ("late", 0.10))
    fields = (
        "min_entry_price", "max_entry_price", "min_edge", "max_edge",
        "stop_loss", "profit_target", "reversal_confirmation_readings",
    )
    lines = [
        "    line/stage           auth  "
        + " ".join(f"{field[-13:]:>13s}" for field in fields)
    ]
    for market_type in SUPPORTED_ENTRY_MARKET_TYPES:
        for stage, fraction in stages:
            effective, _key = policy.execution_policy_for(market_type, fraction)
            name = f"{market_type}/{stage}"
            if effective is None:
                lines.append(f"    {name:20s} BLOCK")
                continue
            values = " ".join(
                f"{getattr(effective, field):>13.2f}" for field in fields
            )
            lines.append(f"    {name:20s} ok   {values}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--lane", choices=("live", "dry"), required=True)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Lane database (defaults to the lane's standard path)",
    )
    parser.add_argument(
        "--payload",
        type=Path,
        default=None,
        help="JSON file with a line_execution_profiles list; defaults to the "
        "embedded evidence-backed recommendation for the lane",
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write; preview is the default")
    parser.add_argument(
        "--allow-running-server",
        action="store_true",
        help="Permit writing while a server listens on --port. The runtime "
        "adopts the save and disarms its latches: the dry lane continues, "
        "the live lane requires re-arming.",
    )
    parser.add_argument(
        "--strip-new-fields",
        action="store_true",
        help="Drop override fields a pre-2026-08-02 server cannot parse, "
        "for applying under a running server started from older code",
    )
    parser.add_argument(
        "--total-sides",
        default=None,
        help="Comma list of game-total sides allowed to enter (e.g. 'under' "
        "or 'over,under'). Lane-wide field; requires a server restarted "
        "onto 2026-08-03+ code to take effect.",
    )
    parser.add_argument(
        "--spread-sides",
        default=None,
        help="Comma list of run-line sides allowed to enter (e.g. "
        "'underdog' or 'favorite,underdog'). Lane-wide field; requires a "
        "server restarted onto 2026-08-03+ code to take effect.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT)
    args = parser.parse_args()

    database = args.db or LANE_DATABASES[args.lane]
    if not database.exists():
        raise SystemExit(f"{database}: not found")

    if args.payload is not None:
        loaded = json.loads(args.payload.read_text(encoding="utf-8"))
        profiles = (
            loaded["line_execution_profiles"]
            if isinstance(loaded, dict)
            else loaded
        )
    else:
        profiles = RECOMMENDED_PROFILES[args.lane]
    if args.strip_new_fields:
        profiles = strip_new_fields(profiles)

    overrides: dict[str, Any] = {}
    if args.total_sides is not None:
        overrides["allowed_total_sides"] = [
            side.strip() for side in args.total_sides.split(",")
            if side.strip()
        ]
    if args.spread_sides is not None:
        overrides["allowed_spread_sides"] = [
            side.strip() for side in args.spread_sides.split(",")
            if side.strip()
        ]
    lane_overrides: dict[str, Any] | None = overrides or None

    listening = server_is_listening(args.port)
    current_payload = load_saved_payload(database)
    try:
        policy = merged_policy(current_payload, profiles, lane_overrides)
    except TradingPolicyError as exc:
        raise SystemExit(f"merged policy is invalid: {exc}") from exc

    print(f"lane: {args.lane}  database: {database}")
    print(f"mode: {policy.execution_mode}  automation_enabled: "
          f"{policy.automation_enabled}  server on :{args.port}: "
          f"{'LISTENING' if listening else 'not listening'}")
    print(f"total sides: stored "
          f"{current_payload.get('allowed_total_sides') or ['over', 'under']} "
          f"-> proposed {list(policy.allowed_total_sides)}")
    print(f"spread sides: stored "
          f"{current_payload.get('allowed_spread_sides') or ['favorite', 'underdog']} "
          f"-> proposed {list(policy.allowed_spread_sides)}")
    print("\ncurrent profiles:")
    print(describe_profiles(current_payload.get("line_execution_profiles") or []))
    print("\nproposed profiles:")
    print(describe_profiles(profiles))
    print("\nresolved line/stage combinations under the proposal:")
    print(describe_resolution(policy))

    if not args.apply:
        print("\npreview only; nothing was written. Re-run with --apply.")
        return 0

    if listening:
        unproven = new_field_overrides(profiles) - proven_runtime_fields(
            current_payload
        )
        if unproven:
            raise SystemExit(
                "refusing: a server is listening and the payload carries "
                f"override field(s) {sorted(unproven)} the running code has "
                "not demonstrably parsed. Restart the server onto current "
                "code first, or re-run with --strip-new-fields."
            )
        if not args.allow_running_server:
            raise SystemExit(
                "refusing: a server is listening. It would adopt this save "
                "and disarm its latches (dry continues; live requires "
                "re-arm). Re-run with --allow-running-server to proceed, "
                "or stop the server."
            )

    token = apply_profiles(database, profiles, lane_overrides=lane_overrides)
    print(f"\napplied. control token rotated to {token[:8]}...")
    if listening:
        print(
            "The running server adopts this within one cycle/status read and "
            "disarms its latches. Dry automation continues; a live lane must "
            "be re-armed (settings -> Arm last)."
        )
    if lane_overrides:
        print(
            "NOTE: allowed_total_sides only takes effect on a server running "
            "2026-08-03+ code, and any save from an older runtime drops it. "
            "Restart the server before arming tonight."
        )
    else:
        print("No server is listening; the policy loads on next start.")
    print(
        "Reminder: any UI save overwrites this; check profiles after any "
        "advisor apply."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
