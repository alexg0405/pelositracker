from datetime import datetime, timedelta, timezone

from app.diagnostics import edge_health
from app.models import Quote, Signal


NOW = datetime.now(timezone.utc)


def signal(edge=.05, action="PAPER_BET", refs=2):
    return Signal("e", "moneyline", "Home", .60, .55, edge, 90, action, [], NOW,
                  quote_source="Polymarket", n_reference_sources=refs,
                  required_edge=.03)


def quotes():
    return [
        Quote("e", "moneyline", "Home", .54, "Polymarket", NOW,
              bid=.53, ask=.55, token_id="p-home"),
        Quote("e", "moneyline", "Home", .60, "Pinnacle", NOW),
        Quote("e", "moneyline", "Away", .40, "Pinnacle", NOW),
    ]


def test_edge_health_distinguishes_no_edge_from_broken_matching():
    unmatched = edge_health(quotes(), [], 120)
    assert unmatched["status"] == "unmatched_selections"
    no_edge = edge_health(quotes(), [signal(edge=-.01, action="WATCH")], 120)
    assert no_edge["status"] == "no_positive_edge"


def test_edge_health_reports_actionable_pipeline():
    health = edge_health(quotes(), [signal()], 120)
    assert health["status"] == "actionable"
    assert health["fresh_reference_sources"] == ["Pinnacle"]
    assert health["actionable_edges"] == 1


def test_edge_health_does_not_call_a_stale_ask_executable():
    stale = Quote("e", "moneyline", "Home", .54, "Polymarket",
                  NOW - timedelta(seconds=121), bid=.53, ask=.55, token_id="p-home")
    health = edge_health([stale, *quotes()[1:]], [], 120)
    assert health["status"] == "waiting_for_market"
    assert health["polymarket_asks"] == 0
