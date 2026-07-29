"""Portable, secret-free research evidence archives.

The archive is newline-delimited JSON inside gzip. It contains closed managed
trades, audit journal rows, adaptive-exit labels, and Model Lab evidence. It
never contains execution policy controls, credentials, open positions, orders,
cookies, or environment values.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from .adaptive_exit_model import (
    RESEARCH_EVIDENCE_TABLES as ADAPTIVE_EVIDENCE_TABLES,
)
from .polymarket_us_trading import (
    RESEARCH_EVIDENCE_TABLES as TRADING_EVIDENCE_TABLES,
    PolymarketUSAutoTrader,
)
from .sport_model_lab import (
    RESEARCH_EVIDENCE_TABLES as MODEL_EVIDENCE_TABLES,
    SportModelLab,
)


BUNDLE_FORMAT = "pelositracker-research-evidence"
BUNDLE_VERSION = 1
MAX_UNCOMPRESSED_BYTES = 2_000_000_000
MAX_ROWS = 5_000_000
MAX_LINE_BYTES = 64_000_000
_TRADER_TABLES = TRADING_EVIDENCE_TABLES | ADAPTIVE_EVIDENCE_TABLES
_ALL_TABLES = _TRADER_TABLES | MODEL_EVIDENCE_TABLES


class ResearchBundleError(ValueError):
    """A bounded validation or merge failure suitable for an API response."""


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError(f"unsupported research evidence value: {type(value).__name__}")


def _line(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
        + b"\n"
    )


def write_research_bundle(
    path: str | Path,
    *,
    traders: Mapping[str, PolymarketUSAutoTrader],
    model_lab: SportModelLab | None,
) -> dict[str, Any]:
    """Write a deterministic, auditable evidence archive to ``path``."""
    destination = Path(path)
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    header = {
        "kind": "manifest",
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "calculation_changes": "none",
        "contains_credentials": False,
        "contains_open_positions": False,
        "sources": [
            *[
                {"name": name, "kind": "managed_trading_evidence"}
                for name in traders
            ],
            *(
                [{"name": "model_lab", "kind": "model_research_evidence"}]
                if model_lab is not None
                else []
            ),
        ],
    }
    with destination.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            compresslevel=6,
            mtime=0,
        ) as archive:
            encoded = _line(header)
            archive.write(encoded)
            digest.update(encoded)
            for source, trader in traders.items():
                for table, rows in trader.iter_research_batches():
                    payload = {
                        "kind": "batch",
                        "source": source,
                        "table": table,
                        "rows": rows,
                    }
                    encoded = _line(payload)
                    archive.write(encoded)
                    digest.update(encoded)
                    counts[table] = counts.get(table, 0) + len(rows)
            if model_lab is not None:
                for table, rows in model_lab.iter_research_batches():
                    payload = {
                        "kind": "batch",
                        "source": "model_lab",
                        "table": table,
                        "rows": rows,
                    }
                    encoded = _line(payload)
                    archive.write(encoded)
                    digest.update(encoded)
                    counts[table] = counts.get(table, 0) + len(rows)
            archive.write(_line({
                "kind": "trailer",
                "sha256": digest.hexdigest(),
                "counts": counts,
                "rows": sum(counts.values()),
            }))
    return {
        "path": str(destination),
        "sha256": digest.hexdigest(),
        "counts": counts,
        "rows": sum(counts.values()),
        "bytes": destination.stat().st_size,
    }


def _read_and_validate(fileobj: BinaryIO) -> dict[str, Any]:
    fileobj.seek(0)
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    total_bytes = 0
    total_rows = 0
    manifest: dict[str, Any] | None = None
    trailer: dict[str, Any] | None = None
    try:
        archive = gzip.GzipFile(fileobj=fileobj, mode="rb")
        with archive:
            for index, raw_line in enumerate(archive):
                total_bytes += len(raw_line)
                if len(raw_line) > MAX_LINE_BYTES:
                    raise ResearchBundleError("research bundle contains an oversized batch")
                if total_bytes > MAX_UNCOMPRESSED_BYTES:
                    raise ResearchBundleError("research bundle is too large")
                try:
                    item = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ResearchBundleError("research bundle contains invalid JSON") from exc
                if not isinstance(item, dict):
                    raise ResearchBundleError("research bundle record must be an object")
                kind = item.get("kind")
                if index == 0:
                    if (
                        kind != "manifest"
                        or item.get("format") != BUNDLE_FORMAT
                        or item.get("version") != BUNDLE_VERSION
                        or item.get("contains_credentials") is not False
                        or item.get("contains_open_positions") is not False
                    ):
                        raise ResearchBundleError("unsupported research bundle manifest")
                    manifest = item
                    digest.update(raw_line)
                    continue
                if kind == "trailer":
                    trailer = item
                    break
                if kind != "batch":
                    raise ResearchBundleError("unsupported research bundle record")
                table = item.get("table")
                rows = item.get("rows")
                if (
                    not isinstance(table, str)
                    or table not in _ALL_TABLES
                    or not isinstance(rows, list)
                ):
                    raise ResearchBundleError("unsupported research evidence table")
                if not rows or len(rows) > 5_000:
                    raise ResearchBundleError("invalid research evidence batch size")
                for row in rows:
                    if not isinstance(row, dict) or not row:
                        raise ResearchBundleError("invalid research evidence row")
                    if any(not isinstance(key, str) for key in row):
                        raise ResearchBundleError("invalid research evidence column")
                    for value in row.values():
                        if not isinstance(value, (str, int, float, bool, type(None))):
                            raise ResearchBundleError(
                                "research evidence values must be scalar"
                            )
                        if isinstance(value, float) and not math.isfinite(value):
                            raise ResearchBundleError(
                                "research evidence contains a non-finite number"
                            )
                    if table == "live_managed_positions" and (
                        str(row.get("status") or "").casefold() != "closed"
                    ):
                        raise ResearchBundleError(
                            "research bundles cannot contain open positions"
                        )
                total_rows += len(rows)
                if total_rows > MAX_ROWS:
                    raise ResearchBundleError("research bundle has too many rows")
                counts[table] = counts.get(table, 0) + len(rows)
                digest.update(raw_line)
    except (OSError, EOFError) as exc:
        raise ResearchBundleError("research bundle is not a valid gzip archive") from exc
    if manifest is None or trailer is None:
        raise ResearchBundleError("research bundle is incomplete")
    if trailer.get("sha256") != digest.hexdigest():
        raise ResearchBundleError("research bundle checksum does not match")
    if trailer.get("counts") != counts or int(trailer.get("rows", -1)) != total_rows:
        raise ResearchBundleError("research bundle row counts do not match")
    return {
        "manifest": manifest,
        "sha256": digest.hexdigest(),
        "counts": counts,
        "rows": total_rows,
        "uncompressed_bytes": total_bytes,
    }


def merge_research_bundle(
    fileobj: BinaryIO,
    *,
    trader: PolymarketUSAutoTrader,
    model_lab: SportModelLab,
) -> dict[str, Any]:
    """Validate twice, then idempotently merge evidence into the central stores."""
    validated = _read_and_validate(fileobj)
    fileobj.seek(0)
    accepted: dict[str, int] = {}
    with gzip.GzipFile(fileobj=fileobj, mode="rb") as archive:
        for raw_line in archive:
            item = json.loads(raw_line)
            if item.get("kind") != "batch":
                continue
            table = item["table"]
            rows = item["rows"]
            if table in MODEL_EVIDENCE_TABLES:
                count = model_lab.merge_research_batch(table, rows)
            else:
                count = trader.merge_research_batch(table, rows)
            accepted[table] = accepted.get(table, 0) + count
    return {
        **validated,
        "accepted": accepted,
        "accepted_rows": sum(accepted.values()),
        "merge_policy": "primary-key idempotent; existing hosted rows are preserved",
    }
