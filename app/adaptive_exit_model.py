"""Persistent, local-only adaptive exit research for managed MLB positions.

The model is deliberately downstream of the established signal engine.  It
reads the engine's current edge as one feature, but never changes probability,
edge, signal quality, calibration, entry gates, or the hard stop-loss.

Learning target
---------------
Each bounded observation asks whether the executable value of the complete
position moves adversely before it moves favorably over a future horizon.  The
labels are resolved only after that future window exists.  Estimates use
event-balanced empirical rates with a conservative baseball-state prior so one
long game cannot masquerade as hundreds of independent games.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import threading
from typing import Any
from collections.abc import Callable, Mapping

from .approval import APPROVAL_TOKEN, approval_granted, approval_instruction
from .database import Database
from .models import Event, GameState
from .sport_model_lab import _mlb_context


ADAPTIVE_EXIT_MODEL_VERSION = "mlb-adaptive-exit-v2-line-aware"
ADAPTIVE_EXIT_CLEAR_PHRASE = APPROVAL_TOKEN
ADAPTIVE_EXIT_OBSERVATION_BUCKET_SECONDS = 30
ADAPTIVE_EXIT_MOVE_THRESHOLD = 0.03
ADAPTIVE_EXIT_NEUTRAL_THRESHOLD = 0.01
_LINE_VALUE = re.compile(r"(?<!\d)([+-]?\d+(?:\.\d+)?)(?!\d)")
ADAPTIVE_EXIT_PROFILES = {
    "observe": {
        "label": "Observe only",
        "description": "Retain and score MLB movement forecasts without changing exits.",
        "sensitivity": 0.0,
        "cold_start_confidence": 0.0,
    },
    "guarded": {
        "label": "Guarded adaptive",
        "description": "Small game-state adjustments that grow only with retained support.",
        "sensitivity": 0.65,
        "cold_start_confidence": 0.20,
    },
    "balanced": {
        "label": "Balanced adaptive",
        "description": "Moderate bounded tightening from game state and learned outcomes.",
        "sensitivity": 1.0,
        "cold_start_confidence": 0.35,
    },
    "responsive": {
        "label": "Responsive adaptive",
        "description": "Faster, larger tightening while preserving the configured hard stop.",
        "sensitivity": 1.25,
        "cold_start_confidence": 0.50,
    },
}

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS adaptive_exit_observations (
    id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    sport TEXT NOT NULL,
    league TEXT NOT NULL,
    market_type TEXT NOT NULL,
    mode TEXT NOT NULL,
    observed_ts DOUBLE PRECISION NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    inning INTEGER NOT NULL,
    half TEXT NOT NULL,
    inning_bucket TEXT NOT NULL,
    score_differential DOUBLE PRECISION NOT NULL,
    score_bucket TEXT NOT NULL,
    selected_batting INTEGER NOT NULL,
    momentum DOUBLE PRECISION,
    momentum_bucket TEXT NOT NULL,
    current_edge DOUBLE PRECISION,
    edge_bucket TEXT NOT NULL,
    exit_value DOUBLE PRECISION NOT NULL,
    highest_exit_value DOUBLE PRECISION NOT NULL,
    return_fraction DOUBLE PRECISION NOT NULL,
    context_key TEXT NOT NULL,
    state_prior_probability DOUBLE PRECISION NOT NULL,
    predicted_adverse_probability DOUBLE PRECISION NOT NULL,
    prediction_confidence DOUBLE PRECISION NOT NULL,
    feature_payload TEXT NOT NULL,
    model_version TEXT NOT NULL,
    resolved_ts DOUBLE PRECISION,
    adverse_label INTEGER,
    outcome TEXT,
    future_min_exit_value DOUBLE PRECISION,
    future_max_exit_value DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_adaptive_exit_pending
    ON adaptive_exit_observations(position_id, resolved_ts, observed_ts);
CREATE INDEX IF NOT EXISTS idx_adaptive_exit_context
    ON adaptive_exit_observations(
        sport, inning_bucket, score_bucket, selected_batting,
        momentum_bucket, edge_bucket, resolved_ts
    );
CREATE INDEX IF NOT EXISTS idx_adaptive_exit_event
    ON adaptive_exit_observations(event_id, observed_ts);
"""

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS exit_recovery_observations (
    position_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    market_type TEXT NOT NULL,
    selection TEXT NOT NULL,
    position_side TEXT NOT NULL,
    mode TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    entry_cost DOUBLE PRECISION NOT NULL,
    exit_value DOUBLE PRECISION NOT NULL,
    exit_reason TEXT NOT NULL,
    exit_ts DOUBLE PRECISION NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    state_payload TEXT NOT NULL,
    last_observed_ts DOUBLE PRECISION,
    last_exit_value DOUBLE PRECISION,
    best_exit_value DOUBLE PRECISION NOT NULL,
    worst_exit_value DOUBLE PRECISION NOT NULL,
    recovered_entry_ts DOUBLE PRECISION,
    recovered_half_loss_ts DOUBLE PRECISION,
    observation_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'tracking',
    resolved_ts DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_exit_recovery_tracking
    ON exit_recovery_observations(status, exit_ts);
CREATE INDEX IF NOT EXISTS idx_exit_recovery_event
    ON exit_recovery_observations(event_id, exit_ts);
"""

RESEARCH_EVIDENCE_TABLES = frozenset({
    "adaptive_exit_observations",
    "exit_recovery_observations",
})


@dataclass(frozen=True, slots=True)
class AdaptiveExitDecision:
    applicable: bool
    active: bool
    profile: str
    reason: str
    model_version: str
    state: dict[str, Any]
    predicted_adverse_probability: float | None
    state_prior_probability: float | None
    confidence: float
    labeled_samples: int
    labeled_events: int
    context_samples: int
    context_events: int
    tightening_fraction: float
    base_profit_target: float
    effective_profit_target: float
    base_trailing_drawdown: float
    effective_trailing_drawdown: float
    base_exit_edge: float
    effective_exit_edge: float
    hard_stop_unchanged: float

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("_", " ").split())


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _is_baseball(event: Event) -> bool:
    value = f"{event.sport} {event.league}".casefold()
    return "baseball" in value or "mlb" in value


def _selection_side(event: Event, selection: str) -> str | None:
    value = _norm(selection)
    home = _norm(event.home)
    away = _norm(event.away)
    if value in {"home", home}:
        return "home"
    if value in {"away", away}:
        return "away"
    return None


def _line_value(selection: str) -> float | None:
    matches = _LINE_VALUE.findall(selection or "")
    if not matches:
        return None
    return _finite(matches[-1])


def _market_type(value: Any) -> str:
    text = _norm(value)
    if any(token in text for token in ("moneyline", "money line", "winner", "h2h")):
        return "moneyline"
    if "spread" in text or "run line" in text:
        return "spread"
    if "total" in text or "over under" in text:
        return "total"
    return text or "unknown"


def _team_side_from_selection(event: Event, selection: str) -> str | None:
    value = _norm(selection)
    for side, team in (("home", event.home), ("away", event.away)):
        normalized = _norm(team)
        if value == normalized or value.startswith(normalized + " "):
            return side
    return _selection_side(event, selection)


def _selection_context(
    event: Event,
    position: Mapping[str, Any],
    state: GameState,
    baseball: Mapping[str, Any],
) -> dict[str, Any] | None:
    market_type = _market_type(position.get("market_type"))
    selection = str(position.get("selection") or "")
    home_score = float(state.home_score)
    away_score = float(state.away_score)
    current_total = home_score + away_score
    batting_side = baseball.get("batting_side")
    selected_team_side: str | None = None
    selection_direction: str
    line = _line_value(selection)
    if market_type == "moneyline":
        selected_team_side = _team_side_from_selection(event, selection)
        if selected_team_side is None:
            return None
        team_margin = (
            home_score - away_score
            if selected_team_side == "home"
            else away_score - home_score
        )
        selection_margin = team_margin
        selection_direction = selected_team_side
    elif market_type == "spread":
        selected_team_side = _team_side_from_selection(event, selection)
        if selected_team_side is None or line is None:
            return None
        team_margin = (
            home_score - away_score
            if selected_team_side == "home"
            else away_score - home_score
        )
        selection_margin = team_margin + line
        selection_direction = selected_team_side
    elif market_type == "total":
        normalized = _norm(selection)
        if line is None or not normalized.startswith(("over ", "under ")):
            return None
        selection_direction = "over" if normalized.startswith("over ") else "under"
        selection_margin = (
            current_total - line
            if selection_direction == "over"
            else line - current_total
        )
    else:
        return None
    selected_batting = (
        -1
        if selected_team_side is None or batting_side is None
        else int(selected_team_side == batting_side)
    )
    return {
        "market_type": market_type,
        "selection_direction": selection_direction,
        "line": line,
        "selection_margin": float(selection_margin),
        "selected_team_side": selected_team_side,
        "selected_batting": selected_batting,
        "current_total_runs": current_total,
    }


def _inning_bucket(inning: int) -> str:
    if inning <= 3:
        return "early_1_3"
    if inning <= 6:
        return "middle_4_6"
    if inning <= 8:
        return "late_7_8"
    if inning == 9:
        return "ninth"
    return "extra"


def _score_bucket(score_differential: float) -> str:
    if score_differential <= -2:
        return "trailing_multi"
    if score_differential == -1:
        return "trailing_one"
    if score_differential == 0:
        return "tied"
    if score_differential == 1:
        return "leading_one"
    return "leading_multi"


def _momentum_bucket(momentum: float | None) -> str:
    if momentum is None:
        return "unknown"
    if momentum <= -0.02:
        return "falling_fast"
    if momentum < -0.005:
        return "falling"
    if momentum >= 0.02:
        return "rising_fast"
    if momentum > 0.005:
        return "rising"
    return "flat"


def _edge_bucket(edge: float | None) -> str:
    if edge is None:
        return "unknown"
    if edge <= -0.03:
        return "negative_material"
    if edge <= 0:
        return "nonpositive"
    if edge < 0.03:
        return "positive_thin"
    return "positive"


class AdaptiveExitModel:
    """Persistent event-balanced movement learner for local MLB lines."""

    def __init__(
        self,
        path: str | None,
        *,
        clock: Callable[[], float],
        database_namespace: str | None = None,
    ):
        self._db = Database.open(
            path,
            sqlite_envs=("POLYMARKET_US_TRADING_DB",),
            sqlite_default=path or "polymarket-us-trading.db",
            postgres_schema=database_namespace,
        )
        self.path = self._db.target
        self._clock = clock
        self._lock = threading.RLock()
        with self._lock:
            self._db.initialize(
                _SCHEMA_V1,
                component="adaptive_exit_learning",
                version=1,
            )
            self._db.initialize(
                _SCHEMA_V2,
                component="adaptive_exit_learning",
                version=2,
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def iter_research_batches(
        self,
        *,
        batch_size: int = 500,
    ):
        """Yield resolved learning evidence without exporting active trackers."""
        with self._lock:
            for table in sorted(RESEARCH_EVIDENCE_TABLES):
                for rows in self._db.iter_table_batches(
                    table, batch_size=batch_size
                ):
                    if table == "adaptive_exit_observations":
                        rows = [
                            row for row in rows
                            if row.get("resolved_ts") is not None
                        ]
                    else:
                        rows = [
                            row for row in rows
                            if row.get("resolved_ts") is not None
                            or str(row.get("status") or "").casefold()
                            not in {"", "tracking"}
                        ]
                    if rows:
                        yield table, rows

    def merge_research_batch(
        self,
        table: str,
        rows: list[dict[str, Any]],
    ) -> int:
        if table not in RESEARCH_EVIDENCE_TABLES:
            raise ValueError("unsupported adaptive-exit evidence table")
        if table == "adaptive_exit_observations":
            rows = [row for row in rows if row.get("resolved_ts") is not None]
        else:
            rows = [
                row for row in rows
                if row.get("resolved_ts") is not None
                or str(row.get("status") or "").casefold()
                not in {"", "tracking"}
            ]
        with self._lock:
            return self._db.merge_table_rows(table, rows)

    @staticmethod
    def profiles() -> dict[str, dict[str, Any]]:
        return {
            name: dict(values)
            for name, values in ADAPTIVE_EXIT_PROFILES.items()
        }

    def base_decision(
        self,
        *,
        profile: str,
        reason: str,
        applicable: bool,
        profit_target: float,
        trailing_drawdown: float,
        exit_edge: float,
        stop_loss: float,
        state: Mapping[str, Any] | None = None,
    ) -> AdaptiveExitDecision:
        return AdaptiveExitDecision(
            applicable=applicable,
            active=False,
            profile=profile,
            reason=reason,
            model_version=ADAPTIVE_EXIT_MODEL_VERSION,
            state=dict(state or {}),
            predicted_adverse_probability=None,
            state_prior_probability=None,
            confidence=0.0,
            labeled_samples=0,
            labeled_events=0,
            context_samples=0,
            context_events=0,
            tightening_fraction=0.0,
            base_profit_target=profit_target,
            effective_profit_target=profit_target,
            base_trailing_drawdown=trailing_drawdown,
            effective_trailing_drawdown=trailing_drawdown,
            base_exit_edge=exit_edge,
            effective_exit_edge=exit_edge,
            hard_stop_unchanged=stop_loss,
        )

    def observe(
        self,
        *,
        position: Mapping[str, Any],
        event: Event | None,
        state: GameState | None,
        exit_value: float,
        highest_exit_value: float,
        return_fraction: float,
        current_edge: float | None,
        profile: str,
        horizon_seconds: int,
        minimum_samples: int,
        maximum_tightening: float,
        profit_target: float,
        trailing_drawdown: float,
        exit_edge: float,
        stop_loss: float,
    ) -> AdaptiveExitDecision:
        if profile not in ADAPTIVE_EXIT_PROFILES:
            profile = "observe"
        if event is None or not _is_baseball(event):
            return self.base_decision(
                profile=profile,
                reason="adaptive exit currently supports MLB/baseball only",
                applicable=False,
                profit_target=profit_target,
                trailing_drawdown=trailing_drawdown,
                exit_edge=exit_edge,
                stop_loss=stop_loss,
            )
        market_type = _market_type(position.get("market_type"))
        if market_type not in {"moneyline", "spread", "total"}:
            return self.base_decision(
                profile=profile,
                reason="adaptive exit supports MLB moneylines, run lines, and totals",
                applicable=False,
                profit_target=profit_target,
                trailing_drawdown=trailing_drawdown,
                exit_edge=exit_edge,
                stop_loss=stop_loss,
            )
        baseball = (
            _mlb_context(state)
            if state is not None
            and (
                not state.quarantined
                or state.quarantine_reason == "unknown or invalid game clock"
            )
            else None
        )
        selection_context = (
            _selection_context(event, position, state, baseball)
            if state is not None and baseball is not None
            else None
        )
        if selection_context is None or baseball is None or state is None:
            return self.base_decision(
                profile=profile,
                reason=(
                    "an explicit MLB inning/half and exact moneyline, run-line, "
                    "or total selection are required"
                ),
                applicable=True,
                profit_target=profit_target,
                trailing_drawdown=trailing_drawdown,
                exit_edge=exit_edge,
                stop_loss=stop_loss,
            )
        if bool(state.ended) or _norm(state.status) in {
            "final",
            "ended",
            "complete",
            "completed",
            "finished",
        }:
            return self.base_decision(
                profile=profile,
                reason="the game state is terminal",
                applicable=True,
                profit_target=profit_target,
                trailing_drawdown=trailing_drawdown,
                exit_edge=exit_edge,
                stop_loss=stop_loss,
            )
        received_ts = state.received_at.timestamp()
        state_age = max(0.0, self._clock() - received_ts)
        if state_age > max(180.0, horizon_seconds):
            return self.base_decision(
                profile=profile,
                reason=f"MLB game state is stale ({state_age:.0f}s old)",
                applicable=True,
                profit_target=profit_target,
                trailing_drawdown=trailing_drawdown,
                exit_edge=exit_edge,
                stop_loss=stop_loss,
            )

        score_difference = float(selection_context["selection_margin"])
        selected_batting = int(selection_context["selected_batting"])
        previous = self._previous_position_mark(str(position["id"]))
        momentum = (
            exit_value - float(previous["exit_value"])
            if previous is not None
            else None
        )
        inning = int(baseball["inning"])
        inning_group = _inning_bucket(inning)
        score_group = _score_bucket(score_difference)
        momentum_group = _momentum_bucket(momentum)
        edge_group = _edge_bucket(current_edge)
        context_key = "|".join(
            (
                market_type,
                str(selection_context["selection_direction"]),
                inning_group,
                score_group,
                str(selected_batting),
                momentum_group,
                edge_group,
            )
        )
        state_payload = {
            "inning": inning,
            "half": baseball["half"],
            "inning_bucket": inning_group,
            "score_differential": score_difference,
            "score_bucket": score_group,
            "selection_margin": score_difference,
            "selection_direction": selection_context["selection_direction"],
            "line": selection_context["line"],
            "market_type": market_type,
            "current_total_runs": selection_context["current_total_runs"],
            "fraction_remaining": baseball.get("fraction_remaining"),
            "selected_team_batting": (
                None if selected_batting < 0 else bool(selected_batting)
            ),
            "momentum": momentum,
            "momentum_bucket": momentum_group,
            "edge_bucket": edge_group,
            "state_age_seconds": state_age,
            # Retain the richer official context for later event-block model
            # comparison. The v1 rule/prior does not silently change behavior
            # from these new fields before that comparison exists.
            "outs": (
                state.sport_state.get("outs")
                if isinstance(state.sport_state, dict) else None
            ),
            "base_mask": (
                state.sport_state.get("base_mask")
                if isinstance(state.sport_state, dict) else None
            ),
            "balls": (
                state.sport_state.get("balls")
                if isinstance(state.sport_state, dict) else None
            ),
            "strikes": (
                state.sport_state.get("strikes")
                if isinstance(state.sport_state, dict) else None
            ),
            "batter_id": (
                (state.sport_state.get("batter") or {}).get("id")
                if isinstance(state.sport_state, dict)
                and isinstance(state.sport_state.get("batter"), dict)
                else None
            ),
            "pitcher_id": (
                (state.sport_state.get("pitcher") or {}).get("id")
                if isinstance(state.sport_state, dict)
                and isinstance(state.sport_state.get("pitcher"), dict)
                else None
            ),
        }
        prior = self._state_prior(
            market_type=market_type,
            inning=inning,
            score_differential=score_difference,
            selected_batting=(
                None if selected_batting < 0 else bool(selected_batting)
            ),
            momentum=momentum,
            current_edge=current_edge,
        )
        support = self._support(
            sport="baseball",
            market_type=market_type,
            inning_bucket=inning_group,
            score_bucket=score_group,
            selected_batting=selected_batting,
            momentum_bucket=momentum_group,
            edge_bucket=edge_group,
            state_prior=prior,
        )
        minimum_samples = max(5, int(minimum_samples))
        profile_values = ADAPTIVE_EXIT_PROFILES[profile]
        sample_support = min(
            1.0,
            support["labeled_samples"] / float(minimum_samples),
        )
        event_support = min(1.0, support["labeled_events"] / 5.0)
        learned_support = math.sqrt(sample_support * event_support)
        confidence = _clamp(
            float(profile_values["cold_start_confidence"])
            + (
                1.0 - float(profile_values["cold_start_confidence"])
            )
            * learned_support,
            0.0,
            1.0,
        )
        adverse_probability = float(support["probability"])
        maximum_tightening = _clamp(maximum_tightening, 0.0, 0.60)
        risk_strength = _clamp(
            (adverse_probability - 0.50) / 0.35,
            0.0,
            1.0,
        )
        tightening = _clamp(
            maximum_tightening
            * risk_strength
            * confidence
            * float(profile_values["sensitivity"]),
            0.0,
            maximum_tightening,
        )
        if profile == "observe":
            tightening = 0.0
        effective_profit_target = max(
            0.03,
            profit_target * (1.0 - 0.50 * tightening),
        )
        effective_trailing = max(
            0.015,
            trailing_drawdown * (1.0 - tightening),
        )
        effective_exit_edge = min(
            0.05,
            exit_edge + 0.03 * tightening,
        )
        active = tightening > 1e-9
        reason = (
            "observe-only forecast; base exits unchanged"
            if profile == "observe"
            else (
                "MLB adverse-movement risk tightened bounded profit exits"
                if active
                else "MLB forecast did not exceed the tightening threshold"
            )
        )
        decision = AdaptiveExitDecision(
            applicable=True,
            active=active,
            profile=profile,
            reason=reason,
            model_version=ADAPTIVE_EXIT_MODEL_VERSION,
            state=state_payload,
            predicted_adverse_probability=adverse_probability,
            state_prior_probability=prior,
            confidence=confidence,
            labeled_samples=support["labeled_samples"],
            labeled_events=support["labeled_events"],
            context_samples=support["context_samples"],
            context_events=support["context_events"],
            tightening_fraction=tightening,
            base_profit_target=profit_target,
            effective_profit_target=effective_profit_target,
            base_trailing_drawdown=trailing_drawdown,
            effective_trailing_drawdown=effective_trailing,
            base_exit_edge=exit_edge,
            effective_exit_edge=effective_exit_edge,
            hard_stop_unchanged=stop_loss,
        )
        self._insert_observation(
            position=position,
            event=event,
            horizon_seconds=horizon_seconds,
            exit_value=exit_value,
            highest_exit_value=highest_exit_value,
            return_fraction=return_fraction,
            current_edge=current_edge,
            context_key=context_key,
            state_prior=prior,
            decision=decision,
        )
        self._resolve_mature_observations(str(position["id"]))
        return decision

    def _previous_position_mark(self, position_id: str) -> dict[str, Any] | None:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT observed_ts,exit_value
                       FROM adaptive_exit_observations
                       WHERE position_id=%s
                       ORDER BY observed_ts DESC LIMIT 1""",
                    (position_id,),
                )
                row = cur.fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _state_prior(
        *,
        market_type: str,
        inning: int,
        score_differential: float,
        selected_batting: bool | None,
        momentum: float | None,
        current_edge: float | None,
    ) -> float:
        # This is a conservative cold-start prior, not a replacement win
        # probability.  It estimates short-horizon adverse *price movement*.
        # Moneylines retain the reviewed v1 cold-start prior. New line types
        # start neutral and are permitted to adapt only from event-balanced
        # labels; this avoids inventing a totals or spread probability formula.
        probability = 0.50
        if market_type == "moneyline":
            probability = 0.34
            if inning >= 7:
                probability += 0.07
            if inning >= 9:
                probability += 0.08
            if inning > 9:
                probability += 0.04
            if abs(score_differential) <= 1:
                probability += 0.07
            if score_differential < 0:
                probability += 0.04
            if selected_batting is False:
                probability += 0.05
        if momentum is not None:
            if momentum <= -0.02:
                probability += 0.10
            elif momentum < -0.005:
                probability += 0.05
            elif momentum >= 0.02:
                probability -= 0.05
        if current_edge is not None:
            if current_edge <= -0.03:
                probability += 0.10
            elif current_edge <= 0:
                probability += 0.06
            elif current_edge >= 0.05:
                probability -= 0.04
        return _clamp(probability, 0.15, 0.85)

    def _event_balanced_labels(
        self,
        cur,
        where: str,
        params: tuple[Any, ...],
    ) -> tuple[int, int, float]:
        self._db.execute(
            cur,
            """SELECT event_id,AVG(adverse_label) event_rate,
                      COUNT(*) sample_count
               FROM adaptive_exit_observations
               WHERE adverse_label IS NOT NULL AND resolved_ts IS NOT NULL
                 AND """
            + where
            + " GROUP BY event_id",
            params,
        )
        rows = [dict(row) for row in cur.fetchall()]
        return (
            sum(int(row["sample_count"] or 0) for row in rows),
            len(rows),
            sum(float(row["event_rate"]) for row in rows),
        )

    def _support(
        self,
        *,
        sport: str,
        market_type: str,
        inning_bucket: str,
        score_bucket: str,
        selected_batting: int,
        momentum_bucket: str,
        edge_bucket: str,
        state_prior: float,
    ) -> dict[str, Any]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                all_samples, all_events, all_event_success = (
                    self._event_balanced_labels(
                        cur,
                        "sport=%s AND market_type=%s",
                        (sport, market_type),
                    )
                )
                context_samples, context_events, context_event_success = (
                    self._event_balanced_labels(
                        cur,
                        """sport=%s AND market_type=%s
                           AND inning_bucket=%s AND score_bucket=%s
                           AND selected_batting=%s AND momentum_bucket=%s
                           AND edge_bucket=%s""",
                        (
                            sport,
                            market_type,
                            inning_bucket,
                            score_bucket,
                            selected_batting,
                            momentum_bucket,
                            edge_bucket,
                        ),
                    )
                )
        prior_strength = 8.0
        overall_rate = (
            all_event_success / all_events if all_events else state_prior
        )
        blended_prior = (
            prior_strength * state_prior
            + min(12, all_events) * overall_rate
        ) / (prior_strength + min(12, all_events))
        context_strength = 6.0
        probability = (
            context_strength * blended_prior + context_event_success
        ) / (context_strength + context_events)
        return {
            "probability": _clamp(probability, 0.05, 0.95),
            "labeled_samples": all_samples,
            "labeled_events": all_events,
            "context_samples": context_samples,
            "context_events": context_events,
        }

    def _insert_observation(
        self,
        *,
        position: Mapping[str, Any],
        event: Event,
        horizon_seconds: int,
        exit_value: float,
        highest_exit_value: float,
        return_fraction: float,
        current_edge: float | None,
        context_key: str,
        state_prior: float,
        decision: AdaptiveExitDecision,
    ) -> None:
        now = self._clock()
        bucket = int(now // ADAPTIVE_EXIT_OBSERVATION_BUCKET_SECONDS)
        identifier = hashlib.sha256(
            f"{position['id']}|{bucket}".encode()
        ).hexdigest()
        state = decision.state
        feature_payload = {
            **state,
            "current_edge": current_edge,
            "return_fraction": return_fraction,
            "highest_exit_value": highest_exit_value,
            "base_profit_target": decision.base_profit_target,
            "effective_profit_target": decision.effective_profit_target,
            "base_trailing_drawdown": decision.base_trailing_drawdown,
            "effective_trailing_drawdown": decision.effective_trailing_drawdown,
            "base_exit_edge": decision.base_exit_edge,
            "effective_exit_edge": decision.effective_exit_edge,
            "hard_stop_unchanged": decision.hard_stop_unchanged,
        }
        row = (
            identifier,
            str(position["id"]),
            event.id,
            event.name,
            "baseball",
            (event.league or "").casefold(),
            str(position.get("market_type") or ""),
            str(position.get("mode") or ""),
            now,
            max(30, int(horizon_seconds)),
            int(state["inning"]),
            str(state["half"]),
            str(state["inning_bucket"]),
            float(state["score_differential"]),
            str(state["score_bucket"]),
            (
                -1
                if state["selected_team_batting"] is None
                else int(bool(state["selected_team_batting"]))
            ),
            _finite(state.get("momentum")),
            str(state["momentum_bucket"]),
            current_edge,
            str(state["edge_bucket"]),
            exit_value,
            highest_exit_value,
            return_fraction,
            context_key,
            state_prior,
            float(decision.predicted_adverse_probability or state_prior),
            decision.confidence,
            json.dumps(feature_payload, sort_keys=True, separators=(",", ":")),
            ADAPTIVE_EXIT_MODEL_VERSION,
        )
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO adaptive_exit_observations
                       (id,position_id,event_id,event_name,sport,league,market_type,
                        mode,observed_ts,horizon_seconds,inning,half,inning_bucket,
                        score_differential,score_bucket,selected_batting,momentum,
                        momentum_bucket,current_edge,edge_bucket,exit_value,
                        highest_exit_value,return_fraction,context_key,
                        state_prior_probability,predicted_adverse_probability,
                        prediction_confidence,feature_payload,model_version)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(id) DO NOTHING""",
                    row,
                )

    def _resolve_mature_observations(self, position_id: str) -> int:
        now = self._clock()
        resolved = 0
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT id,observed_ts,horizon_seconds,exit_value
                       FROM adaptive_exit_observations
                       WHERE position_id=%s AND resolved_ts IS NULL
                         AND observed_ts+horizon_seconds<=%s
                       ORDER BY observed_ts LIMIT 250""",
                    (position_id, now),
                )
                pending = [dict(row) for row in cur.fetchall()]
                for row in pending:
                    start = float(row["observed_ts"])
                    horizon = float(row["horizon_seconds"])
                    end = start + horizon
                    self._db.execute(
                        cur,
                        """SELECT observed_ts,exit_value
                           FROM adaptive_exit_observations
                           WHERE position_id=%s AND observed_ts>%s
                             AND observed_ts<=%s
                           ORDER BY observed_ts""",
                        (
                            position_id,
                            start,
                            end
                            + max(
                                120.0,
                                ADAPTIVE_EXIT_OBSERVATION_BUCKET_SECONDS * 4.0,
                            ),
                        ),
                    )
                    candidates = [dict(value) for value in cur.fetchall()]
                    through_horizon = [
                        value
                        for value in candidates
                        if float(value["observed_ts"]) <= end
                    ]
                    first_after_horizon = next(
                        (
                            value
                            for value in candidates
                            if float(value["observed_ts"]) > end
                        ),
                        None,
                    )
                    future = through_horizon + (
                        [first_after_horizon]
                        if first_after_horizon is not None
                        else []
                    )
                    # A label requires a mark at or just after the horizon. This
                    # avoids treating an interrupted feed as a completed future
                    # window while still tolerating polling jitter.
                    if not future or float(future[-1]["observed_ts"]) < end:
                        continue
                    baseline = float(row["exit_value"])
                    adverse_level = baseline * (
                        1.0 - ADAPTIVE_EXIT_MOVE_THRESHOLD
                    )
                    favorable_level = baseline * (
                        1.0 + ADAPTIVE_EXIT_MOVE_THRESHOLD
                    )
                    first_adverse = next(
                        (
                            float(value["observed_ts"])
                            for value in future
                            if float(value["exit_value"]) <= adverse_level
                        ),
                        None,
                    )
                    first_favorable = next(
                        (
                            float(value["observed_ts"])
                            for value in future
                            if float(value["exit_value"]) >= favorable_level
                        ),
                        None,
                    )
                    label: int | None
                    outcome: str
                    if first_adverse is not None and (
                        first_favorable is None
                        or first_adverse <= first_favorable
                    ):
                        label = 1
                        outcome = "adverse_first"
                    elif first_favorable is not None:
                        label = 0
                        outcome = "favorable_first"
                    else:
                        ending_move = (
                            float(future[-1]["exit_value"]) / baseline - 1.0
                        )
                        if ending_move <= -ADAPTIVE_EXIT_NEUTRAL_THRESHOLD:
                            label = 1
                            outcome = "adverse_at_horizon"
                        elif ending_move >= ADAPTIVE_EXIT_NEUTRAL_THRESHOLD:
                            label = 0
                            outcome = "favorable_at_horizon"
                        else:
                            label = None
                            outcome = "neutral_at_horizon"
                    future_values = [
                        float(value["exit_value"]) for value in future
                    ]
                    self._db.execute(
                        cur,
                        """UPDATE adaptive_exit_observations
                           SET resolved_ts=%s,adverse_label=%s,outcome=%s,
                               future_min_exit_value=%s,future_max_exit_value=%s
                           WHERE id=%s AND resolved_ts IS NULL""",
                        (
                            now,
                            label,
                            outcome,
                            min(future_values),
                            max(future_values),
                            row["id"],
                        ),
                    )
                    resolved += max(0, int(cur.rowcount or 0))
        return resolved

    def track_exit(
        self,
        *,
        position: Mapping[str, Any],
        exit_value: float,
        reason: str,
        horizon_seconds: int,
    ) -> None:
        """Start a non-trading counterfactual mark after a managed exit."""
        now = self._clock()
        adaptive = position.get("adaptive_exit")
        state = (
            adaptive.get("state")
            if isinstance(adaptive, Mapping)
            and isinstance(adaptive.get("state"), Mapping)
            else {}
        )
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO exit_recovery_observations
                       (position_id,event_id,event_name,market_slug,market_type,
                        selection,position_side,mode,quantity,entry_cost,
                        exit_value,exit_reason,exit_ts,horizon_seconds,
                        state_payload,last_observed_ts,last_exit_value,
                        best_exit_value,worst_exit_value,observation_count,status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               %s,%s,%s,%s,0,'tracking')
                       ON CONFLICT(position_id) DO NOTHING""",
                    (
                        str(position["id"]),
                        str(position["event_id"]),
                        str(position["event_name"]),
                        str(position["market_slug"]),
                        _market_type(position.get("market_type")),
                        str(position.get("selection") or ""),
                        str(position["position_side"]),
                        str(position.get("mode") or ""),
                        float(position["quantity"]),
                        float(position["entry_cost"]),
                        float(exit_value),
                        str(reason),
                        now,
                        max(60, int(horizon_seconds)),
                        json.dumps(
                            dict(state),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now,
                        float(exit_value),
                        float(exit_value),
                        float(exit_value),
                    ),
                )

    def active_exit_recoveries(self, *, limit: int = 250) -> list[dict[str, Any]]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT * FROM exit_recovery_observations
                       WHERE status='tracking'
                       ORDER BY exit_ts
                       LIMIT %s""",
                    (max(1, min(int(limit), 1000)),),
                )
                return [dict(row) for row in cur.fetchall()]

    def observe_exit_recovery(
        self,
        position_id: str,
        *,
        exit_value: float,
        terminal: bool = False,
    ) -> dict[str, Any] | None:
        """Update one shadow mark without creating or modifying any order."""
        now = self._clock()
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT * FROM exit_recovery_observations
                       WHERE position_id=%s AND status='tracking'""",
                    (position_id,),
                )
                raw = cur.fetchone()
                if raw is None:
                    return None
                row = dict(raw)
                best = max(float(row["best_exit_value"]), float(exit_value))
                worst = min(float(row["worst_exit_value"]), float(exit_value))
                entry = float(row["entry_cost"])
                sold = float(row["exit_value"])
                recovered_entry_ts = row.get("recovered_entry_ts")
                if recovered_entry_ts is None and best + 1e-9 >= entry:
                    recovered_entry_ts = now
                half_loss_level = sold + max(0.0, entry - sold) * 0.50
                recovered_half_ts = row.get("recovered_half_loss_ts")
                if recovered_half_ts is None and best + 1e-9 >= half_loss_level:
                    recovered_half_ts = now
                matured = (
                    terminal
                    or now >= float(row["exit_ts"])
                    + float(row["horizon_seconds"])
                )
                status = "resolved" if matured else "tracking"
                self._db.execute(
                    cur,
                    """UPDATE exit_recovery_observations SET
                       last_observed_ts=%s,last_exit_value=%s,
                       best_exit_value=%s,worst_exit_value=%s,
                       recovered_entry_ts=%s,recovered_half_loss_ts=%s,
                       observation_count=observation_count+1,status=%s,
                       resolved_ts=%s
                       WHERE position_id=%s AND status='tracking'""",
                    (
                        now,
                        float(exit_value),
                        best,
                        worst,
                        recovered_entry_ts,
                        recovered_half_ts,
                        status,
                        now if matured else None,
                        position_id,
                    ),
                )
        return {
            "position_id": position_id,
            "status": status,
            "best_exit_value": best,
            "worst_exit_value": worst,
            "recovered_entry": recovered_entry_ts is not None,
            "recovered_half_loss": recovered_half_ts is not None,
        }

    def exit_recovery_summary(self) -> dict[str, Any]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) exits,
                              SUM(CASE WHEN status='tracking' THEN 1 ELSE 0 END)
                                  tracking,
                              SUM(CASE WHEN status='resolved' THEN 1 ELSE 0 END)
                                  resolved,
                              SUM(CASE WHEN recovered_entry_ts IS NOT NULL
                                       THEN 1 ELSE 0 END) recovered_entry,
                              SUM(CASE WHEN recovered_half_loss_ts IS NOT NULL
                                       THEN 1 ELSE 0 END) recovered_half_loss,
                              AVG(best_exit_value-exit_value) average_rebound,
                              MAX(best_exit_value-exit_value) maximum_rebound,
                              AVG(exit_value-worst_exit_value)
                                  average_avoided_further_loss
                       FROM exit_recovery_observations""",
                )
                totals = dict(cur.fetchone() or {})
                self._db.execute(
                    cur,
                    """SELECT market_type,exit_reason,COUNT(*) exits,
                              SUM(CASE WHEN recovered_entry_ts IS NOT NULL
                                       THEN 1 ELSE 0 END) recovered_entry,
                              AVG(best_exit_value-exit_value) average_rebound,
                              AVG(exit_value-worst_exit_value)
                                  average_avoided_further_loss
                       FROM exit_recovery_observations
                       GROUP BY market_type,exit_reason
                       ORDER BY exits DESC""",
                )
                segments = [dict(row) for row in cur.fetchall()]
                self._db.execute(
                    cur,
                    """SELECT position_id,event_name,market_type,selection,
                              exit_reason,exit_ts,status,entry_cost,exit_value,
                              last_exit_value,best_exit_value,worst_exit_value,
                              recovered_entry_ts,recovered_half_loss_ts,
                              observation_count
                       FROM exit_recovery_observations
                       ORDER BY exit_ts DESC LIMIT 12""",
                )
                recent = [dict(row) for row in cur.fetchall()]
        for row in recent:
            for field in (
                "exit_ts",
                "recovered_entry_ts",
                "recovered_half_loss_ts",
            ):
                row[field.removesuffix("_ts") + "_at"] = self._iso(row.pop(field))
        return {
            "scope": "non_trading_post_exit_counterfactual",
            "price_basis": (
                "public executable-side snapshot; no hypothetical order is sent"
            ),
            "exits": int(totals.get("exits") or 0),
            "tracking": int(totals.get("tracking") or 0),
            "resolved": int(totals.get("resolved") or 0),
            "recovered_entry": int(totals.get("recovered_entry") or 0),
            "recovered_half_loss": int(
                totals.get("recovered_half_loss") or 0
            ),
            "average_rebound": _finite(totals.get("average_rebound")),
            "maximum_rebound": _finite(totals.get("maximum_rebound")),
            "average_avoided_further_loss": _finite(
                totals.get("average_avoided_further_loss")
            ),
            "segments": segments,
            "recent": recent,
        }

    def summary(self) -> dict[str, Any]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) observations,
                              COUNT(DISTINCT event_id) events,
                              SUM(CASE WHEN resolved_ts IS NOT NULL
                                  THEN 1 ELSE 0 END) resolved,
                              SUM(CASE WHEN adverse_label IS NOT NULL
                                  THEN 1 ELSE 0 END) labeled,
                              COUNT(DISTINCT CASE WHEN adverse_label IS NOT NULL
                                  THEN event_id END) labeled_events,
                              MAX(observed_ts) last_observed_ts
                       FROM adaptive_exit_observations""",
                )
                totals = dict(cur.fetchone() or {})
                self._db.execute(
                    cur,
                    """SELECT AVG(event_brier) brier FROM (
                           SELECT event_id,
                                  AVG(
                                      (predicted_adverse_probability-adverse_label)*
                                      (predicted_adverse_probability-adverse_label)
                                  ) event_brier
                           FROM adaptive_exit_observations
                           WHERE adverse_label IS NOT NULL
                           GROUP BY event_id
                       ) event_scores""",
                )
                score = cur.fetchone()
                totals["brier"] = (
                    score["brier"] if score is not None else None
                )
                self._db.execute(
                    cur,
                    """SELECT event_name,observed_ts,inning,half,
                              score_differential,selected_batting,
                              predicted_adverse_probability,
                              prediction_confidence,feature_payload,outcome
                       FROM adaptive_exit_observations
                       ORDER BY observed_ts DESC LIMIT 12""",
                )
                recent = [dict(row) for row in cur.fetchall()]
                self._db.execute(
                    cur,
                    """SELECT market_type,COUNT(*) observations,
                              COUNT(DISTINCT event_id) events,
                              SUM(CASE WHEN adverse_label IS NOT NULL
                                  THEN 1 ELSE 0 END) labeled
                       FROM adaptive_exit_observations
                       GROUP BY market_type ORDER BY observations DESC""",
                )
                market_segments = [dict(row) for row in cur.fetchall()]
        for row in recent:
            row["observed_at"] = self._iso(row.pop("observed_ts"))
            try:
                row["features"] = json.loads(row.pop("feature_payload"))
            except (TypeError, json.JSONDecodeError):
                row["features"] = {}
            row["selected_batting"] = (
                None
                if int(row["selected_batting"]) < 0
                else bool(row["selected_batting"])
            )
        return {
            "version": ADAPTIVE_EXIT_MODEL_VERSION,
            "scope": "local_mlb_moneyline_spread_total_exit_overlay",
            "engine_impact": "none",
            "labels": (
                "adverse-versus-favorable executable movement over the configured "
                "future horizon"
            ),
            "retention": "persistent_until_explicit_clear",
            "observations": int(totals.get("observations") or 0),
            "events": int(totals.get("events") or 0),
            "resolved_observations": int(totals.get("resolved") or 0),
            "labeled_observations": int(totals.get("labeled") or 0),
            "labeled_events": int(totals.get("labeled_events") or 0),
            "brier_score": _finite(totals.get("brier")),
            "last_observed_at": self._iso(totals.get("last_observed_ts")),
            "profiles": self.profiles(),
            "market_segments": market_segments,
            "exit_recovery": self.exit_recovery_summary(),
            "recent_forecasts": recent,
        }

    def clear(self, confirmation: str) -> dict[str, Any]:
        if not approval_granted(confirmation):
            raise ValueError(
                approval_instruction("clear adaptive exit learning history")
            )
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    "SELECT COUNT(*) FROM adaptive_exit_observations",
                )
                deleted = int(cur.fetchone()[0] or 0)
                self._db.execute(
                    cur,
                    "SELECT COUNT(*) FROM exit_recovery_observations",
                )
                deleted_recoveries = int(cur.fetchone()[0] or 0)
                self._db.execute(
                    cur,
                    "DELETE FROM adaptive_exit_observations",
                )
                self._db.execute(
                    cur,
                    "DELETE FROM exit_recovery_observations",
                )
        return {
            "deleted_observations": deleted,
            "deleted_exit_recoveries": deleted_recoveries,
            "retained_positions": True,
            "retained_journal": True,
            "summary": self.summary(),
        }

    @staticmethod
    def _iso(value: Any) -> str | None:
        number = _finite(value)
        if number is None:
            return None
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
