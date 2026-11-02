"""SimEngine: material flow, state table, temperature, alarms, OEE counters, trace events. See test_simcore_fork.py for forks."""
from __future__ import annotations

import json
import math
import time

import pytest
from test_simcore_support import (
    RUN_ID,
    FifoDriver,
    age_cmd,
    assert_conserved,
    cast_cmd,
    make_lots,
    pack_cmd,
    press_cmd,
    saw_cmd,
)

from werkhalle.domain import catalog as cat
from werkhalle.domain.orders import Lot
from werkhalle.simcore import SimEngine

EPS = 1e-9


def det(**kwargs) -> SimEngine:
    return SimEngine(stochastic=False, run_id=RUN_ID, **kwargs)


def stoch(seed: int = 1, failures: bool = False, **kwargs) -> SimEngine:
    return SimEngine(seed=seed, stochastic=True, random_failures=failures, run_id=RUN_ID, **kwargs)


def run_past(engine: SimEngine, dt: float = 0.0) -> None:
    """Advance to the next event, plus dt."""
    next_time = engine.next_event_time()
    engine.run_until((next_time if next_time is not None else engine.t) + dt)


def events_of(engine: SimEngine, type_: str | None = None) -> list[dict]:
    events = engine.events_after(0, 10**9)["events"]
    return [e for e in events if type_ is None or e["type"] == type_]


def press_lot_to_age(engine: SimEngine, lot: str = "L-1", product: str = "FR-60", n: int = 8, machine: str = "PR1") -> None:
    assert engine.start_operation(press_cmd(lot, product, n, machine))["ok"]
    while lot not in [row["lot_id"] for row in engine.material_view()["buffers"]["AGE_IN"]]:
        run_past(engine)


def age_lot_to_saw(engine: SimEngine, lot: str = "L-1", recipe: str = "T66", machine: str = "OF1") -> None:
    assert engine.start_operation(age_cmd([lot], recipe, machine))["ok"]
    while lot not in [row["lot_id"] for row in engine.material_view()["buffers"]["SAW_IN"]]:
        run_past(engine)


# ---------------------------------------------------------------------- initial stock, billets


def test_initial_stock_is_real_melt_events():
    engine = det()
    events = events_of(engine)
    casts = [e for e in events if e["type"] == "melt_cast"]
    arrivals = [e for e in events if e["type"] == "melt_arrived"]
    assert sum(cat.INITIAL_BILLET_STOCK.values()) == sum(len(e["billets"]) for e in casts)
    assert {e["melt_id"] for e in casts} == {e["melt_id"] for e in arrivals} == {"S-00001", "S-00002", "S-00003"}
    for cast in casts:
        assert cast["t"] == 0.0
        assert cast["t_start"] < 0.0
        assert cast["plan_id"] == cat.INITIAL_MELT_PLAN_ID
        assert cast["temp_c"] >= 650.0
        assert all(b.startswith(f"B-{cast['melt_id'][2:]}-") for b in cast["billets"])
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    store = engine.material_view()["buffers"]["BILLET_STORE"]
    assert store == cat.INITIAL_BILLET_STOCK


def test_without_initial_stock_store_is_empty_and_ids_start_at_one():
    engine = det(initial_stock=False)
    assert events_of(engine) == []
    assert set(engine.material_view()["buffers"]["BILLET_STORE"].values()) == {0}
    assert engine.start_operation(cast_cmd("M-6060-00001", "6060"))["ok"]
    engine.run_until(200.0)
    assert events_of(engine, "melt_cast")[0]["melt_id"] == "S-00001"
    assert events_of(engine, "melt_cast")[0]["billets"][6] == "B-00001-07"


def test_billets_reserved_atomically_at_press_start():
    engine = det()
    before = engine.material_view()["buffers"]["BILLET_STORE"]["6060"]
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    material = engine.material_view()
    assert material["buffers"]["BILLET_STORE"]["6060"] == before - 8
    assert material["in_machine"]["PR1"]["billets_left"] == 8
    snapshot = engine.export_snapshot()
    assert len(snapshot["machines"]["PR1"]["op"]["billets"]) == 8
    assert engine.planning_state().billet_store["6060"] == before - 8


def test_no_material_refusal_moves_nothing():
    engine = det()
    store = engine.material_view()["buffers"]["BILLET_STORE"]["6060"]
    result = engine.start_operation(press_cmd("L-1", "FR-60", store + 1, "PR1"))
    assert result == {"ok": False, "reason": "NO_MATERIAL"}
    assert engine.material_view()["buffers"]["BILLET_STORE"]["6060"] == store
    assert engine.machine_view("PR1")["Status"] == "IDLE"
    assert "L-1:press" not in engine.planning_state().started_op_ids


def test_billets_are_consumed_fifo_by_melt():
    engine = det()
    assert engine.start_operation(cast_cmd("M-6060-00001", "6060"))["ok"]
    engine.run_until(150.0)
    assert engine.material_view()["buffers"]["BILLET_STORE"]["6060"] == 48
    assert engine.start_operation(press_cmd("L-1", "FR-60", 20, "PR1"))["ok"]
    assert engine.start_operation(press_cmd("L-2", "FR-60", 8, "PR2"))["ok"]
    engine.run_until(800.0)
    first = [e for e in events_of(engine, "billet_extruded") if e["lot_id"] == "L-1"]
    second = [e for e in events_of(engine, "billet_extruded") if e["lot_id"] == "L-2"]
    assert [e["billet_id"] for e in first] == [f"B-00001-{i:02d}" for i in range(1, 21)]
    assert [e["billet_id"] for e in second] == [f"B-00001-{i:02d}" for i in range(21, 25)] + [
        f"B-00004-{i:02d}" for i in range(1, 5)
    ]
    assert [e["melt_id"] for e in second] == ["S-00001"] * 4 + ["S-00004"] * 4


@pytest.mark.parametrize("product_id", cat.PRODUCTS)
@pytest.mark.parametrize("n", [1, 3, 4, 5, 8])
def test_baskets_take_whole_strands_only(product_id, n):
    product = cat.PRODUCTS[product_id]
    engine = stoch(seed=n)
    assert engine.start_operation(press_cmd("L-1", product_id, n, min(product.press_min_per_billet)))["ok"]
    engine.run_until(2000.0)
    extruded = {e["strand_id"]: e for e in events_of(engine, "billet_extruded")}
    baskets = [b for e in events_of(engine, "baskets_filled") for b in e["baskets"]]
    assert len(baskets) == Lot("L-1", "A-1", product_id, n).n_baskets
    seen = []
    for basket in baskets:
        assert len(basket["strands"]) <= product.strands_per_basket
        assert basket["bars"] == sum(s["bars"] for s in basket["strands"])
        for strand in basket["strands"]:
            assert strand["bars"] == extruded[strand["id"]]["bars_good"]
            seen.append(strand["id"])
    assert sorted(seen) == sorted(extruded)


# ---------------------------------------------------------------------- refusals


def _press_ok(**override) -> dict:
    return {**press_cmd("L-9", "FR-60", 2, "PR1"), **override}


@pytest.mark.parametrize(
    "cmd, reason",
    [
        ("not a dict", "BAD_COMMAND"),
        (_press_ok(run_id="other"), "STALE_RUN"),
        ({k: v for k, v in _press_ok().items() if k != "run_id"}, "STALE_RUN"),
        (_press_ok(op_ids=[]), "BAD_COMMAND"),
        (_press_ok(op_ids="L-9:press"), "BAD_COMMAND"),
        (_press_ok(op_ids=["a", "a"]), "BAD_COMMAND"),
        (_press_ok(op_ids=["a", "b"]), "BAD_COMMAND"),
        (_press_ok(kind="polish"), "BAD_COMMAND"),
        (_press_ok(machine="PR9"), "BAD_COMMAND"),
        (_press_ok(machine=None), "BAD_COMMAND"),
        (_press_ok(product_id="NOPE"), "BAD_COMMAND"),
        (_press_ok(n_billets=0), "BAD_COMMAND"),
        (_press_ok(n_billets=2.5), "BAD_COMMAND"),
        (_press_ok(n_billets=True), "BAD_COMMAND"),
        ({k: v for k, v in _press_ok().items() if k != "order_id"}, "BAD_COMMAND"),
        ({**cast_cmd("M-1", "9999")}, "BAD_COMMAND"),
        ({**cast_cmd("", "6060")}, "BAD_COMMAND"),
        ({**age_cmd(["L-1"], "T66", "OF1"), "lot_ids": ["L-1", "L-2"]}, "BAD_COMMAND"),
        ({**age_cmd(["L-1"], "T99", "OF1")}, "BAD_COMMAND"),
        ({**age_cmd(["L-1"], "T66", "OF1"), "batch_id": 7}, "BAD_COMMAND"),
        ({**saw_cmd("L-1"), "lot_id": ""}, "BAD_COMMAND"),
        (_press_ok(machine="SAE"), "NOT_ELIGIBLE"),
        (_press_ok(machine="GUSS"), "NOT_ELIGIBLE"),
        (press_cmd("L-9", "ST-200", 2, "PR1"), "NOT_ELIGIBLE"),
        (age_cmd(["L-1"], "T66", "OF1"), "NO_MATERIAL"),
        (saw_cmd("L-1"), "NO_MATERIAL"),
        (pack_cmd("L-1"), "NO_MATERIAL"),
    ],
)
def test_start_operation_refusals(cmd, reason):
    engine = det()
    assert engine.start_operation(cmd) == {"ok": False, "reason": reason}
    assert engine.planning_state().started_op_ids == frozenset()
    assert engine.material_view()["buffers"]["BILLET_STORE"] == cat.INITIAL_BILLET_STOCK


def test_repeat_is_refused_before_any_material_moves():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 2, "PR1"))["ok"]
    store = engine.material_view()["buffers"]["BILLET_STORE"]["6060"]
    # A repeat stays ALREADY_STARTED on the busy machine, on another machine, and after the op finished.
    assert engine.start_operation(press_cmd("L-1", "FR-60", 2, "PR1"))["reason"] == "ALREADY_STARTED"
    assert engine.start_operation(press_cmd("L-1", "FR-60", 2, "PR2"))["reason"] == "ALREADY_STARTED"
    engine.run_until(100.0)
    assert engine.start_operation(press_cmd("L-1", "FR-60", 2, "PR1"))["reason"] == "ALREADY_STARTED"
    assert engine.material_view()["buffers"]["BILLET_STORE"]["6060"] == store
    assert len(events_of(engine, "op_started")) == 1


def test_furnace_load_refusals_are_checked_against_the_buffer():
    engine = det()
    press_lot_to_age(engine, "L-1", "FR-60", 8, "PR1")
    press_lot_to_age(engine, "L-2", "ST-200", 8, "PR2")
    assert engine.start_operation(age_cmd(["L-1", "L-2"], "T66", "OF1"))["reason"] == "NOT_ELIGIBLE"  # mixed recipe
    assert engine.start_operation(age_cmd(["L-2"], "T66", "OF1"))["reason"] == "NOT_ELIGIBLE"  # wrong recipe
    assert engine.start_operation(age_cmd(["L-1", "L-7"], "T66", "OF1"))["reason"] == "NO_MATERIAL"
    assert [row["lot_id"] for row in engine.material_view()["buffers"]["AGE_IN"]] == ["L-1", "L-2"]
    assert engine.start_operation(age_cmd(["L-1"], "T66", "OF1"))["ok"]


def test_furnace_capacity_is_enforced():
    engine = det(initial_stock=False)
    engine._store["6060"].extend({"id": f"B-X-{i}", "melt_id": "S-X"} for i in range(200))
    lots = [f"L-{i}" for i in range(4)]
    for lot in lots:
        press_lot_to_age(engine, lot, "FR-60", 8, "PR1")
    # Four lots of two baskets each: eight baskets fit OF2 (10) but not OF1 (6).
    assert engine.start_operation(age_cmd(lots, "T66", "OF1"))["reason"] == "NOT_ELIGIBLE"
    assert engine.start_operation(age_cmd(lots, "T66", "OF2"))["ok"]


# ---------------------------------------------------------------------- state table


def _engine_in(state: str) -> SimEngine:
    """state: IDLE, SETUP, RUN, FAULT_IDLE, FAULT_SETUP, FAULT_RUN on PR1."""
    engine = det()
    if state in ("SETUP", "FAULT_SETUP"):
        assert engine.start_operation(press_cmd("L-1", "KK-80", 4, "PR1"))["ok"]  # needs a die and alloy change
    elif state in ("RUN", "FAULT_RUN"):
        assert engine.start_operation(press_cmd("L-1", "FR-60", 4, "PR1"))["ok"]  # already mounted
    engine.run_until(engine.t + 2.0)
    if state.startswith("FAULT"):
        assert engine.inject_fault("PR1", "P-201", 50.0)["ok"]
    return engine


@pytest.mark.parametrize(
    "state, inject, status_after",
    [
        ("IDLE", "", "FAULT"),
        ("SETUP", "", "FAULT"),
        ("RUN", "", "FAULT"),
        ("FAULT_IDLE", "ALREADY_FAULTED", "FAULT"),
        ("FAULT_SETUP", "ALREADY_FAULTED", "FAULT"),
        ("FAULT_RUN", "ALREADY_FAULTED", "FAULT"),
    ],
)
def test_inject_table(state, inject, status_after):
    engine = _engine_in(state)
    result = engine.inject_fault("PR1", "P-202", 10.0)
    assert result == {"ok": not inject, "reason": inject}
    assert engine.machine_view("PR1")["Status"] == status_after
    assert engine.machine_view("PR1")["FaultCount"] == 1
    if inject:
        assert engine.machine_view("PR1")["AlarmCode"] == "P-201"
    else:
        assert engine.machine_view("PR1")["AlarmCode"] == "P-202"


@pytest.mark.parametrize(
    "state, reason, status_after",
    [
        ("IDLE", "NOT_FAULTED", "IDLE"),
        ("SETUP", "NOT_FAULTED", "SETUP"),
        ("RUN", "NOT_FAULTED", "RUN"),
        ("FAULT_IDLE", "", "IDLE"),
        ("FAULT_SETUP", "", "SETUP"),
        ("FAULT_RUN", "", "RUN"),
    ],
)
def test_repair_table(state, reason, status_after):
    engine = _engine_in(state)
    assert engine.repair("PR1") == {"ok": not reason, "reason": reason}
    view = engine.machine_view("PR1")
    assert view["Status"] == status_after
    assert view["EstimatedRepairMin"] == 0.0


@pytest.mark.parametrize(
    "state, reason",
    [
        ("IDLE", ""),
        ("SETUP", "MACHINE_BUSY"),
        ("RUN", "MACHINE_BUSY"),
        ("FAULT_IDLE", "MACHINE_FAULT"),
        ("FAULT_SETUP", "MACHINE_FAULT"),
        ("FAULT_RUN", "MACHINE_FAULT"),
    ],
)
def test_start_operation_table(state, reason):
    engine = _engine_in(state)
    result = engine.start_operation(press_cmd("L-5", "FR-60", 1, "PR1"))
    assert result == {"ok": not reason, "reason": reason}


@pytest.mark.parametrize("state", ["IDLE", "SETUP", "RUN"])
def test_automatic_repair_returns_to_the_previous_state(state):
    engine = _engine_in(state)
    assert engine.inject_fault("PR1", "P-201", 5.0)["ok"]
    engine.run_until(engine.t + 4.9)
    assert engine.machine_view("PR1")["Status"] == "FAULT"
    engine.run_until(engine.t + 0.2)
    assert engine.machine_view("PR1")["Status"] == state


def test_unknown_machine_and_code_are_bad_commands():
    engine = det()
    assert engine.inject_fault("XX")["reason"] == "BAD_COMMAND"
    assert engine.inject_fault("PR1", "G-101")["reason"] == "BAD_COMMAND"
    assert engine.inject_fault("PR1", "P-201", -1.0)["reason"] == "BAD_COMMAND"
    assert engine.repair("XX")["reason"] == "BAD_COMMAND"
    assert engine.ack_alarm("XX", 1)["reason"] == "BAD_COMMAND"


@pytest.mark.parametrize("state", ["SETUP", "RUN"])
def test_fault_pauses_work_and_resumes_it_exactly(state):
    reference = _engine_in(state)
    reference.run_until(1000.0)
    undisturbed = events_of(reference, "op_finished")[0]["t"]

    engine = _engine_in(state)
    assert engine.inject_fault("PR1", "P-201", 40.0)["ok"]
    engine.run_until(1000.0)
    finished = events_of(engine, "op_finished")[0]
    assert finished["t"] == pytest.approx(undisturbed + 40.0)
    assert finished["fault_min"] == pytest.approx(40.0)
    assert engine.machine_view("PR1")["FaultMinutes"] == pytest.approx(40.0)


def test_exact_repair_when_injected_lognormal_around_mean_otherwise():
    engine = stoch(seed=3)
    assert engine.inject_fault("SAE", "S-402", 77.0)["ok"]
    assert engine.machine_view("SAE")["EstimatedRepairMin"] == 77.0
    engine.run_until(76.9)
    assert engine.machine_view("SAE")["Status"] == "FAULT"
    engine.run_until(77.1)
    assert engine.machine_view("SAE")["Status"] == "IDLE"

    samples = []
    for seed in range(400):
        engine = stoch(seed=seed)
        assert engine.inject_fault("VP", "V-502")["ok"]
        assert engine.machine_view("VP")["EstimatedRepairMin"] == 12.0  # publishes the mean, not the sample
        samples.append(engine._machines["VP"].repair_end)
    mean = sum(samples) / len(samples)
    assert 10.5 < mean < 13.5
    assert min(samples) != max(samples)


def test_mtbf_failures_tick_on_operating_minutes_only():
    engine = stoch(seed=4, failures=True)
    # Never operating, so never faulted, however long it sits.
    engine.run_until(200_000.0)
    assert all(engine.machine_view(m)["FaultCount"] == 0 for m in cat.MACHINE_ORDER)
    assert engine.next_event_time() is None


def test_random_failures_toggle():
    engine = stoch(seed=2, failures=False)
    assert engine.start_operation(cast_cmd("M-6060-00001", "6060"))["ok"]
    assert engine.next_event_time() is not None
    pending = len(engine._heap)
    engine.set_random_failures(True)
    assert engine.random_failures is True
    assert len(engine._heap) == pending + 2  # failure and warning clock for the running machine
    engine.set_random_failures(False)
    assert engine.random_failures is False
    engine.run_until(1000.0)
    assert engine.machine_view("GUSS")["FaultCount"] == 0

    deterministic = SimEngine(stochastic=False, random_failures=True)
    assert deterministic.random_failures is False
    deterministic.set_random_failures(True)
    assert deterministic.random_failures is False


def test_random_faults_do_happen_with_mean_near_mtbf():
    driver_engine = stoch(seed=11, failures=True)
    lots = make_lots(70)
    FifoDriver(driver_engine, lots).run(horizon=200_000.0)
    views = [driver_engine.machine_view(m) for m in cat.MACHINE_ORDER]
    faults = sum(v["FaultCount"] for v in views)
    operating = sum(v["RunMinutes"] + v["SetupMinutes"] for v in views)
    assert faults > 0
    expected = sum(
        (v["RunMinutes"] + v["SetupMinutes"]) / cat.MACHINES[m].mtbf_operating_min
        for m, v in zip(cat.MACHINE_ORDER, views)
    )
    assert expected * 0.2 < faults < expected * 3.0, (faults, expected, operating)
    assert_conserved(events_of(driver_engine), lots, driver_engine)


# ---------------------------------------------------------------------- temperature


def test_first_order_lag_toward_state_setpoint():
    engine = det()
    spec = cat.MACHINES["PR1"]
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    engine.run_until(9.0)
    view = engine.machine_view("PR1")
    assert view["TemperatureSetpoint"] == spec.temp_run_c
    expected = spec.temp_run_c + (spec.temp_idle_c - spec.temp_run_c) * math.exp(-9.0 / spec.temp_tau_min)
    assert view["Temperature"] == pytest.approx(expected)


def test_setup_setpoint_and_idle_return():
    engine = det()
    spec = cat.MACHINES["PR1"]
    assert engine.start_operation(press_cmd("L-1", "KK-80", 1, "PR1"))["ok"]
    assert engine.machine_view("PR1")["Status"] == "SETUP"
    assert engine.machine_view("PR1")["TemperatureSetpoint"] == spec.temp_setup_c
    engine.run_until(500.0)
    view = engine.machine_view("PR1")
    assert view["Status"] == "IDLE"
    assert view["TemperatureSetpoint"] == spec.temp_idle_c
    assert view["Temperature"] == pytest.approx(spec.temp_idle_c, abs=0.01)


def test_fault_setpoint_is_kept_hot():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR2"))["ok"]
    engine.run_until(30.0)
    assert engine.inject_fault("PR2", "P-204", 500.0)["ok"]
    engine.run_until(400.0)
    view = engine.machine_view("PR2")
    assert view["TemperatureSetpoint"] == cat.MACHINES["PR2"].temp_fault_c
    assert view["Temperature"] == pytest.approx(cat.MACHINES["PR2"].temp_fault_c, abs=0.5)


def test_furnace_setpoint_is_the_recipe_in_setup_and_run():
    engine = det()
    press_lot_to_age(engine, "L-1", "KK-80", 8, "PR1")  # T5, OF1 starts on T66, so it needs a setup
    assert engine.start_operation(age_cmd(["L-1"], "T5", "OF1"))["ok"]
    assert engine.machine_view("OF1")["Status"] == "SETUP"
    assert engine.machine_view("OF1")["TemperatureSetpoint"] == 200.0
    run_past(engine)
    assert engine.machine_view("OF1")["Status"] == "RUN"
    assert engine.machine_view("OF1")["TemperatureSetpoint"] == 200.0


def test_healthy_load_is_within_two_kelvin_and_reports_time_in_band():
    engine = det()
    press_lot_to_age(engine, "L-1", "FR-60", 8, "PR1")
    age_lot_to_saw(engine, "L-1", "T66", "OF1")
    load = events_of(engine, "furnace_load_done")[0]
    assert load["fault_min"] == 0.0
    assert load["soak_min"] == 300.0
    assert abs(load["soak_temp_avg"] - 185.0) <= 2.0
    # Heated from idle (120 C) with no setup, so the soak starts cold: tau * ln(65 K / 5 K) until it is in band.
    expected_in_band = 300.0 - 12.0 * math.log(65.0 / 5.0)
    assert load["in_band_min"] == pytest.approx(expected_in_band, abs=0.01)
    assert load["baskets"] == [f"K-{i:05d}" for i in range(1, 3)]
    assert load["load_id"] == "O-00001"
    assert load["batch_id"] == "B-1"
    assert load["recipe"] == "T66"
    assert load["t_start"] == load["t"] - 300.0


def test_soak_average_counts_in_band_time_only_and_fault_minutes_are_reported():
    engine = det()
    press_lot_to_age(engine, "L-1", "FR-60", 8, "PR1")
    assert engine.start_operation(age_cmd(["L-1"], "T66", "OF1"))["ok"]
    engine.run_until(engine.t + 150.0)
    assert engine.inject_fault("OF1", "O-301", 90.0)["ok"]
    engine.run_until(engine.t + 1000.0)
    load = events_of(engine, "furnace_load_done")[0]
    assert load["fault_min"] == pytest.approx(90.0)
    assert load["soak_min"] == pytest.approx(300.0)  # the pause does not count, the soak still runs out in full
    assert load["in_band_min"] < 300.0 - 12.0 * math.log(65.0 / 5.0)  # had to reheat after the fault
    assert abs(load["soak_temp_avg"] - 185.0) <= 5.0  # the cold pause is not in the average


def test_excursion_during_soak_leaves_the_band():
    engine = det()
    press_lot_to_age(engine, "L-1", "FR-60", 8, "PR1")
    assert engine.start_operation(age_cmd(["L-1"], "T66", "OF1"))["ok"]
    engine.run_until(engine.t + 100.0)
    assert engine.raise_warning("OF1")["ok"]
    engine.run_until(engine.t + 1000.0)
    load = events_of(engine, "furnace_load_done")[0]
    clean = 300.0 - 12.0 * math.log(65.0 / 5.0)
    # +7 K puts it out of a +-5 K band for most of the 25 minutes, and the partial view reads are consistent.
    assert clean - 25.0 < load["in_band_min"] < clean - 15.0
    assert abs(load["soak_temp_avg"] - 185.0) <= 2.0


def test_in_band_time_does_not_depend_on_how_often_it_is_read():
    plain = det()
    polled = det()
    for engine in (plain, polled):
        press_lot_to_age(engine, "L-1", "FR-60", 8, "PR1")
        assert engine.start_operation(age_cmd(["L-1"], "T66", "OF1"))["ok"]
    start = plain.t
    while polled.t < start + 400.0:
        polled.run_until(polled.t + 0.7)
        polled.machine_view("OF1")
    plain.run_until(start + 400.0)
    a = events_of(plain, "furnace_load_done")[0]
    b = events_of(polled, "furnace_load_done")[0]
    assert a["in_band_min"] == pytest.approx(b["in_band_min"], abs=0.01)
    assert a["soak_temp_avg"] == pytest.approx(b["soak_temp_avg"], abs=0.01)


def test_temperature_floors_hold_across_a_stochastic_run_with_faults():
    engine = stoch(seed=21, failures=True)
    lots = make_lots(40)
    driver = FifoDriver(engine, lots)
    for i, lot in enumerate(lots):
        driver.run_until_time(40.0 * (i + 1))
        engine.inject_fault(cat.MACHINE_ORDER[i % len(cat.MACHINE_ORDER)], "", 30.0)
    driver.run(horizon=200_000.0)
    billets = events_of(engine, "billet_extruded")
    melts = events_of(engine, "melt_cast")
    assert len(billets) > 100
    assert min(e["temp_c"] for e in billets) >= 400.0
    assert min(e["temp_c"] for e in melts) >= 650.0
    for load in events_of(engine, "furnace_load_done"):
        if load["fault_min"] == 0.0:
            assert abs(load["soak_temp_avg"] - cat.RECIPES[load["recipe"]].temp_c) <= 2.0


# ---------------------------------------------------------------------- alarm slot


def test_warning_raises_clears_itself_and_really_moves_the_temperature():
    engine = det()
    spec = cat.MACHINES["PR1"].warning
    before = engine.machine_view("PR1")["Temperature"]
    assert engine.raise_warning("PR1") == {"ok": True, "reason": ""}
    view = engine.machine_view("PR1")
    assert view["AlarmActive"] is True
    assert view["AlarmId"] == 1
    assert view["AlarmSeverity"] == 500
    assert view["AlarmCode"] == spec.code
    assert view["AlarmText"] == spec.text
    assert view["Status"] == "IDLE"
    engine.run_until(spec.duration_min - 0.1)
    assert engine.machine_view("PR1")["AlarmActive"] is True
    assert engine.machine_view("PR1")["Temperature"] - before == pytest.approx(spec.delta_k, abs=0.1)
    engine.run_until(spec.duration_min + 0.1)
    view = engine.machine_view("PR1")
    assert view["AlarmActive"] is False
    assert view["AlarmId"] == 1  # the clear belongs to the same alarm
    engine.run_until(spec.duration_min + 60.0)
    assert engine.machine_view("PR1")["Temperature"] == pytest.approx(before, abs=0.01)


def test_warning_cannot_raise_while_faulted_or_while_the_slot_is_taken():
    engine = det()
    assert engine.inject_fault("PR1", "P-201", 100.0)["ok"]
    assert engine.raise_warning("PR1") == {"ok": False, "reason": "MACHINE_FAULT"}
    assert engine.machine_view("PR1")["AlarmId"] == 1
    assert engine.machine_view("PR1")["AlarmSeverity"] == 900
    engine = det()
    assert engine.raise_warning("PR1")["ok"]
    assert engine.raise_warning("PR1") == {"ok": False, "reason": "ALARM_ACTIVE"}
    assert engine.machine_view("PR1")["AlarmId"] == 1


def test_fault_replaces_warning_and_cancels_its_clear():
    engine = det()
    spec = cat.MACHINES["PR1"].warning
    assert engine.raise_warning("PR1")["ok"]
    engine.run_until(5.0)
    assert engine.inject_fault("PR1", "P-204", 200.0)["ok"]
    view = engine.machine_view("PR1")
    assert (view["AlarmId"], view["AlarmSeverity"], view["AlarmCode"]) == (2, 900, "P-204")
    # The warning's clear time passes: the fault keeps the slot.
    engine.run_until(spec.duration_min + 1.0)
    view = engine.machine_view("PR1")
    assert view["AlarmActive"] is True
    assert view["AlarmId"] == 2
    assert view["AlarmCode"] == "P-204"
    # The fault removed the excursion too.
    engine.run_until(100.0)
    assert engine.machine_view("PR1")["Temperature"] == pytest.approx(cat.MACHINES["PR1"].temp_fault_c, abs=0.05)
    engine.run_until(205.1)
    view = engine.machine_view("PR1")
    assert view["AlarmActive"] is False
    assert view["AlarmId"] == 2


def test_old_clear_does_not_touch_a_newer_warning():
    """warning(1) -> fault(2) -> repair -> warning(3): warning 1's clear still fires on the heap and must do nothing."""
    engine = det()
    spec = cat.MACHINES["PR1"].warning
    assert engine.raise_warning("PR1")["ok"]
    engine.run_until(5.0)
    assert engine.inject_fault("PR1", "P-201", 2.0)["ok"]
    engine.run_until(7.1)
    assert engine.machine_view("PR1")["AlarmActive"] is False
    assert engine.raise_warning("PR1")["ok"]
    assert engine.machine_view("PR1")["AlarmId"] == 3
    # Past the clear of alarm 1 (t = duration), before the clear of alarm 3 (t = 7.1 + duration).
    engine.run_until(spec.duration_min + 1.0)
    view = engine.machine_view("PR1")
    assert view["AlarmActive"] is True
    assert view["AlarmId"] == 3
    engine.run_until(7.1 + spec.duration_min + 0.1)
    view = engine.machine_view("PR1")
    assert view["AlarmActive"] is False
    assert view["AlarmId"] == 3


def test_stale_clear_is_not_a_pending_event():
    engine = det()
    assert engine.raise_warning("PR1")["ok"]
    assert engine.inject_fault("PR1", "P-201", 2.0)["ok"]
    assert engine.next_event_time() == pytest.approx(2.0)
    engine.run_until(2.1)
    assert engine.next_event_time() is None


def test_ack_alarm_table():
    engine = det()
    assert engine.ack_alarm("PR1", 0)["reason"] == "STALE_ALARM"
    assert engine.ack_alarm("PR1", 1)["reason"] == "STALE_ALARM"
    assert engine.inject_fault("PR1", "P-201", 5.0)["ok"]
    assert engine.ack_alarm("PR1", 2)["reason"] == "STALE_ALARM"
    assert engine.ack_alarm("PR1", 1) == {"ok": True, "reason": ""}
    assert engine.ack_alarm("PR1", 1)["ok"]
    assert engine.export_snapshot()["machines"]["PR1"]["alarm"]["acked"] is True
    assert engine.repair("PR1")["ok"]
    assert engine.inject_fault("PR1", "P-201", 5.0)["ok"]
    assert engine.ack_alarm("PR1", 1)["reason"] == "STALE_ALARM"
    assert engine.export_snapshot()["machines"]["PR1"]["alarm"]["acked"] is False


def test_random_warnings_occur_and_never_during_a_fault():
    engine = stoch(seed=33, failures=True)
    lots = make_lots(60)
    driver = FifoDriver(engine, lots)
    warnings = 0
    seen = {m: 0 for m in cat.MACHINE_ORDER}
    while not driver.done():
        driver.step()
        next_time = engine.next_event_time()
        if next_time is None:
            break
        engine.run_until(next_time)
        for machine_id in cat.MACHINE_ORDER:
            view = engine.machine_view(machine_id)
            if view["AlarmActive"] and view["AlarmId"] != seen[machine_id]:
                seen[machine_id] = view["AlarmId"]
                if view["AlarmSeverity"] == 500:
                    warnings += 1
                    assert view["Status"] in ("SETUP", "RUN")
            if view["Status"] == "FAULT":
                assert view["AlarmSeverity"] == 900 and view["AlarmActive"]
    assert warnings > 0


# ---------------------------------------------------------------------- durations, deterministic mode


def _phases(engine: SimEngine, lot: str) -> dict:
    result = {}
    for start in events_of(engine, "op_started"):
        if lot in start["lot_ids"]:
            done = next(e for e in events_of(engine, "op_finished") if e["op_ids"] == start["op_ids"])
            result[start["kind"]] = (start["t"], done["t"])
    return result


@pytest.mark.parametrize(
    "product_id, press, furnace",
    [("FR-60", "PR1", "OF1"), ("KK-80", "PR1", "OF2"), ("ST-200", "PR2", "OF2"), ("LS-40", "PR2", "OF1")],
)
def test_deterministic_durations_are_exactly_planned(product_id, press, furnace):
    product = cat.PRODUCTS[product_id]
    engine = det()
    lots = [Lot("L-1", "A-1", product_id, 8)]
    assert engine.start_operation(press_cmd("L-1", product_id, 8, press))["ok"]
    engine.run_until(1000.0)
    assert engine.start_operation(age_cmd(["L-1"], product.recipe, furnace))["ok"]
    engine.run_until(2000.0)
    assert engine.start_operation(saw_cmd("L-1"))["ok"]
    engine.run_until(3000.0)
    assert engine.start_operation(pack_cmd("L-1"))["ok"]
    engine.run_until(4000.0)
    phases = _phases(engine, "L-1")
    setups = {e["kind"]: e["setup_min"] for e in events_of(engine, "op_started")}
    bars = lots[0].planned_bars
    expected = {
        cat.PRESS: cat.planned_minutes(cat.PRESS, cat.ideal_press_min(product, press, 8)),
        cat.AGE: cat.planned_minutes(cat.AGE, cat.RECIPES[product.recipe].soak_min),
        cat.SAW: cat.planned_minutes(cat.SAW, bars * cat.SAW_MIN_PER_BAR),
        cat.PACK: cat.planned_minutes(cat.PACK, lots[0].n_packages * cat.PACK_MIN_PER_PACKAGE),
    }
    for kind, minutes in expected.items():
        start, end = phases[kind]
        assert end - start == pytest.approx(setups[kind] + minutes), kind
    assert_conserved(events_of(engine), lots, engine, complete=True)
    assert sum(e["pieces_scrap"] for e in events_of(engine, "sawn")) == 0
    assert sum(e["bars_scrap"] for e in events_of(engine, "billet_extruded")) == 0


def test_deterministic_billets_are_spread_evenly():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    engine.run_until(100.0)
    times = [e["t"] for e in events_of(engine, "billet_extruded")]
    assert [round(b - a, 9) for a, b in zip([0.0] + times, times)] == [round(61 / 8, 9)] * 8
    assert times[-1] == pytest.approx(61.0)


def test_deterministic_cast_duration_and_transport_lag():
    engine = det()
    assert engine.start_operation(cast_cmd("M-6060-00001", "6060"))["ok"]
    engine.run_until(200.0)
    cast = events_of(engine, "melt_cast")[-1]
    arrived = events_of(engine, "melt_arrived")[-1]
    assert cast["t"] == pytest.approx(93.0)
    assert cast["plan_id"] == "M-6060-00001"
    assert cast["t_start"] == 0.0
    assert len(cast["billets"]) == cat.BILLETS_PER_MELT
    assert arrived["t"] == pytest.approx(93.0 + cat.TRANSPORT_MIN)
    assert arrived["n_billets"] == cat.BILLETS_PER_MELT


def test_empty_plant_lot_takes_exactly_min_lead_time():
    engine = det()
    lots = [Lot("L-1", "A-1", "FR-60", 8)]
    driver = FifoDriver(engine, lots)
    driver.run(horizon=2000.0)
    packed = events_of(engine, "op_finished")[-1]
    assert packed["kind"] == "pack"
    assert packed["t"] == pytest.approx(cat.min_lead_min(cat.PRODUCTS["FR-60"]))


def test_setup_needed_only_on_class_change():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 1, "PR1"))["ok"]
    assert events_of(engine, "op_started")[-1]["setup_min"] == 0
    assert engine.machine_view("PR1")["Status"] == "RUN"
    engine.run_until(30.0)
    assert engine.start_operation(press_cmd("L-2", "FP-120", 1, "PR1"))["ok"]
    start = events_of(engine, "op_started")[-1]
    assert start["setup_min"] == 35
    assert start["setup_class"] == "6060|M-1002"
    assert engine.machine_view("PR1")["SetupClass"] == "6060|M-1002"
    engine.run_until(90.0)
    assert engine.start_operation(press_cmd("L-3", "KK-80", 1, "PR1"))["ok"]
    assert events_of(engine, "op_started")[-1]["setup_min"] == 55


def test_soak_has_no_noise_in_stochastic_mode():
    for seed in (1, 2, 3):
        engine = stoch(seed=seed)
        press_lot_to_age(engine, "L-1", "FR-60", 8, "PR1")
        age_lot_to_saw(engine, "L-1", "T66", "OF1")
        load = events_of(engine, "furnace_load_done")[0]
        assert load["soak_min"] == 300.0


def test_stochastic_durations_have_mean_near_allowance():
    ratios = []
    for seed in range(60):
        engine = stoch(seed=seed)
        assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
        engine.run_until(400.0)
        finished = events_of(engine, "op_finished")[0]
        ratios.append(finished["run_min"] / 56.0)
    mean = sum(ratios) / len(ratios)
    assert min(ratios) >= 1.0
    assert cat.PLANNING_ALLOWANCE[cat.PRESS] - 0.02 < mean < cat.PLANNING_ALLOWANCE[cat.PRESS] + 0.03


def test_scrap_rates_match_the_catalogue():
    engine = stoch(seed=9)
    lots = make_lots(150)
    FifoDriver(engine, lots).run(horizon=500_000.0)
    bars = sum(e["bars_total"] for e in events_of(engine, "billet_extruded"))
    press_scrap = sum(e["bars_scrap"] for e in events_of(engine, "billet_extruded"))
    saw_in = sum(e["bars_in"] for e in events_of(engine, "sawn"))
    saw_scrap = sum(e["pieces_scrap"] for e in events_of(engine, "sawn"))
    assert bars > 5000
    assert 0.005 < press_scrap / bars < 0.015
    assert 0.004 < saw_scrap / saw_in < 0.013


# ---------------------------------------------------------------------- OEE counters


def _ratio_windows(engine: SimEngine, driver: FifoDriver, machine_id: str, window: float, until: float) -> list[tuple]:
    rows = []
    last = engine.machine_view(machine_id)
    next_sample = engine.t + window
    while engine.t < until and not driver.done():
        driver.step()
        target = engine.next_event_time()
        target = next_sample if target is None else min(target, next_sample)
        engine.run_until(target)
        if engine.t >= next_sample:
            next_sample += window
            view = engine.machine_view(machine_id)
            rows.append((view["IdealMinutes"] - last["IdealMinutes"], view["RunMinutes"] - last["RunMinutes"]))
            last = view
    return rows


def test_press_window_stays_in_a_band_around_one_over_allowance():
    engine = stoch(seed=17)
    lots = make_lots(80, products=("FR-60",))
    driver = FifoDriver(engine, lots)
    rows = _ratio_windows(engine, driver, "PR1", 240.0, 4000.0)
    target = 1.0 / cat.PLANNING_ALLOWANCE[cat.PRESS]
    checked = 0
    for ideal, run in rows:
        if run >= 120.0:
            assert abs(ideal / run - target) < 0.04, (ideal, run)
            checked += 1
    assert checked >= 5


def test_uncapped_performance_never_exceeds_one_on_any_window():
    engine = stoch(seed=19, failures=True)
    lots = make_lots(40)
    driver = FifoDriver(engine, lots)
    last = {m: engine.machine_view(m) for m in cat.MACHINE_ORDER}
    step = 0
    worst = 0.0
    while not driver.done():
        driver.step()
        next_time = engine.next_event_time()
        if next_time is None:
            break
        # Also read mid-segment, so windows cut through billets and soak segments.
        engine.run_until(min(next_time, engine.t + 3.0))
        step += 1
        for machine_id in cat.MACHINE_ORDER:
            view = engine.machine_view(machine_id)
            ideal = view["IdealMinutes"] - last[machine_id]["IdealMinutes"]
            run = view["RunMinutes"] - last[machine_id]["RunMinutes"]
            assert ideal >= -EPS
            if run > 0.0:
                worst = max(worst, ideal / run)
            assert ideal <= run * (1.0 + 1e-6) + EPS
            last[machine_id] = view
    assert step > 100
    assert 0.5 < worst <= 1.0 + 1e-6


@pytest.mark.parametrize("seed", [5, 6])
def test_every_machine_cumulative_ideal_at_most_run_and_minutes_add_up(seed):
    engine = stoch(seed=seed, failures=True)
    FifoDriver(engine, make_lots(30)).run(horizon=200_000.0)
    engine.run_until(engine.t + 1.0)
    for machine_id in cat.MACHINE_ORDER:
        view = engine.machine_view(machine_id)
        assert view["IdealMinutes"] <= view["RunMinutes"] + EPS
        total = view["RunMinutes"] + view["SetupMinutes"] + view["FaultMinutes"] + view["IdleMinutes"]
        assert total == pytest.approx(engine.t)
        assert view["TotalCount"] == view["GoodCount"] + view["ScrapCount"]


@pytest.mark.parametrize(
    "furnace, n_billets, expected",
    [("OF2", 8, 2 / 10), ("OF2", 4, 1 / 10), ("OF1", 8, 2 / 6), ("OF1", 1, 1 / 6)],
)
def test_furnace_partial_load_shows_as_performance(furnace, n_billets, expected):
    engine = det()
    press_lot_to_age(engine, "L-1", "FR-60", n_billets, "PR1")
    assert engine.start_operation(age_cmd(["L-1"], "T66", furnace))["ok"]
    start = engine.t
    # Skip the setup of OF2 (starts on T6).
    while engine.machine_view(furnace)["Status"] != "RUN":
        run_past(engine)
    first = engine.machine_view(furnace)
    for fraction in (0.1, 0.5, 0.99):
        engine.run_until(start + fraction * 300.0 + 30.0 * (furnace == "OF2"))
        view = engine.machine_view(furnace)
        assert (view["IdealMinutes"] - first["IdealMinutes"]) / (view["RunMinutes"] - first["RunMinutes"]) == pytest.approx(
            expected
        )
    engine.run_until(start + 400.0)
    view = engine.machine_view(furnace)
    assert view["RunMinutes"] == pytest.approx(300.0)
    assert view["IdealMinutes"] == pytest.approx(300.0 * expected)


def test_full_furnace_has_performance_one():
    engine = det(initial_stock=False)
    engine._store["6060"].extend({"id": f"B-X-{i}", "melt_id": "S-X"} for i in range(40))
    lots = [f"L-{i}" for i in range(3)]
    for lot in lots:
        press_lot_to_age(engine, lot, "FR-60", 8, "PR1")
    assert engine.start_operation(age_cmd(lots, "T66", "OF1"))["ok"]  # 6 of 6 baskets
    engine.run_until(engine.t + 400.0)
    view = engine.machine_view("OF1")
    assert view["IdealMinutes"] == pytest.approx(view["RunMinutes"])
    assert view["TotalCount"] == 6


def test_counters_accrue_continuously_and_pause_in_fault():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    rate = 7.0 / (61.0 / 8.0)
    engine.run_until(1.5)  # mid-billet, no event has fired yet
    view = engine.machine_view("PR1")
    assert view["RunMinutes"] == pytest.approx(1.5)
    assert view["IdealMinutes"] == pytest.approx(1.5 * rate)
    assert view["Progress"] == pytest.approx(1.5 / 61.0)
    assert engine.inject_fault("PR1", "P-201", 20.0)["ok"]
    engine.run_until(11.5)
    faulted = engine.machine_view("PR1")
    assert faulted["RunMinutes"] == view["RunMinutes"]
    assert faulted["IdealMinutes"] == view["IdealMinutes"]
    assert faulted["FaultMinutes"] == pytest.approx(10.0)
    assert faulted["Progress"] == pytest.approx(view["Progress"])
    engine.run_until(22.5)
    resumed = engine.machine_view("PR1")
    assert resumed["RunMinutes"] == pytest.approx(2.5)
    assert resumed["IdealMinutes"] == pytest.approx(2.5 * rate)
    engine.run_until(200.0)
    final = engine.machine_view("PR1")
    assert final["IdealMinutes"] == pytest.approx(56.0)
    assert final["RunMinutes"] == pytest.approx(61.0)


def test_press_ideal_counts_the_whole_billet_regardless_of_scrap():
    engine = stoch(seed=2)
    assert engine.start_operation(press_cmd("L-1", "KK-80", 8, "PR1"))["ok"]
    engine.run_until(500.0)
    assert engine.machine_view("PR1")["IdealMinutes"] == pytest.approx(48.0)


def test_energy_is_power_times_time():
    engine = det()
    spec = cat.MACHINES["PR1"]
    engine.run_until(60.0)
    assert engine.machine_view("PR1")["EnergyKWh"] == pytest.approx(spec.power_idle_kw)
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    assert engine.machine_view("PR1")["PowerKW"] == spec.power_run_kw
    engine.run_until(90.0)
    assert engine.inject_fault("PR1", "P-201", 30.0)["ok"]
    assert engine.machine_view("PR1")["PowerKW"] == spec.power_fault_kw
    engine.run_until(120.0)
    expected = spec.power_idle_kw + spec.power_run_kw * 0.5 + spec.power_fault_kw * 0.5
    assert engine.machine_view("PR1")["EnergyKWh"] == pytest.approx(expected)
    assert engine.total_energy_kwh() >= expected
    engine.run_until(200.0)
    finished = events_of(engine, "op_finished")[0]
    assert finished["energy_kwh"] == pytest.approx(spec.power_run_kw * 61 / 60 + spec.power_fault_kw * 0.5, abs=0.01)
    assert finished["run_min"] == pytest.approx(61.0)
    assert finished["fault_min"] == pytest.approx(30.0)
    assert finished["setup_min"] == 0.0


# ---------------------------------------------------------------------- trace events


def test_conservation_on_trace_events_stochastic_with_faults():
    engine = stoch(seed=23, failures=True)
    lots = make_lots(36)
    FifoDriver(engine, lots).run(horizon=200_000.0)
    engine.run_until(engine.t + 100.0)
    assert_conserved(events_of(engine), lots, engine, complete=True)


def test_conservation_checker_can_fail():
    engine = det()
    lots = make_lots(2)
    FifoDriver(engine, lots).run(horizon=20_000.0)
    engine.run_until(engine.t + 100.0)
    events = json.loads(json.dumps(events_of(engine)))
    assert_conserved(events, lots, engine, complete=True)
    tampered = json.loads(json.dumps(events))
    next(e for e in tampered if e["type"] == "packed")["packages"][0]["pieces"] -= 1
    with pytest.raises(AssertionError):
        assert_conserved(tampered, lots, engine, complete=True)
    tampered = json.loads(json.dumps(events))
    next(e for e in tampered if e["type"] == "billet_extruded")["billet_id"] = "B-99999-01"
    with pytest.raises(AssertionError):
        assert_conserved(tampered, lots, engine, complete=True)
    tampered = json.loads(json.dumps(events))
    next(e for e in tampered if e["type"] == "baskets_filled")["baskets"][0]["bars"] += 1
    with pytest.raises(AssertionError):
        assert_conserved(tampered, lots, engine, complete=True)


def test_event_order_is_consistent():
    engine = stoch(seed=29, failures=True)
    lots = make_lots(24)
    FifoDriver(engine, lots).run(horizon=200_000.0)
    engine.run_until(engine.t + 100.0)
    events = events_of(engine)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(a["t"] <= b["t"] for a, b in zip(events, events[1:]))
    position = {}
    arrived_melts = set()
    cast_melts = set()
    available = set()
    for index, event in enumerate(events):
        type_ = event["type"]
        if type_ == "op_started":
            for op_id in event["op_ids"]:
                assert op_id not in position, "started twice"
                position[op_id] = index
        elif type_ == "op_finished":
            for op_id in event["op_ids"]:
                assert op_id in position, "finished before started"
                position[op_id + "#end"] = index
        elif type_ == "melt_cast":
            cast_melts.add(event["melt_id"])
            available.update(event["billets"])
        elif type_ == "melt_arrived":
            assert event["melt_id"] in cast_melts, "arrived before cast"
            arrived_melts.add(event["melt_id"])
        elif type_ == "billet_extruded":
            assert event["melt_id"] in arrived_melts, "extruded before the melt arrived"
            assert event["billet_id"] in available
            available.discard(event["billet_id"])
            assert event["lot_id"] + ":press" in position
            assert event["lot_id"] + ":press#end" not in position
            assert event["t_start"] <= event["t"]
        elif type_ == "baskets_filled":
            assert event["lot_id"] + ":press#end" not in position
        elif type_ == "furnace_load_done":
            for lot_id in event["lot_ids"]:
                assert lot_id + ":age" in position and lot_id + ":age#end" not in position
        elif type_ == "sawn":
            assert event["lot_id"] + ":saw#end" not in position
        elif type_ == "packed":
            assert event["lot_id"] + ":pack#end" not in position
        elif type_ == "lot_arrived":
            previous = {"age": "press", "saw": "age", "pack": "saw", "ship": "pack"}[event["stage"]]
            assert event["lot_id"] + f":{previous}#end" in position, "arrived before its op finished"
    for lot in lots:
        order = [lot.op_id(kind) for kind in cat.LOT_ROUTE]
        ends = [position[o + "#end"] for o in order]
        starts = [position[o] for o in order]
        assert all(s < e for s, e in zip(starts, ends))
        assert all(e < s for e, s in zip(ends, starts[1:]))


def test_op_started_fields_for_every_kind():
    engine = det()
    FifoDriver(engine, [Lot("L-1", "A-1", "ST-200", 8), Lot("L-2", "A-2", "FR-60", 2)]).run(horizon=20_000.0)
    by_kind = {e["kind"]: e for e in events_of(engine, "op_started")}
    expected = {"seq", "t", "type", "machine", "op_ids", "kind", "lot_ids", "batch_id", "setup_min", "setup_class"}
    for event in by_kind.values():
        assert set(event) == expected
    assert by_kind["press"]["batch_id"] is None
    assert by_kind["age"]["batch_id"] is not None
    finished_keys = {"seq", "t", "type", "machine", "op_ids", "kind", "lot_ids", "energy_kwh", "run_min", "setup_min", "fault_min"}
    assert all(set(e) == finished_keys for e in events_of(engine, "op_finished"))


@pytest.mark.parametrize(
    "type_, keys",
    [
        ("melt_cast", {"machine", "melt_id", "plan_id", "alloy", "billets", "temp_c", "t_start"}),
        ("melt_arrived", {"melt_id", "plan_id", "alloy", "n_billets"}),
        (
            "billet_extruded",
            {"machine", "lot_id", "order_id", "billet_id", "melt_id", "strand_id", "die", "bars_total", "bars_good",
             "bars_scrap", "temp_c", "t_start"},
        ),
        ("baskets_filled", {"machine", "lot_id", "baskets"}),
        (
            "furnace_load_done",
            {"machine", "load_id", "batch_id", "recipe", "lot_ids", "baskets", "soak_temp_avg", "soak_min",
             "in_band_min", "fault_min", "t_start"},
        ),
        ("sawn", {"machine", "lot_id", "baskets", "bars_in", "pieces_good", "pieces_scrap", "length_mm", "t_start"}),
        ("packed", {"machine", "lot_id", "packages", "t_start"}),
        ("lot_arrived", {"lot_id", "stage"}),
    ],
)
def test_trace_event_fields_match_the_contract(type_, keys):
    engine = det()
    FifoDriver(engine, make_lots(3)).run(horizon=20_000.0)
    events = events_of(engine, type_)
    assert events
    for event in events:
        assert set(event) == keys | {"seq", "t", "type"}
        json.dumps(event)


def test_ids_follow_the_plant_format():
    engine = det()
    FifoDriver(engine, make_lots(2)).run(horizon=20_000.0)
    engine.run_until(engine.t + 100.0)
    assert events_of(engine, "melt_cast")[0]["melt_id"] == "S-00001"
    assert events_of(engine, "melt_cast")[0]["billets"][6] == "B-00001-07"
    assert events_of(engine, "billet_extruded")[0]["strand_id"] == "ST-00001"
    assert events_of(engine, "baskets_filled")[0]["baskets"][0]["id"] == "K-00001"
    assert min(e["load_id"] for e in events_of(engine, "furnace_load_done")) == "O-00001"
    assert events_of(engine, "packed")[0]["packages"][0]["id"] == "P-00001"
    assert all(e["billets"][0].endswith("-01") for e in events_of(engine, "melt_cast"))


def test_events_after_window_and_gap_detection():
    engine = det()
    assert engine.events_after(0)["oldest_seq"] == 1
    assert engine.events_after(6) == {"oldest_seq": 1, "newest_seq": 6, "events": []}
    assert [e["seq"] for e in engine.events_after(2, 2)["events"]] == [3, 4]
    empty = det(initial_stock=False)
    assert empty.events_after(0) == {"oldest_seq": 1, "newest_seq": 0, "events": []}


def test_event_ring_keeps_exactly_the_last_twenty_thousand():
    engine = det(initial_stock=False)
    for _ in range(61_000):
        engine._emit("x")
    window = engine.events_after(0, 3)
    assert window["newest_seq"] == 61_000
    assert window["oldest_seq"] == 61_000 - 20_000 + 1
    assert window["events"][0]["seq"] == window["oldest_seq"]  # a client asking for seq 0 can see the gap
    assert len(engine._events) < 40_000
    assert [e["seq"] for e in engine.events_after(60_998)["events"]] == [60_999, 61_000]


def test_material_view_shape_and_bounds():
    engine = det()
    lots = make_lots(25, products=("KK-80",))
    driver = FifoDriver(engine, lots)
    seen_kinds = set()
    while not driver.done():
        driver.step()
        material = engine.material_view()
        json.dumps(material)
        assert set(material) == {"t", "transports", "buffers", "in_machine"}
        assert set(material["buffers"]) == {"BILLET_STORE", "AGE_IN", "SAW_IN", "PACK_IN", "SHIP"}
        assert set(material["buffers"]["SHIP"]) == {"packages", "pieces", "kg", "lots_total", "last_lots"}
        for transport in material["transports"]:
            assert set(transport) == {"id", "kind", "lot_id", "count", "from", "to", "t0", "t1"}
            assert transport["t1"] == transport["t0"] + cat.TRANSPORT_MIN
            seen_kinds.add(transport["kind"])
        for row in material["buffers"]["AGE_IN"] + material["buffers"]["SAW_IN"]:
            assert set(row) == {"lot_id", "baskets"}
        for row in material["buffers"]["PACK_IN"]:
            assert set(row) == {"lot_id", "pieces"}
        for row in material["in_machine"].values():
            assert set(row) == {"lot_ids", "kind", "billets_left", "baskets"}
        engine.run_until(engine.next_event_time())
    engine.run_until(engine.t + 100.0)
    ship = engine.material_view()["buffers"]["SHIP"]
    assert seen_kinds == {"billets", "baskets", "pieces", "packages"}
    assert ship["lots_total"] == 25
    assert len(ship["last_lots"]) == 20
    assert ship["last_lots"][-1] == lots[-1].id or set(ship["last_lots"]) <= {lot.id for lot in lots}
    assert ship["kg"] == pytest.approx(ship["pieces"] * cat.PRODUCTS["KK-80"].kg_per_bar)


def test_machine_view_has_every_variable_of_the_address_space():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "ST-200", 2, "PR2"))["ok"]
    view = engine.machine_view("PR2")
    assert set(view) == {
        "Name", "Kind", "Unit", "Status", "Temperature", "TemperatureSetpoint", "PowerKW", "EnergyKWh", "TotalCount",
        "GoodCount", "ScrapCount", "RunMinutes", "SetupMinutes", "FaultMinutes", "IdleMinutes", "IdealMinutes",
        "SetupClass", "CurrentOperation", "Progress", "AlarmId", "AlarmActive", "AlarmCode", "AlarmText",
        "AlarmSeverity", "EstimatedRepairMin", "FaultCount",
    }
    op = json.loads(view["CurrentOperation"])
    assert op == {
        "op_ids": ["L-1:press"],
        "kind": "press",
        "lot_ids": ["L-1"],
        "order_ids": ["A-1"],
        "product_id": "ST-200",
        "phase": "RUN",
        "batch_id": None,
    }
    assert engine.machine_view("PR1")["CurrentOperation"] == ""


def test_current_operation_phase_stays_visible_during_a_fault():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "KK-80", 2, "PR1"))["ok"]
    assert json.loads(engine.machine_view("PR1")["CurrentOperation"])["phase"] == "SETUP"
    assert engine.inject_fault("PR1", "P-201", 5.0)["ok"]
    assert json.loads(engine.machine_view("PR1")["CurrentOperation"])["phase"] == "SETUP"
    assert engine.machine_view("PR1")["Status"] == "FAULT"


def test_same_seed_same_events_different_seed_differs():
    def run(seed: int) -> list[dict]:
        engine = stoch(seed=seed, failures=True)
        FifoDriver(engine, make_lots(12)).run(horizon=100_000.0)
        return events_of(engine)

    assert run(7) == run(7)
    assert run(7) != run(8)


def test_engine_time_is_a_float_and_never_goes_back():
    engine = det()
    engine.run_until(10)
    assert isinstance(engine.t, float)
    engine.run_until(5.0)
    assert engine.t == 10.0


# ---------------------------------------------------------------------- scenario hooks and timeline


def test_schedule_fault_fires_at_the_given_time():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    engine.schedule_fault("PR1", 10.0, "P-204", 25.0)
    engine.run_until(9.9)
    assert engine.machine_view("PR1")["Status"] == "RUN"
    engine.run_until(10.0)
    view = engine.machine_view("PR1")
    assert view["Status"] == "FAULT"
    assert view["AlarmCode"] == "P-204"
    assert view["EstimatedRepairMin"] == 25.0
    engine.run_until(300.0)
    assert events_of(engine, "op_finished")[0]["t"] == pytest.approx(61.0 + 25.0)
    with pytest.raises(KeyError):
        engine.schedule_fault("PR1", 10.0, "G-101")


def test_schedule_fault_on_faulted_machine_is_ignored():
    engine = det()
    engine.schedule_fault("PR1", 1.0, "P-201", 10.0)
    engine.schedule_fault("PR1", 2.0, "P-202", 500.0)
    engine.run_until(50.0)
    view = engine.machine_view("PR1")
    assert view["Status"] == "IDLE"
    assert view["FaultCount"] == 1


def test_schedule_slowdown_scales_run_work_in_the_window():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    engine.schedule_slowdown("PR1", 2.0, 10.0, 20.0)
    engine.run_until(200.0)
    finished = events_of(engine, "op_finished")[0]
    # 10 at full speed, 20 in the window doing 10 minutes' worth, then the remaining 41.
    assert finished["t"] == pytest.approx(10.0 + 20.0 + 41.0)
    view = engine.machine_view("PR1")
    assert view["IdealMinutes"] == pytest.approx(56.0)
    assert view["IdealMinutes"] <= view["RunMinutes"]
    with pytest.raises(ValueError):
        engine.schedule_slowdown("PR1", 0.0, 10.0, 20.0)


def test_slowdown_and_fault_interleave():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    engine.schedule_slowdown("PR1", 2.0, 10.0, 40.0)
    engine.schedule_fault("PR1", 20.0, "P-201", 10.0)
    engine.run_until(300.0)
    # 10 normal, 10 slowed (=5 work), 10 fault, 20 slowed (=10 work), rest (61-25=36) normal.
    assert events_of(engine, "op_finished")[0]["t"] == pytest.approx(10.0 + 10.0 + 10.0 + 20.0 + 36.0)


def test_timeline_records_status_changes_and_ops():
    engine = det()
    assert engine.timeline == []
    engine.set_timeline_recording(True)
    assert {row["machine"] for row in engine.timeline} == set(cat.MACHINE_ORDER)
    engine.timeline.clear()
    assert engine.start_operation(press_cmd("L-1", "KK-80", 2, "PR1"))["ok"]
    engine.run_until(30.0)
    assert engine.inject_fault("PR1", "P-201", 10.0)["ok"]
    engine.run_until(500.0)
    statuses = [row["status"] for row in engine.timeline if row["type"] == "status"]
    assert statuses == ["SETUP", "FAULT", "SETUP", "RUN", "IDLE"]
    ops = [row for row in engine.timeline if row["type"] == "op"]
    assert len(ops) == 1
    assert ops[0]["op_ids"] == ["L-1:press"]
    assert ops[0]["start"] == 0.0
    assert ops[0]["fault_min"] == pytest.approx(10.0)
    assert ops[0]["setup_min"] == pytest.approx(55.0)
    assert ops[0]["end"] == pytest.approx(55.0 + 10.0 + cat.planned_minutes(cat.PRESS, 12.0))
    recording_off = len(engine.timeline)
    engine.set_timeline_recording(False)
    engine.inject_fault("PR1", "P-201", 1.0)
    assert len(engine.timeline) == recording_off


# ---------------------------------------------------------------------- speed


def test_two_thousand_busy_minutes_take_well_under_half_a_second():
    engine = stoch(seed=31)
    lots = make_lots(90)
    driver = FifoDriver(engine, lots)
    begin = time.perf_counter()
    driver.run_until_time(2000.0)
    elapsed = time.perf_counter() - begin
    assert engine.t >= 2000.0
    assert engine.seq > 800, "the plant was not busy"
    assert elapsed < 0.5, f"{elapsed:.3f} s for {engine.seq} events"
