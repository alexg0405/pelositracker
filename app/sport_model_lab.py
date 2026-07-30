"""Local, research-only sport observations and candidate model fitting.

This module is deliberately downstream of the established signal engine.  It
records the engine's outputs and game state, labels them after final results,
and can fit an offline comparison candidate.  A candidate is never installed
into the engine and can never authorize an order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import shutil
import threading
import time
from typing import Any, Iterable, Mapping
from uuid import uuid4

from .accounts import line_type
from .database import Database
from .gameclock import game_progress, league_rule
from .models import Event, GameState, Quote, Signal


MODEL_LAB_VERSION = "sport-model-lab-v5-trusted-settlement"
OBSERVATION_BUCKET_SECONDS = 15
MARKET_MOVEMENT_HORIZON_SECONDS = 180
MARKET_MOVEMENT_MAX_DELAY_SECONDS = 360
RESEARCH_MIN_EVENTS = 10
RESEARCH_MIN_OBSERVATIONS = 50
PRODUCTION_MIN_EVENTS = 200
PRODUCTION_MIN_OBSERVATIONS = 1000
WALK_FORWARD_MAX_FOLDS = 8
BOOTSTRAP_DRAWS = 1000
TRUSTED_SETTLEMENT_SOURCES = (
    "explicit_verified_result",
    "official_mlb_state",
    "trusted_terminal_state",
)
_TRUSTED_SETTLEMENT_SQL = ",".join(
    f"'{source}'" for source in TRUSTED_SETTLEMENT_SOURCES
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sport_model_observations (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    canonical_event_id TEXT,
    event_name TEXT NOT NULL,
    sport TEXT NOT NULL,
    league TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    outcome_side TEXT,
    observed_ts DOUBLE PRECISION NOT NULL,
    provider_ts DOUBLE PRECISION,
    state_hash TEXT,
    home_score DOUBLE PRECISION,
    away_score DOUBLE PRECISION,
    score_differential DOUBLE PRECISION,
    fraction_remaining DOUBLE PRECISION,
    possession_indicator DOUBLE PRECISION,
    consensus_probability DOUBLE PRECISION,
    model_probability DOUBLE PRECISION NOT NULL,
    market_probability DOUBLE PRECISION NOT NULL,
    edge DOUBLE PRECISION NOT NULL,
    signal_quality DOUBLE PRECISION NOT NULL,
    reference_sources INTEGER NOT NULL,
    market_move DOUBLE PRECISION,
    score_swing DOUBLE PRECISION,
    decision_id TEXT,
    engine_version TEXT,
    configuration_hash TEXT,
    features_json TEXT NOT NULL,
    result_label INTEGER,
    settled_ts DOUBLE PRECISION,
    canceled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_model_lab_segment
    ON sport_model_observations(sport, league, market, settled_ts);
CREATE INDEX IF NOT EXISTS idx_model_lab_event
    ON sport_model_observations(event_id, observed_ts);

CREATE TABLE IF NOT EXISTS sport_model_candidates (
    id TEXT PRIMARY KEY,
    created_ts DOUBLE PRECISION NOT NULL,
    sport TEXT NOT NULL,
    league TEXT NOT NULL,
    market TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_candidates_created
    ON sport_model_candidates(created_ts DESC);
"""

_RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS sport_model_targets (
    observation_id TEXT NOT NULL,
    target_name TEXT NOT NULL,
    horizon_seconds INTEGER NOT NULL DEFAULT 0,
    label_value DOUBLE PRECISION NOT NULL,
    label_ts DOUBLE PRECISION NOT NULL,
    source TEXT NOT NULL,
    target_version TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (observation_id, target_name, horizon_seconds)
);
CREATE INDEX IF NOT EXISTS idx_model_targets_name
    ON sport_model_targets(target_name, label_ts);

CREATE TABLE IF NOT EXISTS sport_model_exports (
    id TEXT PRIMARY KEY,
    created_ts DOUBLE PRECISION NOT NULL,
    directory TEXT NOT NULL,
    manifest_hash TEXT NOT NULL UNIQUE,
    observation_count INTEGER NOT NULL,
    target_count INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model_exports_created
    ON sport_model_exports(created_ts DESC);

CREATE TABLE IF NOT EXISTS sport_model_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts DOUBLE PRECISION NOT NULL
);
"""

RESEARCH_EVIDENCE_TABLES = frozenset({
    "sport_model_observations",
    "sport_model_candidates",
    "sport_model_targets",
})

TARGET_DEFINITIONS = {
    "event_outcome": {
        "version": "event-outcome-v2-trusted-settlement",
        "kind": "binary",
        "meaning": "Whether the selected moneyline outcome won at official settlement.",
        "source": "provenance-checked terminal result",
    },
    "market_probability_change_3m": {
        "version": "market-probability-change-v1",
        "kind": "continuous",
        "horizon_seconds": MARKET_MOVEMENT_HORIZON_SECONDS,
        "meaning": (
            "Change in the monitored market probability at the first observation "
            "between 180 and 360 seconds later. This is not an executable P/L label."
        ),
        "source": "bounded model-lab observations",
    },
    "after_cost_strategy_pnl": {
        "version": "after-cost-return-v1",
        "kind": "continuous",
        "meaning": (
            "Realized return per dollar of cost basis after spread, fees, and fill "
            "effects, linked by the exact entry decision identifier."
        ),
        "source": "managed-position execution ledger",
    },
}


@dataclass(frozen=True, slots=True)
class SportProfile:
    key: str
    label: str
    aliases: tuple[str, ...]
    supported: bool
    available_features: tuple[str, ...]
    missing_features: tuple[str, ...]
    note: str


SPORT_PROFILES = (
    SportProfile(
        "basketball",
        "Basketball",
        ("basketball", "nba", "wnba", "ncaab"),
        True,
        ("score differential", "game fraction", "possession", "price movement"),
        (),
        "Lead and clock changes are captured per selection.",
    ),
    SportProfile(
        "football",
        "Football",
        ("football", "americanfootball", "nfl", "ncaaf"),
        True,
        ("score differential", "game fraction", "possession", "price movement"),
        (),
        "Clock-aware lead movement is captured; drive detail is not inferred.",
    ),
    SportProfile(
        "hockey",
        "Hockey",
        ("hockey", "icehockey", "nhl"),
        True,
        ("score differential", "game fraction", "price movement"),
        ("power-play and goalie state",),
        "Useful core state is captured without inventing unavailable rink context.",
    ),
    SportProfile(
        "soccer",
        "Soccer",
        ("soccer", "mls", "epl"),
        True,
        ("score differential", "game fraction", "price movement"),
        ("cards and expected-goals feed",),
        "Low-scoring clock and result-state movement is captured.",
    ),
    SportProfile(
        "baseball",
        "Baseball / MLB",
        ("baseball", "mlb"),
        True,
        (
            "existing consensus prior",
            "score differential",
            "inning and half",
            "batting side",
            "outs, base occupancy, and ball/strike count",
            "batter and pitcher identity for future hierarchical effects",
            "late/close-game interactions",
            "price movement",
        ),
        (
            "confirmed lineups",
            "validated batter/current-pitcher quality priors",
            "bullpen availability",
            "park and weather",
        ),
        "Official MLB base/out/count and personnel identity are captured locally. "
        "Candidate fits remain shadow-only until chronological after-cost validation.",
    ),
    SportProfile(
        "tennis",
        "Tennis",
        ("tennis",),
        False,
        ("price movement",),
        ("structured sets", "game score", "server", "best-of format"),
        "Observations are retained, but generic home/away score is not a valid tennis feature set.",
    ),
)

_FEATURE_NAMES = (
    "baseline_logit",
    "score_differential",
    "fraction_elapsed",
    "lead_time_interaction",
    "market_move",
    "score_swing",
    "possession_indicator",
)

_BASEBALL_FEATURE_NAMES = (
    "score_differential",
    "fraction_elapsed",
    "lead_time_interaction",
    "batting_side_indicator",
    "home_team_indicator",
    "late_inning_indicator",
    "one_run_game_indicator",
    "tie_game_indicator",
    "extra_innings_indicator",
    "market_move",
    "score_swing",
    "outs",
    "runner_on_first",
    "runner_on_second",
    "runner_on_third",
    "runners_on",
    "runner_in_scoring_position",
    "balls",
    "strikes",
    "two_out_indicator",
    "oriented_runners_on",
    "oriented_runner_in_scoring_position",
    "oriented_outs_remaining",
    "oriented_count_advantage",
    "base_out_state_available",
    "count_state_available",
    "personnel_state_available",
)

RESEARCH_REFERENCES = (
    {
        "title": "Lindsey (1963), An Investigation of Strategies in Baseball",
        "url": "https://doi.org/10.1287/opre.11.4.477",
        "use": "inning, score, outs, and base occupancy define live run/win state",
    },
    {
        "title": "Bukiet, Harold & Palacios (1997), A Markov Chain Approach to Baseball",
        "url": "https://doi.org/10.1287/opre.45.1.14",
        "use": "plate-appearance transitions, player ability, run distributions",
    },
    {
        "title": "Choi (2023), PhD thesis on sequential inference",
        "url": (
            "https://www.ml.cmu.edu/research/joint_phd_dissertations/"
            "thesis_yojoongc_phd_stat_2023.pdf"
        ),
        "use": "chronological forecast comparison and market-forecast benchmark",
    },
    {
        "title": "MLB Statcast CSV documentation",
        "url": "https://baseballsavant.mlb.com/csv-docs",
        "use": "official field definitions for base/out, pitcher, batter, xwOBA, and win expectancy",
    },
    {
        "title": "Li, Huang & Li (2022), MLB feature selection",
        "url": "https://doi.org/10.3390/e24020288",
        "use": "feature selection and non-neural baselines before adding complexity",
    },
    {
        "title": "MLB Game Strategy Explorer (2016-2025 state tables)",
        "url": "https://baseballsavant.mlb.com/game-strategy-explorer",
        "use": "official empirical win/run state uses inning, score, outs, bases, and count",
    },
    {
        "title": "Wheatcroft (2024), calibration for sports-betting model selection",
        "url": "https://doi.org/10.1016/j.mlwa.2024.100539",
        "use": "select probability models on calibration and after-cost decisions, not accuracy alone",
    },
    {
        "title": "Gupta & Ramdas (2023), Online Platt Scaling with Calibeating",
        "url": "https://proceedings.mlr.press/v202/gupta23c.html",
        "use": "future reviewed online recalibration under sequential forecast drift",
    },
)

MLB_RESEARCH_BLUEPRINT = {
    "status": "phase_2_official_base_out_collecting",
    "objective": (
        "Estimate calibrated live moneyline win probability, not a momentum score."
    ),
    "priority_order": [
        {
            "rank": 1,
            "group": "market_and_team_prior",
            "parameters": [
                "existing independent-source consensus",
                "home/away identity",
                "pregame team-strength information already reflected by the market",
            ],
            "availability": "captured",
        },
        {
            "rank": 2,
            "group": "live_game_state",
            "parameters": [
                "score differential",
                "inning",
                "top/bottom half",
                "batting side",
                "outs",
                "runners on first/second/third",
                "ball/strike count",
                "current batter and pitcher identity",
                "late/close-game interactions",
            ],
            "availability": "captured from the official MLB linescore feed",
        },
        {
            "rank": 3,
            "group": "base_out_state",
            "parameters": [
                "outs",
                "runners on first/second/third",
                "balls and strikes",
            ],
            "availability": "captured from the official MLB linescore feed",
        },
        {
            "rank": 4,
            "group": "personnel_and_context",
            "parameters": [
                "confirmed lineup quality",
                "starter/current pitcher quality and times through order",
                "bullpen availability and recent workload",
                "park and weather run environment",
                "batter/pitcher handedness and xwOBA matchup",
            ],
            "availability": "required for the richer transition model",
        },
    ],
    "candidate_model": (
        "regularized state-aware logistic residual model versus the unchanged "
        "existing consensus; a base/out Markov transition candidate is the next phase"
    ),
    "validation": (
        "chronological event-block holdout, equal event weighting, Brier score and "
        "log loss; no automatic installation"
    ),
    "production_blocker": (
        "MLB candidates are research artifacts even when they beat a small holdout. "
        "Larger samples, frozen chronological testing, after-cost validation, "
        "calibration, and explicit review remain required."
    ),
}

_BASEBALL_WORD_HALF = re.compile(
    r"\b(top|bottom|bot)\s*(?:of\s*(?:the\s*)?)?"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:\s+inning)?\b",
    re.IGNORECASE,
)
_BASEBALL_COMPACT_HALF = re.compile(r"\b([tb])\s*(\d{1,2})\b", re.IGNORECASE)
_BASEBALL_TRAILING_HALF = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s+inning)?\s+(top|bottom|bot)\b",
    re.IGNORECASE,
)
_BASEBALL_END_INNING = re.compile(
    r"\bend\s+(?:of\s+(?:the\s*)?)?(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s+inning)?\b",
    re.IGNORECASE,
)


def _profile_for(event: Event) -> SportProfile | None:
    text = f"{event.sport} {event.league}".casefold().replace("_", " ")
    for profile in SPORT_PROFILES:
        if any(alias in text for alias in profile.aliases):
            return profile
    return None


def _norm(value: str) -> str:
    return " ".join((value or "").casefold().replace("_", " ").split())


def _baseball_live_state(period: str, clock: str = "") -> dict[str, Any] | None:
    """Parse explicit inning progress without inventing base/out information."""
    text = " ".join(part for part in (period, clock) if part).strip()
    if not text:
        return None
    side: str | None = None
    inning: int | None = None
    match = _BASEBALL_WORD_HALF.search(text)
    if match:
        side, inning_text = match.groups()
        inning = int(inning_text)
    else:
        match = _BASEBALL_COMPACT_HALF.search(text)
        if match:
            side, inning_text = match.groups()
            inning = int(inning_text)
        else:
            match = _BASEBALL_TRAILING_HALF.search(text)
            if match:
                inning_text, side = match.groups()
                inning = int(inning_text)
    if inning is None:
        match = _BASEBALL_END_INNING.search(text)
        if match:
            inning = int(match.group(1))
            side = "end"
    if inning is None or side is None or inning < 1 or inning > 30:
        return None
    if side.casefold() == "end":
        half = "end"
        batting_side = None
        completed_halves = inning * 2
    else:
        half = "bottom" if side.casefold() in {"bottom", "bot", "b"} else "top"
        batting_side = "home" if half == "bottom" else "away"
        completed_halves = (inning - 1) * 2 + (1 if half == "bottom" else 0)
    # Regulation progress is only a coarse interaction. Extra innings remain
    # explicit rather than being treated as a plausible regulation clock.
    fraction_remaining = max(0.0, min(1.0, (18 - completed_halves) / 18))
    return {
        "inning": inning,
        "half": half,
        "batting_side": batting_side,
        "fraction_remaining": fraction_remaining,
        "extra_innings_indicator": float(inning > 9),
    }


def _mlb_context(state: GameState | None) -> dict[str, Any] | None:
    """Combine explicit inning parsing with structured official MLB state."""
    if state is None:
        return None
    parsed = _baseball_live_state(state.period, state.clock)
    structured = (
        state.sport_state
        if isinstance(state.sport_state, dict)
        and str(state.sport_state.get("schema") or "").startswith("mlb-linescore-")
        else {}
    )
    if parsed is None and not structured:
        return None
    result = dict(parsed or {})
    inning = _finite(structured.get("inning"))
    if inning is not None:
        result["inning"] = int(inning)
        result["extra_innings_indicator"] = float(inning > 9)
    half = str(structured.get("half") or result.get("half") or "").casefold()
    if half in {"top", "bottom", "end"}:
        result["half"] = half
        result["batting_side"] = (
            "away" if half == "top" else "home" if half == "bottom" else None
        )
        inning_number = int(result.get("inning") or 0)
        completed_halves = (
            inning_number * 2
            if half == "end"
            else max(0, (inning_number - 1) * 2 + (1 if half == "bottom" else 0))
        )
        result["fraction_remaining"] = max(
            0.0, min(1.0, (18 - completed_halves) / 18)
        )
    for name in ("outs", "balls", "strikes", "base_mask"):
        value = _finite(structured.get(name))
        result[name] = int(value) if value is not None else None
    runners = structured.get("runners")
    result["runners"] = runners if isinstance(runners, dict) else {}
    result["batter"] = (
        structured.get("batter") if isinstance(structured.get("batter"), dict) else None
    )
    result["pitcher"] = (
        structured.get("pitcher") if isinstance(structured.get("pitcher"), dict) else None
    )
    result["structured"] = bool(structured)
    return result


def _feature_names_for(sport: str, league: str = "") -> tuple[str, ...]:
    event = Event(
        name="feature-profile",
        sport=sport,
        league=league,
        home="home",
        away="away",
    )
    profile = _profile_for(event)
    return _BASEBALL_FEATURE_NAMES if profile and profile.key == "baseball" else _FEATURE_NAMES


def _outcome_side(event: Event, outcome: str) -> str | None:
    value = _norm(outcome)
    if value in {"home", _norm(event.home)}:
        return "home"
    if value in {"away", _norm(event.away)}:
        return "away"
    if value in {"draw", "tie"}:
        return "draw"
    return None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _logit(probability: float) -> float:
    value = min(1.0 - 1e-6, max(1e-6, probability))
    return math.log(value / (1.0 - value))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 40.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -40.0))
    return z / (1.0 + z)


def _metric(rows: list[dict[str, Any]], predictions: list[float]) -> dict[str, float]:
    if not rows:
        return {"brier": 0.0, "log_loss": 0.0}
    total_weight = sum(row["weight"] for row in rows)
    brier = 0.0
    log_loss = 0.0
    for row, probability in zip(rows, predictions):
        label = row["label"]
        weight = row["weight"]
        clipped = min(1.0 - 1e-9, max(1e-9, probability))
        brier += weight * (clipped - label) ** 2
        log_loss -= weight * (
            label * math.log(clipped) + (1 - label) * math.log(1 - clipped)
        )
    return {
        "brier": brier / total_weight,
        "log_loss": log_loss / total_weight,
    }


class SportModelLab:
    """Thread-safe observation store and manual research fitter."""

    def __init__(self, path: str | None, export_root: str | None = None):
        self._db = Database.open(
            path,
            sqlite_envs=("MODEL_LAB_DB",),
            sqlite_default=path or "model-lab.db",
        )
        self.path = self._db.target
        if export_root:
            self.export_root = Path(export_root).expanduser().resolve()
        elif self._db.backend == "sqlite" and self.path not in {":memory:", ""}:
            database_path = Path(self.path).expanduser().resolve()
            self.export_root = database_path.parent / "research-exports"
        else:
            self.export_root = None
        self._lock = threading.RLock()
        with self._lock:
            self._db.initialize(_SCHEMA, component="sport_model_lab", version=1)
            self._db.initialize(
                _RESEARCH_SCHEMA,
                component="sport_model_lab",
                version=2,
            )
            self._db.migrate_columns("sport_model_lab", 3, {
                "sport_model_observations": {
                    "sport_state_json": "TEXT",
                    "state_completeness": "DOUBLE PRECISION",
                    "mlb_outs": "INTEGER",
                    "mlb_base_mask": "INTEGER",
                    "mlb_balls": "INTEGER",
                    "mlb_strikes": "INTEGER",
                    "batter_id": "TEXT",
                    "pitcher_id": "TEXT",
                },
            })
            self._db.initialize(
                "CREATE INDEX IF NOT EXISTS idx_model_lab_decision "
                "ON sport_model_observations(decision_id);",
                component="sport_model_lab",
                version=4,
            )
            self._db.migrate_columns("sport_model_lab", 5, {
                "sport_model_observations": {
                    "settlement_source": "TEXT",
                    "settlement_details_json": "TEXT",
                },
            })
            self._backfill_research_targets()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def iter_research_batches(
        self,
        *,
        batch_size: int = 500,
    ):
        """Yield model evidence only; local export metadata stays local."""
        with self._lock:
            for table in sorted(RESEARCH_EVIDENCE_TABLES):
                yield from (
                    (table, rows)
                    for rows in self._db.iter_table_batches(
                        table, batch_size=batch_size
                    )
                )

    def merge_research_batch(
        self,
        table: str,
        rows: list[dict[str, Any]],
    ) -> int:
        if table not in RESEARCH_EVIDENCE_TABLES:
            raise ValueError("unsupported model-lab evidence table")
        with self._lock:
            return self._db.merge_table_rows(table, rows)

    def record(
        self,
        event: Event,
        signals: Iterable[Signal],
        quotes: Iterable[Quote],
        states: Iterable[GameState],
        *,
        as_of: datetime,
    ) -> int:
        """Record one bounded time bucket per moneyline selection."""
        quote_values = list(quotes)
        state_values = list(states)
        profile = _profile_for(event)
        if profile and profile.key == "baseball":
            # Prefer the newest official structured MLB state even if a later
            # generic Polymarket score packet arrived between quote updates.
            state = next(
                (
                    value for value in reversed(state_values)
                    if isinstance(value.sport_state, dict)
                    and str(value.sport_state.get("schema") or "").startswith(
                        "mlb-linescore-"
                    )
                ),
                state_values[-1] if state_values else None,
            )
        else:
            state = state_values[-1] if state_values else None
        baseball_state = (
            _mlb_context(state)
            if state is not None and profile and profile.key == "baseball"
            and (
                not state.quarantined
                or state.quarantine_reason == "unknown or invalid game clock"
            )
            else None
        )
        if baseball_state is not None:
            fraction = float(baseball_state["fraction_remaining"])
        else:
            _, fraction = (
                game_progress(event.sport, state.period, state.clock, event.league)
                if state is not None
                else (None, None)
            )
        observed_ts = as_of.timestamp()
        bucket = int(observed_ts // OBSERVATION_BUCKET_SECONDS)
        inserted = 0
        for signal in signals:
            if line_type(signal.market) != "moneyline":
                continue
            canonical_market = "moneyline"
            side = _outcome_side(event, signal.outcome)
            if side is None:
                continue
            model_probability = _finite(
                signal.calibrated_consensus_probability
                if signal.calibrated_consensus_probability is not None
                else signal.model_probability
            )
            market_probability = _finite(signal.market_probability)
            if (
                model_probability is None
                or market_probability is None
                or not 0.0 < model_probability < 1.0
                or not 0.0 < market_probability < 1.0
            ):
                continue
            home_score = _finite(state.home_score) if state is not None else None
            away_score = _finite(state.away_score) if state is not None else None
            score_diff = self._oriented_score(side, home_score, away_score)
            possession = (
                float(side == baseball_state["batting_side"])
                if baseball_state is not None
                and baseball_state["batting_side"] is not None
                and side != "draw"
                else self._possession_indicator(event, side, state)
            )
            previous = self._previous_observation(
                event.id, canonical_market, signal.outcome
            )
            execution_quote = self._execution_quote(
                signal,
                quote_values,
                canonical_market,
            )
            market_move = (
                market_probability - float(previous["market_probability"])
                if previous is not None
                else None
            )
            score_swing = (
                score_diff - float(previous["score_differential"])
                if previous is not None
                and score_diff is not None
                and previous["score_differential"] is not None
                else None
            )
            features = {
                "baseline_logit": _logit(model_probability),
                "score_differential": score_diff,
                "fraction_remaining": fraction,
                "fraction_elapsed": None if fraction is None else 1.0 - fraction,
                "lead_time_interaction": (
                    None
                    if fraction is None or score_diff is None
                    else score_diff * (1.0 - fraction)
                ),
                "market_move": market_move,
                "score_swing": score_swing,
                "possession_indicator": possession,
                "batting_side_indicator": possession if baseball_state else None,
                "home_team_indicator": (
                    float(side == "home") if baseball_state and side != "draw" else None
                ),
                "late_inning_indicator": (
                    float(int(baseball_state["inning"]) >= 7)
                    if baseball_state else None
                ),
                "one_run_game_indicator": (
                    float(abs(score_diff) == 1.0)
                    if baseball_state and score_diff is not None else None
                ),
                "tie_game_indicator": (
                    float(score_diff == 0.0)
                    if baseball_state and score_diff is not None else None
                ),
                "extra_innings_indicator": (
                    baseball_state["extra_innings_indicator"]
                    if baseball_state else None
                ),
                "outs": baseball_state.get("outs") if baseball_state else None,
                "runner_on_first": (
                    float(bool((baseball_state.get("base_mask") or 0) & 1))
                    if baseball_state and baseball_state.get("base_mask") is not None
                    else None
                ),
                "runner_on_second": (
                    float(bool((baseball_state.get("base_mask") or 0) & 2))
                    if baseball_state and baseball_state.get("base_mask") is not None
                    else None
                ),
                "runner_on_third": (
                    float(bool((baseball_state.get("base_mask") or 0) & 4))
                    if baseball_state and baseball_state.get("base_mask") is not None
                    else None
                ),
                "runners_on": (
                    int(baseball_state.get("base_mask") or 0).bit_count()
                    if baseball_state and baseball_state.get("base_mask") is not None
                    else None
                ),
                "runner_in_scoring_position": (
                    float(bool(int(baseball_state.get("base_mask") or 0) & 6))
                    if baseball_state and baseball_state.get("base_mask") is not None
                    else None
                ),
                "balls": baseball_state.get("balls") if baseball_state else None,
                "strikes": baseball_state.get("strikes") if baseball_state else None,
                "two_out_indicator": (
                    float(baseball_state.get("outs") == 2)
                    if baseball_state and baseball_state.get("outs") is not None
                    else None
                ),
                # Base/out/count values describe the batting team. Orient their
                # sign to the selected outcome; otherwise a linear model would
                # incorrectly learn that a runner on third helps both teams.
                "oriented_runners_on": (
                    (
                        1.0 if side == baseball_state.get("batting_side") else -1.0
                    )
                    * int(baseball_state.get("base_mask") or 0).bit_count()
                    if baseball_state
                    and side in {"home", "away"}
                    and baseball_state.get("batting_side") in {"home", "away"}
                    and baseball_state.get("base_mask") is not None
                    else None
                ),
                "oriented_runner_in_scoring_position": (
                    (
                        1.0 if side == baseball_state.get("batting_side") else -1.0
                    )
                    * float(bool(int(baseball_state.get("base_mask") or 0) & 6))
                    if baseball_state
                    and side in {"home", "away"}
                    and baseball_state.get("batting_side") in {"home", "away"}
                    and baseball_state.get("base_mask") is not None
                    else None
                ),
                "oriented_outs_remaining": (
                    (
                        1.0 if side == baseball_state.get("batting_side") else -1.0
                    )
                    * (3.0 - float(baseball_state["outs"]))
                    if baseball_state
                    and side in {"home", "away"}
                    and baseball_state.get("batting_side") in {"home", "away"}
                    and baseball_state.get("outs") is not None
                    else None
                ),
                "oriented_count_advantage": (
                    (
                        1.0 if side == baseball_state.get("batting_side") else -1.0
                    )
                    * (
                        float(baseball_state["balls"])
                        - float(baseball_state["strikes"])
                    )
                    if baseball_state
                    and side in {"home", "away"}
                    and baseball_state.get("batting_side") in {"home", "away"}
                    and baseball_state.get("balls") is not None
                    and baseball_state.get("strikes") is not None
                    else None
                ),
                "base_out_state_available": (
                    float(
                        baseball_state.get("outs") is not None
                        and baseball_state.get("base_mask") is not None
                    )
                    if baseball_state else 0.0
                ),
                "count_state_available": (
                    float(
                        baseball_state.get("balls") is not None
                        and baseball_state.get("strikes") is not None
                    )
                    if baseball_state else 0.0
                ),
                "personnel_state_available": (
                    float(
                        bool((baseball_state.get("batter") or {}).get("id"))
                        and bool((baseball_state.get("pitcher") or {}).get("id"))
                    )
                    if baseball_state else 0.0
                ),
                "baseball_inning": (
                    baseball_state["inning"] if baseball_state else None
                ),
                "baseball_half": (
                    baseball_state["half"] if baseball_state else None
                ),
                "state_status": state.status if state is not None else None,
                "profile": profile.key if profile else "unknown",
                # These fields describe the exact quote selected by the existing
                # engine when it can be linked unambiguously. They are retained
                # for later cost-aware research and never replace the engine's
                # own probability, edge, or execution calculations.
                "execution_source": (
                    execution_quote.source if execution_quote is not None else None
                ),
                "execution_token_id": (
                    execution_quote.token_id if execution_quote is not None else None
                ),
                "execution_bid": (
                    _finite(execution_quote.bid)
                    if execution_quote is not None else None
                ),
                "execution_ask": (
                    _finite(execution_quote.ask)
                    if execution_quote is not None else None
                ),
                "execution_spread": (
                    float(execution_quote.ask) - float(execution_quote.bid)
                    if execution_quote is not None
                    and _finite(execution_quote.ask) is not None
                    and _finite(execution_quote.bid) is not None
                    else None
                ),
                "execution_bid_size": (
                    _finite(execution_quote.bid_size)
                    if execution_quote is not None else None
                ),
                "execution_ask_size": (
                    _finite(execution_quote.ask_size)
                    if execution_quote is not None else None
                ),
                "execution_fee_rate": (
                    _finite(execution_quote.fee_rate)
                    if execution_quote is not None else None
                ),
                "execution_depth_complete": (
                    bool(execution_quote.depth_complete)
                    if execution_quote is not None else None
                ),
                "execution_snapshot_id": (
                    execution_quote.book_hash
                    if execution_quote is not None else None
                ),
            }
            key_text = "|".join(
                (
                    event.id,
                    canonical_market,
                    signal.outcome.casefold(),
                    str(bucket),
                )
            )
            observation_id = hashlib.sha256(key_text.encode("utf-8")).hexdigest()
            provider_ts = (
                state.provider_timestamp.timestamp()
                if state is not None and state.provider_timestamp is not None
                else None
            )
            row = (
                observation_id,
                event.id,
                event.canonical_event_id,
                event.name,
                event.sport.casefold(),
                event.league.casefold(),
                canonical_market,
                signal.outcome,
                side,
                observed_ts,
                provider_ts,
                state.state_hash if state is not None else None,
                home_score,
                away_score,
                score_diff,
                fraction,
                possession,
                _finite(signal.consensus_probability),
                model_probability,
                market_probability,
                float(signal.edge),
                float(signal.confidence),
                int(signal.n_reference_sources),
                market_move,
                score_swing,
                signal.decision_id or signal.decision_hash,
                signal.engine_version,
                signal.configuration_hash,
                json.dumps(features, sort_keys=True, separators=(",", ":")),
                json.dumps(
                    state.sport_state if state is not None else {},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                (
                    sum(
                        (
                            float(baseball_state is not None),
                            float(
                                baseball_state is not None
                                and baseball_state.get("outs") is not None
                                and baseball_state.get("base_mask") is not None
                            ),
                            float(
                                baseball_state is not None
                                and baseball_state.get("balls") is not None
                                and baseball_state.get("strikes") is not None
                            ),
                            float(
                                baseball_state is not None
                                and bool((baseball_state.get("batter") or {}).get("id"))
                                and bool((baseball_state.get("pitcher") or {}).get("id"))
                            ),
                        )
                    ) / 4.0
                    if profile and profile.key == "baseball"
                    else None
                ),
                baseball_state.get("outs") if baseball_state else None,
                baseball_state.get("base_mask") if baseball_state else None,
                baseball_state.get("balls") if baseball_state else None,
                baseball_state.get("strikes") if baseball_state else None,
                str((baseball_state.get("batter") or {}).get("id") or "")
                if baseball_state else None,
                str((baseball_state.get("pitcher") or {}).get("id") or "")
                if baseball_state else None,
            )
            with self._lock:
                with self._db.transaction(dict_rows=True) as cur:
                    self._db.execute(
                        cur,
                        """INSERT INTO sport_model_observations
                           (id,event_id,canonical_event_id,event_name,sport,league,
                            market,outcome,outcome_side,observed_ts,provider_ts,state_hash,
                            home_score,away_score,score_differential,fraction_remaining,
                            possession_indicator,consensus_probability,model_probability,
                            market_probability,edge,signal_quality,reference_sources,
                            market_move,score_swing,decision_id,engine_version,
                            configuration_hash,features_json,sport_state_json,
                            state_completeness,mlb_outs,mlb_base_mask,mlb_balls,
                            mlb_strikes,batter_id,pitcher_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   %s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(id) DO NOTHING""",
                        row,
                    )
                    was_inserted = max(0, int(cur.rowcount or 0))
                    inserted += was_inserted
                    if was_inserted:
                        self._label_market_movement_targets(
                            cur,
                            event_id=event.id,
                            market=canonical_market,
                            outcome=signal.outcome,
                            observed_ts=observed_ts,
                            market_probability=market_probability,
                        )
        return inserted

    def settle_event(
        self,
        event: Event,
        home_score: float,
        away_score: float,
        *,
        canceled: bool = False,
        settlement_source: str = "explicit_verified_result",
        settlement_details: Mapping[str, Any] | None = None,
    ) -> int:
        settled_ts = time.time()
        source = str(settlement_source or "").strip().casefold()
        if not source:
            raise ValueError("settlement_source is required")
        details_json = json.dumps(
            dict(settlement_details or {}),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    "SELECT id,outcome_side FROM sport_model_observations "
                    "WHERE event_id=%s AND result_label IS NULL",
                    (event.id,),
                )
                rows = [dict(row) for row in cur.fetchall()]
                for row in rows:
                    side = row["outcome_side"]
                    label = None
                    if not canceled:
                        label = int(
                            (side == "home" and home_score > away_score)
                            or (side == "away" and away_score > home_score)
                            or (side == "draw" and home_score == away_score)
                        )
                    self._db.execute(
                        cur,
                        """UPDATE sport_model_observations
                           SET result_label=%s,settled_ts=%s,canceled=%s,
                               settlement_source=%s,settlement_details_json=%s
                           WHERE id=%s""",
                        (
                            label,
                            settled_ts,
                            int(canceled),
                            source,
                            details_json,
                            row["id"],
                        ),
                    )
                    if label is not None:
                        definition = TARGET_DEFINITIONS["event_outcome"]
                        self._db.execute(
                            cur,
                            """INSERT INTO sport_model_targets
                               (observation_id,target_name,horizon_seconds,label_value,
                                label_ts,source,target_version,metadata_json)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                               ON CONFLICT(observation_id,target_name,horizon_seconds)
                               DO UPDATE SET
                                 label_value=excluded.label_value,
                                 label_ts=excluded.label_ts,
                                 source=excluded.source,
                                 target_version=excluded.target_version,
                                 metadata_json=excluded.metadata_json""",
                            (
                                row["id"],
                                "event_outcome",
                                0,
                                float(label),
                                settled_ts,
                                definition["source"],
                                definition["version"],
                                json.dumps(
                                    {
                                        "home_score": float(home_score),
                                        "away_score": float(away_score),
                                        "outcome_side": side,
                                        "settlement_source": source,
                                        "trusted_for_fit": (
                                            source in TRUSTED_SETTLEMENT_SOURCES
                                        ),
                                    },
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                            ),
                        )
        return len(rows)

    def link_execution_results(
        self,
        positions: Iterable[Mapping[str, Any]],
    ) -> int:
        """Attach exact, after-cost managed-position outcomes to entry decisions.

        This does not backfill rejected opportunities or infer counterfactual
        fills. Those limits are recorded in target metadata so a future policy
        model cannot silently treat selected trades as an unbiased sample.
        """
        definition = TARGET_DEFINITIONS["after_cost_strategy_pnl"]
        inserted = 0
        with self._lock:
            with self._db.transaction(dict_rows=True) as cur:
                for position in positions:
                    if str(position.get("status") or "") != "closed":
                        continue
                    decision_id = str(position.get("entry_decision_id") or "").strip()
                    pnl = _finite(position.get("realized_pnl"))
                    cost_basis = _finite(position.get("cost_basis"))
                    if not decision_id or pnl is None or cost_basis is None or cost_basis <= 0:
                        continue
                    self._db.execute(
                        cur,
                        """SELECT id,event_id FROM sport_model_observations
                           WHERE decision_id=%s ORDER BY observed_ts DESC""",
                        (decision_id,),
                    )
                    observations = [dict(row) for row in cur.fetchall()]
                    for observation in observations:
                        opened = _finite(position.get("opened_ts"))
                        closed = _finite(position.get("closed_ts"))
                        metadata = {
                            "managed_position_id": position.get("id"),
                            "event_id": observation["event_id"],
                            "mode": position.get("mode"),
                            "realized_pnl_usd": pnl,
                            "cost_basis_usd": cost_basis,
                            "holding_seconds": (
                                max(0.0, closed - opened)
                                if opened is not None and closed is not None
                                else None
                            ),
                            "exit_reason": position.get("exit_reason"),
                            "entry_decision_id": decision_id,
                            "selection_bias": (
                                "Observed only because the historical policy "
                                "selected and executed this entry."
                            ),
                        }
                        self._db.execute(
                            cur,
                            """INSERT INTO sport_model_targets
                               (observation_id,target_name,horizon_seconds,label_value,
                                label_ts,source,target_version,metadata_json)
                               VALUES (%s,%s,0,%s,%s,%s,%s,%s)
                               ON CONFLICT(observation_id,target_name,horizon_seconds)
                               DO NOTHING""",
                            (
                                observation["id"],
                                "after_cost_strategy_pnl",
                                pnl / cost_basis,
                                closed or time.time(),
                                definition["source"],
                                definition["version"],
                                json.dumps(
                                    metadata,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                            ),
                        )
                        inserted += max(0, int(cur.rowcount or 0))
        return inserted

    def summary(self) -> dict[str, Any]:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT sport,league,COUNT(*) observations,
                              COUNT(DISTINCT event_id) observed_events,
                              SUM(CASE WHEN fraction_remaining IS NOT NULL
                                  AND score_differential IS NOT NULL
                                  THEN 1 ELSE 0 END) state_observations,
                              COUNT(DISTINCT CASE WHEN fraction_remaining IS NOT NULL
                                  AND score_differential IS NOT NULL
                                  THEN event_id END) state_events,
                              SUM(CASE WHEN result_label IS NOT NULL THEN 1 ELSE 0 END)
                                  settled_observations,
                              COUNT(DISTINCT CASE WHEN result_label IS NOT NULL
                                  THEN event_id END) settled_events,
                              SUM(CASE WHEN result_label IS NOT NULL
                                  AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL})
                                  THEN 1 ELSE 0 END) trusted_settled_observations,
                              COUNT(DISTINCT CASE WHEN result_label IS NOT NULL
                                  AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL})
                                  THEN event_id END) trusted_settled_events,
                              SUM(CASE WHEN result_label IS NOT NULL
                                  AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL})
                                  AND fraction_remaining IS NOT NULL
                                  AND score_differential IS NOT NULL
                                  THEN 1 ELSE 0 END) fit_observations,
                              COUNT(DISTINCT CASE WHEN result_label IS NOT NULL
                                  AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL})
                                  AND fraction_remaining IS NOT NULL
                                  AND score_differential IS NOT NULL
                                  THEN event_id END) fit_events,
                              SUM(CASE WHEN state_completeness>=0.75
                                  THEN 1 ELSE 0 END) rich_state_observations,
                              COUNT(DISTINCT CASE WHEN state_completeness>=0.75
                                  THEN event_id END) rich_state_events,
                              AVG(state_completeness) average_state_completeness,
                              MAX(observed_ts) last_observed_ts
                       FROM sport_model_observations
                       WHERE canceled=0
                       GROUP BY sport,league ORDER BY observations DESC""".format(
                           _TRUSTED_SETTLEMENT_SQL=_TRUSTED_SETTLEMENT_SQL
                       ),
                )
                segments = [dict(row) for row in cur.fetchall()]
                self._db.execute(
                    cur,
                    """SELECT id,created_ts,sport,league,market,status,payload
                       FROM sport_model_candidates ORDER BY created_ts DESC LIMIT 10""",
                )
                candidates = [dict(row) for row in cur.fetchall()]
                self._db.execute(
                    cur,
                    """SELECT event_name,sport,league,outcome,observed_ts,
                              score_differential,fraction_remaining,market_move,
                              score_swing,market_probability,model_probability,
                              signal_quality,features_json
                       FROM sport_model_observations
                       ORDER BY observed_ts DESC LIMIT 24""",
                )
                recent = [dict(row) for row in cur.fetchall()]
                self._db.execute(
                    cur,
                    """SELECT target_name,horizon_seconds,COUNT(*) target_count,
                              COUNT(DISTINCT o.event_id) event_count,
                              MIN(t.label_ts) first_label_ts,
                              MAX(t.label_ts) last_label_ts
                       FROM sport_model_targets t
                       JOIN sport_model_observations o ON o.id=t.observation_id
                       GROUP BY target_name,horizon_seconds
                       ORDER BY target_name,horizon_seconds""",
                )
                target_counts = [dict(row) for row in cur.fetchall()]
                self._db.execute(
                    cur,
                    """SELECT id,created_ts,directory,manifest_hash,
                              observation_count,target_count,candidate_count
                       FROM sport_model_exports
                       ORDER BY created_ts DESC LIMIT 1""",
                )
                export_row = cur.fetchone()
                latest_export = dict(export_row) if export_row is not None else None
        for segment in segments:
            segment["fit_supported"] = self._segment_supported(
                segment["sport"], segment["league"]
            )
            segment["research_fit_ready"] = (
                segment["fit_supported"]
                and int(segment["fit_events"] or 0) >= RESEARCH_MIN_EVENTS
                and int(segment["fit_observations"] or 0) >= RESEARCH_MIN_OBSERVATIONS
            )
            segment["last_observed_at"] = self._iso(segment.pop("last_observed_ts"))
        for candidate in candidates:
            candidate["created_at"] = self._iso(candidate.pop("created_ts"))
            candidate["details"] = json.loads(candidate.pop("payload"))
        for row in recent:
            row["observed_at"] = self._iso(row.pop("observed_ts"))
            try:
                features = json.loads(row.pop("features_json"))
            except (TypeError, json.JSONDecodeError):
                features = {}
            row["baseball_inning"] = features.get("baseball_inning")
            row["baseball_half"] = features.get("baseball_half")
        for target in target_counts:
            target["first_label_at"] = self._iso(target.pop("first_label_ts"))
            target["last_label_at"] = self._iso(target.pop("last_label_ts"))
        if latest_export:
            latest_export["created_at"] = self._iso(
                latest_export.pop("created_ts")
            )
        return {
            "version": MODEL_LAB_VERSION,
            "mode": "research_only",
            "engine_impact": "none",
            "observation_bucket_seconds": OBSERVATION_BUCKET_SECONDS,
            "thresholds": {
                "research_min_events": RESEARCH_MIN_EVENTS,
                "research_min_observations": RESEARCH_MIN_OBSERVATIONS,
                "production_min_events": PRODUCTION_MIN_EVENTS,
                "production_min_observations": PRODUCTION_MIN_OBSERVATIONS,
            },
            "profiles": [asdict(profile) for profile in SPORT_PROFILES],
            "mlb_research_blueprint": MLB_RESEARCH_BLUEPRINT,
            "research_references": list(RESEARCH_REFERENCES),
            "target_definitions": TARGET_DEFINITIONS,
            "target_counts": target_counts,
            "archive": {
                "available": self.export_root is not None,
                "directory": str(self.export_root) if self.export_root else None,
                "format": "canonical-jsonl-with-sha256-manifest",
                "latest_export": latest_export,
            },
            "segments": segments,
            "candidates": candidates,
            "recent_momentum": recent,
        }

    def create_export(self) -> dict[str, Any]:
        """Write an immutable, content-hashed local research snapshot.

        The export is intentionally separate from the capped dashboard journal.
        It contains model-lab observations, explicit targets, and candidate
        artifacts only; credentials and live-order secrets are never included.
        """
        if self.export_root is None:
            raise ValueError("Research exports require a local filesystem database")
        created_ts = time.time()
        export_id = str(uuid4())
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT * FROM sport_model_observations
                       ORDER BY observed_ts,event_id,market,outcome,id""",
                )
                observations = [dict(row) for row in cur.fetchall()]
                self._db.execute(
                    cur,
                    """SELECT * FROM sport_model_targets
                       ORDER BY label_ts,observation_id,target_name,horizon_seconds""",
                )
                targets = [dict(row) for row in cur.fetchall()]
                self._db.execute(
                    cur,
                    """SELECT * FROM sport_model_candidates
                       ORDER BY created_ts,id""",
                )
                candidates = [dict(row) for row in cur.fetchall()]

            for row in observations:
                row["features"] = json.loads(row.pop("features_json"))
            for row in targets:
                row["metadata"] = json.loads(row.pop("metadata_json"))
            for row in candidates:
                row["details"] = json.loads(row.pop("payload"))

            self.export_root.mkdir(parents=True, exist_ok=True)
            pending = self.export_root / f".pending-{export_id}"
            pending.mkdir()
            try:
                file_hashes = {
                    "observations.jsonl": self._write_jsonl(
                        pending / "observations.jsonl", observations
                    ),
                    "targets.jsonl": self._write_jsonl(
                        pending / "targets.jsonl", targets
                    ),
                    "candidates.jsonl": self._write_jsonl(
                        pending / "candidates.jsonl", candidates
                    ),
                }
                manifest_body = {
                    "export_schema_version": "sport-model-research-export-v1",
                    "model_lab_version": MODEL_LAB_VERSION,
                    "created_at": self._iso(created_ts),
                    "engine_impact": "none",
                    "immutable_snapshot": True,
                    "observation_bucket_seconds": OBSERVATION_BUCKET_SECONDS,
                    "counts": {
                        "observations": len(observations),
                        "targets": len(targets),
                        "candidates": len(candidates),
                    },
                    "target_definitions": TARGET_DEFINITIONS,
                    "files": {
                        name: {"sha256": digest}
                        for name, digest in sorted(file_hashes.items())
                    },
                    "notes": [
                        "Rows are canonical JSON Lines sorted deterministically.",
                        "Final-game, short-horizon movement, and after-cost P/L "
                        "targets are distinct by contract.",
                        "This snapshot cannot install a model or authorize an order.",
                    ],
                }
                canonical_manifest = json.dumps(
                    manifest_body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                manifest_hash = hashlib.sha256(
                    canonical_manifest.encode("utf-8")
                ).hexdigest()
                manifest = {
                    **manifest_body,
                    "manifest_sha256": manifest_hash,
                }
                (pending / "manifest.json").write_text(
                    json.dumps(
                        manifest,
                        sort_keys=True,
                        indent=2,
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                stamp = datetime.fromtimestamp(
                    created_ts, timezone.utc
                ).strftime("%Y%m%dT%H%M%SZ")
                destination = self.export_root / (
                    f"{stamp}-{manifest_hash[:12]}"
                )
                if destination.exists():
                    destination = self.export_root / (
                        f"{stamp}-{manifest_hash[:12]}-{export_id[:8]}"
                    )
                pending.replace(destination)
            except BaseException:
                shutil.rmtree(pending, ignore_errors=True)
                raise

            payload = {
                "export_schema_version": manifest["export_schema_version"],
                "files": manifest["files"],
                "target_definitions": TARGET_DEFINITIONS,
                "engine_impact": "none",
            }
            try:
                with self._db.transaction() as cur:
                    self._db.execute(
                        cur,
                        """INSERT INTO sport_model_exports
                           (id,created_ts,directory,manifest_hash,observation_count,
                            target_count,candidate_count,payload)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            export_id,
                            created_ts,
                            str(destination),
                            manifest_hash,
                            len(observations),
                            len(targets),
                            len(candidates),
                            json.dumps(
                                payload,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
            except BaseException:
                shutil.rmtree(destination, ignore_errors=True)
                raise
        return {
            "id": export_id,
            "created_at": self._iso(created_ts),
            "directory": str(destination),
            "manifest_hash": manifest_hash,
            "counts": manifest["counts"],
            "engine_impact": "none",
        }

    def advisory_evidence(
        self,
        decision_ids: Iterable[str] = (),
        *,
        sport: str = "baseball",
    ) -> dict[str, Any]:
        """Return shadow-model readiness and optional per-decision comparisons.

        This method evaluates frozen research artifacts only. It never installs
        one, returns an execution authorization, or writes into the live engine.
        """
        sport_key = sport.strip().casefold()
        requested = sorted({
            str(value) for value in decision_ids if str(value).strip()
        })[:1000]
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) observations,
                              COUNT(DISTINCT event_id) observed_events,
                              SUM(CASE WHEN result_label IS NOT NULL
                                       AND canceled=0 THEN 1 ELSE 0 END)
                                  settled_observations,
                              COUNT(DISTINCT CASE WHEN result_label IS NOT NULL
                                                   AND canceled=0
                                                  THEN event_id END)
                                  settled_events,
                              SUM(CASE WHEN result_label IS NOT NULL
                                       AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL})
                                       AND canceled=0 THEN 1 ELSE 0 END)
                                  trusted_settled_observations,
                              COUNT(DISTINCT CASE WHEN result_label IS NOT NULL
                                                   AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL})
                                                   AND canceled=0
                                                  THEN event_id END)
                                  trusted_settled_events,
                              SUM(CASE WHEN result_label IS NOT NULL
                                       AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL})
                                       AND canceled=0
                                       AND fraction_remaining IS NOT NULL
                                       AND score_differential IS NOT NULL
                                       THEN 1 ELSE 0 END) fit_observations,
                              COUNT(DISTINCT CASE WHEN result_label IS NOT NULL
                                                   AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL})
                                                   AND canceled=0
                                                   AND fraction_remaining IS NOT NULL
                                                   AND score_differential IS NOT NULL
                                                  THEN event_id END) fit_events
                              ,SUM(CASE WHEN state_completeness>=0.75
                                        THEN 1 ELSE 0 END) rich_state_observations
                              ,COUNT(DISTINCT CASE WHEN state_completeness>=0.75
                                                   THEN event_id END)
                                  rich_state_events
                       FROM sport_model_observations WHERE sport=%s""".format(
                           _TRUSTED_SETTLEMENT_SQL=_TRUSTED_SETTLEMENT_SQL
                       ),
                    (sport_key,),
                )
                segment_row = cur.fetchone()
                segment = dict(segment_row) if segment_row is not None else {}
                self._db.execute(
                    cur,
                    """SELECT id,created_ts,sport,league,market,status,payload
                       FROM sport_model_candidates
                       WHERE sport=%s
                       ORDER BY created_ts DESC""",
                    (sport_key,),
                )
                candidate_rows = [dict(row) for row in cur.fetchall()]
                self._db.execute(
                    cur,
                    """SELECT COUNT(*) labels,
                              COUNT(DISTINCT o.event_id) events
                       FROM sport_model_targets t
                       JOIN sport_model_observations o ON o.id=t.observation_id
                       WHERE o.sport=%s AND t.target_name=%s""",
                    (sport_key, "after_cost_strategy_pnl"),
                )
                cost_row = cur.fetchone()
                after_cost = dict(cost_row) if cost_row is not None else {}
                observation_rows: list[dict[str, Any]] = []
                if requested:
                    placeholders = ",".join(["%s"] * len(requested))
                    self._db.execute(
                        cur,
                        f"""SELECT decision_id,event_id,sport,league,market,outcome,
                                   observed_ts,fraction_remaining,
                                   state_completeness,
                                   model_probability,market_probability,
                                   features_json
                            FROM sport_model_observations
                            WHERE decision_id IN ({placeholders})
                            ORDER BY observed_ts DESC""",
                        tuple(requested),
                    )
                    observation_rows = [dict(row) for row in cur.fetchall()]

        candidate = None
        for row in candidate_rows:
            if row["status"] in {
                "insufficient_data",
                "blocked_missing_sport_state",
            }:
                continue
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, Mapping)
                and payload.get("label_policy")
                == "trusted_settlement_source_v1"
            ):
                candidate = row
                break
        details: dict[str, Any] = {}
        if candidate is not None:
            try:
                details = json.loads(candidate["payload"])
            except (TypeError, json.JSONDecodeError):
                details = {}
        walk_forward = details.get("walk_forward_evaluation") or {}
        interval = walk_forward.get("event_block_bootstrap") or {}
        candidate_beats_baseline = (
            candidate is not None
            and candidate["status"] == "candidate_beats_baseline"
            and float(walk_forward.get("brier_improvement") or 0.0) > 0.0
        )
        shadow_ready = candidate is not None
        advisory_ready = (
            shadow_ready
            and candidate_beats_baseline
            and int(walk_forward.get("test_events") or 0) >= 10
        )
        production_counts_ready = (
            int(segment.get("trusted_settled_events") or 0) >= PRODUCTION_MIN_EVENTS
            and int(segment.get("trusted_settled_observations") or 0)
            >= PRODUCTION_MIN_OBSERVATIONS
        )
        uncertainty_ready = (
            interval.get("lower_95") is not None
            and float(interval["lower_95"]) > 0.0
        )
        richer_state_ready = (
            sport_key != "baseball"
            or (
                int(segment.get("rich_state_events") or 0) >= 100
                and int(segment.get("rich_state_observations") or 0) >= 1000
                and "outs" in (details.get("features") or [])
            )
        )
        after_cost_ready = (
            int(after_cost.get("events") or 0) >= 30
            and int(after_cost.get("labels") or 0) >= 100
        )
        live_eligible = (
            advisory_ready
            and production_counts_ready
            and uncertainty_ready
            and richer_state_ready
            and after_cost_ready
            and bool(details.get("production_ready"))
        )
        decision_contexts = []
        seen_contexts: set[str] = set()
        for row in observation_rows:
            decision_id = str(row.get("decision_id") or "")
            if not decision_id or decision_id in seen_contexts:
                continue
            seen_contexts.add(decision_id)
            decision_contexts.append({
                "decision_id": decision_id,
                "event_id": row.get("event_id"),
                "market": row.get("market"),
                "observed_at": self._iso(row.get("observed_ts")),
                "fraction_remaining": _finite(
                    row.get("fraction_remaining")
                ),
                "state_completeness": _finite(
                    row.get("state_completeness")
                ),
            })
        scores = []
        coefficients = details.get("coefficients") or {}
        feature_names = details.get("features") or []
        means = details.get("feature_means") or {}
        scales = details.get("feature_scales") or {}
        if candidate is not None and coefficients and feature_names:
            seen_decisions: set[str] = set()
            for row in observation_rows:
                decision_id = str(row.get("decision_id") or "")
                if not decision_id or decision_id in seen_decisions:
                    continue
                if str(row.get("sport") or "") != str(candidate["sport"]):
                    continue
                candidate_league = str(candidate.get("league") or "")
                if candidate_league and candidate_league != str(row.get("league") or ""):
                    continue
                try:
                    features = json.loads(row["features_json"])
                    linear = (
                        _logit(float(row["model_probability"]))
                        if details.get("uses_existing_probability_as_fixed_offset")
                        else 0.0
                    ) + float(coefficients.get("intercept") or 0.0)
                    for name in feature_names:
                        value = _finite(features.get(name))
                        mean = _finite(means.get(name))
                        scale = _finite(scales.get(name))
                        if mean is None:
                            mean = 0.0
                        if scale is None or scale <= 0:
                            scale = 1.0
                        standardized = (
                            0.0 if value is None else (value - mean) / scale
                        )
                        linear += (
                            float(coefficients.get(name) or 0.0) * standardized
                        )
                    probability = _sigmoid(linear)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                seen_decisions.add(decision_id)
                scores.append({
                    "decision_id": decision_id,
                    "event_id": row["event_id"],
                    "outcome": row["outcome"],
                    "candidate_probability": probability,
                    "existing_engine_probability": float(
                        row["model_probability"]
                    ),
                    "market_probability": float(row["market_probability"]),
                })
        blockers = []
        if not shadow_ready:
            blockers.append("no fitted research candidate")
        if not advisory_ready:
            blockers.append(
                "candidate has not beaten the baseline across at least 10 "
                "chronological out-of-sample events"
            )
        if not production_counts_ready:
            blockers.append(
                f"need {PRODUCTION_MIN_EVENTS} settled events and "
                f"{PRODUCTION_MIN_OBSERVATIONS} settled observations"
            )
        if not uncertainty_ready:
            blockers.append(
                "whole-event 95% Brier-improvement interval is not above zero"
            )
        if not richer_state_ready:
            blockers.append(
                "MLB base/out and personnel context is not yet captured"
            )
        if not after_cost_ready:
            blockers.append(
                "exact observation-to-entry/exit after-cost P/L target is not linked"
            )
        return {
            "mode": "shadow_only",
            "engine_impact": "none",
            "sport": sport_key,
            "stage": (
                "live_eligible" if live_eligible
                else "validated_advisory_shadow" if advisory_ready
                else "fitted_shadow" if shadow_ready
                else "collecting"
            ),
            "segment": {
                key: int(segment.get(key) or 0)
                for key in (
                    "observations",
                    "observed_events",
                    "settled_observations",
                    "settled_events",
                    "trusted_settled_observations",
                    "trusted_settled_events",
                    "fit_observations",
                    "fit_events",
                    "rich_state_observations",
                    "rich_state_events",
                )
            },
            "after_cost_labels": {
                "labels": int(after_cost.get("labels") or 0),
                "events": int(after_cost.get("events") or 0),
                "selection_bias": (
                    "Only actually executed entries can receive this label; "
                    "counterfactual rejected trades remain unknown."
                ),
            },
            "candidate": (
                {
                    "id": candidate["id"],
                    "status": candidate["status"],
                    "artifact_hash": details.get("artifact_hash"),
                    "created_at": self._iso(candidate["created_ts"]),
                    "walk_forward_test_events": int(
                        walk_forward.get("test_events") or 0
                    ),
                    "walk_forward_brier_improvement": (
                        walk_forward.get("brier_improvement")
                    ),
                    "event_interval_lower_95": interval.get("lower_95"),
                    "event_interval_upper_95": interval.get("upper_95"),
                }
                if candidate is not None else None
            ),
            "shadow_ready": shadow_ready,
            "advisory_ready": advisory_ready,
            "live_eligible": live_eligible,
            "decision_score_coverage": {
                "requested": len(requested),
                "scored": len(scores),
            },
            "decision_scores": scores,
            "decision_contexts": decision_contexts,
            "live_blockers": blockers,
            "live_requirements": {
                "minimum_settled_events": PRODUCTION_MIN_EVENTS,
                "minimum_settled_observations": PRODUCTION_MIN_OBSERVATIONS,
                "positive_whole_event_uncertainty_interval": True,
                "richer_mlb_base_out_and_personnel_state": sport_key == "baseball",
                "exact_after_cost_execution_labels": True,
                "explicit_reviewed_frozen_artifact": True,
            },
        }

    def fit_candidate(
        self,
        *,
        sport: str,
        league: str = "",
        market: str = "moneyline",
    ) -> dict[str, Any]:
        sport_key = sport.strip().casefold()
        league_key = league.strip().casefold()
        feature_names = _feature_names_for(sport_key, league_key)
        baseball_segment = feature_names == _BASEBALL_FEATURE_NAMES
        if not self._segment_supported(sport_key, league_key):
            return self._store_fit_result(
                sport_key,
                league_key,
                market,
                "blocked_missing_sport_state",
                {
                    "reason": "The sport profile lacks safe structured live-state inputs.",
                    "research_only": True,
                    "promoted": False,
                },
            )
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                sql = (
                    "SELECT event_id,observed_ts,settled_ts,model_probability,"
                    "result_label,features_json FROM sport_model_observations "
                    "WHERE sport=%s AND market=%s AND canceled=0 "
                    "AND result_label IS NOT NULL "
                    f"AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL}) "
                    "AND fraction_remaining IS NOT NULL "
                    "AND score_differential IS NOT NULL"
                )
                params: list[Any] = [sport_key, market]
                if league_key:
                    sql += " AND league=%s"
                    params.append(league_key)
                sql += " ORDER BY settled_ts,event_id,observed_ts"
                self._db.execute(cur, sql, tuple(params))
                raw_rows = [dict(row) for row in cur.fetchall()]
        event_order: list[str] = []
        for row in raw_rows:
            if row["event_id"] not in event_order:
                event_order.append(row["event_id"])
        if (
            len(event_order) < RESEARCH_MIN_EVENTS
            or len(raw_rows) < RESEARCH_MIN_OBSERVATIONS
        ):
            return self._store_fit_result(
                sport_key,
                league_key,
                market,
                "insufficient_data",
                {
                    "reason": (
                        f"Need {RESEARCH_MIN_EVENTS} settled events and "
                        f"{RESEARCH_MIN_OBSERVATIONS} observations for a "
                        "research comparison."
                    ),
                    "settled_events": len(event_order),
                    "settled_observations": len(raw_rows),
                    "research_only": True,
                    "promoted": False,
                },
            )
        split_index = max(1, min(len(event_order) - 1, int(len(event_order) * 0.8)))
        train_events = set(event_order[:split_index])
        train = self._prepare_rows(
            [row for row in raw_rows if row["event_id"] in train_events],
            feature_names,
        )
        test = self._prepare_rows(
            [row for row in raw_rows if row["event_id"] not in train_events],
            feature_names,
        )
        means, scales = self._feature_scaler(train, feature_names)
        coefficients = self._fit_logistic(
            train,
            means,
            scales,
            feature_names,
            baseline_offset=baseball_segment,
        )
        candidate_predictions = [
            self._predict(
                row,
                coefficients,
                means,
                scales,
                baseline_offset=baseball_segment,
            )
            for row in test
        ]
        baseline_predictions = [row["baseline_probability"] for row in test]
        candidate_metrics = _metric(test, candidate_predictions)
        baseline_metrics = _metric(test, baseline_predictions)
        walk_forward = self._walk_forward_evaluation(
            raw_rows,
            feature_names,
            event_order,
        )
        payload = {
            "research_only": True,
            "promoted": False,
            "label_policy": "trusted_settlement_source_v1",
            "trusted_settlement_sources": list(
                TRUSTED_SETTLEMENT_SOURCES
            ),
            "split_policy": "chronological_event_block_80_20",
            "validation_policy": (
                "primary chronological 80/20 holdout plus expanding-window "
                "walk-forward event blocks"
            ),
            "event_weighting": "equal_total_weight_per_event",
            "model_form": (
                "fixed_existing_consensus_logit_plus_regularized_state_correction"
                if baseball_segment
                else "regularized_logistic"
            ),
            "uses_existing_probability_as_fixed_offset": baseball_segment,
            "features": list(feature_names),
            "coefficients": {
                "intercept": coefficients[0],
                **{
                    name: coefficients[index + 1]
                    for index, name in enumerate(feature_names)
                },
            },
            "feature_means": dict(zip(feature_names, means)),
            "feature_scales": dict(zip(feature_names, scales)),
            "train_events": len(train_events),
            "test_events": len(event_order) - len(train_events),
            "train_observations": len(train),
            "test_observations": len(test),
            "candidate_metrics": candidate_metrics,
            "existing_engine_metrics": baseline_metrics,
            "brier_improvement": baseline_metrics["brier"] - candidate_metrics["brier"],
            "walk_forward_evaluation": walk_forward,
            "production_ready": (
                not baseball_segment
                and len(event_order) >= PRODUCTION_MIN_EVENTS
                and len(raw_rows) >= PRODUCTION_MIN_OBSERVATIONS
                and candidate_metrics["brier"] < baseline_metrics["brier"]
            ),
            "research_phase": (
                "mlb_official_base_out_shadow"
                if baseball_segment else "generic_live_state"
            ),
            "mlb_blueprint": MLB_RESEARCH_BLUEPRINT if baseball_segment else None,
            "production_note": (
                "Even a production-ready research result requires explicit review, "
                "versioning, calibration, and a separate installation step."
            ),
        }
        status = (
            "candidate_beats_baseline"
            if payload["brier_improvement"] > 0
            else "candidate_does_not_beat_baseline"
        )
        return self._store_fit_result(
            sport_key, league_key, market, status, payload
        )

    def _walk_forward_evaluation(
        self,
        raw_rows: list[dict[str, Any]],
        feature_names: tuple[str, ...],
        event_order: list[str],
    ) -> dict[str, Any]:
        """Expanding-window, whole-event out-of-sample evaluation.

        Each fold trains only on earlier settled games. Test games retain equal
        total weight, and the final uncertainty interval resamples whole games,
        never individual 15-second observations.
        """
        initial_train_count = max(5, int(len(event_order) * 0.5))
        remaining = len(event_order) - initial_train_count
        block_size = max(1, math.ceil(remaining / WALK_FORWARD_MAX_FOLDS))
        folds: list[dict[str, Any]] = []
        combined_rows: list[dict[str, Any]] = []
        combined_candidate: list[float] = []
        combined_baseline: list[float] = []
        for start in range(initial_train_count, len(event_order), block_size):
            train_ids = set(event_order[:start])
            test_event_ids = event_order[start:start + block_size]
            test_ids = set(test_event_ids)
            train = self._prepare_rows(
                [row for row in raw_rows if row["event_id"] in train_ids],
                feature_names,
            )
            test = self._prepare_rows(
                [row for row in raw_rows if row["event_id"] in test_ids],
                feature_names,
            )
            if not train or not test:
                continue
            means, scales = self._feature_scaler(train, feature_names)
            coefficients = self._fit_logistic(
                train,
                means,
                scales,
                feature_names,
                baseline_offset=(
                    feature_names == _BASEBALL_FEATURE_NAMES
                ),
            )
            candidate_predictions = [
                self._predict(
                    row,
                    coefficients,
                    means,
                    scales,
                    baseline_offset=(
                        feature_names == _BASEBALL_FEATURE_NAMES
                    ),
                )
                for row in test
            ]
            baseline_predictions = [
                row["baseline_probability"] for row in test
            ]
            candidate_metrics = _metric(test, candidate_predictions)
            baseline_metrics = _metric(test, baseline_predictions)
            folds.append(
                {
                    "fold": len(folds) + 1,
                    "train_events": len(train_ids),
                    "test_events": len(test_event_ids),
                    "test_event_ids": test_event_ids,
                    "train_observations": len(train),
                    "test_observations": len(test),
                    "candidate_metrics": candidate_metrics,
                    "existing_engine_metrics": baseline_metrics,
                    "brier_improvement": (
                        baseline_metrics["brier"] - candidate_metrics["brier"]
                    ),
                }
            )
            combined_rows.extend(test)
            combined_candidate.extend(candidate_predictions)
            combined_baseline.extend(baseline_predictions)

        candidate_metrics = _metric(combined_rows, combined_candidate)
        baseline_metrics = _metric(combined_rows, combined_baseline)
        event_improvements: list[dict[str, Any]] = []
        event_ids = list(dict.fromkeys(row["event_id"] for row in combined_rows))
        for event_id in event_ids:
            indices = [
                index
                for index, row in enumerate(combined_rows)
                if row["event_id"] == event_id
            ]
            event_rows = [combined_rows[index] for index in indices]
            event_candidate = [combined_candidate[index] for index in indices]
            event_baseline = [combined_baseline[index] for index in indices]
            candidate_event_metric = _metric(event_rows, event_candidate)
            baseline_event_metric = _metric(event_rows, event_baseline)
            event_improvements.append(
                {
                    "event_id": event_id,
                    "brier_improvement": (
                        baseline_event_metric["brier"]
                        - candidate_event_metric["brier"]
                    ),
                }
            )
        improvement_values = [
            row["brier_improvement"] for row in event_improvements
        ]
        interval = self._event_bootstrap_interval(improvement_values)
        return {
            "policy": "expanding_window_chronological_event_blocks",
            "max_folds": WALK_FORWARD_MAX_FOLDS,
            "folds": folds,
            "test_events": len(event_ids),
            "test_observations": len(combined_rows),
            "candidate_metrics": candidate_metrics,
            "existing_engine_metrics": baseline_metrics,
            "brier_improvement": (
                baseline_metrics["brier"] - candidate_metrics["brier"]
            ),
            "event_brier_improvements": event_improvements,
            "candidate_better_event_fraction": (
                sum(value > 0.0 for value in improvement_values)
                / len(improvement_values)
                if improvement_values else 0.0
            ),
            "event_block_bootstrap": interval,
            "candidate_calibration": self._calibration_bins(
                combined_rows, combined_candidate
            ),
            "existing_engine_calibration": self._calibration_bins(
                combined_rows, combined_baseline
            ),
            "production_note": (
                "This evaluation is diagnostic only. Its predictions are not "
                "installed and do not change an entry, exit, or order."
            ),
        }

    @staticmethod
    def _event_bootstrap_interval(values: list[float]) -> dict[str, Any]:
        if len(values) < 2:
            return {
                "draws": 0,
                "events": len(values),
                "mean": values[0] if values else 0.0,
                "lower_95": None,
                "upper_95": None,
                "probability_improvement_positive": None,
            }
        seed_material = json.dumps(
            [round(value, 12) for value in values],
            separators=(",", ":"),
        )
        seed = int(
            hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16],
            16,
        )
        generator = random.Random(seed)
        estimates = []
        for _ in range(BOOTSTRAP_DRAWS):
            sample = [generator.choice(values) for _ in values]
            estimates.append(sum(sample) / len(sample))
        estimates.sort()
        lower = estimates[int(0.025 * (len(estimates) - 1))]
        upper = estimates[int(0.975 * (len(estimates) - 1))]
        return {
            "draws": BOOTSTRAP_DRAWS,
            "events": len(values),
            "mean": sum(values) / len(values),
            "lower_95": lower,
            "upper_95": upper,
            "probability_improvement_positive": (
                sum(value > 0.0 for value in estimates) / len(estimates)
            ),
            "resampling_unit": "settled_event",
        }

    @staticmethod
    def _calibration_bins(
        rows: list[dict[str, Any]],
        predictions: list[float],
        *,
        bins: int = 5,
    ) -> list[dict[str, Any]]:
        result = []
        for index in range(bins):
            lower = index / bins
            upper = (index + 1) / bins
            members = [
                (row, probability)
                for row, probability in zip(rows, predictions)
                if lower <= probability < upper
                or (index == bins - 1 and probability == upper)
            ]
            if not members:
                continue
            total_weight = sum(row["weight"] for row, _ in members)
            result.append(
                {
                    "lower": lower,
                    "upper": upper,
                    "observations": len(members),
                    "events": len(
                        {row["event_id"] for row, _ in members}
                    ),
                    "mean_prediction": (
                        sum(
                            row["weight"] * probability
                            for row, probability in members
                        )
                        / total_weight
                    ),
                    "observed_rate": (
                        sum(
                            row["weight"] * row["label"]
                            for row, _ in members
                        )
                        / total_weight
                    ),
                }
            )
        return result

    def _store_fit_result(
        self,
        sport: str,
        league: str,
        market: str,
        status: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_id = str(uuid4())
        created_ts = time.time()
        full = {
            **payload,
            "lab_version": MODEL_LAB_VERSION,
            "candidate_id": candidate_id,
        }
        full["artifact_hash"] = hashlib.sha256(
            json.dumps(full, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._lock:
            with self._db.transaction() as cur:
                self._db.execute(
                    cur,
                    """INSERT INTO sport_model_candidates
                       (id,created_ts,sport,league,market,status,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        candidate_id,
                        created_ts,
                        sport,
                        league,
                        market,
                        status,
                        json.dumps(full, sort_keys=True, separators=(",", ":")),
                    ),
                )
        return {
            "id": candidate_id,
            "created_at": self._iso(created_ts),
            "sport": sport,
            "league": league,
            "market": market,
            "status": status,
            "details": full,
        }

    @staticmethod
    def _oriented_score(
        side: str,
        home_score: float | None,
        away_score: float | None,
    ) -> float | None:
        if home_score is None or away_score is None or side == "draw":
            return None if side == "draw" else None
        difference = home_score - away_score
        return difference if side == "home" else -difference

    @staticmethod
    def _possession_indicator(
        event: Event,
        side: str,
        state: GameState | None,
    ) -> float | None:
        if state is None or not state.possession or side == "draw":
            return None
        possession = _norm(state.possession)
        selected = _norm(event.home if side == "home" else event.away)
        opponent = _norm(event.away if side == "home" else event.home)
        if possession in {side, selected}:
            return 1.0
        if possession in {
            "away" if side == "home" else "home",
            opponent,
        }:
            return 0.0
        return None

    def _previous_observation(
        self, event_id: str, market: str, outcome: str
    ) -> dict[str, Any] | None:
        with self._lock:
            with self._db.cursor(dict_rows=True) as cur:
                self._db.execute(
                    cur,
                    """SELECT market_probability,score_differential
                       FROM sport_model_observations
                       WHERE event_id=%s AND market=%s AND outcome=%s
                       ORDER BY observed_ts DESC LIMIT 1""",
                    (event_id, market, outcome),
                )
                row = cur.fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _execution_quote(
        signal: Signal,
        quotes: list[Quote],
        market: str,
    ) -> Quote | None:
        candidates = [
            quote
            for quote in quotes
            if line_type(quote.market) == market
            and _norm(quote.outcome) == _norm(signal.outcome)
        ]
        if signal.token_id:
            token_matches = [
                quote for quote in candidates if quote.token_id == signal.token_id
            ]
            if len(token_matches) == 1:
                return token_matches[0]
        source = _norm(signal.quote_source)
        if source:
            source_matches = [
                quote for quote in candidates if _norm(quote.source) == source
            ]
            if len(source_matches) == 1:
                return source_matches[0]
        return candidates[0] if len(candidates) == 1 else None

    def _label_market_movement_targets(
        self,
        cur: Any,
        *,
        event_id: str,
        market: str,
        outcome: str,
        observed_ts: float,
        market_probability: float,
    ) -> None:
        lower = observed_ts - MARKET_MOVEMENT_MAX_DELAY_SECONDS
        upper = observed_ts - MARKET_MOVEMENT_HORIZON_SECONDS
        self._db.execute(
            cur,
            """SELECT o.id,o.observed_ts,o.market_probability
               FROM sport_model_observations o
               WHERE o.event_id=%s AND o.market=%s AND o.outcome=%s
                 AND o.observed_ts>=%s AND o.observed_ts<=%s
                 AND NOT EXISTS (
                     SELECT 1 FROM sport_model_targets t
                     WHERE t.observation_id=o.id
                       AND t.target_name=%s
                       AND t.horizon_seconds=%s
                 )
               ORDER BY o.observed_ts""",
            (
                event_id,
                market,
                outcome,
                lower,
                upper,
                "market_probability_change_3m",
                MARKET_MOVEMENT_HORIZON_SECONDS,
            ),
        )
        rows = [dict(row) for row in cur.fetchall()]
        definition = TARGET_DEFINITIONS["market_probability_change_3m"]
        for row in rows:
            actual_horizon = observed_ts - float(row["observed_ts"])
            self._db.execute(
                cur,
                """INSERT INTO sport_model_targets
                   (observation_id,target_name,horizon_seconds,label_value,label_ts,
                    source,target_version,metadata_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(observation_id,target_name,horizon_seconds) DO NOTHING""",
                (
                    row["id"],
                    "market_probability_change_3m",
                    MARKET_MOVEMENT_HORIZON_SECONDS,
                    market_probability - float(row["market_probability"]),
                    observed_ts,
                    definition["source"],
                    definition["version"],
                    json.dumps(
                        {
                            "actual_horizon_seconds": actual_horizon,
                            "future_market_probability": market_probability,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )

    def _backfill_research_targets(self) -> None:
        """Create explicit target rows for legacy observations exactly once.

        The original outcome column remains untouched for compatibility. Target
        rows make final-game outcomes and short-horizon movement impossible to
        confuse in exports or future fitting code.
        """
        with self._db.transaction(dict_rows=True) as cur:
            outcome_definition = TARGET_DEFINITIONS["event_outcome"]
            self._db.execute(
                cur,
                """INSERT INTO sport_model_targets
                   (observation_id,target_name,horizon_seconds,label_value,label_ts,
                    source,target_version,metadata_json)
                   SELECT id,%s,0,result_label,settled_ts,%s,%s,%s
                   FROM sport_model_observations
                   WHERE result_label IS NOT NULL AND canceled=0
                     AND settlement_source IN ({_TRUSTED_SETTLEMENT_SQL})
                   ON CONFLICT(observation_id,target_name,horizon_seconds)
                   DO UPDATE SET
                     label_value=excluded.label_value,
                     label_ts=excluded.label_ts,
                     source=excluded.source,
                     target_version=excluded.target_version,
                     metadata_json=excluded.metadata_json""".format(
                       _TRUSTED_SETTLEMENT_SQL=_TRUSTED_SETTLEMENT_SQL
                   ),
                (
                    "event_outcome",
                    outcome_definition["source"],
                    outcome_definition["version"],
                    "{}",
                ),
            )
            self._db.execute(
                cur,
                "SELECT value FROM sport_model_metadata WHERE key=%s",
                ("research-target-backfill",),
            )
            marker = cur.fetchone()
            if marker is not None and marker[0] == "target-backfill-v1":
                return
            self._db.execute(
                cur,
                """SELECT id,event_id,market,outcome,observed_ts,market_probability
                   FROM sport_model_observations
                   ORDER BY event_id,market,outcome,observed_ts""",
            )
            rows = [dict(row) for row in cur.fetchall()]
            by_selection: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
            for row in rows:
                key = (row["event_id"], row["market"], row["outcome"])
                by_selection.setdefault(key, []).append(row)
            movement_definition = TARGET_DEFINITIONS[
                "market_probability_change_3m"
            ]
            target_rows: list[tuple[Any, ...]] = []
            for selection_rows in by_selection.values():
                future_index = 1
                for index, row in enumerate(selection_rows):
                    future_index = max(future_index, index + 1)
                    observed_ts = float(row["observed_ts"])
                    while (
                        future_index < len(selection_rows)
                        and float(selection_rows[future_index]["observed_ts"])
                        - observed_ts
                        < MARKET_MOVEMENT_HORIZON_SECONDS
                    ):
                        future_index += 1
                    if future_index >= len(selection_rows):
                        break
                    future = selection_rows[future_index]
                    actual_horizon = float(future["observed_ts"]) - observed_ts
                    if actual_horizon > MARKET_MOVEMENT_MAX_DELAY_SECONDS:
                        continue
                    target_rows.append(
                        (
                            row["id"],
                            "market_probability_change_3m",
                            MARKET_MOVEMENT_HORIZON_SECONDS,
                            float(future["market_probability"])
                            - float(row["market_probability"]),
                            float(future["observed_ts"]),
                            movement_definition["source"],
                            movement_definition["version"],
                            json.dumps(
                                {
                                    "actual_horizon_seconds": actual_horizon,
                                    "future_market_probability": float(
                                        future["market_probability"]
                                    ),
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        )
                    )
            if target_rows:
                self._db.execute_many(
                    cur,
                    self._db.sql(
                        """INSERT INTO sport_model_targets
                           (observation_id,target_name,horizon_seconds,label_value,
                            label_ts,source,target_version,metadata_json)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(observation_id,target_name,horizon_seconds)
                           DO NOTHING"""
                    ),
                    target_rows,
                )
            self._db.execute(
                cur,
                """INSERT INTO sport_model_metadata(key,value,updated_ts)
                   VALUES (%s,%s,%s)
                   ON CONFLICT(key) DO UPDATE
                   SET value=excluded.value,updated_ts=excluded.updated_ts""",
                ("research-target-backfill", "target-backfill-v1", time.time()),
            )

    @staticmethod
    def _prepare_rows(
        rows: list[dict[str, Any]],
        feature_names: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["event_id"]] = counts.get(row["event_id"], 0) + 1
        result = []
        for row in rows:
            features = json.loads(row["features_json"])
            values = [
                _finite(features.get(name))
                for name in feature_names
            ]
            baseline_probability = float(row["model_probability"])
            result.append(
                {
                    "event_id": row["event_id"],
                    # Preserve missingness here. The scaler learns a mean from
                    # training events only and `_scaled_vector` maps missing to
                    # standardized zero. Raw zero is a real score/count value.
                    "values": values,
                    "label": int(row["result_label"]),
                    "baseline_probability": baseline_probability,
                    "baseline_logit": _logit(baseline_probability),
                    "weight": 1.0 / counts[row["event_id"]],
                }
            )
        return result

    @staticmethod
    def _feature_scaler(
        rows: list[dict[str, Any]],
        feature_names: tuple[str, ...],
    ) -> tuple[list[float], list[float]]:
        means = []
        scales = []
        for index in range(len(feature_names)):
            present = [
                (row["weight"], row["values"][index])
                for row in rows
                if row["values"][index] is not None
            ]
            present_weight = sum(weight for weight, _ in present)
            if not present or present_weight <= 0:
                means.append(0.0)
                scales.append(1.0)
                continue
            mean = sum(weight * value for weight, value in present) / present_weight
            variance = sum(
                weight * (value - mean) ** 2 for weight, value in present
            ) / present_weight
            means.append(mean)
            scales.append(max(math.sqrt(variance), 1e-6))
        return means, scales

    @staticmethod
    def _scaled_vector(
        row: dict[str, Any],
        means: list[float],
        scales: list[float],
    ) -> list[float]:
        return [
            0.0 if value is None else (value - mean) / scale
            for value, mean, scale in zip(row["values"], means, scales)
        ]

    @staticmethod
    def _fit_logistic(
        rows: list[dict[str, Any]],
        means: list[float],
        scales: list[float],
        feature_names: tuple[str, ...],
        *,
        baseline_offset: bool = False,
    ) -> list[float]:
        """Fit the existing regularized logistic objective with L-BFGS.

        The original implementation rebuilt every standardized feature vector
        during each of 1,200 gradient passes. That was tolerable for the
        initial research minimum, but made a fit over tens of thousands of
        retained rows appear to hang. Precomputing the vectors and using a
        deterministic limited-memory quasi-Newton optimizer preserves the
        model form, event weights, baseline offset, and L2 penalty while
        converging in far fewer complete data passes.
        """
        dimension = len(feature_names) + 1
        coefficients = [0.0] * dimension
        total_weight = sum(row["weight"] for row in rows)
        if not rows or total_weight <= 0:
            return coefficients
        prepared = [
            (
                tuple(SportModelLab._scaled_vector(row, means, scales)),
                float(row["label"]),
                float(row["weight"]) / total_weight,
                float(row["baseline_logit"]) if baseline_offset else 0.0,
            )
            for row in rows
        ]

        def objective_gradient(values: list[float]) -> tuple[float, list[float]]:
            objective = 0.0
            gradient = [0.0] * dimension
            for vector, label, weight, offset in prepared:
                linear = offset + values[0]
                for index, feature in enumerate(vector, start=1):
                    linear += values[index] * feature
                if linear >= 0.0:
                    objective += weight * (
                        (1.0 - label) * linear + math.log1p(math.exp(-linear))
                    )
                else:
                    objective += weight * (
                        -label * linear + math.log1p(math.exp(linear))
                    )
                error = (_sigmoid(linear) - label) * weight
                gradient[0] += error
                for index, feature in enumerate(vector, start=1):
                    gradient[index] += error * feature
            for index in range(1, dimension):
                objective += 0.01 * values[index] * values[index]
                gradient[index] += 0.02 * values[index]
            return objective, gradient

        objective, gradient = objective_gradient(coefficients)
        history: list[tuple[list[float], list[float], float]] = []
        for _ in range(160):
            if max(abs(value) for value in gradient) <= 1e-7:
                break

            direction = list(gradient)
            alphas: list[float] = []
            for step, change, reciprocal in reversed(history):
                alpha = reciprocal * sum(
                    left * right for left, right in zip(step, direction)
                )
                alphas.append(alpha)
                for index in range(dimension):
                    direction[index] -= alpha * change[index]
            if history:
                last_step, last_change, _ = history[-1]
                curvature = sum(
                    left * right
                    for left, right in zip(last_step, last_change)
                )
                change_norm = sum(value * value for value in last_change)
                scale = curvature / change_norm if change_norm > 0.0 else 1.0
            else:
                scale = 1.0
            direction = [scale * value for value in direction]
            for (step, change, reciprocal), alpha in zip(
                history, reversed(alphas)
            ):
                beta = reciprocal * sum(
                    left * right for left, right in zip(change, direction)
                )
                for index in range(dimension):
                    direction[index] += step[index] * (alpha - beta)
            direction = [-value for value in direction]
            directional_derivative = sum(
                left * right for left, right in zip(gradient, direction)
            )
            if directional_derivative >= 0.0:
                direction = [-value for value in gradient]
                directional_derivative = -sum(
                    value * value for value in gradient
                )

            step_size = 1.0
            accepted = False
            next_values = coefficients
            next_objective = objective
            next_gradient = gradient
            for _ in range(20):
                trial = [
                    value + step_size * change
                    for value, change in zip(coefficients, direction)
                ]
                trial_objective, trial_gradient = objective_gradient(trial)
                if trial_objective <= (
                    objective
                    + 1e-4 * step_size * directional_derivative
                ):
                    next_values = trial
                    next_objective = trial_objective
                    next_gradient = trial_gradient
                    accepted = True
                    break
                step_size *= 0.5
            if not accepted:
                break

            step = [
                new - old for new, old in zip(next_values, coefficients)
            ]
            change = [
                new - old for new, old in zip(next_gradient, gradient)
            ]
            curvature = sum(
                left * right for left, right in zip(step, change)
            )
            if curvature > 1e-12:
                history.append((step, change, 1.0 / curvature))
                if len(history) > 8:
                    history.pop(0)
            relative_improvement = abs(objective - next_objective) / max(
                1.0, abs(objective)
            )
            coefficients = next_values
            objective = next_objective
            gradient = next_gradient
            if (
                max(abs(value) for value in step) <= 1e-8
                and relative_improvement <= 1e-10
            ):
                break
        return coefficients

    @staticmethod
    def _predict(
        row: dict[str, Any],
        coefficients: list[float],
        means: list[float],
        scales: list[float],
        *,
        baseline_offset: bool = False,
    ) -> float:
        vector = SportModelLab._scaled_vector(row, means, scales)
        return _sigmoid(
            (row["baseline_logit"] if baseline_offset else 0.0)
            + coefficients[0]
            + sum(
                coefficient * value
                for coefficient, value in zip(coefficients[1:], vector)
            )
        )

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        with path.open("xb") as handle:
            for row in rows:
                line = (
                    json.dumps(
                        row,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
                handle.write(line)
                digest.update(line)
        return digest.hexdigest()

    @staticmethod
    def _segment_supported(sport: str, league: str) -> bool:
        event = Event(
            name="profile",
            sport=sport,
            league=league,
            home="home",
            away="away",
        )
        profile = _profile_for(event)
        if not profile or not profile.supported:
            return False
        if profile.key == "baseball":
            return True
        return bool(league_rule(sport, league))

    @staticmethod
    def _iso(value: Any) -> str | None:
        number = _finite(value)
        if number is None:
            return None
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
