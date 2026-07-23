from datetime import datetime, timedelta, timezone

from app.identity import (CanonicalEvent, CanonicalMarket, MappingStatus,
                          ProviderEventCandidate, apply_linkage_override,
                          resolve_event_mapping, resolve_event_mappings)


def _target(home="New York Knicks", away="Boston Celtics", start=None):
    return CanonicalEvent.create("basketball", "nba", start, home, away)


def _candidate(object_id, home="New York Knicks", away="Boston Celtics", start=None):
    return ProviderEventCandidate(object_id, home, away, start, "basketball", "nba")


START = datetime(2026, 7, 20, 19, tzinfo=timezone.utc)


def test_canonical_identity_is_stable_across_case_and_punctuation():
    first = CanonicalEvent.create("Basketball", "NBA", START,
                                  "New York Knicks", "Boston Celtics")
    second = CanonicalEvent.create("basketball", "nba", START,
                                   "new-york knicks", "BOSTON CELTICS")
    assert first.canonical_event_id == second.canonical_event_id
    assert first.home.participant_id == second.home.participant_id


def test_opposite_spread_lines_get_distinct_market_identities():
    event = "event-1"
    minus = CanonicalMarket.create(event, "spread", -6.5)
    plus = CanonicalMarket.create(event, "spread", +6.5)
    even = CanonicalMarket.create(event, "spread", 0)
    assert minus.market_id != plus.market_id      # sign must not collapse
    # +6.5 and unsigned 6.5 are the same line and should agree.
    assert plus.market_id == CanonicalMarket.create(event, "spread", "+6.5").market_id
    assert len({minus.market_id, plus.market_id, even.market_id}) == 3


def test_resolver_rejects_same_teams_at_wrong_start():
    target = CanonicalEvent.create("basketball", "nba", START,
                                   "New York Knicks", "Boston Celtics")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("wrong", "New York Knicks", "Boston Celtics",
                               START + timedelta(days=1), "basketball", "nba")
    ])
    assert decision.status is MappingStatus.QUARANTINED
    assert decision.canonical_id is None


def test_resolver_quarantines_indistinguishable_doubleheader():
    target = CanonicalEvent.create("baseball", "mlb", START,
                                   "New York Yankees", "Boston Red Sox")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("game-1", "New York Yankees", "Boston Red Sox",
                               START, "baseball", "mlb"),
        ProviderEventCandidate("game-2", "New York Yankees", "Boston Red Sox",
                               START, "baseball", "mlb"),
    ])
    assert decision.status is MappingStatus.AMBIGUOUS


def test_manchester_and_team_variants_do_not_cross_match():
    target = CanonicalEvent.create("soccer", "epl", START,
                                   "Manchester United", "Arsenal")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("wrong-city", "Manchester City", "Arsenal",
                               START, "soccer", "epl"),
        ProviderEventCandidate("wrong-women", "Manchester United Women", "Arsenal Women",
                               START, "soccer", "epl"),
        ProviderEventCandidate("wrong-u21", "Manchester United U21", "Arsenal U21",
                               START, "soccer", "epl"),
    ])
    assert decision.status is MappingStatus.QUARANTINED


def test_resolver_needs_start_time_evidence_to_map():
    target = CanonicalEvent.create("basketball", "nba", None,
                                   "New York Knicks", "Boston Celtics")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("x", "New York Knicks", "Boston Celtics", None,
                               "basketball", "nba")])
    assert decision.status is MappingStatus.AMBIGUOUS   # name-only cannot rule out a rematch
    assert decision.canonical_id is None


def test_resolver_enforces_its_confidence_threshold():
    target = CanonicalEvent.create("basketball", "nba", START,
                                   "New York Knicks", "Boston Celtics")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("x", "New York Knicks", "Boston Celtics", START,
                               "basketball", "nba")], threshold=1.5)
    assert decision.status is MappingStatus.QUARANTINED  # perfect match still below 1.5
    assert decision.canonical_id is None


def test_reversed_orientation_is_recorded_not_silently_relabelled():
    target = CanonicalEvent.create("basketball", "nba", START,
                                   "New York Knicks", "Boston Celtics")
    decision = resolve_event_mapping("fixture", target, [
        ProviderEventCandidate("reversed", "Boston Celtics", "New York Knicks",
                               START, "basketball", "nba")
    ])
    assert decision.status is MappingStatus.MAPPED
    assert decision.orientation == "reversed"


# --- Fellegi-Sunter global one-to-one linkage (resolve_event_mappings) --------


def test_linkage_maps_a_clean_unambiguous_pair():
    [decision] = resolve_event_mappings(
        "fixture", [_target(start=START)], [_candidate("g1", start=START)])
    assert decision.status is MappingStatus.MAPPED
    assert decision.provider_object_id == "g1"
    assert decision.orientation == "direct"
    assert decision.posterior > 0.9


def test_linkage_records_reversed_orientation():
    [decision] = resolve_event_mappings(
        "fixture", [_target(start=START)],
        [_candidate("rev", home="Boston Celtics", away="New York Knicks", start=START)])
    assert decision.status is MappingStatus.MAPPED
    assert decision.orientation == "reversed"


def test_linkage_does_not_auto_accept_a_missing_start_rematch():
    [decision] = resolve_event_mappings(
        "fixture", [_target(start=None)], [_candidate("x", start=None)])
    assert decision.status is MappingStatus.AMBIGUOUS


def test_linkage_quarantines_a_wrong_day_candidate():
    [decision] = resolve_event_mappings(
        "fixture", [_target(start=START)],
        [_candidate("wrongday", start=START + timedelta(days=1))])
    assert decision.status is MappingStatus.QUARANTINED
    assert decision.weight <= 0.0


def test_two_targets_cannot_map_to_the_same_provider_event():
    # Both targets sit within tolerance of the one candidate with near-equal
    # weight, so neither may claim it -- the candidate is never double-assigned.
    t1 = _target(start=START)
    t2 = _target(start=START + timedelta(hours=2))
    cand = _candidate("only", start=START + timedelta(hours=1))
    decisions = resolve_event_mappings("fixture", [t1, t2], [cand])
    assert all(d.status is not MappingStatus.MAPPED for d in decisions)
    assert [d.status for d in decisions] == [MappingStatus.AMBIGUOUS] * 2


def test_clear_winner_claims_the_provider_and_rival_is_not_double_mapped():
    t1 = _target(start=START)                       # exact
    t2 = _target(start=START + timedelta(days=1))   # different day -> non-match
    cand = _candidate("only", start=START)
    decisions = resolve_event_mappings("fixture", [t1, t2], [cand])
    mapped = [d for d in decisions if d.status is MappingStatus.MAPPED]
    assert len(mapped) == 1
    assert mapped[0].target_canonical_id == t1.canonical_event_id


def test_candidate_order_does_not_change_the_mapping():
    target = _target(start=START)
    a = _candidate("a", start=START)
    b = ProviderEventCandidate("b", "Los Angeles Lakers", "Miami Heat", START,
                               "basketball", "nba")
    forward = resolve_event_mappings("fixture", [target], [a, b])
    backward = resolve_event_mappings("fixture", [target], [b, a])
    assert forward[0].provider_object_id == backward[0].provider_object_id == "a"
    assert forward[0].status is backward[0].status is MappingStatus.MAPPED


def test_linkage_override_records_full_provenance():
    [ambiguous] = resolve_event_mappings(
        "fixture", [_target(start=None)], [_candidate("x", start=None)])
    assert ambiguous.status is MappingStatus.AMBIGUOUS
    overridden = apply_linkage_override(
        ambiguous, actor="ops@desk", at=START, reason="confirmed by schedule desk",
        provider_object_id="x", new_evidence='{"source":"manual"}')
    assert overridden.status is MappingStatus.MAPPED
    assert overridden.override is not None
    assert overridden.override.actor == "ops@desk"
    assert overridden.override.reason == "confirmed by schedule desk"
    assert overridden.override.old_evidence == ambiguous.evidence_json
    assert overridden.override.new_evidence == '{"source":"manual"}'
    assert overridden.override.at == START.isoformat()
