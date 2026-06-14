"""Poisson / Dixon-Coles scoreline mathematics."""


from wc_kalshi.modeling.poisson import (
    dc_tau,
    one_x_two,
    poisson_pmf,
    remaining_goal_matrix,
)


def test_poisson_pmf_sums_to_one():
    total = sum(poisson_pmf(1.7, k) for k in range(40))
    assert abs(total - 1.0) < 1e-9


def test_poisson_pmf_edge_cases():
    assert poisson_pmf(0.0, 0) == 1.0
    assert poisson_pmf(0.0, 3) == 0.0


def test_dc_tau_unit_outside_low_scores():
    assert dc_tau(3, 2, 1.0, 1.0, -0.05) == 1.0
    # rho=0 makes the correction vanish everywhere
    for x in range(2):
        for y in range(2):
            assert dc_tau(x, y, 1.2, 1.0, 0.0) == 1.0


def test_remaining_goal_matrix_normalized():
    m = remaining_goal_matrix(1.5, 1.1, rho=-0.05, max_goals=12)
    assert abs(m.sum() - 1.0) < 1e-9
    assert (m >= 0).all()


def test_one_x_two_sums_to_one():
    ph, pd, pa = one_x_two(1.4, 1.0, current_diff=0, rho=-0.05)
    assert abs(ph + pd + pa - 1.0) < 1e-9


def test_higher_rate_team_more_likely():
    ph, pd, pa = one_x_two(2.0, 0.7, current_diff=0)
    assert ph > pa


def test_current_lead_dominates_when_little_time_left():
    # tiny remaining rates + a current 2-goal lead => home almost certain
    ph, pd, pa = one_x_two(0.05, 0.05, current_diff=2)
    assert ph > 0.95
