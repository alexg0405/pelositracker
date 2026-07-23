"""Shared provider-event matching helpers."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Callable, Iterable, TypeVar


T = TypeVar("T")


_PHRASE_ALIASES = (
    (re.compile(r"\bn\.?\s*y\.?\b", re.IGNORECASE), "new york"),
    (re.compile(r"\bl\.?\s*a\.?\b", re.IGNORECASE), "los angeles"),
    (re.compile(r"\bs\.?\s*f\.?\b", re.IGNORECASE), "san francisco"),
    (re.compile(r"\bk\.?\s*c\.?\b", re.IGNORECASE), "kansas city"),
    (re.compile(r"\bd\.?\s*c\.?\b", re.IGNORECASE), "dc"),
    (re.compile(r"\bokc\b", re.IGNORECASE), "oklahoma city"),
)
_TOKEN_ALIASES = {
    "lafc": "los angeles",
    "man": "manchester",
    "nycfc": "new york city",
    "nyrb": "new york red bulls",
    "phila": "philadelphia",
    "philly": "philadelphia",
    "psg": "paris saint germain",
    "sixers": "76ers",
    "utd": "united",
}
_CLUB_NOISE = {"afc", "cf", "club", "fc", "sc", "the"}
_AMBIGUOUS_NICKNAMES = {
    "athletic", "city", "county", "racing", "sporting", "state", "town", "united"
}


def normalized_team_tokens(value: object) -> tuple[str, ...]:
    """Return provider-independent team identity tokens.

    Full team names are deliberately retained. Matching on only the final word
    confuses simultaneous fixtures such as Manchester United/City and
    Leeds United/Leicester City. A small, explicit abbreviation set covers the
    common provider variants without introducing fuzzy substring matches.
    """
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(char for char in raw if not unicodedata.combining(char))
    for pattern, replacement in _PHRASE_ALIASES:
        raw = pattern.sub(replacement, raw)
    tokens = re.findall(r"[a-z0-9]+", raw.casefold())
    normalized = []
    for token in tokens:
        if token in _CLUB_NOISE or token == "game" or re.fullmatch(r"g?\d+", token):
            continue
        normalized.extend(_TOKEN_ALIASES.get(token, token).split())
    return tuple(normalized)


def team_match_score(target: object, candidate: object) -> int | None:
    """Score a safe team-name match, or return ``None`` when identities differ."""
    expected = normalized_team_tokens(target)
    offered = normalized_team_tokens(candidate)
    if not expected or not offered:
        return None
    if expected == offered:
        return 100 + len(expected)

    # Some feeds display only a distinctive nickname ("Dodgers", "Yankees").
    # Generic soccer suffixes are intentionally not accepted on their own.
    if len(expected) == 1 or len(offered) == 1:
        short = expected[0] if len(expected) == 1 else offered[0]
        long = offered if len(expected) == 1 else expected
        if short in _AMBIGUOUS_NICKNAMES:
            return None
        return 30 if short == long[-1] else None

    shared = set(expected) & set(offered)
    # Require a distinctive final name plus another identity token. This
    # supports "Michigan St Spartans" / "Michigan State Spartans" and
    # "St Louis Cardinals" / "Saint Louis Cardinals" without suffix matching.
    if expected[-1] == offered[-1] and len(shared) >= 2:
        return 60 + len(shared)
    return None


def best_team_pair_match(candidates: Iterable[T], target_home: object, target_away: object,
                         get_names: Callable[[T], Iterable[object]], target_start,
                         get_start: Callable[[T], object], *,
                         tolerance_seconds: float = 4 * 3600) -> T | None:
    """Match both teams first, then use start time to disambiguate rematches."""
    scored: list[tuple[int, T]] = []
    for candidate in candidates:
        names = list(get_names(candidate))
        if len(names) != 2:
            continue
        direct_home = team_match_score(target_home, names[0])
        direct_away = team_match_score(target_away, names[1])
        reverse_home = team_match_score(target_home, names[1])
        reverse_away = team_match_score(target_away, names[0])
        orientations = [home + away for home, away in (
            (direct_home, direct_away), (reverse_home, reverse_away)
        ) if home is not None and away is not None]
        if orientations:
            scored.append((max(orientations), candidate))
    if not scored:
        return None
    best_score = max(score for score, _ in scored)
    return closest_start(
        [candidate for score, candidate in scored if score == best_score],
        target_start,
        get_start,
        tolerance_seconds=tolerance_seconds,
    )


def start_timestamp(value) -> float | None:
    """Parse common provider time representations into UTC epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    else:
        raw = str(value).strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def closest_start(candidates: Iterable[T], target_start,
                  get_start: Callable[[T], object], *, tolerance_seconds: float = 4 * 3600) -> T | None:
    """Choose the candidate nearest the tracked start, rejecting wrong games.

    Team names alone are ambiguous for doubleheaders. If a tracked start exists,
    a provider game more than four hours away is safer to reject than to mix its
    prices into the wrong event.
    """
    options = list(candidates)
    if not options:
        return None
    target = start_timestamp(target_start)
    if target is None:
        # Team names alone cannot distinguish a rematch/doubleheader, so without a
        # tracked start time only an unambiguous single match is safe to accept.
        return options[0] if len(options) == 1 else None
    timed = [(abs(at - target), option) for option in options
             if (at := start_timestamp(get_start(option))) is not None]
    if not timed:
        return options[0] if len(options) == 1 else None
    delta, selected = min(timed, key=lambda item: item[0])
    return selected if delta <= tolerance_seconds else None
