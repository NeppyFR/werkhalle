"""Overall equipment effectiveness from cumulative plant counters (PLAN section 9).

Everything here is pure: no clock, no I/O, no OPC UA. The plant publishes cumulative minutes and counts; a window is the
difference of two samples of those counters, so no result depends on how often the samples were taken, and thinning old
samples cannot change a window whose ends are still present.

Formulas, per window:

    no_demand   = min(no_demand, idle)
    starved     = min(starved, idle - no_demand)
    planned     = run + setup + fault + idle - no_demand
    planned_net = planned - starved
    availability     = run / planned            (ISO 22400)
    availability_net = run / planned_net
    performance = min(1, ideal / run), the uncapped value stays available
    quality     = good / total
    oee_iso = availability     x performance x quality
    oee_net = availability_net x performance x quality

An undefined input gives None, never 0: a machine that produced nothing has an unknown quality, not a bad one.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

ROLLING_WINDOW_MIN = 480.0
WINDOW_START = "start"
WINDOW_8H = "8h"
WINDOWS = (WINDOW_START, WINDOW_8H)

NO_DEMAND = "no_demand"
STARVED = "starved"

_ROUND_MINUTES = 3
_ROUND_RATIO = 4


@dataclass(frozen=True)
class CounterSample:
    """Cumulative counters of one machine at sim minute `t`.

    `no_demand` and `starved` are cumulative too, but they come from the MES (`LossAccumulator`), not from the plant.
    """

    t: float
    run: float = 0.0
    setup: float = 0.0
    fault: float = 0.0
    idle: float = 0.0
    ideal: float = 0.0
    no_demand: float = 0.0
    starved: float = 0.0
    total: int = 0
    good: int = 0
    scrap: int = 0
    fault_count: int = 0
    in_fault: bool = False


@dataclass(frozen=True)
class Window:
    """What happened between two samples. All fields are differences, never negative."""

    t0: float
    t1: float
    run: float = 0.0
    setup: float = 0.0
    fault: float = 0.0
    idle: float = 0.0
    ideal: float = 0.0
    no_demand: float = 0.0
    starved: float = 0.0
    total: int = 0
    good: int = 0
    scrap: int = 0
    fault_count: int = 0


@dataclass(frozen=True)
class OeeResult:
    """OEE of one window. Ratios are None when undefined. `no_demand` and `starved` are the capped values."""

    run: float
    setup: float
    fault: float
    idle: float
    ideal: float
    no_demand: float
    starved: float
    planned: float
    planned_net: float
    availability: float | None
    availability_net: float | None
    performance: float | None
    performance_uncapped: float | None
    quality: float | None
    oee_iso: float | None
    oee_net: float | None
    total: int
    good: int
    scrap: int
    fault_count: int
    mtbf_min: float | None
    mttr_min: float | None


@dataclass(frozen=True)
class PlantOee:
    """Plant-wide OEE, see `aggregate_plant`."""

    availability: float | None
    availability_net: float | None
    performance: float | None
    quality: float | None
    oee_iso: float | None
    oee_net: float | None


def difference(baseline: CounterSample, latest: CounterSample) -> Window:
    """Window between two samples. A counter that went backwards (a plant restart) counts as zero, not as negative."""

    def d(a: float, b: float) -> float:
        return max(b - a, 0)

    return Window(
        t0=baseline.t,
        t1=latest.t,
        run=d(baseline.run, latest.run),
        setup=d(baseline.setup, latest.setup),
        fault=d(baseline.fault, latest.fault),
        idle=d(baseline.idle, latest.idle),
        ideal=d(baseline.ideal, latest.ideal),
        no_demand=d(baseline.no_demand, latest.no_demand),
        starved=d(baseline.starved, latest.starved),
        total=d(baseline.total, latest.total),
        good=d(baseline.good, latest.good),
        scrap=d(baseline.scrap, latest.scrap),
        # A fault already running when the window opens has its count before the window, but its minutes in it.
        fault_count=d(baseline.fault_count, latest.fault_count) + (1 if baseline.in_fault else 0),
    )


def _product(*factors: float | None) -> float | None:
    result = 1.0
    for factor in factors:
        if factor is None:
            return None
        result *= factor
    return result


def compute_oee(w: Window) -> OeeResult:
    """Apply the formulas of the module docstring to one window.

    `no_demand` can exceed `idle` because the MES samples it on a wall-clock tick: at factor 480 one tick is 8 sim minutes,
    while the plant's idle counter is exact. Without the caps availability would rise above 1.
    """
    idle = max(w.idle, 0.0)
    no_demand = min(max(w.no_demand, 0.0), idle)
    starved = min(max(w.starved, 0.0), idle - no_demand)
    busy = w.run + w.setup + w.fault
    # Sums of non-negative floats never drop below `run`, so availability cannot round to just above 1.
    planned = busy + (idle - no_demand)
    planned_net = busy + max(idle - no_demand - starved, 0.0)

    availability = w.run / planned if planned > 0 else None
    availability_net = w.run / planned_net if planned_net > 0 else None
    performance_uncapped = w.ideal / w.run if w.run > 0 else None
    performance = min(1.0, performance_uncapped) if performance_uncapped is not None else None
    quality = w.good / w.total if w.total > 0 else None

    return OeeResult(
        run=w.run,
        setup=w.setup,
        fault=w.fault,
        idle=idle,
        ideal=w.ideal,
        no_demand=no_demand,
        starved=starved,
        planned=planned,
        planned_net=planned_net,
        availability=availability,
        availability_net=availability_net,
        performance=performance,
        performance_uncapped=performance_uncapped,
        quality=quality,
        oee_iso=_product(availability, performance, quality),
        oee_net=_product(availability_net, performance, quality),
        total=w.total,
        good=w.good,
        scrap=w.scrap,
        fault_count=w.fault_count,
        mtbf_min=w.run / w.fault_count if w.fault_count > 0 else None,
        mttr_min=w.fault / w.fault_count if w.fault_count > 0 else None,
    )


def aggregate_plant(results: Iterable[OeeResult]) -> PlantOee:
    """Plant-wide OEE from per-machine results.

    Availability and performance come from summed minutes, so a long-running machine weighs more than one that barely ran.
    Quality cannot be summed, because machines count different things (Bolzen, Stangen, Körbe, Pakete), so it is the mean
    of the machine qualities weighted by ideal minutes, the value-adding time each one represents.
    """
    results = list(results)
    run = sum(r.run for r in results)
    planned = sum(r.planned for r in results)
    planned_net = sum(r.planned_net for r in results)
    ideal = sum(r.ideal for r in results)

    availability = run / planned if planned > 0 else None
    availability_net = run / planned_net if planned_net > 0 else None
    performance = min(1.0, ideal / run) if run > 0 else None
    weighted = [(r.quality, r.ideal) for r in results if r.quality is not None and r.ideal > 0]
    weight = sum(w for _, w in weighted)
    quality = sum(q * w for q, w in weighted) / weight if weight > 0 else None

    return PlantOee(
        availability=availability,
        availability_net=availability_net,
        performance=performance,
        quality=quality,
        oee_iso=_product(availability, performance, quality),
        oee_net=_product(availability_net, performance, quality),
    )


class LossAccumulator:
    """Accumulates the idle minutes the plant cannot classify: no demand and starvation.

    The plant only knows IDLE. Whether an idle machine has nothing to do (`no_demand`) or has a head op that cannot start
    (`starved`) is known to the MES, which reports it on a wall-clock tick. Each interval is attributed to the state seen at
    its start. The result is coarse by one tick and may exceed the plant's idle counter; `compute_oee` caps it.
    """

    def __init__(self) -> None:
        self._last: dict[str, tuple[float, str, str | None]] = {}
        self._no_demand: dict[str, float] = {}
        self._starved: dict[str, float] = {}

    def update(self, machine: str, t: float, status: str, reason: str | None) -> None:
        """Report one observation. `status` is the machine status, `reason` is NO_DEMAND, STARVED or None."""
        previous = self._last.get(machine)
        if previous is not None:
            last_t, last_status, last_reason = previous
            dt = t - last_t
            if dt > 0 and last_status == "IDLE":
                if last_reason == NO_DEMAND:
                    self._no_demand[machine] = self._no_demand.get(machine, 0.0) + dt
                elif last_reason == STARVED:
                    self._starved[machine] = self._starved.get(machine, 0.0) + dt
        self._last[machine] = (t, status, reason)

    def no_demand(self, machine: str) -> float:
        """Cumulative no-demand minutes of a machine."""
        return self._no_demand.get(machine, 0.0)

    def starved(self, machine: str) -> float:
        """Cumulative starved minutes of a machine."""
        return self._starved.get(machine, 0.0)

    def reset(self) -> None:
        """Forget everything, for a new plant run."""
        self._last.clear()
        self._no_demand.clear()
        self._starved.clear()


def idle_reason(waiting_on: str | None) -> str | None:
    """Map the German `waiting_on` label of `/api/state` to a loss class. `Störung` and None are no idle loss."""
    if waiting_on == "Kein Auftrag":
        return NO_DEMAND
    if waiting_on in ("Material", "Vorgänger", "Kein Plan", "Entscheidung offen"):
        return STARVED
    return None


class SampleSeries:
    """Time-ordered samples of one machine, thinned so they stay bounded while every window stays answerable.

    Samples inside the rolling window keep `fine_step_min` resolution. Older ones are only needed for the trend and thin to
    `coarse_step_min`, doubling whenever more than `max_coarse` remain. The first sample (the since-start baseline) and the
    newest sample always survive, so since-start results are exact however much was pruned.
    """

    def __init__(self, *, rolling_min: float = ROLLING_WINDOW_MIN, fine_step_min: float = 1.0,
                 coarse_step_min: float = 30.0, max_coarse: int = 400) -> None:
        self._rolling_min = rolling_min
        self._fine_step = fine_step_min
        self._coarse_step = coarse_step_min
        self._max_coarse = max_coarse
        # One extra hour of fine samples, so a window end slightly behind `latest` still finds a fine baseline.
        self._fine_span = rolling_min + 60.0
        self._limit = int(self._fine_span / fine_step_min) + max_coarse + 16
        self._samples: list[CounterSample] = []
        self._times: list[float] = []

    def __len__(self) -> int:
        return len(self._samples)

    @property
    def first(self) -> CounterSample | None:
        """The since-start baseline."""
        return self._samples[0] if self._samples else None

    @property
    def latest(self) -> CounterSample | None:
        """The newest sample."""
        return self._samples[-1] if self._samples else None

    def add(self, sample: CounterSample) -> None:
        """Append a sample, or replace the newest one while that is still less than a fine step past the one before it.

        So retained samples are at least a fine step apart, the newest is always fresh, and a paused plant adds nothing. A sample
        older than the newest one is ignored: time only moves forward within a run.
        """
        samples = self._samples
        if not samples:
            samples.append(sample)
            self._times.append(sample.t)
            return
        newest = samples[-1]
        if sample.t < newest.t:
            return
        provisional = len(samples) >= 2 and newest.t - samples[-2].t < self._fine_step
        if sample.t == newest.t or provisional:
            samples[-1] = sample
            self._times[-1] = sample.t
        else:
            samples.append(sample)
            self._times.append(sample.t)
        if len(self._samples) > self._limit:
            self._compact()

    def at_or_before(self, t: float) -> CounterSample | None:
        """The newest sample not after `t`, or None when all samples are later."""
        index = bisect_right(self._times, t) - 1
        return self._samples[index] if index >= 0 else None

    def _compact(self) -> None:
        latest = self._samples[-1].t
        cutoff = latest - self._fine_span
        while True:
            kept = [self._samples[0]]
            coarse = 0
            for sample in self._samples[1:-1]:
                if sample.t >= cutoff:
                    kept.append(sample)
                elif sample.t - kept[-1].t >= self._coarse_step:
                    kept.append(sample)
                    coarse += 1
            kept.append(self._samples[-1])
            if coarse <= self._max_coarse:
                break
            self._coarse_step *= 2
        self._samples = kept
        self._times = [s.t for s in kept]


def _rounded(value: float | None, digits: int = _ROUND_RATIO) -> float | None:
    return None if value is None else round(value, digits)


class OeeTracker:
    """Sample history and loss accumulation for all machines, and the JSON of `GET /api/oee`.

    The runtime calls `update` on a tick with the shadow's counters and the current idle reason. Queries take no clock: the
    window ends at the machine's newest sample unless `t` is given. Not thread-safe; use it from the event loop.

    "Since start" means since the first sample. On a fresh run that is the plant's start; after an MES restart in a running plant it
    is the moment the MES came back, because the accumulated losses only exist from then on.
    """

    def __init__(self, *, rolling_min: float = ROLLING_WINDOW_MIN, fine_step_min: float = 1.0,
                 coarse_step_min: float = 30.0, max_coarse: int = 400, trend_step_min: float = 60.0,
                 trend_max_points: int = 120) -> None:
        self._rolling_min = rolling_min
        self._series_args = dict(rolling_min=rolling_min, fine_step_min=fine_step_min,
                                 coarse_step_min=coarse_step_min, max_coarse=max_coarse)
        self._trend_step = trend_step_min
        self._trend_max = trend_max_points
        self._series: dict[str, SampleSeries] = {}
        self.losses = LossAccumulator()

    def update(self, machine: str, t: float, counters: Mapping[str, float], *, status: str = "",
               waiting_on: str | None = None) -> None:
        """Record one tick of one machine.

        `counters` holds the cumulative plant values with the keys of `CounterSample` (run, setup, fault, idle, ideal,
        total, good, scrap, fault_count); `Shadow.counters()` returns exactly that. `status` and the `waiting_on` label feed
        the loss accumulator.
        """
        self.losses.update(machine, t, status, idle_reason(waiting_on))
        sample = CounterSample(
            t=t,
            run=float(counters.get("run", 0.0)),
            setup=float(counters.get("setup", 0.0)),
            fault=float(counters.get("fault", 0.0)),
            idle=float(counters.get("idle", 0.0)),
            ideal=float(counters.get("ideal", 0.0)),
            no_demand=self.losses.no_demand(machine),
            starved=self.losses.starved(machine),
            total=int(counters.get("total", 0)),
            good=int(counters.get("good", 0)),
            scrap=int(counters.get("scrap", 0)),
            fault_count=int(counters.get("fault_count", 0)),
            in_fault=status == "FAULT",
        )
        self.add_sample(machine, sample)

    def add_sample(self, machine: str, sample: CounterSample) -> None:
        """Record a ready-made sample, including its own cumulative losses."""
        series = self._series.get(machine)
        if series is None:
            series = self._series[machine] = SampleSeries(**self._series_args)
        series.add(sample)

    def machines(self) -> list[str]:
        """Machine ids with at least one sample, in first-seen order."""
        return list(self._series)

    def series(self, machine: str) -> SampleSeries | None:
        """The sample series of a machine, for inspection."""
        return self._series.get(machine)

    def result(self, machine: str, window: str = WINDOW_START, t: float | None = None) -> OeeResult | None:
        """OEE of one machine for WINDOW_START or WINDOW_8H, ending at `t` (default: its newest sample).

        Returns None when the machine has no sample at or before `t`. The 8 h baseline is the newest sample not after
        `end - 480`, so the window is at most one sample spacing longer than 8 h, and counts stay whole numbers.
        """
        series = self._series.get(machine)
        if series is None:
            return None
        latest = series.latest if t is None else series.at_or_before(t)
        if latest is None:
            return None
        if window == WINDOW_START:
            baseline = series.first
        elif window == WINDOW_8H:
            baseline = series.at_or_before(latest.t - self._rolling_min) or series.first
        else:
            raise ValueError(f"unknown OEE window {window!r}")
        return compute_oee(difference(baseline, latest))

    def plant(self, window: str = WINDOW_START, t: float | None = None) -> PlantOee:
        """Plant-wide OEE of one window, see `aggregate_plant`."""
        results = (self.result(m, window, t) for m in self._series)
        return aggregate_plant(r for r in results if r is not None)

    def state_summary(self, machine: str, window: str = WINDOW_8H) -> dict | None:
        """The `oee` object of a machine in `/api/state`. `oee` and `availability` are the net values."""
        result = self.result(machine, window)
        if result is None:
            return None
        return {
            "oee": _rounded(result.oee_net),
            "oee_iso": _rounded(result.oee_iso),
            "availability": _rounded(result.availability_net),
            "performance": _rounded(result.performance),
            "quality": _rounded(result.quality),
        }

    def response(self, window: str, machines: Sequence[Mapping] = (), t: float | None = None) -> dict:
        """The JSON of `GET /api/oee?window=start|8h`.

        `machines` gives display data as dicts with `id`, `name`, `unit` (`Shadow.machines_meta()`) and sets the order;
        machines without samples still appear, with None ratios and zero minutes. `t` defaults to the newest sample seen.
        """
        meta = list(machines) or [{"id": m, "name": m, "unit": ""} for m in self._series]
        end = t if t is not None else self._latest_time()
        rows = []
        results = []
        for machine in meta:
            result = self.result(machine["id"], window, end)
            if result is not None:
                results.append(result)
            rows.append(_machine_row(machine, result))
        plant = aggregate_plant(results)
        return {
            "window": window,
            "t": end if end is not None else 0.0,
            "plant": {
                "oee": _rounded(plant.oee_net),
                "oee_iso": _rounded(plant.oee_iso),
                "availability": _rounded(plant.availability_net),
                "availability_iso": _rounded(plant.availability),
                "performance": _rounded(plant.performance),
                "quality": _rounded(plant.quality),
            },
            "machines": rows,
            "trend": self.trend(window, [m["id"] for m in meta], end),
        }

    def trend(self, window: str, machines: Sequence[str] | None = None, t: float | None = None) -> dict:
        """OEE over time: `{"t":[...],"oee":{machine:[...]}}`. Every point is the net OEE of `window` ending at that time.

        Points sit on multiples of the trend step plus the newest time, and only inside the window. The step grows for long runs,
        so a chart never gets more than `trend_max_points` points.
        """
        ids = list(machines) if machines is not None else list(self._series)
        end = t if t is not None else self._latest_time()
        if end is None:
            return {"t": [], "oee": {m: [] for m in ids}}
        step = self._trend_step
        first = end - self._rolling_min if window == WINDOW_8H else 0.0
        if (end - first) / step > self._trend_max:
            step = math.ceil((end - first) / self._trend_max / self._trend_step) * self._trend_step
        points = [point for point in (step * i for i in range(1, int(end // step) + 1)) if point >= first]
        if not points or points[-1] < end:
            points.append(end)
        oee: dict[str, list[float | None]] = {}
        for machine in ids:
            values: list[float | None] = []
            for point in points:
                result = self.result(machine, window, point)
                values.append(_rounded(result.oee_net) if result is not None else None)
            oee[machine] = values
        return {"t": [round(p, _ROUND_MINUTES) for p in points], "oee": oee}

    def reset(self) -> None:
        """Forget all samples and losses, for a new plant run."""
        self._series.clear()
        self.losses.reset()

    def _latest_time(self) -> float | None:
        latest = [s.latest.t for s in self._series.values() if s.latest is not None]
        return max(latest) if latest else None


def _machine_row(machine: Mapping, result: OeeResult | None) -> dict:
    """One entry of `machines` in `GET /api/oee`. `oee` and `availability` are net, `availability_iso` is ISO 22400."""
    r = result if result is not None else compute_oee(Window(t0=0.0, t1=0.0))
    return {
        "id": machine["id"],
        "name": machine.get("name", machine["id"]),
        "oee": _rounded(r.oee_net),
        "oee_iso": _rounded(r.oee_iso),
        "availability": _rounded(r.availability_net),
        "availability_iso": _rounded(r.availability),
        "performance": _rounded(r.performance),
        "performance_uncapped": _rounded(r.performance_uncapped),
        "quality": _rounded(r.quality),
        "minutes": {
            "run": round(r.run, _ROUND_MINUTES),
            "setup": round(r.setup, _ROUND_MINUTES),
            "fault": round(r.fault, _ROUND_MINUTES),
            "idle": round(r.idle, _ROUND_MINUTES),
            "no_demand": round(r.no_demand, _ROUND_MINUTES),
            "starved": round(r.starved, _ROUND_MINUTES),
            "planned": round(r.planned, _ROUND_MINUTES),
            "planned_net": round(r.planned_net, _ROUND_MINUTES),
            "ideal": round(r.ideal, _ROUND_MINUTES),
        },
        "counts": {"total": r.total, "good": r.good, "scrap": r.scrap, "unit": machine.get("unit", "")},
        "fault_count": r.fault_count,
        "mtbf_min": _rounded(r.mtbf_min, 1),
        "mttr_min": _rounded(r.mttr_min, 1),
    }


__all__ = [
    "CounterSample", "LossAccumulator", "NO_DEMAND", "OeeResult", "OeeTracker", "PlantOee", "ROLLING_WINDOW_MIN",
    "STARVED", "SampleSeries", "WINDOWS", "WINDOW_8H", "WINDOW_START", "Window", "aggregate_plant", "compute_oee",
    "difference", "idle_reason",
]
