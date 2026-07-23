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


def test_line_probability_partition_sums_to_one_with_a_real_push():
    part = pj.line_probability(8.0, 8.0, "rebounds")  # integer line -> real push mass
    assert part.distribution == "poisson"
    assert part.push > 0.0
    assert part.under + part.push + part.over == pytest.approx(
        1.0, abs=part.numerical_error_bound + 1e-9)


def test_half_integer_line_has_no_push():
    part = pj.line_probability(8.0, 7.5, "rebounds")
    assert part.push == 0.0
    assert part.under + part.over == pytest.approx(
        1.0, abs=part.numerical_error_bound + 1e-9)


def test_high_mean_poisson_tail_does_not_underflow():
    # mu here made the old forward `exp(-mu)` recurrence underflow to zero.
    part = pj.line_probability(744.0, 743.5, "rebounds")
    assert 0.0 < part.over < 1.0
    assert part.over == pytest.approx(0.5, abs=0.05)  # ~symmetric around the mean
    # the exact-sum regime is likewise finite and normalized
    exact = pj.line_probability(60.0, 59.5, "rebounds")
    assert exact.under + exact.over == pytest.approx(1.0, abs=1e-6)


def test_tail_probability_is_not_clipped_to_a_floor():
    # A line ~22 above the mean: a real, tiny probability, not the old 0.001 floor.
    tiny = pj.over_probability(3.0, 25.5, "rebounds")
    assert 0.0 < tiny < 1e-6


def test_equicorrelation_domain_is_enforced():
    # d=3 -> admissible rho in [-0.5, 1]; outside that the covariance is not PSD.
    with pytest.raises(ValueError):
        pj.pra_line_probability([10, 10, 10], [3, 3, 3], -0.9, 25.5)
    with pytest.raises(ValueError):
        pj.pra_line_probability([10, 10, 10], [3, 3, 3], 1.5, 25.5)
    ok = pj.pra_line_probability([10, 10, 10], [3, 3, 3], 0.2, 25.5)
    assert ok.under + ok.over == pytest.approx(1.0, abs=ok.numerical_error_bound + 1e-9)


def test_malformed_inputs_raise():
    with pytest.raises(ValueError):
        pj.line_probability(float("nan"), 5.5, "rebounds")
    with pytest.raises(ValueError):
        pj.line_probability(-1.0, 5.5, "rebounds")  # negative count mean
    with pytest.raises(ValueError):
        pj.line_probability(5.0, 5.5, "points", sigma=0.0)
