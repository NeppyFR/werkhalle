"""Simulated time driven by the wall clock, per PLAN section 2."""
from __future__ import annotations

import time
from collections.abc import Callable

ALLOWED_FACTORS = (0.0, 60.0, 120.0, 240.0, 480.0)
MAX_WALL_STEP_S = 2.0


class SimClock:
    """sim = anchor_sim + (wall - anchor_wall) * factor / 60, re-anchored on every factor change.

    A wall step longer than `max_step_s` (laptop sleep, a stalled loop) counts as a pause beyond that step, so the plant
    does not jump hours ahead when the machine wakes up. Factor 0 is a pause.
    """

    def __init__(
        self,
        factor: float = 120.0,
        start_min: float = 0.0,
        wall: Callable[[], float] = time.monotonic,
        max_step_s: float = MAX_WALL_STEP_S,
    ):
        if float(factor) not in ALLOWED_FACTORS:
            raise ValueError(f"time factor {factor} not in {ALLOWED_FACTORS}")
        self._wall = wall
        self._max_step_s = max_step_s
        self._factor = float(factor)
        self._anchor_sim = float(start_min)
        self._anchor_wall = wall()
        self._last_wall = self._anchor_wall

    @property
    def factor(self) -> float:
        return self._factor

    def now(self) -> float:
        wall = self._wall()
        step = wall - self._last_wall
        if step > self._max_step_s:
            self._anchor_wall += step - self._max_step_s
        self._last_wall = wall
        return self._anchor_sim + (wall - self._anchor_wall) * self._factor / 60.0

    def set_factor(self, factor: float) -> bool:
        """Apply an allowed factor and re-anchor. Anything else changes nothing and returns False."""
        try:
            factor = float(factor)
        except (TypeError, ValueError):
            return False
        if factor not in ALLOWED_FACTORS:
            return False
        self._anchor_sim = self.now()
        self._anchor_wall = self._last_wall
        self._factor = factor
        return True
