from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .execution import BookLevel, D


class BookGapError(ValueError):
    pass


@dataclass(slots=True)
class OrderBookState:
    token_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    book_hash: str | None = None
    timestamp_ms: int | None = None
    synchronized: bool = False

    def apply_snapshot(self, payload: dict) -> None:
        asset = str(payload.get("asset_id") or self.token_id)
        if asset != self.token_id:
            raise ValueError("snapshot token mismatch")
        self.bids = self._levels(payload.get("bids", []))
        self.asks = self._levels(payload.get("asks", []))
        self.book_hash = str(payload.get("hash")) if payload.get("hash") else None
        self.timestamp_ms = self._timestamp(payload)
        self.synchronized = self.book_hash is not None and self.timestamp_ms is not None

    def apply_change(self, payload: dict, change: dict) -> None:
        if not self.synchronized:
            raise BookGapError("delta received before a verified snapshot")
        timestamp = self._timestamp(payload)
        if timestamp is None or (self.timestamp_ms is not None and timestamp < self.timestamp_ms):
            self.synchronized = False
            raise BookGapError("out-of-order or timestamp-less delta")
        expected = payload.get("previous_hash")
        if expected is not None and str(expected) != self.book_hash:
            self.synchronized = False
            raise BookGapError("order-book hash gap")
        side = str(change.get("side") or "").casefold()
        levels = self.bids if side in {"buy", "bid"} else self.asks if side in {"sell", "ask"} else None
        if levels is None:
            raise ValueError("unknown order-book side")
        price, size = D(change.get("price")), D(change.get("size"))
        if size == 0:
            levels.pop(price, None)
        elif size > 0:
            levels[price] = size
        else:
            raise ValueError("negative order-book size")
        self.timestamp_ms = timestamp
        if payload.get("hash"):
            self.book_hash = str(payload["hash"])

    def best_bid(self) -> BookLevel | None:
        return BookLevel(max(self.bids), self.bids[max(self.bids)]) if self.bids else None

    def best_ask(self) -> BookLevel | None:
        return BookLevel(min(self.asks), self.asks[min(self.asks)]) if self.asks else None

    def ask_levels(self) -> tuple[BookLevel, ...]:
        return tuple(BookLevel(price, self.asks[price]) for price in sorted(self.asks))

    @staticmethod
    def _levels(raw: list[dict]) -> dict[Decimal, Decimal]:
        levels: dict[Decimal, Decimal] = {}
        for item in raw:
            price, size = D(item.get("price")), D(item.get("size"))
            if not (D(0) < price < D(1)) or size < 0:
                raise ValueError("invalid snapshot level")
            if size > 0:
                levels[price] = size
        return levels

    @staticmethod
    def _timestamp(payload: dict) -> int | None:
        raw = payload.get("timestamp")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
