"""Snapshots and forks: nothing hidden leaks out, nothing in flight is lost, every fork runs to completion."""
from __future__ import annotations

import json

import pytest
from test_simcore_support import (
    RUN_ID,
    FifoDriver,
    age_cmd,
    assert_conserved,
    cast_cmd,
    make_lots,
    press_cmd,
    saw_cmd,
)

from werkhalle.domain import catalog as cat
from werkhalle.domain.state import FAULT_REMAINING_FLOOR_MIN, PlanningState
from werkhalle.simcore import SimEngine, planning_state_from_snapshot

TOP_LEVEL_KEYS = {
    "version", "run_id", "seq", "t", "machines", "billet_store", "buffers", "transports", "started_op_ids",
    "finished_op_ids", "counters",
}


def det(**kwargs) -> SimEngine:
    return SimEngine(stochastic=False, run_id=RUN_ID, **kwargs)


def stoch(seed: int = 1, failures: bool = False, **kwargs) -> SimEngine:
    return SimEngine(seed=seed, stochastic=True, random_failures=failures, run_id=RUN_ID, **kwargs)


def events_of(engine: SimEngine, type_: str | None = None) -> list[dict]:
    events = engine.events_after(0, 10**9)["events"]
    return [e for e in events if type_ is None or e["type"] == type_]


def assert_close(a, b, path: str = "$") -> None:
    if isinstance(a, dict):
        assert isinstance(b, dict) and set(a) == set(b), path
        for key in a:
            assert_close(a[key], b[key], f"{path}.{key}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            assert_close(x, y, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert a == pytest.approx(b, abs=1e-6), path
    else:
        assert a == b, path


def numbers_in(value) -> list[float]:
    if isinstance(value, dict):
        return [n for v in value.values() for n in numbers_in(v)]
    if isinstance(value, list):
        return [n for v in value for n in numbers_in(v)]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    return []


def keys_in(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_in(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_in(v)}
    return set()


def busy_engine(seed: int = 3, failures: bool = True) -> tuple[SimEngine, list]:
    engine = stoch(seed=seed, failures=failures)
    lots = make_lots(12)
    FifoDriver(engine, lots).run_until_time(420.0)
    return engine, lots


# ---------------------------------------------------------------------- shape, atomicity, exclusions


def test_snapshot_is_json_serialisable_and_independent_of_the_engine():
    engine, _ = busy_engine()
    snapshot = engine.export_snapshot()
    assert set(snapshot) == TOP_LEVEL_KEYS
    assert json.loads(json.dumps(snapshot)) == snapshot
    assert snapshot["run_id"] == RUN_ID
    assert snapshot["seq"] == engine.seq
    assert snapshot["t"] == engine.t
    assert list(snapshot["machines"]) == list(cat.MACHINE_ORDER)
    # Mutating everything in the snapshot leaves the engine as it was.
    again = engine.export_snapshot()
    for data in snapshot["machines"].values():
        if data["op"] is not None:
            data["op"]["op_ids"].append("junk")
            data["op"]["lot_ids"].clear()
            for basket in data["op"].get("baskets", []):
                basket["bars"] = -1
    for billets in snapshot["billet_store"].values():
        billets.clear()
    for transport in snapshot["transports"]:
        transport["payload"].clear()
    for lot in snapshot["buffers"]["AGE_IN"]:
        lot["baskets"].clear()
    assert_close(again, engine.export_snapshot())


def test_snapshot_does_not_disturb_the_run():
    plain = stoch(seed=5, failures=True)
    watched = stoch(seed=5, failures=True)
    lots = make_lots(10)
    FifoDriver(plain, lots).run(horizon=100_000.0)
    driver = FifoDriver(watched, lots)
    while not driver.done():
        driver.step()
        watched.export_snapshot()
        watched.planning_state()
        watched.run_until(watched.next_event_time())
    assert events_of(plain) == events_of(watched)


def test_snapshot_excludes_everything_sampled():
    engine = stoch(seed=8, failures=True)
    lots = make_lots(6)
    driver = FifoDriver(engine, lots)
    driver.run_until_time(50.0)
    assert engine.inject_fault("PR1")["ok"]  # no repairMin: the end is sampled and hidden
    assert engine.inject_fault("SAE")["ok"]
    engine.run_until(engine.t + 1.0)
    snapshot = engine.export_snapshot()
    hidden = []
    for m in engine._machines.values():
        if m.status == "FAULT":
            hidden += [m.repair_end, m.repair_end - engine.t, m.repair_end - m.fault["started_t"]]
        if m.status in ("SETUP", "RUN"):
            hidden += [m.phase_end, m.phase_end - engine.t, m.seg_dur]
        if m.clock_since is not None:
            hidden += [m.failure_left, m.warning_left, m.clock_since + m.failure_left, m.clock_since + m.warning_left]
    assert len(hidden) >= 6
    numbers = numbers_in(snapshot)
    for value in hidden:
        assert not any(abs(n - value) < 1e-9 for n in numbers), value
    banned = ("rng", "repair_end", "failure", "next_", "seg_dur", "phase_end", "clock", "warning_left")
    assert not [k for k in keys_in(snapshot) if any(b in k for b in banned)]


def test_snapshot_contains_what_the_plan_lists():
    engine, _ = busy_engine()
    assert engine.inject_fault("SAE", "S-401", 55.0)["ok"]
    engine.run_until(engine.t + 5.0)
    snapshot = engine.export_snapshot()
    saw = snapshot["machines"]["SAE"]
    assert saw["fault"]["code"] == "S-401"
    assert saw["fault"]["estimate_min"] == 55.0
    assert saw["fault"]["elapsed_min"] == pytest.approx(5.0)
    assert saw["alarm"]["active"] is True
    assert {"run_min", "setup_min", "fault_min", "idle_min", "ideal_min", "energy_kwh", "total", "good", "scrap", "fault_count"} == set(saw["counters"])
    press_ops = [m["op"] for m in snapshot["machines"].values() if m["op"] and m["op"]["kind"] == "press"]
    assert press_ops
    for op in press_ops:
        assert 0.0 <= op["remaining_fraction"] <= 1.0
        assert op["phase"] in ("SETUP", "RUN")
        assert all(set(b) == {"id", "melt_id"} for b in op["billets"])
    assert all(set(billet) == {"id", "melt_id"} for billets in snapshot["billet_store"].values() for billet in billets)
    assert snapshot["started_op_ids"] == sorted(snapshot["started_op_ids"])
    assert set(snapshot["finished_op_ids"]) <= set(snapshot["started_op_ids"])
    for transport in snapshot["transports"]:
        assert set(transport) >= {"id", "kind", "lot_id", "count", "from", "to", "t0", "t1", "payload"}
    assert set(snapshot["counters"]) == {"melt", "strand", "basket", "load", "package", "transport"}


# ---------------------------------------------------------------------- rebuild


def _engine_with_everything_in_flight() -> tuple[SimEngine, dict]:
    """A snapshot with a melt and a lot on transports, a paused press, a running load, and filled buffers."""
    engine = det()
    engine._store["6060"].extend({"id": f"B-E-{i:02d}", "melt_id": "S-00001"} for i in range(40))
    assert engine.start_operation(cast_cmd("M-6063-00001", "6063"))["ok"]
    assert engine.start_operation(press_cmd("L-1", "FR-60", 3, "PR1"))["ok"]
    assert engine.start_operation(press_cmd("L-2", "FR-60", 2, "PR2"))["ok"]
    engine.run_until(90.0)
    assert engine.start_operation(press_cmd("L-3", "FR-60", 6, "PR1"))["ok"]
    assert engine.start_operation(age_cmd(["L-1"], "T66", "OF1"))["ok"]
    assert engine.start_operation(press_cmd("L-4", "KK-80", 1, "PR2"))["ok"]
    engine.run_until(112.0)
    assert engine.inject_fault("PR1", "P-202", 500.0)["ok"]
    assert engine.inject_fault("PR2", "P-201", 500.0)["ok"]
    assert engine.raise_warning("VP")["ok"]
    engine.run_until(113.0)
    return engine, engine.export_snapshot()


def test_fork_reproduces_the_snapshot_exactly():
    engine, snapshot = _engine_with_everything_in_flight()
    fork = SimEngine.from_snapshot(snapshot)
    assert_close(snapshot, fork.export_snapshot())
    assert fork.t == engine.t
    assert fork.seq == engine.seq
    assert fork.run_id == RUN_ID


def test_fork_is_detached_from_the_snapshot_it_came_from():
    _, snapshot = _engine_with_everything_in_flight()
    frozen = json.loads(json.dumps(snapshot))
    fork = SimEngine.from_snapshot(snapshot)
    fork.run_until(fork.t + 10_000.0)
    assert snapshot == frozen
    second = SimEngine.from_snapshot(snapshot)
    assert_close(frozen, second.export_snapshot())


def test_fork_rebuilds_every_in_flight_thing():
    _, snapshot = _engine_with_everything_in_flight()
    states = {m: d["status"] for m, d in snapshot["machines"].items()}
    assert states["PR1"] == "FAULT" and snapshot["machines"]["PR1"]["op"]["phase"] == "RUN"
    assert states["PR2"] == "FAULT" and snapshot["machines"]["PR2"]["op"]["phase"] == "SETUP"
    assert states["OF1"] == "RUN"
    assert snapshot["machines"]["OF1"]["op"]["phase"] == "RUN"
    assert snapshot["machines"]["VP"]["alarm"]["active"] is True
    fork = SimEngine.from_snapshot(snapshot)
    material = fork.material_view()
    assert material["buffers"]["AGE_IN"] or material["transports"]
    assert fork.machine_view("OF1")["TemperatureSetpoint"] == 185.0
    assert fork.machine_view("PR1")["Status"] == "FAULT"
    # Everything that was underway resolves on its own.
    fork.run_until(fork.t + 5000.0)
    assert fork.next_event_time() is None
    assert all(fork.machine_view(m)["Status"] == "IDLE" for m in cat.MACHINE_ORDER)
    # A paused press carried its reserved billets through, and baskets kept their payload.
    extruded = events_of(fork, "billet_extruded")
    assert sorted(e["lot_id"] for e in extruded).count("L-3") >= 1
    assert all(e["billet_id"] for e in extruded)
    # The cast finished in the fork with the melt id it was started with, then arrived in the store.
    arrived = [e for e in events_of(fork, "melt_arrived")]
    assert [e["plan_id"] for e in arrived] == ["M-6063-00001"]
    assert arrived[0]["melt_id"] == "S-00004"
    assert fork.material_view()["buffers"]["BILLET_STORE"]["6063"] == 24 + 24 - 1


def test_fork_events_continue_the_sequence():
    engine, snapshot = _engine_with_everything_in_flight()
    fork = SimEngine.from_snapshot(snapshot)
    window = fork.events_after(snapshot["seq"])
    assert window == {"oldest_seq": snapshot["seq"] + 1, "newest_seq": snapshot["seq"], "events": []}
    fork.run_until(fork.t + 5000.0)
    events = fork.events_after(snapshot["seq"], 10**9)["events"]
    assert [e["seq"] for e in events] == list(range(snapshot["seq"] + 1, snapshot["seq"] + 1 + len(events)))
    assert fork.events_after(0, 1)["oldest_seq"] == snapshot["seq"] + 1


def test_setup_remainder_and_paused_setup_are_exact():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "KK-80", 1, "PR1"))["ok"]  # 55 min setup
    engine.run_until(20.0)
    fork = SimEngine.from_snapshot(engine.export_snapshot())
    fork.run_until(54.9)
    assert fork.machine_view("PR1")["Status"] == "SETUP"
    fork.run_until(55.1)
    assert fork.machine_view("PR1")["Status"] == "RUN"
    assert fork.machine_view("PR1")["SetupMinutes"] == pytest.approx(55.0)

    assert engine.inject_fault("PR1", "P-201", 30.0)["ok"]
    engine.run_until(25.0)
    snapshot = engine.export_snapshot()
    assert snapshot["machines"]["PR1"]["op"]["setup_left_min"] == pytest.approx(35.0)
    fork = SimEngine.from_snapshot(snapshot)
    # Remaining repair = estimate - elapsed = 25, then the 35 of setup still left.
    fork.run_until(49.9)
    assert fork.machine_view("PR1")["Status"] == "FAULT"
    fork.run_until(50.1)
    assert fork.machine_view("PR1")["Status"] == "SETUP"
    fork.run_until(84.9)
    assert fork.machine_view("PR1")["Status"] == "SETUP"
    fork.run_until(85.1)
    assert fork.machine_view("PR1")["Status"] == "RUN"


def test_deterministic_fork_of_a_running_billet_uses_the_planned_rate():
    engine = stoch(seed=1)
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    engine.run_until(engine.next_event_time())
    engine.run_until(engine.next_event_time())  # two billets done
    now = engine.t
    fork = SimEngine.from_snapshot(engine.export_snapshot())
    fork.run_until(now + 1000.0)
    # Six billets left at the planned share of 61 minutes, whatever the plant's noise did so far.
    assert events_of(fork, "op_finished")[0]["t"] == pytest.approx(now + 6 * 61.0 / 8.0)


def test_fork_mid_lot_finishes_exactly_the_billets_that_were_left():
    engine = stoch(seed=4)
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    engine.run_until(25.0)
    snapshot = engine.export_snapshot()
    op = snapshot["machines"]["PR1"]["op"]
    done = op["billets_done"]
    assert done == len([e for e in events_of(engine, "billet_extruded")])
    assert len(op["billets"]) == 8 - done
    fork = SimEngine.from_snapshot(snapshot)
    fork.run_until(1000.0)
    extruded = [e["billet_id"] for e in events_of(engine, "billet_extruded") + events_of(fork, "billet_extruded")]
    assert extruded == [f"B-00001-{i:02d}" for i in range(1, 9)]


@pytest.mark.parametrize(
    "estimate, elapsed, remaining",
    [(100.0, 30.0, 70.0), (100.0, 95.0, FAULT_REMAINING_FLOOR_MIN), (100.0, 300.0, FAULT_REMAINING_FLOOR_MIN)],
)
def test_deterministic_fork_repair_is_estimate_minus_elapsed_with_floor(estimate, elapsed, remaining):
    engine = stoch(seed=1)
    assert engine.inject_fault("PR1", "P-201", 10_000.0)["ok"]
    engine.run_until(elapsed)
    snapshot = engine.export_snapshot()
    # The estimate is what the plant published, the true end (10 000) stays out of it.
    snapshot["machines"]["PR1"]["fault"]["estimate_min"] = estimate
    fork = SimEngine.from_snapshot(snapshot)
    assert fork.next_event_time() == pytest.approx(elapsed + remaining)
    fork.run_until(elapsed + remaining - 0.01)
    assert fork.machine_view("PR1")["Status"] == "FAULT"
    fork.run_until(elapsed + remaining + 0.01)
    assert fork.machine_view("PR1")["Status"] == "IDLE"
    assert fork.machine_view("PR1")["AlarmActive"] is False


def test_overrun_repair_still_blocks_the_machine_in_a_fork():
    engine = stoch(seed=1)
    assert engine.inject_fault("PR1", "P-201", 30.0)["ok"]
    engine.run_until(29.0)
    state = engine.planning_state()
    assert state.machine("PR1").fault_remaining_min == FAULT_REMAINING_FLOOR_MIN
    assert state.machine("PR1").fault_estimate_min == 30.0
    assert state.machine("PR1").fault_elapsed_min == pytest.approx(29.0)


def test_stochastic_fork_samples_repair_afresh():
    engine = stoch(seed=1)
    assert engine.inject_fault("PR1", "P-204", 240.0)["ok"]
    engine.run_until(40.0)
    snapshot = engine.export_snapshot()
    ends = {SimEngine.from_snapshot(snapshot, seed=seed, stochastic=True).next_event_time() for seed in range(20)}
    assert len(ends) > 10
    assert min(ends) >= 40.0 + FAULT_REMAINING_FLOOR_MIN
    assert max(ends) > 40.0 + 200.0
    same = {SimEngine.from_snapshot(snapshot, seed=3, stochastic=True).next_event_time() for _ in range(3)}
    assert len(same) == 1


def test_fork_flags():
    snapshot = stoch(seed=1).export_snapshot()
    fork = SimEngine.from_snapshot(snapshot)
    assert fork.stochastic is False
    assert fork.random_failures is False
    fork = SimEngine.from_snapshot(snapshot, stochastic=True, random_failures=True)
    assert fork.stochastic is True
    assert fork.random_failures is True
    fork = SimEngine.from_snapshot(snapshot, stochastic=False, random_failures=True)
    assert fork.random_failures is False


def test_fork_refuses_stale_runs_and_duplicates():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 2, "PR1"))["ok"]
    fork = SimEngine.from_snapshot(engine.export_snapshot())
    assert fork.start_operation(press_cmd("L-1", "FR-60", 2, "PR2"))["reason"] == "ALREADY_STARTED"
    assert fork.start_operation({**press_cmd("L-2", "FR-60", 2, "PR2"), "run_id": "other"})["reason"] == "STALE_RUN"


def test_fork_keeps_warning_and_its_clear():
    engine = det()
    spec = cat.MACHINES["SAE"].warning
    assert engine.raise_warning("SAE")["ok"]
    engine.run_until(5.0)
    snapshot = engine.export_snapshot()
    fork = SimEngine.from_snapshot(snapshot)
    assert fork.machine_view("SAE")["AlarmActive"] is True
    assert fork.machine_view("SAE")["Temperature"] == pytest.approx(engine.machine_view("SAE")["Temperature"])
    fork.run_until(spec.duration_min - 0.1)
    view = fork.machine_view("SAE")
    assert view["AlarmActive"] is True
    assert view["Temperature"] == pytest.approx(cat.MACHINES["SAE"].temp_idle_c + spec.delta_k, abs=0.1)
    fork.run_until(spec.duration_min + 0.1)
    assert fork.machine_view("SAE")["AlarmActive"] is False
    assert fork.machine_view("SAE")["AlarmId"] == 1
    fork.run_until(spec.duration_min + 60.0)
    assert fork.machine_view("SAE")["Temperature"] == pytest.approx(cat.MACHINES["SAE"].temp_idle_c, abs=0.01)


def test_fork_mid_soak_keeps_load_and_in_band_time():
    reference = det()
    assert reference.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    reference.run_until(100.0)
    assert reference.start_operation(age_cmd(["L-1"], "T66", "OF1"))["ok"]
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    engine.run_until(100.0)
    assert engine.start_operation(age_cmd(["L-1"], "T66", "OF1"))["ok"]
    engine.run_until(200.0)
    reference.run_until(1000.0)
    fork = SimEngine.from_snapshot(engine.export_snapshot())
    fork.run_until(1000.0)
    a = events_of(reference, "furnace_load_done")[0]
    b = events_of(fork, "furnace_load_done")[0]
    for key in ("t", "soak_temp_avg", "soak_min", "in_band_min", "fault_min", "t_start"):
        assert a[key] == pytest.approx(b[key], abs=0.01), key
    assert a["baskets"] == b["baskets"] and a["load_id"] == b["load_id"]


# ---------------------------------------------------------------------- closed loop, fork at many points


def _drive_base_run(seed: int, n_lots: int):
    """Stochastic closed loop with random failures and injected faults. Yields after every processed event."""
    engine = stoch(seed=seed, failures=True)
    lots = make_lots(n_lots)
    driver = FifoDriver(engine, lots)
    victims = list(cat.MACHINE_ORDER)
    next_fault = 35.0
    while not driver.done():
        driver.step()
        yield engine, lots, driver
        next_time = engine.next_event_time()
        assert next_time is not None
        if next_time >= next_fault:
            victim = victims.pop(0)
            victims.append(victim)
            engine.run_until(next_fault)
            engine.inject_fault(victim, "", 25.0)
            next_fault += 85.0
            continue
        engine.run_until(next_time)


def test_fork_at_every_event_of_a_closed_loop_completes_with_nothing_lost():
    forks = 0
    faulted_forks = 0
    paused_forks = 0
    for engine, lots, _ in _drive_base_run(seed=13, n_lots=9):
        snapshot = json.loads(json.dumps(engine.export_snapshot()))
        history = events_of(engine)
        fork = SimEngine.from_snapshot(snapshot, seed=1)
        FifoDriver(fork, lots).run(horizon=snapshot["t"] + 50_000.0)
        fork.run_until(fork.t + 1000.0)
        assert fork.next_event_time() is None, "work left over after completion"
        assert_conserved(history + fork.events_after(snapshot["seq"], 10**9)["events"], lots, fork, complete=True)
        forks += 1
        states = [d["status"] for d in snapshot["machines"].values()]
        faulted_forks += "FAULT" in states
        paused_forks += any(d["status"] == "FAULT" and d["op"] for d in snapshot["machines"].values())
    assert forks > 60
    assert faulted_forks > 5
    assert paused_forks > 3


def test_stochastic_forks_also_complete_with_nothing_lost():
    forks = 0
    for engine, lots, _ in _drive_base_run(seed=14, n_lots=8):
        if engine.seq % 11:
            continue
        snapshot = engine.export_snapshot()
        history = events_of(engine)
        fork = SimEngine.from_snapshot(snapshot, seed=forks, stochastic=True, random_failures=True)
        FifoDriver(fork, lots).run(horizon=snapshot["t"] + 100_000.0)
        fork.run_until(fork.t + 5000.0)
        assert fork.next_event_time() is None
        assert_conserved(history + fork.events_after(snapshot["seq"], 10**9)["events"], lots, fork, complete=True)
        forks += 1
    assert forks > 8


def test_deterministic_fork_of_a_deterministic_run_gives_the_same_completion_times():
    base = det()
    lots = make_lots(8)
    driver = FifoDriver(base, lots)
    snapshots = []
    while not driver.done():
        driver.step()
        snapshots.append(base.export_snapshot())
        base.run_until(base.next_event_time())
    expected = {e["lot_id"]: e["t"] for e in events_of(base, "lot_arrived") if e["stage"] == "ship"}
    for snapshot in snapshots[::5]:
        fork = SimEngine.from_snapshot(snapshot)
        FifoDriver(fork, lots).run(horizon=100_000.0)
        got = {e["lot_id"]: e["t"] for e in events_of(fork, "lot_arrived") if e["stage"] == "ship"}
        for lot_id, t in got.items():
            assert t == pytest.approx(expected[lot_id], abs=1e-6), (lot_id, snapshot["t"])


def test_fork_of_a_fork_also_completes():
    engine = stoch(seed=2, failures=True)
    lots = make_lots(8)
    FifoDriver(engine, lots).run_until_time(200.0)
    history = events_of(engine)
    seq = engine.seq
    first = SimEngine.from_snapshot(engine.export_snapshot())
    FifoDriver(first, lots).run_until_time(500.0)
    history += first.events_after(seq, 10**9)["events"]
    seq = first.seq
    second = SimEngine.from_snapshot(first.export_snapshot())
    FifoDriver(second, lots).run(horizon=100_000.0)
    second.run_until(second.t + 100.0)
    assert_conserved(history + second.events_after(seq, 10**9)["events"], lots, second, complete=True)


def test_fork_scenario_hooks_work():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 8, "PR1"))["ok"]
    engine.run_until(10.0)
    fork = SimEngine.from_snapshot(engine.export_snapshot())
    fork.schedule_fault("PR1", 20.0, "P-204", 100.0)
    fork.schedule_slowdown("SAE", 2.0, 0.0, 10.0)
    fork.set_timeline_recording(True)
    fork.run_until(400.0)
    assert fork.machine_view("PR1")["FaultCount"] == 1
    assert events_of(fork, "op_finished")[0]["t"] == pytest.approx(161.0)
    assert any(row["status"] == "FAULT" for row in fork.timeline if row["type"] == "status")


# ---------------------------------------------------------------------- planning state


def test_planning_state_at_start():
    engine = SimEngine(run_id="r-1")
    state = engine.planning_state()
    assert isinstance(state, PlanningState)
    assert state.run_id == "r-1"
    assert state.seq == 6
    assert state.now == 0.0
    assert state.billet_store == cat.INITIAL_BILLET_STOCK
    assert state.melts_in_transit == ()
    assert state.lots_in_transit == ()
    assert state.lots_buffered == {}
    assert state.started_op_ids == frozenset()
    assert state.machine("PR1").setup_class == cat.INITIAL_SETUP_CLASS["PR1"]
    assert state.machine("PR1").status == "IDLE"
    assert state.machine("PR1").op_ids == ()
    assert state.machine("PR1").remaining_work_min == 0.0
    assert [m.id for m in state.machines] == list(cat.MACHINE_ORDER)


def test_planning_state_remaining_work_at_planning_rate():
    engine = stoch(seed=2)
    assert engine.start_operation(press_cmd("L-1", "KK-80", 8, "PR1"))["ok"]  # setup 55, planned ceil(48 * 1.08) = 52
    engine.run_until(5.0)
    machine = engine.planning_state().machine("PR1")
    assert machine.status == "SETUP"
    assert machine.setup_class == "6063|M-2001"  # the class being set up to
    assert machine.op_ids == ("L-1:press",)
    assert machine.kind == "press"
    assert machine.lot_ids == ("L-1",)
    assert machine.started_min == 0.0
    assert machine.remaining_work_min == pytest.approx(50.0 + 52.0)

    assert engine.inject_fault("PR1", "P-201", 60.0)["ok"]
    engine.run_until(30.0)
    machine = engine.planning_state().machine("PR1")
    assert machine.status == "FAULT"
    assert machine.op_ids == ("L-1:press",)  # still on the machine while paused
    assert machine.remaining_work_min == pytest.approx(50.0 + 52.0)  # excludes the fault pause
    assert machine.fault_code == "P-201"
    assert machine.fault_estimate_min == 60.0
    assert machine.fault_elapsed_min == pytest.approx(25.0)
    assert machine.fault_remaining_min == pytest.approx(35.0)

    engine.run_until(115.0 + 26.0)
    machine = engine.planning_state().machine("PR1")
    assert machine.status == "RUN"
    assert 0.0 < machine.remaining_work_min < 52.0


def test_planning_state_lists_transits_and_buffers_from_one_snapshot():
    engine = det()
    assert engine.start_operation(cast_cmd("M-6060-00001", "6060"))["ok"]
    assert engine.start_operation(press_cmd("L-1", "FR-60", 1, "PR1"))["ok"]
    engine.run_until(9.0)  # press done, lot on the transport to AGE_IN
    state = engine.planning_state()
    assert [(t.lot_id, t.stage) for t in state.lots_in_transit] == [("L-1", "age")]
    assert state.lots_in_transit[0].arrives_min == pytest.approx(8.0 + 8.0)
    assert "L-1:press" in state.finished_op_ids
    assert "L-1:press" in state.started_op_ids
    engine.run_until(20.0)
    state = engine.planning_state()
    assert state.lots_buffered == {"L-1": "age"}
    assert state.lots_in_transit == ()
    engine.run_until(94.0)
    state = engine.planning_state()
    assert [(m.plan_id, m.alloy, m.n_billets) for m in state.melts_in_transit] == [("M-6060-00001", "6060", 24)]
    assert state.melts_in_transit[0].melt_id == "S-00004"
    assert state.melts_in_transit[0].arrives_min == pytest.approx(93.0 + 8.0)
    assert state.billet_store["6060"] == 23
    assert state.machine("GUSS").status == "IDLE"
    engine.run_until(110.0)
    assert engine.planning_state().billet_store["6060"] == 47


def test_planning_state_load_and_ship_stage():
    engine = det()
    assert engine.start_operation(press_cmd("L-1", "FR-60", 2, "PR1"))["ok"]
    engine.run_until(30.0)
    assert engine.start_operation(age_cmd(["L-1"], "T66", "OF2", "B-r1-1"))["ok"]
    machine = engine.planning_state().machine("OF2")
    assert machine.batch_id == "B-r1-1"
    assert machine.lot_ids == ("L-1",)
    assert machine.op_ids == ("L-1:age",)
    assert machine.remaining_work_min == pytest.approx(30.0 + 300.0)
    engine.run_until(30.0 + 330.0 + 1.0)
    assert [t.stage for t in engine.planning_state().lots_in_transit] == ["saw"]
    engine.run_until(30.0 + 330.0 + 10.0)
    assert engine.planning_state().lots_buffered == {"L-1": "saw"}
    assert engine.start_operation(saw_cmd("L-1"))["ok"]


def test_planning_state_from_snapshot_equals_engine_view_and_survives_json():
    engine, _ = busy_engine()
    snapshot = engine.export_snapshot()
    a = engine.planning_state()
    b = planning_state_from_snapshot(json.loads(json.dumps(snapshot)))
    assert a == b


def test_store_and_lots_stay_consistent_at_every_step():
    """Every billet is in exactly one place, the invariant melt allocation relies on."""
    engine = stoch(seed=7, failures=True)
    lots = make_lots(10)
    driver = FifoDriver(engine, lots)
    cast_total = 72
    while not driver.done():
        driver.step()
        snapshot = engine.export_snapshot()
        in_store = sum(len(b) for b in snapshot["billet_store"].values())
        on_press = sum(
            len(m["op"]["billets"]) for m in snapshot["machines"].values() if m["op"] and m["op"]["kind"] == "press"
        )
        on_transport = sum(t["count"] for t in snapshot["transports"] if t["kind"] == "billets")
        extruded = len(events_of(engine, "billet_extruded"))
        cast_total = sum(len(e["billets"]) for e in events_of(engine, "melt_cast"))
        assert in_store + on_press + on_transport + extruded == cast_total
        engine.run_until(engine.next_event_time())
    assert cast_total >= sum(lot.n_billets for lot in lots)
