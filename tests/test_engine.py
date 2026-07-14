from datetime import datetime, timezone

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
