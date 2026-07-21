from app.resilience import RetryBackoff


def test_retry_backoff_is_capped_jittered_and_resettable(monkeypatch):
    monkeypatch.setattr("app.resilience.random.uniform", lambda low, high: high)
    backoff = RetryBackoff(base_seconds=1, cap_seconds=4, jitter_fraction=.25)
    assert [backoff.next_delay() for _ in range(4)] == [1.25, 2.5, 4.0, 4.0]
    backoff.reset()
    assert backoff.next_delay() == 1.25
