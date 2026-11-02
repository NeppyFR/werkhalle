"""OEE: every expected value below is worked out by hand from the formulas in PLAN section 9."""
from __future__ import annotations

import pytest

from werkhalle.mes.oee import (
    NO_DEMAND,
    STARVED,
    WINDOW_8H,
    WINDOW_START,
    CounterSample,
    LossAccumulator,
    OeeTracker,
    SampleSeries,
    Window,
    aggregate_plant,
    compute_oee,
    difference,
    idle_reason,
)


def window(**kw) -> Window:
    return Window(t0=0.0, t1=1000.0, **kw)


class TestFormulas:
    def test_fixed_counter_set_iso_vs_net(self):
        # planned = 700 + 100 + 60 + 300 - 40 = 1120; planned_net = 1120 - 120 = 1000
        # A = 700/1120 = 0.625; A_net = 700/1000 = 0.7; P = 640/700; Q = 316/320
        r = compute_oee(window(run=700, setup=100, fault=60, idle=300, no_demand=40, starved=120, ideal=640,
                               total=320, good=316, scrap=4))
        assert r.planned == 1120
        assert r.planned_net == 1000
        assert r.availability == pytest.approx(0.625)
        assert r.availability_net == pytest.approx(0.7)
        assert r.performance == pytest.approx(640 / 700)
        assert r.quality == pytest.approx(316 / 320)
        assert r.oee_iso == pytest.approx(0.625 * 640 / 700 * 316 / 320)
        assert r.oee_net == pytest.approx(0.7 * 640 / 700 * 316 / 320)
        # 0.7 * (640/700) = 0.64, times 0.9875 is exact enough to check by hand.
        assert r.oee_net == pytest.approx(0.632)
        assert r.oee_net > r.oee_iso

    def test_starvation_is_removed_only_from_net(self):
        base = window(run=100, idle=100, ideal=100, total=10, good=10)
        starved = window(run=100, idle=100, starved=60, ideal=100, total=10, good=10)
        assert compute_oee(base).oee_iso == pytest.approx(0.5)
        assert compute_oee(starved).oee_iso == pytest.approx(0.5)
        assert compute_oee(base).oee_net == pytest.approx(0.5)
        assert compute_oee(starved).oee_net == pytest.approx(100 / 140)

    def test_no_demand_is_removed_from_both(self):
        r = compute_oee(window(run=100, idle=100, no_demand=60, ideal=100, total=10, good=10))
        assert r.planned == 140
        assert r.oee_iso == pytest.approx(100 / 140)
        assert r.oee_net == pytest.approx(100 / 140)

    def test_furnace_two_of_ten_baskets_gives_performance_0_2(self):
        # OF2 holds 10 baskets, soak 300 min: ideal = 300 x 2/10 = 60 while the furnace runs 300.
        r = compute_oee(window(run=300, ideal=300 * 2 / 10, total=2, good=2))
        assert r.performance == pytest.approx(0.2)
        assert r.availability == pytest.approx(1.0)
        assert r.oee_iso == pytest.approx(0.2)

    def test_ideal_above_run_is_capped_and_uncapped_value_is_exposed(self):
        r = compute_oee(window(run=100, ideal=125, total=10, good=10))
        assert r.performance == 1.0
        assert r.performance_uncapped == pytest.approx(1.25)
        assert r.oee_iso == pytest.approx(1.0)

    def test_mtbf_and_mttr(self):
        r = compute_oee(window(run=700, fault=120, fault_count=2, idle=10))
        assert r.mtbf_min == pytest.approx(350)
        assert r.mttr_min == pytest.approx(60)
        clean = compute_oee(window(run=700))
        assert clean.mtbf_min is None
        assert clean.mttr_min is None


class TestEdgeCases:
    def test_no_run_time_availability_zero_rest_undefined(self):
        r = compute_oee(window(idle=100))
        assert r.availability == 0.0
        assert r.performance is None
        assert r.performance_uncapped is None
        assert r.quality is None
        assert r.oee_iso is None
        assert r.oee_net is None

    def test_run_without_output_has_undefined_quality_not_zero(self):
        r = compute_oee(window(run=50, ideal=50))
        assert r.availability == 1.0
        assert r.performance == 1.0
        assert r.quality is None
        assert r.oee_iso is None

    def test_zero_planned_time_is_undefined_not_zero(self):
        r = compute_oee(window())
        assert r.planned == 0
        assert r.availability is None
        assert r.availability_net is None
        assert r.oee_iso is None
        assert r.oee_net is None

    def test_all_idle_time_is_no_demand_gives_zero_planned(self):
        r = compute_oee(window(idle=100, no_demand=100))
        assert r.planned == 0
        assert r.availability is None

    def test_all_idle_time_is_starved_gives_zero_net_planned_only(self):
        r = compute_oee(window(idle=100, starved=100))
        assert r.planned == 100
        assert r.planned_net == 0
        assert r.availability == 0.0
        assert r.availability_net is None

    def test_zero_output_with_scrap_only_is_quality_zero(self):
        r = compute_oee(window(run=10, ideal=10, total=5, good=0, scrap=5))
        assert r.quality == 0.0
        assert r.oee_iso == 0.0

    def test_no_demand_larger_than_idle_at_factor_480(self):
        # One wall-clock tick is 8 sim minutes at factor 480, so the MES can book more no-demand than the plant's exact idle.
        r = compute_oee(window(run=100, idle=3, no_demand=8, ideal=100, total=10, good=10))
        assert r.no_demand == 3
        assert r.planned == 100
        assert r.availability == 1.0
        assert r.availability_net == 1.0

    @pytest.mark.parametrize("no_demand", [0.0, 1.0, 3.0, 3.1, 8.0, 800.0])
    @pytest.mark.parametrize("starved", [0.0, 1.0, 3.0, 3.1, 8.0, 800.0])
    def test_availability_never_above_one(self, no_demand, starved):
        r = compute_oee(window(run=0.1, setup=0.2, idle=3.0, no_demand=no_demand, starved=starved, ideal=0.1))
        assert 0.0 <= r.availability <= 1.0
        assert 0.0 <= r.availability_net <= 1.0
        assert r.no_demand + r.starved <= r.idle + 1e-12
        assert r.planned_net <= r.planned

    def test_no_demand_wins_over_starved_when_both_exceed_idle(self):
        r = compute_oee(window(run=10, idle=5, no_demand=4, starved=4))
        assert r.no_demand == 4
        assert r.starved == 1
        assert r.planned == 11
        assert r.planned_net == 10


class TestWindows:
    def test_difference_subtracts_each_counter(self):
        a = CounterSample(t=10, run=1, setup=2, fault=3, idle=4, ideal=5, no_demand=1, starved=1, total=6, good=5, scrap=1,
                          fault_count=1)
        b = CounterSample(t=70, run=11, setup=3, fault=4, idle=6, ideal=9, no_demand=2, starved=1, total=16, good=14, scrap=2,
                          fault_count=3)
        w = difference(a, b)
        assert (w.t0, w.t1) == (10, 70)
        assert (w.run, w.setup, w.fault, w.idle, w.ideal) == (10, 1, 1, 2, 4)
        assert (w.no_demand, w.starved) == (1, 0)
        assert (w.total, w.good, w.scrap, w.fault_count) == (10, 9, 1, 2)

    def test_counter_going_backwards_is_not_negative(self):
        w = difference(CounterSample(t=0, run=100, total=10), CounterSample(t=1, run=5, total=2))
        assert w.run == 0
        assert w.total == 0

    def test_window_straddling_a_fault(self):
        # Fault from t=100 to t=160, machine running otherwise. The window [130, 190] holds the last 30 fault minutes and 30 run minutes.
        tracker = OeeTracker()
        tracker.add_sample("PR1", CounterSample(t=0))
        tracker.add_sample("PR1", CounterSample(t=100, run=100, ideal=90, total=100, good=100))
        tracker.add_sample("PR1", CounterSample(t=130, run=100, fault=30, ideal=90, total=100, good=100, fault_count=1))
        tracker.add_sample("PR1", CounterSample(t=160, run=100, fault=60, ideal=90, total=100, good=100, fault_count=1))
        tracker.add_sample("PR1", CounterSample(t=190, run=130, fault=60, ideal=117, total=130, good=130, fault_count=1))

        w = difference(tracker.series("PR1").at_or_before(130), tracker.series("PR1").at_or_before(190))
        r = compute_oee(w)
        assert r.fault == 30
        assert r.run == 30
        assert r.fault_count == 0  # the fault began before this window
        assert r.mttr_min is None  # so no repair time can be attributed
        assert r.availability == pytest.approx(0.5)
        assert r.performance == pytest.approx(0.9)
        assert r.oee_iso == pytest.approx(0.45)

    def test_fault_entirely_inside_window(self):
        r = compute_oee(difference(CounterSample(t=0), CounterSample(t=100, run=70, fault=30, ideal=70, total=7, good=7,
                                                                     fault_count=1)))
        assert r.availability == pytest.approx(0.7)
        assert r.fault_count == 1
        assert r.mttr_min == pytest.approx(30)
        assert r.mtbf_min == pytest.approx(70)


class TestLossAccumulator:
    def test_interval_goes_to_the_state_seen_at_its_start(self):
        losses = LossAccumulator()
        losses.update("PR1", 0.0, "IDLE", NO_DEMAND)
        losses.update("PR1", 8.0, "IDLE", STARVED)
        losses.update("PR1", 16.0, "RUN", None)
        losses.update("PR1", 24.0, "IDLE", NO_DEMAND)
        losses.update("PR1", 32.0, "IDLE", NO_DEMAND)
        assert losses.no_demand("PR1") == 16.0  # [0, 8] and [24, 32]
        assert losses.starved("PR1") == 8.0  # [8, 16]

    def test_only_idle_counts_even_with_a_reason(self):
        losses = LossAccumulator()
        losses.update("VP", 0.0, "FAULT", NO_DEMAND)
        losses.update("VP", 8.0, "RUN", STARVED)
        losses.update("VP", 16.0, "RUN", None)
        assert losses.no_demand("VP") == 0.0
        assert losses.starved("VP") == 0.0

    def test_time_standing_still_or_going_back_adds_nothing(self):
        losses = LossAccumulator()
        losses.update("VP", 10.0, "IDLE", NO_DEMAND)
        losses.update("VP", 10.0, "IDLE", NO_DEMAND)
        losses.update("VP", 5.0, "IDLE", NO_DEMAND)
        assert losses.no_demand("VP") == 0.0

    def test_waiting_on_labels_map_to_loss_classes(self):
        assert idle_reason("Kein Auftrag") == NO_DEMAND
        assert idle_reason("Material") == STARVED
        assert idle_reason("Vorgänger") == STARVED
        assert idle_reason("Störung") is None
        assert idle_reason(None) is None

    def test_tracker_merges_accumulated_losses_into_samples(self):
        tracker = OeeTracker()
        counters = {"run": 0.0, "idle": 0.0}
        tracker.update("PR1", 0.0, counters, status="IDLE", waiting_on="Kein Auftrag")
        tracker.update("PR1", 8.0, {"run": 0.0, "idle": 8.0}, status="IDLE", waiting_on="Material")
        tracker.update("PR1", 16.0, {"run": 8.0, "idle": 8.0, "ideal": 8.0, "total": 2, "good": 2}, status="RUN")
        r = tracker.result("PR1", WINDOW_START)
        assert r.no_demand == 8.0
        assert r.starved == 0.0  # [8, 16] is starved by the accumulator, but the plant says idle stopped at 8
        assert r.availability == 1.0


def steady_counters(t: float) -> CounterSample:
    """A machine running 60 % of the time, idle 30 %, in fault 10 %, with counters linear in t."""
    return CounterSample(t=t, run=0.6 * t, idle=0.3 * t, fault=0.1 * t, ideal=0.54 * t, total=int(t), good=int(0.98 * t),
                         scrap=int(t) - int(0.98 * t), fault_count=int(t // 500))


class TestDownsampling:
    def test_since_start_stays_exact_under_heavy_pruning(self):
        series = SampleSeries(fine_step_min=1.0, coarse_step_min=30.0, max_coarse=10)
        for minute in range(0, 20001):
            series.add(steady_counters(float(minute)))
        assert series.first.t == 0.0
        assert series.latest.t == 20000.0
        assert len(series) < 560 + 10 + 16 + 2
        exact = compute_oee(difference(steady_counters(0.0), steady_counters(20000.0)))
        pruned = compute_oee(difference(series.first, series.latest))
        assert pruned == exact

    def test_rolling_window_matches_unpruned_reference(self):
        pruned = OeeTracker(max_coarse=5)
        reference = []
        # A step in behaviour inside the rolling window, so a wrong baseline would show.
        for minute in range(0, 6001):
            t = float(minute)
            sample = steady_counters(t)
            if minute > 5800:
                sample = CounterSample(t=t, run=sample.run, idle=sample.idle + (minute - 5800), fault=sample.fault,
                                       ideal=sample.ideal, total=sample.total, good=sample.good, scrap=sample.scrap)
            pruned.add_sample("PR1", sample)
            reference.append(sample)
        assert len(pruned.series("PR1")) < len(reference) / 5
        expected = compute_oee(difference(reference[6000 - 480], reference[6000]))
        assert pruned.result("PR1", WINDOW_8H) == expected

    def test_faster_than_fine_step_keeps_newest_and_grows_slowly(self):
        series = SampleSeries(fine_step_min=1.0)
        for i in range(0, 1000):
            series.add(CounterSample(t=i * 0.1, run=i * 0.1))
        assert series.latest.t == pytest.approx(99.9)
        assert series.latest.run == pytest.approx(99.9)
        assert len(series) <= 102

    def test_factor_zero_does_not_grow_the_series(self):
        series = SampleSeries()
        series.add(CounterSample(t=0.0))
        series.add(CounterSample(t=5.0, run=5.0))
        for _ in range(100):
            series.add(CounterSample(t=5.0, run=5.0))
        assert len(series) == 2

    def test_older_samples_thin_out_but_8h_window_is_never_thinned(self):
        series = SampleSeries(fine_step_min=1.0, coarse_step_min=30.0, max_coarse=400)
        for minute in range(0, 4000):
            series.add(CounterSample(t=float(minute), run=float(minute)))
        # Force a compaction, so the test does not depend on when the limit happened to be hit.
        series._compact()
        latest = series.latest.t
        fine = [s.t for s in series._samples if s.t >= latest - 480]
        assert fine == [float(m) for m in range(int(latest) - 480, int(latest) + 1)]
        old = [s.t for s in series._samples if s.t < latest - 540]
        gaps = [b - a for a, b in zip(old, old[1:])]
        assert gaps and min(gaps) >= 30.0

    def test_sample_going_back_in_time_is_ignored(self):
        series = SampleSeries()
        series.add(CounterSample(t=10.0, run=10.0))
        series.add(CounterSample(t=5.0, run=5.0))
        assert series.latest.t == 10.0


class TestPlantAggregate:
    def test_minutes_are_summed_and_quality_weighted_by_ideal(self):
        a = compute_oee(window(run=100, idle=100, ideal=80, total=100, good=90))  # q = 0.9
        b = compute_oee(window(run=300, idle=100, ideal=240, total=10, good=10))  # q = 1.0
        plant = aggregate_plant([a, b])
        assert plant.availability == pytest.approx(400 / 600)
        assert plant.performance == pytest.approx(320 / 400)
        assert plant.quality == pytest.approx((0.9 * 80 + 1.0 * 240) / 320)
        assert plant.oee_iso == pytest.approx(400 / 600 * 320 / 400 * plant.quality)

    def test_machine_without_output_does_not_drag_quality_to_zero(self):
        idle_machine = compute_oee(window(idle=100))
        working = compute_oee(window(run=100, ideal=100, total=10, good=9))
        plant = aggregate_plant([idle_machine, working])
        assert plant.quality == pytest.approx(0.9)
        assert plant.availability == pytest.approx(0.5)

    def test_empty_plant_is_undefined(self):
        plant = aggregate_plant([])
        assert plant.availability is None
        assert plant.performance is None
        assert plant.quality is None
        assert plant.oee_iso is None
        assert plant.oee_net is None


class TestResponseShape:
    def build(self) -> OeeTracker:
        tracker = OeeTracker()
        tracker.add_sample("PR2", CounterSample(t=0.0))
        tracker.add_sample("PR2", CounterSample(t=600.0, run=400, setup=50, fault=30, idle=120, ideal=360, no_demand=20, starved=40,
                                                total=100, good=98, scrap=2, fault_count=1))
        tracker.add_sample("PR2", CounterSample(t=1200.0, run=800, setup=100, fault=60, idle=240, ideal=720, no_demand=40, starved=80,
                                                total=200, good=196, scrap=4, fault_count=2))
        return tracker

    def test_api_json_since_start(self):
        data = self.build().response(WINDOW_START, [{"id": "PR2", "name": "Presse 2", "unit": "Stangen"}])
        assert data["window"] == "start"
        assert data["t"] == 1200.0
        row = data["machines"][0]
        assert row["id"] == "PR2"
        assert row["name"] == "Presse 2"
        # planned = 800+100+60+240-40 = 1160; planned_net = 1160-80 = 1080
        assert row["minutes"] == {"run": 800, "setup": 100, "fault": 60, "idle": 240, "no_demand": 40, "starved": 80,
                                  "planned": 1160, "planned_net": 1080, "ideal": 720}
        assert row["availability"] == pytest.approx(800 / 1080, abs=1e-4)
        assert row["availability_iso"] == pytest.approx(800 / 1160, abs=1e-4)
        assert row["performance"] == pytest.approx(0.9)
        assert row["quality"] == pytest.approx(0.98)
        assert row["oee"] == pytest.approx(800 / 1080 * 0.9 * 0.98, abs=1e-4)
        assert row["oee_iso"] == pytest.approx(800 / 1160 * 0.9 * 0.98, abs=1e-4)
        assert row["counts"] == {"total": 200, "good": 196, "scrap": 4, "unit": "Stangen"}
        assert row["fault_count"] == 2
        assert row["mtbf_min"] == 400
        assert row["mttr_min"] == 30
        assert data["plant"]["oee"] == row["oee"]
        assert data["plant"]["oee_iso"] == row["oee_iso"]

    def test_api_json_rolling_window_uses_the_later_half(self):
        tracker = self.build()
        result = tracker.result("PR2", WINDOW_8H)
        # Baseline is the newest sample not after 1200 - 480 = 720, which is t=600.
        assert result.run == 400
        assert result.fault_count == 1

    def test_trend_points_and_values(self):
        data = self.build().response(WINDOW_START, [{"id": "PR2", "name": "Presse 2", "unit": "Stangen"}])
        trend = data["trend"]
        assert trend["t"] == [60.0 * i for i in range(1, 21)]
        assert len(trend["oee"]["PR2"]) == 20
        # At t=600 the window [0, 600] holds half the final counters, with the same ratios.
        assert trend["oee"]["PR2"][9] == pytest.approx(400 / 540 * 0.9 * 0.98, abs=1e-4)

    def test_trend_of_a_long_run_stays_within_the_point_limit(self):
        tracker = OeeTracker()
        for minute in range(0, 30001, 5):
            tracker.add_sample("PR1", steady_counters(float(minute)))
        trend = tracker.trend(WINDOW_8H)
        assert 2 <= len(trend["t"]) <= 121
        assert trend["t"][-1] == 30000.0
        assert all(v is None or 0.0 <= v <= 1.0 for v in trend["oee"]["PR1"])

    def test_machine_without_samples_appears_with_nulls(self):
        data = OeeTracker().response(WINDOW_8H, [{"id": "VP", "name": "Verpackung", "unit": "Pakete"}])
        row = data["machines"][0]
        assert row["oee"] is None
        assert row["availability"] is None
        assert row["minutes"]["run"] == 0
        assert row["counts"]["unit"] == "Pakete"
        assert data["plant"]["oee"] is None
        assert data["trend"] == {"t": [], "oee": {"VP": []}}

    def test_state_summary_shape(self):
        summary = self.build().state_summary("PR2", WINDOW_START)
        assert set(summary) == {"oee", "oee_iso", "availability", "performance", "quality"}
        assert summary["availability"] == pytest.approx(800 / 1080, abs=1e-4)
        assert OeeTracker().state_summary("PR2") is None

    def test_unknown_window_is_refused(self):
        with pytest.raises(ValueError):
            self.build().result("PR2", "morgen")

    def test_reset_forgets_everything(self):
        tracker = self.build()
        tracker.losses.update("PR2", 0.0, "IDLE", NO_DEMAND)
        tracker.losses.update("PR2", 10.0, "IDLE", NO_DEMAND)
        tracker.reset()
        assert tracker.machines() == []
        assert tracker.losses.no_demand("PR2") == 0.0


def test_plant_performance_is_capped_at_one():
    plant = aggregate_plant([compute_oee(window(run=100, ideal=150, total=10, good=10))])
    assert plant.performance == 1.0
