from __future__ import annotations

from collections import defaultdict, deque
import math
from threading import RLock

from .lines import comparison_keys, quote_line_side
from .models import Event, GameState, Quote, Signal, canonical_source

# Full state/quote history is persisted by HistoryDB. The live store only needs
# enough recent states for transition validation and the latest valid quote per
# engine selection/source. Keeping hundreds of complete order-book snapshots in
# RAM was the dashboard's largest long-lived buffer.
_MAX_STATES = 64


class Store:
    def __init__(self):
        self.events: dict[str, Event] = {}
        self.states: dict[str, deque[GameState]] = defaultdict(
            lambda: deque(maxlen=_MAX_STATES)
        )
        self.quotes: dict[str, dict[tuple[str, str, str], Quote]] = defaultdict(dict)
        self.signals: dict[str, list[Signal]] = defaultdict(list)
        self.state_updates: dict[str, int] = defaultdict(int)
        self.quote_updates: dict[str, int] = defaultdict(int)
        self.lock = RLock()

    def add_event(self, event: Event) -> Event:
        with self.lock:
            self.events[event.id] = event
        return event

    def add_state(self, value: GameState) -> None:
        with self.lock:
            if value.event_id not in self.events:
                return
            self.states[value.event_id].append(value)
            self.state_updates[value.event_id] += 1

    def add_quotes(self, values: list[Quote]) -> None:
        with self.lock:
            for value in values:
                event = self.events.get(value.event_id)
                if event is None:
                    continue
                self.quote_updates[value.event_id] += 1
                if not self._valid_quote(value):
                    continue
                point, side = quote_line_side(
                    value.market, value.outcome, event.home, event.away
                )
                market_key, outcome_key = comparison_keys(
                    value.market, value.outcome, event.home, event.away, point, side
                )
                key = (market_key, outcome_key, canonical_source(value.source))
                current = self.quotes[value.event_id].get(key)
                # A provider's ``last_update`` describes when its price changed,
                # not when the aggregator most recently confirmed that unchanged
                # price. Keep a newly received confirmation when provider times
                # tie so an actively polled book does not falsely expire.
                if current is None or (
                    value.observed_at,
                    value.received_at,
                ) > (
                    current.observed_at,
                    current.received_at,
                ):
                    self.quotes[value.event_id][key] = value

    def set_signals(self, event_id: str, values: list[Signal]) -> None:
        with self.lock:
            if event_id not in self.events:
                return
            self.signals[event_id] = values

    def quote_values(self, event_id: str) -> list[Quote]:
        with self.lock:
            return list(self.quotes.get(event_id, {}).values())

    def remove_event(self, event_id: str) -> None:
        """Atomically release every live reference owned by an event."""
        with self.lock:
            self.events.pop(event_id, None)
            self.states.pop(event_id, None)
            self.quotes.pop(event_id, None)
            self.signals.pop(event_id, None)
            self.state_updates.pop(event_id, None)
            self.quote_updates.pop(event_id, None)

    @staticmethod
    def _valid_quote(value: Quote) -> bool:
        def probability(number: float | None, *, bid: bool = False) -> bool:
            return (
                number is not None
                and math.isfinite(number)
                and (0.0 <= number < 1.0 if bid else 0.0 < number < 1.0)
            )

        def size(number: float | None) -> bool:
            return number is None or (math.isfinite(number) and number >= 0.0)

        return (
            probability(value.probability)
            and (value.ask is None or probability(value.ask))
            and (value.bid is None or probability(value.bid, bid=True))
            and size(value.ask_size)
            and size(value.liquidity)
        )
