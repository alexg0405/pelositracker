"""Memoized market and source identity must be indistinguishable from computing it.

`base_market_type` was measured running ~13 times per quote during ingestion,
re-deriving the same normalized key from the same handful of market names, and
that was about half of ingestion's cost. Memoizing it is only acceptable if the
functions are genuinely pure over their arguments, the caches stay bounded, and
no result changes -- which is what these tests pin.
"""
import app.lines as lines
import app.models as models
from app.lines import (
    base_market_type,
    canonical_scoped_market,
    clear_market_caches,
    comparison_keys,
    is_spread_market,
    is_total_market,
    market_cache_stats,
    market_scope,
    quote_line_side,
)
from app.models import (
    canonical_source,
    classify_source,
    clear_source_caches,
    source_cache_stats,
)


_MARKETS = [
    "moneyline", "h2h", "spread", "spreads", "totals", "over/under",
    "first_five_spread", "h2h_1st_5_innings", "totals_1st_1_innings",
    "nrfi", "yrfi", "sports_market_type_moneyline",
    "Player Points", "  SPREADS  ", "", "away_team_full_game_winner",
    "unrecognized_prop_market",
]
_SOURCES = [
    "Pinnacle", "pinnacle", "DraftKings", "draft kings", "Polymarket",
    "William Hill", "williamhill_us", "TheOddsAPI:Pinnacle", "", "Betfair",
]


def _uncached(function):
    """The undecorated implementation behind an ``lru_cache`` wrapper."""
    return function.__wrapped__


def test_market_identity_results_are_unchanged_by_caching():
    for market in _MARKETS:
        assert base_market_type(market) == _uncached(base_market_type)(market)
        assert market_scope(market) == _uncached(market_scope)(market)
        assert lines._market_key(market) == _uncached(lines._market_key)(market)


def test_source_identity_results_are_unchanged_by_caching():
    for source in _SOURCES:
        assert canonical_source(source) == _uncached(canonical_source)(source)
        assert classify_source(source) == _uncached(classify_source)(source)


def test_repeated_calls_return_the_same_answer_not_a_stale_one():
    """A cache hit must be the answer for *its own* argument."""
    for _ in range(3):
        assert base_market_type("spreads") == "spread"
        assert base_market_type("totals") == "total"
        assert base_market_type("h2h") == "moneyline"
        assert market_scope("first_five_total") == "first_five_innings"
        assert market_scope("moneyline") == "full_game"
        assert canonical_source("Pinnacle") == "pinnacle"
        assert canonical_source("DraftKings") == "draftkings"


def test_aliases_and_empty_values_survive_memoization():
    """These are the two branches a naive cache key would be most likely to lose."""
    assert canonical_source("William Hill") == "caesars"
    assert canonical_source("williamhill_us") == "caesars"
    assert canonical_source("") == "unknown"
    assert canonical_source(None) == "unknown"
    assert base_market_type("") == "market"
    # Repeat: the second call is served from the cache.
    assert canonical_source("William Hill") == "caesars"
    assert canonical_source("") == "unknown"
    assert base_market_type("") == "market"


def test_exchange_classification_still_distinguishes_venues():
    assert classify_source("Polymarket") == (1.0, True)
    assert classify_source("Pinnacle") == (1.0, False)
    # Cached tuples are immutable, so sharing one between callers is safe.
    assert classify_source("Polymarket") is classify_source("Polymarket")


def test_dependent_helpers_follow_the_cached_normalizer():
    assert is_spread_market("SPREADS") is True
    assert is_total_market("over/under") is True
    assert is_spread_market("moneyline") is False
    assert canonical_scoped_market("h2h_1st_5_innings") == "first_five_moneyline"
    assert comparison_keys("spread", "Home Team -2.5", "Home Team", "Away Team",
                           -2.5, "home") == ("spread:-2.5", "home")
    assert quote_line_side("total", "Over 210.5", "Home Team",
                           "Away Team") == (210.5, "over")


def test_caches_are_bounded_and_report_their_hit_rate():
    clear_market_caches()
    clear_source_caches()

    for _ in range(5):
        for market in _MARKETS:
            base_market_type(market)
        for source in _SOURCES:
            canonical_source(source)

    market_stats = market_cache_stats()["base_market_type"]
    source_stats = source_cache_stats()["canonical_source"]
    assert market_stats["misses"] == len(_MARKETS)  # each key computed once
    assert market_stats["hits"] == len(_MARKETS) * 4
    assert source_stats["misses"] == len(_SOURCES)
    assert market_stats["maxsize"] > 0 and source_stats["maxsize"] > 0
    assert market_stats["entries"] <= market_stats["maxsize"]


def test_high_cardinality_markets_evict_instead_of_growing_without_bound():
    """A provider emitting unbounded distinct market names must not leak memory.

    The worst case has to be cache misses -- today's cost -- not growth.
    """
    clear_market_caches()
    maxsize = market_cache_stats()["base_market_type"]["maxsize"]
    for index in range(maxsize + 500):
        base_market_type(f"synthetic_prop_market_{index}")
    stats = market_cache_stats()["base_market_type"]
    assert stats["entries"] == maxsize
    # Results stay correct for evicted and retained keys alike.
    assert base_market_type("synthetic_prop_market_0") == "synthetic_prop_market_0"
    clear_market_caches()


def test_store_and_engine_see_the_same_identity_after_caching():
    """The store's retention key and the engine's comparison key must still agree.

    They are computed by different modules that each imported these helpers by
    name, so a cache applied in one place has to be visible to both.
    """
    from app.models import Quote, now_utc
    from app.store import Store

    event = models.Event(name="Away Team at Home Team", sport="basketball",
                         home="Home Team", away="Away Team", id="e")
    store = Store()
    store.add_event(event)
    now = now_utc()
    store.add_quotes([
        Quote("e", "spread", "Home Team -2.5", 0.52, "Pinnacle", now,
              bid=0.51, ask=0.53, received_at=now),
        # Same book, second spelling: canonical_source collapses case and
        # punctuation. (An aggregator *prefix* would not collapse -- that is a
        # different source family, deliberately.)
        Quote("e", "spread", "Home Team -2.5", 0.53, "PINNACLE", now,
              bid=0.52, ask=0.54, received_at=now),
    ])
    # Both feeds are the same underlying book, so the store keeps one quote.
    assert len(store.quote_values("e")) == 1
    assert canonical_source("TheOddsAPI:Pinnacle") != canonical_source("Pinnacle")
    key = next(iter(store.quotes["e"]))
    point, side = quote_line_side("spread", "Home Team -2.5",
                                  "Home Team", "Away Team")
    assert key == (*comparison_keys("spread", "Home Team -2.5", "Home Team",
                                    "Away Team", point, side),
                   canonical_source("Pinnacle"))
