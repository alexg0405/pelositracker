import pytest

from app import projections as pj


def test_normal_over_matches_hand_value():
    p = pj.over_probability(25.0, 24.5, "points", sigma=8.0)
    assert p == pytest.approx(0.5249, abs=0.01)


def test_poisson_count_over():
    # Poisson(8), P(X > 7) = 1 - CDF(7) ~ 0.547
    assert pj.over_probability(8.0, 7.5, "rebounds") == pytest.approx(0.547, abs=0.01)


def test_negbin_has_fatter_upper_tail_than_poisson():
    poisson = pj.over_probability(8.0, 13.5, "rebounds")
    negbin = pj.over_probability(8.0, 13.5, "rebounds", dispersion=1.8)
    assert negbin > poisson  # overdispersion lifts the upper tail


def test_over_is_monotonic_decreasing_in_line():
    lower = pj.over_probability(20.0, 18.5, "points", sigma=7.0)
    higher = pj.over_probability(20.0, 22.5, "points", sigma=7.0)
    assert lower > higher


def test_blend_endpoints_and_middle():
    assert pj.blend_logit(0.7, 0.5, alpha=0.0) == pytest.approx(0.5, abs=1e-6)  # all market
    assert pj.blend_logit(0.7, 0.5, alpha=1.0) == pytest.approx(0.7, abs=1e-6)  # all model
    assert 0.5 < pj.blend_logit(0.7, 0.5, alpha=0.3) < 0.7


def test_correlated_sum_widens_variance():
    # mean 34 below line 40.5: positive correlation fattens the tail -> higher over
    independent = pj.pra_over_probability([20, 8, 6], [7, 3, 2], 0.0, 40.5)
    correlated = pj.pra_over_probability([20, 8, 6], [7, 3, 2], 0.4, 40.5)
    assert correlated > independent


def test_project_returns_none_without_a_data_source():
    assert pj.project("LeBron James", "points") is None
