"""The plant's clock, with a fake wall so nothing here depends on real time."""
from __future__ import annotations

import pytest

from werkhalle.plant.clock import ALLOWED_FACTORS, SimClock


class Wall:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_default_rate_one_real_second_is_two_sim_minutes():
    wall = Wall()
    clock = SimClock(wall=wall)
    assert clock.factor == 120.0
    wall.now += 1.0
    assert clock.now() == pytest.approx(2.0)
    wall.now += 0.5
    assert clock.now() == pytest.approx(3.0)


def test_factor_change_reanchors_without_a_jump():
    wall = Wall()
    clock = SimClock(factor=60.0, wall=wall)
    wall.now += 2.0
    assert clock.now() == pytest.approx(2.0)
    assert clock.set_factor(480) is True
    assert clock.factor == 480.0
    assert clock.now() == pytest.approx(2.0)
    wall.now += 1.0
    assert clock.now() == pytest.approx(10.0)


def test_factor_change_counts_time_since_the_last_read():
    wall = Wall()
    clock = SimClock(factor=60.0, wall=wall)
    wall.now += 1.0
    assert clock.set_factor(240.0)
    assert clock.now() == pytest.approx(1.0)


def test_zero_pauses_and_resumes():
    wall = Wall()
    clock = SimClock(factor=120.0, wall=wall)
    wall.now += 1.0
    assert clock.set_factor(0.0)
    paused_at = clock.now()
    wall.now += 1.5
    assert clock.now() == paused_at
    assert clock.set_factor(120.0)
    wall.now += 1.0
    assert clock.now() == pytest.approx(paused_at + 2.0)


@pytest.mark.parametrize("bad", [1, 77, 120.5, -60, 1000, float("nan"), float("inf"), None, "fast"])
def test_invalid_factor_changes_nothing(bad):
    wall = Wall()
    clock = SimClock(factor=120.0, wall=wall)
    wall.now += 1.0
    assert clock.set_factor(bad) is False
    assert clock.factor == 120.0
    wall.now += 1.0
    assert clock.now() == pytest.approx(4.0)


def test_allowed_factors_are_the_plan_set():
    assert set(ALLOWED_FACTORS) == {0.0, 60.0, 120.0, 240.0, 480.0}
    wall = Wall()
    for factor in ALLOWED_FACTORS:
        assert SimClock(factor=factor, wall=wall).factor == factor


def test_constructor_rejects_invalid_factor():
    with pytest.raises(ValueError):
        SimClock(factor=7)


def test_long_wall_gap_counts_as_pause_beyond_two_seconds():
    wall = Wall()
    clock = SimClock(factor=120.0, wall=wall)
    wall.now += 1.0
    assert clock.now() == pytest.approx(2.0)
    wall.now += 3600.0  # laptop sleep
    assert clock.now() == pytest.approx(2.0 + 2.0 * 2.0)
    wall.now += 1.0
    assert clock.now() == pytest.approx(2.0 + 4.0 + 2.0)


def test_step_at_the_limit_is_not_a_gap():
    wall = Wall()
    clock = SimClock(factor=120.0, wall=wall)
    wall.now += 2.0
    assert clock.now() == pytest.approx(4.0)


def test_sim_time_never_goes_back_across_factor_changes_and_gaps():
    wall = Wall()
    clock = SimClock(factor=480.0, wall=wall)
    last = clock.now()
    for step, factor in [(0.1, 60), (5.0, 480), (0.3, 0), (0.2, 120), (9.0, 240), (0.01, 480)]:
        wall.now += step
        now = clock.now()
        assert now >= last
        last = now
        assert clock.set_factor(factor)
        assert clock.now() == pytest.approx(last)
