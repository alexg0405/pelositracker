from datetime import datetime, timedelta, timezone

from app.engine import SignalEngine
from app.models import Quote


NOW = datetime.now(timezone.utc)


def q(source, outcome, p, bid=None, ask=None):
    return Quote("e", "moneyline", outcome, p, source, NOW,
                 bid=p - .01 if bid is None else bid,
                 ask=p + .01 if ask is None else ask)


def test_single_source_has_no_independent_reference():
    """One book cannot price against itself, so no edge is estimable."""
    engine = SignalEngine(confidence_threshold=0, edge_threshold=.03)
    result = engine.evaluate("e", [q("Pinnacle", "home", .50), q("Pinnacle", "away", .50)], [])
    assert result[0].action == "WATCH"
    assert all(x.n_reference_sources == 0 for x in result)
    assert any("no independent fair" in reason for reason in result[0].reasons)


def test_soft_book_lagging_the_sharp_consensus_is_a_paper_bet():
    """Sharp books anchor fair; a soft book we can buy cheaper is the edge."""
    engine = SignalEngine(confidence_threshold=50, edge_threshold=.02)
    quotes = [
        q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        # DraftKings lags: home buyable at 0.55.
        q("DraftKings", "home", .545, bid=.54, ask=.55),
        q("DraftKings", "away", .455, bid=.45, ask=.46),
    ]
    result = engine.evaluate("e", quotes, [])
    home = next(x for x in result if x.outcome == "home")
    assert home.quote_source == "DraftKings"      # cheapest executable
    assert home.n_reference_sources == 2          # leave-one-out excludes DK
    assert home.edge > .02
    assert home.action == "PAPER_BET"
    assert .58 < home.market_fair_prob < .62       # ~0.60 sharp consensus


def test_wide_spread_blocks_signal():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=-1)
    quotes = [Quote("e", "moneyline", side, .5, src, NOW, bid=.40, ask=.51)
              for src in ("Pinnacle", "Betfair") for side in ("home", "away")]
    result = engine.evaluate("e", quotes, [])
    assert all(x.action == "WATCH" for x in result)
    assert any("wide executable spread" in reason for reason in result[0].reasons)


def test_traditional_book_pair_is_devigged_with_shin():
    """A booksum > 1 (vig-laden) traditional pair is de-vigged via Shin."""
    engine = SignalEngine(confidence_threshold=0, edge_threshold=-1)
    # booksum = 1.10 (10% overround) on each unknown traditional book.
    quotes = [q(src, side, p)
              for src in ("BookA", "BookB")
              for side, p in (("home", .66), ("away", .44))]
    result = engine.evaluate("e", quotes, [])
    home = next(x for x in result if x.outcome == "home")
    assert home.devig_method == "shin"
    assert 0.0 < home.market_fair_prob < 1.0
    assert home.overround > 1.05                    # vig detected


def test_alternate_lines_are_grouped_and_devigged_separately():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        Quote("e", "spread", "Home -1.5", .60, "Pinnacle", NOW),
        Quote("e", "spread", "Away +1.5", .50, "Pinnacle", NOW),
        Quote("e", "spread", "Home -2.5", .55, "Pinnacle", NOW),
        Quote("e", "spread", "Away +2.5", .55, "Pinnacle", NOW),
        Quote("e", "spread", "Home -1.5", .49, "Polymarket", NOW, bid=.48, ask=.50),
        Quote("e", "spread", "Away +1.5", .51, "Polymarket", NOW, bid=.50, ask=.52),
    ]
    result = engine.evaluate("e", quotes, [], home_outcome="Home", away_outcome="Away")
    home = next(x for x in result if x.outcome == "Home -1.5")
    assert home.quote_source == "Polymarket"
    assert home.n_reference_sources == 1
    assert .52 < home.market_fair_prob < .58
    assert home.edge > .02


def test_exchange_quote_without_an_ask_is_never_actionable():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        Quote("e", "moneyline", "home", .54, "Polymarket", NOW, bid=.54, ask=None),
        Quote("e", "moneyline", "away", .46, "Polymarket", NOW, bid=.45, ask=.47),
    ]
    home = next(x for x in engine.evaluate("e", quotes, []) if x.outcome == "home")
    assert home.edge > 0
    assert home.action == "WATCH"
    assert any("no executable ask" in reason for reason in home.reasons)


def test_same_book_from_two_adapters_counts_as_one_reference():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
        q("pinnacle", "home", .60), q("pinnacle", "away", .40),
        Quote("e", "moneyline", "home", .54, "Polymarket", NOW,
              bid=.53, ask=.55, ask_size=100),
        Quote("e", "moneyline", "away", .46, "Polymarket", NOW,
              bid=.45, ask=.47, ask_size=100),
    ]
    home = next(x for x in engine.evaluate("e", quotes, []) if x.outcome == "home")
    assert home.n_reference_sources == 1
    assert home.action == "WATCH"
    assert any("fewer than 2 independent" in reason for reason in home.reasons)


def test_incomplete_three_way_book_is_excluded_from_devig():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        # Incomplete Action price set: home/away without Draw must not be
        # normalized as though this were a binary market.
        q("Bookmaker", "home", .55), q("Bookmaker", "away", .30),
        q("Pinnacle", "home", .50), q("Pinnacle", "away", .25),
        q("Pinnacle", "draw", .30),
        Quote("e", "moneyline", "home", .44, "Polymarket", NOW,
              bid=.43, ask=.45, ask_size=100),
        Quote("e", "moneyline", "away", .29, "Polymarket", NOW,
              bid=.28, ask=.30, ask_size=100),
        Quote("e", "moneyline", "draw", .24, "Polymarket", NOW,
              bid=.23, ask=.25, ask_size=100),
    ]
    home = next(x for x in engine.evaluate("e", quotes, []) if x.outcome == "home")
    assert home.n_reference_sources == 1
    assert home.action == "WATCH"


def test_stale_opposing_leg_makes_the_whole_book_stale():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0,
                          max_age_seconds=120)
    old = NOW - timedelta(seconds=121)
    quotes = [
        q("Pinnacle", "home", .60),
        Quote("e", "moneyline", "away", .40, "Pinnacle", old),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        Quote("e", "moneyline", "home", .54, "Polymarket", NOW,
              bid=.53, ask=.55, ask_size=100),
        Quote("e", "moneyline", "away", .46, "Polymarket", NOW,
              bid=.45, ask=.47, ask_size=100),
    ]
    home = next(x for x in engine.evaluate("e", quotes, []) if x.outcome == "home")
    assert home.n_reference_sources == 1


def test_unknown_exchange_depth_is_not_reported_as_zero_fillability():
    engine = SignalEngine(confidence_threshold=0, edge_threshold=0, edge_z=0)
    quotes = [
        q("Pinnacle", "home", .60), q("Pinnacle", "away", .40),
        q("Betfair", "home", .60), q("Betfair", "away", .40),
        Quote("e", "moneyline", "home", .54, "Polymarket", NOW,
              bid=.53, ask=.55, liquidity=None, ask_size=None),
        Quote("e", "moneyline", "away", .46, "Polymarket", NOW,
              bid=.45, ask=.47, liquidity=None, ask_size=None),
    ]
    home = next(x for x in engine.evaluate("e", quotes, []) if x.outcome == "home")
    assert home.edge > 0
    assert home.fillable_size is None
    assert home.action == "PAPER_BET"
