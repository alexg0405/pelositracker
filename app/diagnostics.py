"""User-facing health summaries for the edge pipeline."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .models import Quote, Signal, canonical_source


def edge_health(quotes: list[Quote], signals: list[Signal], max_age_seconds: float) -> dict:
    now = datetime.now(timezone.utc)
    latest: dict[tuple[str, str, str], Quote] = {}
    latest_tokens: dict[str, Quote] = {}
    for quote in quotes:
        key = (quote.source.casefold(), quote.market, quote.outcome)
        if key not in latest or quote.observed_at >= latest[key].observed_at:
            latest[key] = quote
        if quote.token_id and (quote.token_id not in latest_tokens
                               or quote.observed_at >= latest_tokens[quote.token_id].observed_at):
            latest_tokens[quote.token_id] = quote

    fresh = [quote for quote in latest.values()
             if max(0.0, (now - quote.observed_at).total_seconds()) <= max_age_seconds]
    reference_names: dict[str, str] = {}
    for quote in fresh:
        if canonical_source(quote.source) != "polymarket":
            reference_names.setdefault(canonical_source(quote.source), quote.source)
    references = sorted(reference_names.values(), key=str.casefold)
    poly_asks = [quote for quote in latest_tokens.values()
                 if quote.source.casefold() == "polymarket" and quote.accepting_orders
                 and quote.ask is not None
                 and max(0.0, (now - quote.observed_at).total_seconds()) <= max_age_seconds]
    matched = [signal for signal in signals
               if signal.quote_source.casefold() == "polymarket"
               and signal.n_reference_sources > 0]
    actionable = [signal for signal in matched if signal.action == "PAPER_BET"]
    positive = [signal for signal in matched if signal.edge > 0]
    blockers = Counter(reason for signal in positive if signal.action != "PAPER_BET"
                       for reason in signal.reasons[-3:])

    if not poly_asks:
        status, message = "waiting_for_market", "Waiting for an executable Polymarket ask."
    elif not references:
        status, message = "waiting_for_references", "No fresh independent reference feed is connected yet."
    elif not matched:
        status, message = "unmatched_selections", "Reference feeds are live, but no selections matched across providers."
    elif not positive:
        status, message = "no_positive_edge", "Prices are matched; no positive executable edge exists right now."
    elif not actionable:
        status, message = "gated", "Positive raw edge exists, but a safety or risk gate is blocking entry."
    else:
        status = "actionable"
        message = f"{len(actionable)} selection(s) currently clear every engine gate."

    return {
        "status": status,
        "message": message,
        "polymarket_asks": len(poly_asks),
        "fresh_reference_sources": references,
        "matched_selections": len(matched),
        "positive_edges": len(positive),
        "actionable_edges": len(actionable),
        "max_edge": max((signal.edge for signal in matched), default=None),
        "top_blockers": [reason for reason, _ in blockers.most_common(3)],
    }
