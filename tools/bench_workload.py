r"""Deterministic decision-path workloads built from checked-in fixture specs.

A benchmark is only useful if its input is fixed, so ``benchmarks/fixtures/*.json``
holds *specs* -- market counts, book counts, depth, how much of the input is
superseded or invalid -- rather than pre-serialized engine requests. Building the
``Quote``/``GameState`` objects from the spec at run time means the fixture can
never silently drift out of the current request schema: if
``REQUEST_SCHEMA_VERSION`` changes, the benchmark exercises the new schema and
the reported request size moves, which is exactly the signal we want.

Generation is arithmetic, not random: the same spec always yields byte-identical
quotes, so two benchmark runs differ only by machine noise and code changes.
Every name is synthetic ("Home Team", "Book 03"); no provider data is embedded.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.models import GameState, Quote

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "benchmarks" / "fixtures"

# A fixed decision instant. Quote ages are expressed relative to it so freshness
# gates behave identically on every run and on every machine.
AS_OF = datetime(2026, 6, 1, 20, 30, 0, tzinfo=timezone.utc)

HOME = "Home Team"
AWAY = "Away Team"
EXCHANGE_SOURCE = "Polymarket"
EVENT_ID = "bench-event"

def _alias_of(book: str) -> str:
    """A second spelling of one book, as aggregator feeds actually deliver them.

    ``canonical_source`` collapses case and punctuation (it does *not* strip an
    aggregator prefix -- ``"TheOddsAPI:Pinnacle"`` is a different source family
    from ``"Pinnacle"``), so an alias has to differ only in those. Including
    aliases keeps the store's and engine's canonical-source reduction on the
    measured path, where it must not inflate the independent-source count.
    """
    return book.upper().replace(" ", "_")


def fixture_names() -> list[str]:
    return sorted(path.stem for path in FIXTURE_DIR.glob("*.json"))


def load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURE_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"unknown fixture {name!r}; available: {', '.join(fixture_names())}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _book_name(index: int) -> str:
    return f"Book {index:02d}"


def _ask_levels(depth: int, best_ask: float) -> tuple[tuple[float, float], ...]:
    """A monotonically worsening ask ladder of ``depth`` levels."""
    levels = []
    price = best_ask
    for level in range(depth):
        price = min(0.99, round(best_ask + level * 0.01, 4))
        levels.append((price, 120.0 + 10.0 * level))
    return tuple(levels)


def _quote(market: str, outcome: str, probability: float, source: str,
           *, age_seconds: float, depth: int = 0, quarantined: bool = False,
           sequence: int = 0) -> Quote:
    observed_at = AS_OF - timedelta(seconds=age_seconds)
    is_exchange = source == EXCHANGE_SOURCE
    best_ask = min(0.98, round(probability + 0.01, 4))
    return Quote(
        EVENT_ID, market, outcome, probability, source,
        observed_at=observed_at,
        provider_timestamp=observed_at,
        received_at=observed_at,
        processed_at=observed_at,
        bid=max(0.01, round(probability - 0.01, 4)),
        ask=best_ask,
        ask_size=250.0,
        liquidity=5_000.0,
        quarantined=quarantined,
        quarantine_reason="benchmark-quarantine" if quarantined else None,
        depth_complete=is_exchange and depth > 0,
        fee_rate=0.0 if is_exchange else None,
        tick_size=0.01 if is_exchange else None,
        min_order_size=5.0 if is_exchange else None,
        ask_levels=_ask_levels(depth, best_ask) if is_exchange and depth else (),
        token_id=f"tok-{market}-{outcome}" if is_exchange else None,
        book_hash=f"hash-{sequence:06d}" if is_exchange else None,
        sequence=sequence,
    )


def _selections(spec: dict[str, Any]) -> list[tuple[str, str, float]]:
    """Every (market, outcome, base probability) the fixture prices."""
    selections: list[tuple[str, str, float]] = [
        ("moneyline", HOME, 0.55),
        ("moneyline", AWAY, 0.45),
    ]
    for index in range(int(spec.get("spread_lines", 0))):
        line = 1.5 + index
        selections.append(("spread", f"{HOME} -{line}", 0.52 - index * 0.01))
        selections.append(("spread", f"{AWAY} +{line}", 0.48 + index * 0.01))
    for index in range(int(spec.get("total_lines", 0))):
        line = 210.5 + index * 2
        selections.append(("total", f"Over {line}", 0.51))
        selections.append(("total", f"Under {line}", 0.49))
    for index in range(int(spec.get("player_props", 0))):
        market = f"player_points_{index:02d}"
        selections.append((market, f"Player {index:02d} Over 18.5", 0.53))
        selections.append((market, f"Player {index:02d} Under 18.5", 0.47))
    return selections


def build_quotes(spec: dict[str, Any]) -> list[Quote]:
    """Every quote the fixture feeds the engine, superseded ones included.

    The stale duplicates are deliberate: the store keeps only the freshest valid
    quote per selection, and so does the engine's own reduction, so a fixture
    without a superseded tail would not measure that reduction at all.
    """
    books = int(spec.get("sportsbooks", 2))
    depth = int(spec.get("exchange_book_depth", 0))
    duplicates = int(spec.get("stale_duplicates_per_selection", 0))
    aliases = int(spec.get("aggregator_aliases", 0))
    selections = _selections(spec)
    sources = [_book_name(index) for index in range(books)]
    sources.append(EXCHANGE_SOURCE)
    sources.extend(_alias_of(_book_name(index))
                   for index in range(min(aliases, books)))

    quotes: list[Quote] = []
    sequence = 0
    for market, outcome, base in selections:
        for source_index, source in enumerate(sources):
            drift = ((source_index % 5) - 2) * 0.004
            probability = min(0.97, max(0.03, round(base + drift, 4)))
            for stale in range(duplicates, -1, -1):
                sequence += 1
                quotes.append(_quote(
                    market, outcome, probability, source,
                    # Fresh quote at 1s; each superseded copy 5s older.
                    age_seconds=1.0 + stale * 5.0,
                    depth=depth if stale == 0 else max(1, depth // 3),
                    sequence=sequence,
                ))

    # Rejected input. These never reach scoring; they measure the reduction.
    for index in range(int(spec.get("invalid_quotes", 0))):
        sequence += 1
        market, outcome, _ = selections[index % len(selections)]
        quotes.append(_quote(
            market, outcome,
            # Out of the open unit interval, so both the store's validity check
            # and the engine's own reduction must drop it.
            1.0 + index,
            _book_name(index % max(1, books)),
            age_seconds=30.0, sequence=sequence,
        ))
    for index in range(int(spec.get("quarantined_quotes", 0))):
        sequence += 1
        market, outcome, base = selections[index % len(selections)]
        quotes.append(_quote(
            market, outcome, base, EXCHANGE_SOURCE,
            age_seconds=2.0, depth=depth, quarantined=True, sequence=sequence,
        ))
    return quotes


def build_states(spec: dict[str, Any]) -> list[GameState]:
    count = int(spec.get("states", 0))
    states: list[GameState] = []
    for index in range(count):
        observed_at = AS_OF - timedelta(seconds=float(count - index))
        states.append(GameState(
            EVENT_ID,
            home_score=float(50 + index),
            away_score=float(48 + index),
            period="3",
            clock="06:30",
            source="official_feed",
            observed_at=observed_at,
            provider_timestamp=observed_at,
            received_at=observed_at,
            processed_at=observed_at,
            possession="home",
            home_team_id="home-id",
            away_team_id="away-id",
            overtime_number=0,
            sequence=index,
        ))
    return states


def build_engine():
    """A ``SignalEngine`` sized for the fixtures.

    Imported lazily so listing fixtures does not require the compiled engine.
    """
    from app.engine import SignalEngine

    engine = SignalEngine()
    engine.max_age_seconds = 600.0  # fixture ages are fixed; do not gate on them
    return engine


def build_event(spec: dict[str, Any]):
    from app.models import Event

    return Event(
        name=f"{AWAY} at {HOME}",
        sport=spec.get("sport", ""),
        home=HOME,
        away=AWAY,
        league=spec.get("league", ""),
        id=EVENT_ID,
    )


def build_store(spec: dict[str, Any], quotes: list[Quote],
                states: list[GameState]):
    """A live ``Store`` loaded with the fixture, as ingestion would leave it."""
    from app.store import Store

    store = Store()
    store.add_event(build_event(spec))
    store.add_quotes(quotes)
    for state in states:
        store.add_state(state)
    return store


def build_workload(name: str) -> dict[str, Any]:
    """The fixture as the decision path actually sees it.

    ``recompute`` scores ``store.quote_values()``, not the raw provider stream:
    the store already keeps one quote per (comparison market, outcome, canonical
    source) and drops invalid ones. Benchmarking the raw stream would therefore
    overstate the scoring workload by the duplicate factor. The raw list is kept
    alongside so the ingestion lane can measure the cost the store itself pays.
    """
    spec = load_fixture(name)
    quotes = build_quotes(spec)
    states = build_states(spec)
    store = build_store(spec, quotes, states)
    store_quotes = store.quote_values(EVENT_ID)
    engine = build_engine()
    prepared = engine.prepare_request(
        EVENT_ID, store_quotes, states, AWAY,
        sport=spec.get("sport", ""), league=spec.get("league", ""),
        home_outcome=HOME, as_of=AS_OF,
        canonical_event_id=f"canonical-{name}",
    )
    return {
        "spec": spec,
        "engine": engine,
        "event": build_event(spec),
        "supplied_quotes": quotes,
        "quotes": store_quotes,
        "states": states,
        "prepared": prepared,
    }
