"""The plant as an event-driven simulation: a heap of events, temperature and energy analytic in between.

All times are simulated minutes. Nothing here knows about OPC UA or wall-clock time. The same code runs behind the
OPC UA server (stochastic) and inside the twin (usually deterministic, forked from a snapshot).

Hidden on purpose: sampled repair ends, next-failure times, sampled segment durations and the RNG state never enter a
snapshot, so a fork cannot peek at the future of the plant it was forked from.
"""
from __future__ import annotations

import heapq
import json
import math
import random
from collections import deque
from collections.abc import Mapping
from typing import Any

from ..domain import catalog as cat
from ..domain.state import (
    FAULT_REMAINING_FLOOR_MIN,
    LotInTransit,
    MachineState,
    MeltInTransit,
    PlanningState,
)

IDLE = "IDLE"
SETUP = "SETUP"
RUN = "RUN"
FAULT = "FAULT"

BAD_COMMAND = "BAD_COMMAND"
STALE_RUN = "STALE_RUN"
ALREADY_STARTED = "ALREADY_STARTED"
MACHINE_FAULT = "MACHINE_FAULT"
MACHINE_BUSY = "MACHINE_BUSY"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
NO_MATERIAL = "NO_MATERIAL"
ALREADY_FAULTED = "ALREADY_FAULTED"
NOT_FAULTED = "NOT_FAULTED"
STALE_ALARM = "STALE_ALARM"
ALARM_ACTIVE = "ALARM_ACTIVE"

RING_SIZE = 20_000
LAST_LOTS = 20
SEVERITY_WARNING = 500
SEVERITY_FAULT = 900

# Tolerance around the recipe temperature within which a furnace counts as soaking properly.
SOAK_BAND_K = 5.0
# Time constant of the excursion term, short so a warning really shows delta_k well before it clears.
EXCURSION_TAU_MIN = 3.0
EXCURSION_ZERO_K = 1e-3
SOAK_NUMERIC_STEP_MIN = 0.25

NOISE_GAMMA_SHAPE = 2.0
MICROSTOP_PROBABILITY = 0.03
MICROSTOP_MIN = (0.5, 2.5)
REPAIR_SIGMA = 0.5

_STAGE_BY_BUFFER = {"AGE_IN": "age", "SAW_IN": "saw", "PACK_IN": "pack", "SHIP": "ship"}

# Heap entries are (time, insertion number, kind, machine or payload, token); the number keeps ties deterministic.
_K_PHASE = 0
_K_REPAIR = 1
_K_TRANSPORT = 2
_K_FAILURE = 3
_K_WARNING = 4
_K_WARNING_CLEAR = 5
_K_SCHEDULED_FAULT = 6
_K_SPEED = 7


def _result(reason: str = "") -> dict:
    return {"ok": not reason, "reason": reason}


def _copy(value: Any) -> Any:
    """Deep copy of JSON-shaped data, several times faster than copy.deepcopy."""
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _id_list(value: Any) -> list[str] | None:
    """Non-empty sequence of unique non-empty strings as a list, else None."""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if not all(_is_id(item) for item in value) or len(set(value)) != len(value):
        return None
    return list(value)


MAX_REPAIR_MIN = 100_000.0


class _Machine:
    __slots__ = (
        "id", "spec", "kind", "capacity", "tau",
        "status", "setup_class", "op",
        "acc_t", "run_min", "setup_min", "fault_min", "idle_min", "ideal_min", "energy_kwh", "power_kw", "ideal_rate",
        "total", "good", "scrap", "fault_count",
        "temp_t", "temp_base", "setpoint", "excursion", "excursion_target",
        "phase_token", "phase_end", "seg_dur", "seg_ideal", "speed", "paused_phase", "paused_left",
        "fault", "repair_token", "repair_end",
        "alarm_id", "alarm_active", "alarm_code", "alarm_text", "alarm_severity", "alarm_acked", "warning_clear_at",
        "clock_token", "clock_since", "failure_left", "warning_left",
    )

    def __init__(self, spec: cat.MachineSpec):
        self.id = spec.id
        self.spec = spec
        self.kind = spec.kind
        self.capacity = spec.capacity
        self.tau = spec.temp_tau_min
        self.status = IDLE
        self.setup_class: str = cat.INITIAL_SETUP_CLASS[spec.id]
        self.op: dict | None = None
        self.acc_t = 0.0
        self.run_min = 0.0
        self.setup_min = 0.0
        self.fault_min = 0.0
        self.idle_min = 0.0
        self.ideal_min = 0.0
        self.energy_kwh = 0.0
        self.power_kw = spec.power_idle_kw
        self.ideal_rate = 0.0
        self.total = 0
        self.good = 0
        self.scrap = 0
        self.fault_count = 0
        self.temp_t = 0.0
        self.temp_base = spec.temp_idle_c
        self.setpoint = spec.temp_idle_c
        self.excursion = 0.0
        self.excursion_target = 0.0
        self.phase_token = 0
        self.phase_end = 0.0
        self.seg_dur = 0.0  # duration of the current work segment at speed 1
        self.seg_ideal = 0.0
        self.speed = 1.0  # duration multiplier of RUN work while a slowdown is active
        self.paused_phase: str | None = None
        self.paused_left = 0.0  # work left when a fault paused the op, RUN work at speed 1
        self.fault: dict | None = None
        self.repair_token = 0
        self.repair_end = 0.0
        self.alarm_id = 0
        self.alarm_active = False
        self.alarm_code = ""
        self.alarm_text = ""
        self.alarm_severity = 0
        self.alarm_acked = False
        self.warning_clear_at: float | None = None
        self.clock_token = 0
        self.clock_since: float | None = None
        self.failure_left: float | None = None
        self.warning_left: float | None = None


class SimEngine:
    """See docs/PLAN.md sections 2 and 3 for the rules this implements."""

    def __init__(
        self,
        seed: int = 1,
        stochastic: bool = True,
        random_failures: bool = True,
        run_id: str = "",
        initial_stock: bool = True,
    ):
        self._setup(seed, stochastic, random_failures, run_id)
        if initial_stock:
            self._emit_initial_stock()

    def _setup(self, seed: int, stochastic: bool, random_failures: bool, run_id: str) -> None:
        self.run_id = run_id
        self.stochastic = bool(stochastic)
        # A deterministic engine has no random faults at all, whatever the caller asks for.
        self.random_failures = bool(random_failures) and self.stochastic
        self.t = 0.0
        self.seq = 0
        self.timeline: list[dict] = []
        self._timeline_on = False
        self._rng = random.Random(seed)
        self._heap: list[tuple] = []
        self._n = 0
        self._events: list[dict] = []
        self._machines = {spec.id: _Machine(spec) for spec in cat.MACHINES.values()}
        self._store: dict[str, deque] = {alloy: deque() for alloy in cat.ALLOYS}
        self._age_in: dict[str, dict] = {}
        self._saw_in: dict[str, dict] = {}
        self._pack_in: dict[str, dict] = {}
        self._ship = {"packages": 0, "pieces": 0, "kg": 0.0, "lots_total": 0, "last_lots": []}
        self._transports: dict[str, dict] = {}
        self._started: set[str] = set()
        self._finished: set[str] = set()
        self._counters = {"melt": 0, "strand": 0, "basket": 0, "load": 0, "package": 0, "transport": 0}

    # ------------------------------------------------------------------ time

    def run_until(self, t: float) -> None:
        """Process every event up to and including t, then stand at t."""
        heap = self._heap
        while heap and heap[0][0] <= t:
            item = heapq.heappop(heap)
            if self._stale(item):
                continue
            if item[0] > self.t:
                self.t = item[0]
            self._dispatch(item)
        if t > self.t:
            self.t = float(t)

    def next_event_time(self) -> float | None:
        heap = self._heap
        while heap and self._stale(heap[0]):
            heapq.heappop(heap)
        return heap[0][0] if heap else None

    def _push(self, t: float, kind: int, arg: Any, token: int = 0) -> None:
        self._n += 1
        heapq.heappush(self._heap, (t, self._n, kind, arg, token))

    @staticmethod
    def _stale(item: tuple) -> bool:
        kind, arg, token = item[2], item[3], item[4]
        if kind == _K_PHASE:
            return token != arg.phase_token
        if kind == _K_REPAIR:
            return token != arg.repair_token
        if kind == _K_FAILURE or kind == _K_WARNING:
            return token != arg.clock_token
        if kind == _K_WARNING_CLEAR:
            return token != arg.alarm_id or not arg.alarm_active
        return False

    def _dispatch(self, item: tuple) -> None:
        kind, arg = item[2], item[3]
        if kind == _K_PHASE:
            self._on_phase_end(arg)
        elif kind == _K_TRANSPORT:
            self._on_transport_arrival(arg)
        elif kind == _K_REPAIR:
            self._end_fault(arg)
        elif kind == _K_FAILURE:
            self._on_random_failure(arg)
        elif kind == _K_WARNING:
            self._on_random_warning(arg)
        elif kind == _K_WARNING_CLEAR:
            self._clear_warning(arg)
        elif kind == _K_SCHEDULED_FAULT:
            self.inject_fault(*arg)
        elif kind == _K_SPEED:
            machine, factor = arg
            self._change_speed(machine, machine.speed * factor)

    # ------------------------------------------------------------------ continuous quantities

    def _accrue(self, m: _Machine) -> None:
        """Integrate counters and energy up to now. Idempotent, so any reader may call it at any time."""
        t = self.t
        dt = t - m.acc_t
        if dt <= 0.0:
            return
        energy = m.power_kw * dt / 60.0
        m.energy_kwh += energy
        status = m.status
        op = m.op
        if status == RUN:
            m.run_min += dt
            m.ideal_min += dt * m.ideal_rate
            op["run_done_min"] += dt
            op["energy_kwh"] += energy
            if m.kind == cat.AGE:
                self._accrue_soak(m, op, m.acc_t, t)
        elif status == SETUP:
            m.setup_min += dt
            op["setup_done_min"] += dt
            op["energy_kwh"] += energy
        elif status == FAULT:
            m.fault_min += dt
            if op is not None:
                op["fault_total_min"] += dt
                op["energy_kwh"] += energy
        else:
            m.idle_min += dt
        m.acc_t = t

    def _accrue_soak(self, m: _Machine, op: dict, a: float, b: float) -> None:
        """Time in band and the temperature integral over it, for the soak segment [a, b]."""
        tau = m.tau
        s0 = a - m.temp_t
        s1 = b - m.temp_t
        base = m.setpoint
        d = m.temp_base - base
        ex_target = m.excursion_target
        ex_gap = m.excursion - ex_target
        ex_now = ex_target + ex_gap * math.exp(-s0 / EXCURSION_TAU_MIN)
        if ex_target == 0.0 and abs(ex_now) < EXCURSION_ZERO_K:
            # The usual case: a single exponential toward the recipe, so band entry and integral are exact.
            size = abs(d)
            s_in = tau * math.log(size / SOAK_BAND_K) if size > SOAK_BAND_K else 0.0
            lo = max(s0, s_in)
            if s1 > lo:
                op["soak_in_band_min"] += s1 - lo
                op["soak_temp_integral"] += base * (s1 - lo) + d * tau * (math.exp(-lo / tau) - math.exp(-s1 / tau))
            return
        steps = max(1, math.ceil((s1 - s0) / SOAK_NUMERIC_STEP_MIN))
        step = (s1 - s0) / steps
        in_band = 0.0
        integral = 0.0
        for i in range(steps):
            s = s0 + (i + 0.5) * step
            temp = base + d * math.exp(-s / tau) + ex_target + ex_gap * math.exp(-s / EXCURSION_TAU_MIN)
            if abs(temp - base) <= SOAK_BAND_K:
                in_band += step
                integral += temp * step
        op["soak_in_band_min"] += in_band
        op["soak_temp_integral"] += integral

    def _temperature(self, m: _Machine) -> float:
        dt = self.t - m.temp_t
        temp = m.setpoint + (m.temp_base - m.setpoint) * math.exp(-dt / m.tau)
        if m.excursion != 0.0 or m.excursion_target != 0.0:
            temp += m.excursion_target + (m.excursion - m.excursion_target) * math.exp(-dt / EXCURSION_TAU_MIN)
        return temp

    def _rebase_temperature(self, m: _Machine) -> None:
        """Fold the analytic curve into a new starting point, before the setpoint or the excursion target changes."""
        self._accrue(m)
        dt = self.t - m.temp_t
        if dt > 0.0:
            m.temp_base = m.setpoint + (m.temp_base - m.setpoint) * math.exp(-dt / m.tau)
            if m.excursion != m.excursion_target:
                m.excursion = m.excursion_target + (m.excursion - m.excursion_target) * math.exp(
                    -dt / EXCURSION_TAU_MIN
                )
            m.temp_t = self.t
        if m.excursion_target == 0.0 and abs(m.excursion) < EXCURSION_ZERO_K:
            m.excursion = 0.0

    def _setpoint_for(self, m: _Machine, status: str) -> float:
        spec = m.spec
        if status == FAULT:
            return spec.temp_fault_c
        if status == IDLE:
            return spec.temp_idle_c
        if m.kind == cat.AGE:
            return m.op["recipe_temp_c"]
        return spec.temp_setup_c if status == SETUP else spec.temp_run_c

    @staticmethod
    def _power_for(spec: cat.MachineSpec, status: str) -> float:
        if status == RUN:
            return spec.power_run_kw
        if status == SETUP:
            return spec.power_setup_kw
        if status == FAULT:
            return spec.power_fault_kw
        return spec.power_idle_kw

    def _set_status(self, m: _Machine, status: str) -> None:
        self._rebase_temperature(m)
        m.status = status
        m.setpoint = self._setpoint_for(m, status)
        m.power_kw = self._power_for(m.spec, status)
        if self._timeline_on:
            self.timeline.append(self._status_row(m))

    def _status_row(self, m: _Machine) -> dict:
        op = m.op
        return {
            "t": self.t,
            "type": "status",
            "machine": m.id,
            "status": m.status,
            "op_ids": list(op["op_ids"]) if op else [],
            "lot_ids": list(op["lot_ids"]) if op else [],
        }

    # ------------------------------------------------------------------ trace events

    def _emit(self, type_: str, **fields: Any) -> None:
        self.seq += 1
        event = {"seq": self.seq, "t": self.t, "type": type_}
        event.update(fields)
        events = self._events
        events.append(event)
        if len(events) >= 2 * RING_SIZE:
            del events[: len(events) - RING_SIZE]

    def events_after(self, seq: int, max_count: int = 500) -> dict:
        """Events newer than seq, oldest first. `oldest_seq > seq + 1` tells the caller it missed some."""
        events = self._events
        newest = self.seq
        oldest = max(events[0]["seq"], newest - RING_SIZE + 1) if events else newest + 1
        if not events:
            return {"oldest_seq": oldest, "newest_seq": newest, "events": []}
        begin = max(seq + 1, oldest) - events[0]["seq"]
        return {"oldest_seq": oldest, "newest_seq": newest, "events": events[begin : begin + max(0, max_count)]}

    def _next_id(self, counter: str) -> int:
        self._counters[counter] += 1
        return self._counters[counter]

    def _emit_initial_stock(self) -> None:
        melts: list[tuple[str, int]] = []
        for alloy, count in sorted(cat.INITIAL_BILLET_STOCK.items()):
            while count > 0:
                n = min(count, cat.BILLETS_PER_MELT)
                melts.append((alloy, n))
                count -= n
        for index, (alloy, n) in enumerate(melts):
            melt_no = self._next_id("melt")
            melt_id = f"S-{melt_no:05d}"
            billets = [f"B-{melt_no:05d}-{i:02d}" for i in range(1, n + 1)]
            age = len(melts) - index
            self._emit(
                "melt_cast",
                machine="GUSS",
                melt_id=melt_id,
                plan_id=cat.INITIAL_MELT_PLAN_ID,
                alloy=alloy,
                billets=billets,
                temp_c=cat.MACHINES["GUSS"].temp_run_c,
                t_start=-age * (cat.MELT_MIN + cat.TRANSPORT_MIN),
            )
            self._emit("melt_arrived", melt_id=melt_id, plan_id=cat.INITIAL_MELT_PLAN_ID, alloy=alloy, n_billets=n)
            self._store[alloy].extend({"id": billet, "melt_id": melt_id} for billet in billets)

    # ------------------------------------------------------------------ commands

    def start_operation(self, cmd: dict) -> dict:
        """Start one op or furnace load. Refusals leave every buffer and machine exactly as they were."""
        if not isinstance(cmd, Mapping):
            return _result(BAD_COMMAND)
        if cmd.get("run_id", "") != self.run_id:
            return _result(STALE_RUN)
        op_ids = _id_list(cmd.get("op_ids"))
        kind = cmd.get("kind")
        machine_id = cmd.get("machine")
        if (
            op_ids is None
            or not isinstance(kind, str)
            or kind not in cat.KINDS
            or not isinstance(machine_id, str)
            or machine_id not in self._machines
            or not self._well_formed(kind, cmd, op_ids)
        ):
            return _result(BAD_COMMAND)
        if any(op_id in self._started for op_id in op_ids):
            return _result(ALREADY_STARTED)
        m = self._machines[machine_id]
        if m.status == FAULT:
            return _result(MACHINE_FAULT)
        if m.status != IDLE:
            return _result(MACHINE_BUSY)
        if m.kind != kind:
            return _result(NOT_ELIGIBLE)
        if kind == cat.CAST:
            return self._start_cast(m, cmd, op_ids)
        if kind == cat.PRESS:
            return self._start_press(m, cmd, op_ids)
        if kind == cat.AGE:
            return self._start_age(m, cmd, op_ids)
        if kind == cat.SAW:
            return self._start_saw(m, cmd, op_ids)
        return self._start_pack(m, cmd, op_ids)

    @staticmethod
    def _well_formed(kind: str, cmd: Mapping, op_ids: list[str]) -> bool:
        if kind == cat.AGE:
            lot_ids = _id_list(cmd.get("lot_ids"))
            batch_id = cmd.get("batch_id")
            return (
                lot_ids is not None
                and len(lot_ids) == len(op_ids)
                and isinstance(cmd.get("recipe"), str)
                and cmd["recipe"] in cat.RECIPES
                and (batch_id is None or isinstance(batch_id, str))
            )
        if len(op_ids) != 1:
            return False
        if kind == cat.CAST:
            alloy = cmd.get("alloy")
            return _is_id(cmd.get("melt_plan_id")) and isinstance(alloy, str) and alloy in cat.ALLOYS
        if kind == cat.PRESS:
            n = cmd.get("n_billets")
            product_id = cmd.get("product_id")
            return (
                _is_id(cmd.get("lot_id"))
                and _is_id(cmd.get("order_id"))
                and isinstance(product_id, str)
                and product_id in cat.PRODUCTS
                and isinstance(n, int)
                and not isinstance(n, bool)
                and n > 0
            )
        return _is_id(cmd.get("lot_id"))

    def _new_op(
        self,
        m: _Machine,
        op_ids: list[str],
        kind: str,
        lot_ids: list[str],
        order_ids: list[str],
        product_id: str | None,
        batch_id: str | None,
        to_class: str,
        work_min: float,
        ideal_total_min: float,
    ) -> dict:
        return {
            "op_ids": op_ids,
            "kind": kind,
            "lot_ids": lot_ids,
            "order_ids": order_ids,
            "product_id": product_id,
            "batch_id": batch_id,
            "setup_class": to_class,
            "setup_min": cat.setup_minutes(kind, m.setup_class, to_class),
            "started_t": self.t,
            "run_started_t": None,
            "work_min": work_min,  # duration at full rate, soak for a furnace whatever its fill
            "ideal_total_min": ideal_total_min,  # what OEE credits, scaled by fill for a furnace
            "planned_total_min": cat.planned_minutes(kind, work_min),
            "run_done_min": 0.0,
            "setup_done_min": 0.0,
            "fault_total_min": 0.0,
            "energy_kwh": 0.0,
        }

    def _start_cast(self, m: _Machine, cmd: Mapping, op_ids: list[str]) -> dict:
        alloy = cmd["alloy"]
        op = self._new_op(m, op_ids, cat.CAST, [], [], None, None, alloy, cat.MELT_MIN, cat.MELT_MIN)
        melt_no = self._next_id("melt")
        op["melt"] = {"melt_id": f"S-{melt_no:05d}", "melt_no": melt_no, "plan_id": cmd["melt_plan_id"], "alloy": alloy}
        return self._launch(m, op)

    def _start_press(self, m: _Machine, cmd: Mapping, op_ids: list[str]) -> dict:
        product = cat.PRODUCTS[cmd["product_id"]]
        if m.id not in product.press_min_per_billet:
            return _result(NOT_ELIGIBLE)
        n = cmd["n_billets"]
        store = self._store[product.alloy]
        if len(store) < n:
            return _result(NO_MATERIAL)
        work = cat.ideal_press_min(product, m.id, n)
        op = self._new_op(
            m, op_ids, cat.PRESS, [cmd["lot_id"]], [cmd["order_id"]], product.id, None,
            cat.setup_class(cat.PRESS, product), work, work,
        )
        op["n_billets"] = n
        op["billets"] = [store.popleft() for _ in range(n)]  # reserved atomically, FIFO by melt
        op["billets_done"] = 0
        op["billet_t_start"] = None
        op["open_strands"] = []
        op["baskets"] = []
        return self._launch(m, op)

    def _start_age(self, m: _Machine, cmd: Mapping, op_ids: list[str]) -> dict:
        recipe = cat.RECIPES[cmd["recipe"]]
        lot_ids = list(cmd["lot_ids"])
        if any(lot_id not in self._age_in for lot_id in lot_ids):
            return _result(NO_MATERIAL)
        lots = [self._age_in[lot_id] for lot_id in lot_ids]
        n_baskets = sum(len(lot["baskets"]) for lot in lots)
        if n_baskets > m.capacity or any(lot["recipe"] != recipe.id for lot in lots):
            return _result(NOT_ELIGIBLE)
        for lot_id in lot_ids:
            del self._age_in[lot_id]
        op = self._new_op(
            m, op_ids, cat.AGE, lot_ids, list(dict.fromkeys(lot["order_id"] for lot in lots)), lots[0]["product_id"],
            cmd.get("batch_id"), recipe.id, recipe.soak_min, cat.ideal_age_min_per_basket(recipe, m.id) * n_baskets,
        )
        op["lots"] = lots
        op["recipe"] = recipe.id
        op["recipe_temp_c"] = recipe.temp_c
        op["load_id"] = f"O-{self._next_id('load'):05d}"
        op["soak_in_band_min"] = 0.0
        op["soak_temp_integral"] = 0.0
        return self._launch(m, op)

    def _start_saw(self, m: _Machine, cmd: Mapping, op_ids: list[str]) -> dict:
        lot = self._saw_in.get(cmd["lot_id"])
        if lot is None:
            return _result(NO_MATERIAL)
        del self._saw_in[lot["lot_id"]]
        product = cat.PRODUCTS[lot["product_id"]]
        work = sum(basket["bars"] for basket in lot["baskets"]) * cat.SAW_MIN_PER_BAR
        op = self._new_op(
            m, op_ids, cat.SAW, [lot["lot_id"]], [lot["order_id"]], product.id, None,
            cat.setup_class(cat.SAW, product), work, work,
        )
        op["lots"] = [lot]
        return self._launch(m, op)

    def _start_pack(self, m: _Machine, cmd: Mapping, op_ids: list[str]) -> dict:
        lot = self._pack_in.get(cmd["lot_id"])
        if lot is None:
            return _result(NO_MATERIAL)
        del self._pack_in[lot["lot_id"]]
        product = cat.PRODUCTS[lot["product_id"]]
        work = math.ceil(lot["pieces"] / product.bars_per_package) * cat.PACK_MIN_PER_PACKAGE
        op = self._new_op(
            m, op_ids, cat.PACK, [lot["lot_id"]], [lot["order_id"]], product.id, None,
            cat.setup_class(cat.PACK, product), work, work,
        )
        op["lots"] = [lot]
        return self._launch(m, op)

    def _launch(self, m: _Machine, op: dict) -> dict:
        m.op = op
        m.setup_class = op["setup_class"]
        self._started.update(op["op_ids"])
        self._emit(
            "op_started",
            machine=m.id,
            op_ids=list(op["op_ids"]),
            kind=op["kind"],
            lot_ids=list(op["lot_ids"]),
            batch_id=op["batch_id"],
            setup_min=op["setup_min"],
            setup_class=op["setup_class"],
        )
        self._clock_start(m)
        if op["setup_min"] > 0:
            self._set_status(m, SETUP)
            self._schedule_phase_end(m, self.t + op["setup_min"])
        else:
            self._begin_run(m, op)
        return _result()

    # ------------------------------------------------------------------ phases and segments

    def _schedule_phase_end(self, m: _Machine, at: float) -> None:
        m.phase_end = at
        m.phase_token += 1
        self._push(at, _K_PHASE, m, m.phase_token)

    def _on_phase_end(self, m: _Machine) -> None:
        if m.status == SETUP:
            self._begin_run(m, m.op)
        else:
            self._finish_segment(m, m.op)

    def _begin_run(self, m: _Machine, op: dict) -> None:
        self._set_status(m, RUN)
        op["run_started_t"] = self.t
        self._start_segment(m, op)

    def _start_segment(self, m: _Machine, op: dict) -> None:
        self._accrue(m)
        if op["kind"] == cat.PRESS:
            op["billet_t_start"] = self.t
        self._sample_segment(m, op)
        m.ideal_rate = m.seg_ideal / (m.seg_dur * m.speed) if m.seg_dur > 0.0 else 0.0
        self._schedule_phase_end(m, self.t + m.seg_dur * m.speed)

    def _sample_segment(self, m: _Machine, op: dict) -> None:
        """Set the ideal and actual duration of the next work segment: one billet on a press, else the whole op."""
        kind = op["kind"]
        n = op["n_billets"] if kind == cat.PRESS else 1
        m.seg_ideal = op["ideal_total_min"] / n
        if not self.stochastic:
            m.seg_dur = op["planned_total_min"] / n
        elif kind == cat.AGE:
            m.seg_dur = op["work_min"]
        else:
            m.seg_dur = self._noisy_duration(kind, op["work_min"] / n)

    def _noisy_duration(self, kind: str, work: float) -> float:
        """At least the ideal time, so ideal/run can never exceed 1. Mean of the noise factor = the allowance."""
        rng = self._rng
        mean_excess = cat.PLANNING_ALLOWANCE[kind] - 1.0
        duration = work * (1.0 + rng.gammavariate(NOISE_GAMMA_SHAPE, mean_excess / NOISE_GAMMA_SHAPE))
        if rng.random() < MICROSTOP_PROBABILITY:
            duration += rng.uniform(*MICROSTOP_MIN)
        return duration

    def _scrap(self, bars: int, rate: float) -> int:
        if not self.stochastic or bars <= 0:
            return 0
        return self._rng.binomialvariate(bars, rate)

    def _finish_segment(self, m: _Machine, op: dict) -> None:
        self._accrue(m)
        kind = op["kind"]
        if kind == cat.PRESS:
            self._finish_billet(m, op)
        elif kind == cat.CAST:
            self._finish_cast(m, op)
        elif kind == cat.AGE:
            self._finish_age(m, op)
        elif kind == cat.SAW:
            self._finish_saw(m, op)
        else:
            self._finish_pack(m, op)

    def _finish_billet(self, m: _Machine, op: dict) -> None:
        product = cat.PRODUCTS[op["product_id"]]
        billet = op["billets"].pop(0)
        bars = product.bars_per_billet
        scrap = self._scrap(bars, cat.PRESS_SCRAP_RATE)
        good = bars - scrap
        m.total += bars
        m.good += good
        m.scrap += scrap
        strand_id = f"ST-{self._next_id('strand'):05d}"
        self._emit(
            "billet_extruded",
            machine=m.id,
            lot_id=op["lot_ids"][0],
            order_id=op["order_ids"][0],
            billet_id=billet["id"],
            melt_id=billet["melt_id"],
            strand_id=strand_id,
            die=product.die,
            bars_total=bars,
            bars_good=good,
            bars_scrap=scrap,
            temp_c=round(self._temperature(m), 2),
            t_start=op["billet_t_start"],
        )
        op["billets_done"] += 1
        op["open_strands"].append({"id": strand_id, "bars": good, "billet_id": billet["id"], "melt_id": billet["melt_id"]})
        last = not op["billets"]
        if len(op["open_strands"]) >= product.strands_per_basket or last:
            self._close_basket(m, op)
        if not last:
            self._start_segment(m, op)
            return
        lot_id = op["lot_ids"][0]
        lot = {
            "lot_id": lot_id,
            "order_id": op["order_ids"][0],
            "product_id": product.id,
            "alloy": product.alloy,
            "recipe": product.recipe,
            "n_billets": op["n_billets"],
            "melt_ids": sorted({s["melt_id"] for b in op["baskets"] for s in b["strands"]}),
            "baskets": op["baskets"],
        }
        self._finish_op(m, op)
        self._transport("baskets", lot_id, len(lot["baskets"]), m.id, "AGE_IN", {"lot": lot})

    def _close_basket(self, m: _Machine, op: dict) -> None:
        """Whole strands only: a strand is never split across two baskets."""
        strands = op["open_strands"]
        op["open_strands"] = []
        basket = {"id": f"K-{self._next_id('basket'):05d}", "bars": sum(s["bars"] for s in strands), "strands": strands}
        op["baskets"].append(basket)
        self._emit(
            "baskets_filled",
            machine=m.id,
            lot_id=op["lot_ids"][0],
            baskets=[
                {
                    "id": basket["id"],
                    "bars": basket["bars"],
                    "strands": [{"id": s["id"], "bars": s["bars"]} for s in strands],
                }
            ],
        )

    def _finish_cast(self, m: _Machine, op: dict) -> None:
        melt = op["melt"]
        billets = [f"B-{melt['melt_no']:05d}-{i:02d}" for i in range(1, cat.BILLETS_PER_MELT + 1)]
        m.total += len(billets)
        m.good += len(billets)
        self._emit(
            "melt_cast",
            machine=m.id,
            melt_id=melt["melt_id"],
            plan_id=melt["plan_id"],
            alloy=melt["alloy"],
            billets=billets,
            temp_c=round(self._temperature(m), 2),
            t_start=op["run_started_t"],
        )
        self._finish_op(m, op)
        payload = {"melt_id": melt["melt_id"], "plan_id": melt["plan_id"], "alloy": melt["alloy"], "billets": billets}
        self._transport("billets", None, len(billets), m.id, "BILLET_STORE", payload)

    def _finish_age(self, m: _Machine, op: dict) -> None:
        lots = op["lots"]
        basket_ids = [basket["id"] for lot in lots for basket in lot["baskets"]]
        m.total += len(basket_ids)
        m.good += len(basket_ids)
        in_band = op["soak_in_band_min"]
        self._emit(
            "furnace_load_done",
            machine=m.id,
            load_id=op["load_id"],
            batch_id=op["batch_id"],
            recipe=op["recipe"],
            lot_ids=list(op["lot_ids"]),
            baskets=basket_ids,
            soak_temp_avg=round(op["soak_temp_integral"] / in_band, 2) if in_band > 0.0 else None,
            soak_min=round(op["run_done_min"], 3),
            in_band_min=round(in_band, 3),
            fault_min=round(op["fault_total_min"], 3),
            t_start=op["run_started_t"],
        )
        self._finish_op(m, op)
        for lot in lots:
            self._transport("baskets", lot["lot_id"], len(lot["baskets"]), m.id, "SAW_IN", {"lot": lot})

    def _finish_saw(self, m: _Machine, op: dict) -> None:
        lot = op["lots"][0]
        product = cat.PRODUCTS[lot["product_id"]]
        rows = []
        bars_in = pieces_good = pieces_scrap = 0
        for basket in lot["baskets"]:
            scrap = self._scrap(basket["bars"], cat.SAW_SCRAP_RATE)
            good = basket["bars"] - scrap
            basket["pieces_good"] = good
            basket["pieces_scrap"] = scrap
            rows.append({"id": basket["id"], "bars_in": basket["bars"], "pieces_good": good, "pieces_scrap": scrap})
            bars_in += basket["bars"]
            pieces_good += good
            pieces_scrap += scrap
        m.total += bars_in
        m.good += pieces_good
        m.scrap += pieces_scrap
        lot["pieces"] = pieces_good
        self._emit(
            "sawn",
            machine=m.id,
            lot_id=lot["lot_id"],
            baskets=rows,
            bars_in=bars_in,
            pieces_good=pieces_good,
            pieces_scrap=pieces_scrap,
            length_mm=product.length_mm,
            t_start=op["run_started_t"],
        )
        self._finish_op(m, op)
        self._transport("pieces", lot["lot_id"], pieces_good, m.id, "PACK_IN", {"lot": lot})

    def _finish_pack(self, m: _Machine, op: dict) -> None:
        lot = op["lots"][0]
        per_package = cat.PRODUCTS[lot["product_id"]].bars_per_package
        packages: list[dict] = []
        for basket in lot["baskets"]:
            left = basket["pieces_good"]
            while left > 0:
                if not packages or packages[-1]["pieces"] >= per_package:
                    packages.append({"id": f"P-{self._next_id('package'):05d}", "pieces": 0, "baskets": []})
                package = packages[-1]
                take = min(left, per_package - package["pieces"])
                package["pieces"] += take
                package["baskets"].append({"id": basket["id"], "pieces": take})
                left -= take
        m.total += len(packages)
        m.good += len(packages)
        lot["package_ids"] = [package["id"] for package in packages]
        self._emit("packed", machine=m.id, lot_id=lot["lot_id"], packages=packages, t_start=op["run_started_t"])
        self._finish_op(m, op)
        self._transport("packages", lot["lot_id"], len(packages), m.id, "SHIP", {"lot": lot})

    def _finish_op(self, m: _Machine, op: dict) -> None:
        self._accrue(m)
        self._clock_stop(m)
        self._emit(
            "op_finished",
            machine=m.id,
            op_ids=list(op["op_ids"]),
            kind=op["kind"],
            lot_ids=list(op["lot_ids"]),
            energy_kwh=round(op["energy_kwh"], 4),
            run_min=round(op["run_done_min"], 3),
            setup_min=round(op["setup_done_min"], 3),
            fault_min=round(op["fault_total_min"], 3),
        )
        self._finished.update(op["op_ids"])
        if self._timeline_on:
            self.timeline.append(
                {
                    "t": self.t,
                    "type": "op",
                    "machine": m.id,
                    "op_ids": list(op["op_ids"]),
                    "kind": op["kind"],
                    "lot_ids": list(op["lot_ids"]),
                    "batch_id": op["batch_id"],
                    "start": op["started_t"],
                    "run_start": op["run_started_t"],
                    "end": self.t,
                    "setup_min": op["setup_done_min"],
                    "fault_min": op["fault_total_min"],
                }
            )
        m.op = None
        m.seg_dur = 0.0
        m.seg_ideal = 0.0
        self._set_status(m, IDLE)

    # ------------------------------------------------------------------ transports and buffers

    def _transport(self, kind: str, lot_id: str | None, count: int, source: str, target: str, payload: dict) -> None:
        transport = {
            "id": f"T-{self._next_id('transport')}",
            "kind": kind,
            "lot_id": lot_id,
            "count": count,
            "from": source,
            "to": target,
            "t0": self.t,
            "t1": self.t + cat.TRANSPORT_MIN,
            "payload": payload,
        }
        self._transports[transport["id"]] = transport
        self._push(transport["t1"], _K_TRANSPORT, transport)

    def _on_transport_arrival(self, transport: dict) -> None:
        del self._transports[transport["id"]]
        target = transport["to"]
        payload = transport["payload"]
        if target == "BILLET_STORE":
            melt_id = payload["melt_id"]
            self._emit(
                "melt_arrived",
                melt_id=melt_id,
                plan_id=payload["plan_id"],
                alloy=payload["alloy"],
                n_billets=len(payload["billets"]),
            )
            self._store[payload["alloy"]].extend({"id": billet, "melt_id": melt_id} for billet in payload["billets"])
            return
        lot = payload["lot"]
        lot_id = lot["lot_id"]
        if target == "AGE_IN":
            self._age_in[lot_id] = lot
        elif target == "SAW_IN":
            self._saw_in[lot_id] = lot
        elif target == "PACK_IN":
            self._pack_in[lot_id] = lot
        else:
            ship = self._ship
            ship["packages"] += transport["count"]
            ship["pieces"] += lot["pieces"]
            ship["kg"] += lot["pieces"] * cat.PRODUCTS[lot["product_id"]].kg_per_bar
            ship["lots_total"] += 1
            ship["last_lots"].append(lot_id)
            del ship["last_lots"][:-LAST_LOTS]
        self._emit("lot_arrived", lot_id=lot_id, stage=_STAGE_BY_BUFFER[target])

    # ------------------------------------------------------------------ faults, repair

    def inject_fault(self, machine_id: str, code: str = "", repair_min: float = 0.0) -> dict:
        """IDLE/SETUP/RUN to FAULT. An explicit repair_min is exact; otherwise the repair is sampled and stays hidden."""
        m = self._machines.get(machine_id)
        # The comparison also refuses NaN and infinity, which would poison every estimate that reads this repair.
        if m is None or isinstance(repair_min, bool) or not isinstance(repair_min, (int, float)) or not 0.0 <= repair_min <= MAX_REPAIR_MIN:
            return _result(BAD_COMMAND)
        if m.status == FAULT:
            return _result(ALREADY_FAULTED)
        try:
            spec = cat.fault_spec(machine_id, code)
        except KeyError:
            return _result(BAD_COMMAND)
        self._enter_fault(m, spec, float(repair_min))
        return _result()

    def _enter_fault(self, m: _Machine, spec: cat.FaultSpec, repair_min: float) -> None:
        self._accrue(m)
        self._clock_stop(m)
        status = m.status
        if status == RUN or status == SETUP:
            left = max(m.phase_end - self.t, 0.0)
            m.paused_phase = status
            m.paused_left = left / m.speed if status == RUN else left
        else:
            m.paused_phase = None
            m.paused_left = 0.0
        m.phase_token += 1
        self._rebase_temperature(m)
        m.excursion_target = 0.0
        self._set_status(m, FAULT)
        m.fault_count += 1
        # A new AlarmId cancels the clear of a warning in the slot: a clear only acts on its own id.
        m.alarm_id += 1
        m.alarm_active = True
        m.alarm_code = spec.code
        m.alarm_text = spec.text
        m.alarm_severity = SEVERITY_FAULT
        m.alarm_acked = False
        m.warning_clear_at = None
        estimate = repair_min if repair_min > 0.0 else spec.repair_mean_min
        m.fault = {"code": spec.code, "text": spec.text, "estimate_min": estimate, "started_t": self.t}
        if repair_min > 0.0:
            true_repair = repair_min
        elif self.stochastic:
            true_repair = self._sample_repair(spec.repair_mean_min)
        else:
            true_repair = spec.repair_mean_min
        self._schedule_repair(m, self.t + true_repair)

    def _sample_repair(self, mean: float) -> float:
        mu = math.log(mean) - REPAIR_SIGMA * REPAIR_SIGMA / 2.0
        return self._rng.lognormvariate(mu, REPAIR_SIGMA)

    def _schedule_repair(self, m: _Machine, at: float) -> None:
        m.repair_end = at
        m.repair_token += 1
        self._push(at, _K_REPAIR, m, m.repair_token)

    def repair(self, machine_id: str) -> dict:
        m = self._machines.get(machine_id)
        if m is None:
            return _result(BAD_COMMAND)
        if m.status != FAULT:
            return _result(NOT_FAULTED)
        self._end_fault(m)
        return _result()

    def _end_fault(self, m: _Machine) -> None:
        self._accrue(m)
        m.repair_token += 1
        m.fault = None
        m.alarm_active = False
        op = m.op
        if op is None:
            self._set_status(m, IDLE)
            return
        phase = m.paused_phase
        self._set_status(m, phase)
        self._clock_start(m)
        if phase == RUN:
            m.ideal_rate = m.seg_ideal / (m.seg_dur * m.speed) if m.seg_dur > 0.0 else 0.0
            self._schedule_phase_end(m, self.t + m.paused_left * m.speed)
        else:
            self._schedule_phase_end(m, self.t + m.paused_left)
        m.paused_phase = None
        m.paused_left = 0.0

    def ack_alarm(self, machine_id: str, alarm_id: int) -> dict:
        m = self._machines.get(machine_id)
        if m is None:
            return _result(BAD_COMMAND)
        if not isinstance(alarm_id, int) or alarm_id <= 0 or alarm_id != m.alarm_id:
            return _result(STALE_ALARM)
        m.alarm_acked = True
        return _result()

    # ------------------------------------------------------------------ warnings

    def raise_warning(self, machine_id: str) -> dict:
        """Force the machine's self-clearing warning now; refused in FAULT and while an alarm holds the slot."""
        m = self._machines.get(machine_id)
        if m is None:
            return _result(BAD_COMMAND)
        if m.status == FAULT:
            return _result(MACHINE_FAULT)
        if m.alarm_active:
            return _result(ALARM_ACTIVE)
        self._raise_warning(m)
        return _result()

    def _raise_warning(self, m: _Machine) -> None:
        spec = m.spec.warning
        self._rebase_temperature(m)
        m.excursion_target = spec.delta_k
        m.alarm_id += 1
        m.alarm_active = True
        m.alarm_code = spec.code
        m.alarm_text = spec.text
        m.alarm_severity = SEVERITY_WARNING
        m.alarm_acked = False
        m.warning_clear_at = self.t + spec.duration_min
        self._push(m.warning_clear_at, _K_WARNING_CLEAR, m, m.alarm_id)

    def _clear_warning(self, m: _Machine) -> None:
        self._rebase_temperature(m)
        m.excursion_target = 0.0
        m.alarm_active = False
        m.warning_clear_at = None

    # ------------------------------------------------------------------ random failures: clocks on operating minutes

    def set_random_failures(self, enabled: bool) -> None:
        """Takes effect at once for running machines. A deterministic engine stays free of random faults."""
        enabled = bool(enabled) and self.stochastic
        if enabled == self.random_failures:
            return
        if not enabled:
            for m in self._machines.values():
                self._clock_stop(m)
        self.random_failures = enabled
        if enabled:
            for m in self._machines.values():
                if m.status == SETUP or m.status == RUN:
                    self._clock_start(m)

    def _clock_start(self, m: _Machine) -> None:
        if not self.random_failures or m.clock_since is not None:
            return
        rng = self._rng
        if m.failure_left is None:
            m.failure_left = rng.expovariate(1.0 / m.spec.mtbf_operating_min)
        if m.warning_left is None:
            m.warning_left = rng.expovariate(1.0 / m.spec.warning.mtbf_min)
        m.clock_since = self.t
        m.clock_token += 1
        self._push(self.t + m.failure_left, _K_FAILURE, m, m.clock_token)
        self._push(self.t + m.warning_left, _K_WARNING, m, m.clock_token)

    def _clock_stop(self, m: _Machine) -> None:
        if m.clock_since is None:
            return
        elapsed = self.t - m.clock_since
        m.failure_left = max(m.failure_left - elapsed, 0.0)
        m.warning_left = max(m.warning_left - elapsed, 0.0)
        m.clock_since = None
        m.clock_token += 1

    def _on_random_failure(self, m: _Machine) -> None:
        self._clock_stop(m)
        m.failure_left = None
        self._enter_fault(m, self._rng.choice(m.spec.faults), 0.0)

    def _on_random_warning(self, m: _Machine) -> None:
        elapsed = self.t - m.clock_since
        m.failure_left = max(m.failure_left - elapsed, 0.0)
        m.clock_since = self.t
        m.warning_left = self._rng.expovariate(1.0 / m.spec.warning.mtbf_min)
        self._push(self.t + m.warning_left, _K_WARNING, m, m.clock_token)
        if not m.alarm_active:
            self._raise_warning(m)

    # ------------------------------------------------------------------ scenario hooks

    def schedule_fault(self, machine_id: str, at_min: float, code: str = "", repair_min: float = 0.0) -> None:
        """Inject a fault at a future time. Refused silently then if the machine is already faulted."""
        cat.fault_spec(machine_id, code)
        self._push(max(at_min, self.t), _K_SCHEDULED_FAULT, (machine_id, code, repair_min))

    def schedule_slowdown(self, machine_id: str, factor: float, at_min: float, duration_min: float) -> None:
        """Scale the duration of RUN work by factor during the window. Overlapping windows multiply."""
        if factor <= 0.0 or duration_min < 0.0:
            raise ValueError("slowdown needs factor > 0 and duration >= 0")
        m = self._machines[machine_id]
        begin = max(at_min, self.t)
        self._push(begin, _K_SPEED, (m, factor))
        self._push(begin + duration_min, _K_SPEED, (m, 1.0 / factor))

    def _change_speed(self, m: _Machine, speed: float) -> None:
        self._accrue(m)
        old = m.speed
        m.speed = speed
        if m.status == RUN:
            left = max(m.phase_end - self.t, 0.0) / old
            m.ideal_rate = m.seg_ideal / (m.seg_dur * speed) if m.seg_dur > 0.0 else 0.0
            self._schedule_phase_end(m, self.t + left * speed)

    def set_timeline_recording(self, enabled: bool) -> None:
        if enabled and not self._timeline_on:
            self.timeline.extend(self._status_row(m) for m in self._machines.values())
        self._timeline_on = bool(enabled)

    # ------------------------------------------------------------------ views

    def _segment_fraction_left(self, m: _Machine) -> float:
        if m.seg_dur <= 0.0:
            return 0.0
        left = m.paused_left if m.status == FAULT else (m.phase_end - self.t) / m.speed
        return min(max(left / m.seg_dur, 0.0), 1.0)

    def _phase_of(self, m: _Machine) -> str:
        return m.paused_phase if m.status == FAULT else m.status

    def _progress(self, m: _Machine) -> float:
        op = m.op
        if op is None or self._phase_of(m) == SETUP:
            return 0.0
        fraction = self._segment_fraction_left(m)
        if op["kind"] == cat.PRESS:
            return (op["billets_done"] + 1.0 - fraction) / op["n_billets"]
        return 1.0 - fraction

    def _current_operation(self, m: _Machine) -> str:
        op = m.op
        if op is None:
            return ""
        return json.dumps(
            {
                "op_ids": op["op_ids"],
                "kind": op["kind"],
                "lot_ids": op["lot_ids"],
                "order_ids": op["order_ids"],
                "product_id": op["product_id"],
                "phase": self._phase_of(m),
                "batch_id": op["batch_id"],
            },
            ensure_ascii=False,
        )

    def machine_view(self, machine_id: str) -> dict:
        """One machine, keyed by the OPC UA variable names of PLAN section 4."""
        m = self._machines[machine_id]
        self._accrue(m)
        spec = m.spec
        return {
            "Name": spec.name,
            "Kind": spec.kind,
            "Unit": spec.unit,
            "Status": m.status,
            "Temperature": self._temperature(m),
            "TemperatureSetpoint": m.setpoint,
            "PowerKW": m.power_kw,
            "EnergyKWh": m.energy_kwh,
            "TotalCount": m.total,
            "GoodCount": m.good,
            "ScrapCount": m.scrap,
            "RunMinutes": m.run_min,
            "SetupMinutes": m.setup_min,
            "FaultMinutes": m.fault_min,
            "IdleMinutes": m.idle_min,
            "IdealMinutes": m.ideal_min,
            "SetupClass": m.setup_class,
            "CurrentOperation": self._current_operation(m),
            "Progress": self._progress(m),
            "AlarmId": m.alarm_id,
            "AlarmActive": m.alarm_active,
            "AlarmCode": m.alarm_code,
            "AlarmText": m.alarm_text,
            "AlarmSeverity": m.alarm_severity,
            "EstimatedRepairMin": m.fault["estimate_min"] if m.fault else 0.0,
            "FaultCount": m.fault_count,
        }

    def total_power_kw(self) -> float:
        return sum(m.power_kw for m in self._machines.values())

    def total_energy_kwh(self) -> float:
        total = 0.0
        for m in self._machines.values():
            self._accrue(m)
            total += m.energy_kwh
        return total

    def material_view(self) -> dict:
        """Everything the hall view needs, bounded: SHIP is counts plus the last lots."""
        ship = self._ship
        in_machine = {}
        for m in self._machines.values():
            op = m.op
            if op is None:
                continue
            kind = op["kind"]
            if kind == cat.PRESS:
                billets_left = len(op["billets"])
                baskets = len(op["baskets"])
            elif kind == cat.CAST:
                billets_left = 0
                baskets = 0
            else:
                billets_left = 0
                baskets = sum(len(lot["baskets"]) for lot in op["lots"])
            in_machine[m.id] = {
                "lot_ids": list(op["lot_ids"]),
                "kind": kind,
                "billets_left": billets_left,
                "baskets": baskets,
            }
        return {
            "t": self.t,
            "transports": [
                {
                    "id": tr["id"],
                    "kind": tr["kind"],
                    "lot_id": tr["lot_id"],
                    "count": tr["count"],
                    "from": tr["from"],
                    "to": tr["to"],
                    "t0": tr["t0"],
                    "t1": tr["t1"],
                }
                for tr in self._transports.values()
            ],
            "buffers": {
                "BILLET_STORE": {alloy: len(store) for alloy, store in self._store.items()},
                "AGE_IN": [{"lot_id": lot_id, "baskets": len(lot["baskets"])} for lot_id, lot in self._age_in.items()],
                "SAW_IN": [{"lot_id": lot_id, "baskets": len(lot["baskets"])} for lot_id, lot in self._saw_in.items()],
                "PACK_IN": [{"lot_id": lot_id, "pieces": lot["pieces"]} for lot_id, lot in self._pack_in.items()],
                "SHIP": {
                    "packages": ship["packages"],
                    "pieces": ship["pieces"],
                    "kg": round(ship["kg"], 3),
                    "lots_total": ship["lots_total"],
                    "last_lots": list(ship["last_lots"]),
                },
            },
            "in_machine": in_machine,
        }

    # ------------------------------------------------------------------ snapshot

    def export_snapshot(self) -> dict:
        """JSON-serialisable state at this instant; shares nothing with the engine. Holds nothing sampled."""
        return {
            "version": 1,
            "run_id": self.run_id,
            "seq": self.seq,
            "t": self.t,
            "machines": {m.id: self._machine_snapshot(m) for m in self._machines.values()},
            "billet_store": {alloy: _copy(list(store)) for alloy, store in self._store.items()},
            "buffers": {
                "AGE_IN": _copy(list(self._age_in.values())),
                "SAW_IN": _copy(list(self._saw_in.values())),
                "PACK_IN": _copy(list(self._pack_in.values())),
                "SHIP": _copy(self._ship),
            },
            "transports": _copy(list(self._transports.values())),
            "started_op_ids": sorted(self._started),
            "finished_op_ids": sorted(self._finished),
            "counters": dict(self._counters),
        }

    def _machine_snapshot(self, m: _Machine) -> dict:
        self._accrue(m)
        op = None
        if m.op is not None:
            op = _copy(m.op)
            phase = self._phase_of(m)
            if phase == SETUP:
                setup_left = max(m.paused_left if m.status == FAULT else m.phase_end - self.t, 0.0)
                fraction = 1.0
            else:
                setup_left = 0.0
                fraction = self._segment_fraction_left(m)
            op["phase"] = phase
            op["setup_left_min"] = setup_left
            op["remaining_fraction"] = fraction
            op["progress"] = self._progress(m)
            op["remaining_work_min"] = self._remaining_work(m.op, phase, setup_left, fraction)
        dt = self.t - m.temp_t
        return {
            "id": m.id,
            "status": m.status,
            "setup_class": m.setup_class,
            "op": op,
            "fault": None
            if m.fault is None
            else {
                "code": m.fault["code"],
                "text": m.fault["text"],
                "estimate_min": m.fault["estimate_min"],
                "elapsed_min": self.t - m.fault["started_t"],
            },
            "temperature": self._temperature(m),
            "temp_base": m.setpoint + (m.temp_base - m.setpoint) * math.exp(-dt / m.tau),
            "excursion": m.excursion_target + (m.excursion - m.excursion_target) * math.exp(-dt / EXCURSION_TAU_MIN),
            "excursion_target": m.excursion_target,
            "setpoint": m.setpoint,
            "counters": {
                "run_min": m.run_min,
                "setup_min": m.setup_min,
                "fault_min": m.fault_min,
                "idle_min": m.idle_min,
                "ideal_min": m.ideal_min,
                "energy_kwh": m.energy_kwh,
                "total": m.total,
                "good": m.good,
                "scrap": m.scrap,
                "fault_count": m.fault_count,
            },
            "alarm": {
                "id": m.alarm_id,
                "active": m.alarm_active,
                "code": m.alarm_code,
                "text": m.alarm_text,
                "severity": m.alarm_severity,
                "acked": m.alarm_acked,
                "clear_at": m.warning_clear_at,
            },
        }

    @staticmethod
    def _remaining_work(op: dict, phase: str, setup_left: float, fraction: float) -> float:
        """Setup and run work left at planning rate, excluding any fault pause."""
        planned = op["planned_total_min"]
        if phase == SETUP:
            return setup_left + planned
        if op["kind"] == cat.PRESS:
            return planned * (op["n_billets"] - op["billets_done"] - 1.0 + fraction) / op["n_billets"]
        return planned * fraction

    @classmethod
    def from_snapshot(
        cls, snapshot: dict, seed: int = 1, stochastic: bool = False, random_failures: bool = False
    ) -> "SimEngine":
        """Rebuild every in-flight thing. Hidden futures are re-drawn, or for a deterministic fork set to the plan."""
        snap = json.loads(json.dumps(snapshot))
        self = cls.__new__(cls)
        self._setup(seed, stochastic, random_failures, snap["run_id"])
        self.t = float(snap["t"])
        self.seq = int(snap["seq"])
        self._counters.update(snap["counters"])
        for alloy, billets in snap["billet_store"].items():
            self._store[alloy] = deque(billets)
        buffers = snap["buffers"]
        self._age_in = {lot["lot_id"]: lot for lot in buffers["AGE_IN"]}
        self._saw_in = {lot["lot_id"]: lot for lot in buffers["SAW_IN"]}
        self._pack_in = {lot["lot_id"]: lot for lot in buffers["PACK_IN"]}
        self._ship = buffers["SHIP"]
        for transport in snap["transports"]:
            self._transports[transport["id"]] = transport
            self._push(transport["t1"], _K_TRANSPORT, transport)
        self._started = set(snap["started_op_ids"])
        self._finished = set(snap["finished_op_ids"])
        for machine_id, data in snap["machines"].items():
            self._restore_machine(self._machines[machine_id], data)
        return self

    def _restore_machine(self, m: _Machine, data: dict) -> None:
        t = self.t
        m.acc_t = t
        m.temp_t = t
        m.status = data["status"]
        m.setup_class = data["setup_class"]
        m.temp_base = data["temp_base"]
        m.excursion = data["excursion"]
        m.excursion_target = data["excursion_target"]
        m.setpoint = data["setpoint"]
        m.power_kw = self._power_for(m.spec, m.status)
        counters = data["counters"]
        m.run_min = counters["run_min"]
        m.setup_min = counters["setup_min"]
        m.fault_min = counters["fault_min"]
        m.idle_min = counters["idle_min"]
        m.ideal_min = counters["ideal_min"]
        m.energy_kwh = counters["energy_kwh"]
        m.total = counters["total"]
        m.good = counters["good"]
        m.scrap = counters["scrap"]
        m.fault_count = counters["fault_count"]
        alarm = data["alarm"]
        m.alarm_id = alarm["id"]
        m.alarm_active = alarm["active"]
        m.alarm_code = alarm["code"]
        m.alarm_text = alarm["text"]
        m.alarm_severity = alarm["severity"]
        m.alarm_acked = alarm["acked"]
        m.warning_clear_at = alarm["clear_at"]
        if m.alarm_active and m.warning_clear_at is not None:
            self._push(m.warning_clear_at, _K_WARNING_CLEAR, m, m.alarm_id)
        fault = data["fault"]
        if fault is not None:
            elapsed = fault["elapsed_min"]
            m.fault = {
                "code": fault["code"],
                "text": fault["text"],
                "estimate_min": fault["estimate_min"],
                "started_t": t - elapsed,
            }
            drawn = self._sample_repair(fault["estimate_min"]) if self.stochastic else fault["estimate_min"]
            self._schedule_repair(m, t + max(drawn - elapsed, FAULT_REMAINING_FLOOR_MIN))
        op = data["op"]
        if op is None:
            return
        phase = op.pop("phase")
        setup_left = op.pop("setup_left_min")
        fraction = op.pop("remaining_fraction")
        op.pop("progress")
        op.pop("remaining_work_min")
        m.op = op
        if phase == RUN:
            self._sample_segment(m, op)
            left = m.seg_dur * fraction
        else:
            left = setup_left
        if m.status == FAULT:
            m.paused_phase = phase
            m.paused_left = left
            return
        self._clock_start(m)
        if phase == RUN:
            m.ideal_rate = m.seg_ideal / m.seg_dur if m.seg_dur > 0.0 else 0.0
        self._schedule_phase_end(m, t + left)

    # ------------------------------------------------------------------ planning view

    def planning_state(self) -> PlanningState:
        return planning_state_from_snapshot(self.export_snapshot())


def planning_state_from_snapshot(snapshot: dict) -> PlanningState:
    """The neutral view the builder and the tracker consume. Store, ops and lots all come from this one snapshot."""
    machines = []
    for machine_id, data in snapshot["machines"].items():
        op = data["op"]
        fault = data["fault"]
        machines.append(
            MachineState(
                id=machine_id,
                status=data["status"],
                setup_class=data["setup_class"],
                op_ids=tuple(op["op_ids"]) if op else (),
                kind=op["kind"] if op else None,
                lot_ids=tuple(op["lot_ids"]) if op else (),
                batch_id=op["batch_id"] if op else None,
                started_min=op["started_t"] if op else None,
                remaining_work_min=op["remaining_work_min"] if op else 0.0,
                fault_code=fault["code"] if fault else "",
                fault_estimate_min=fault["estimate_min"] if fault else 0.0,
                fault_elapsed_min=fault["elapsed_min"] if fault else 0.0,
                fault_remaining_min=(
                    max(fault["estimate_min"] - fault["elapsed_min"], FAULT_REMAINING_FLOOR_MIN) if fault else 0.0
                ),
            )
        )
    melts = []
    lots = []
    for transport in snapshot["transports"]:
        payload = transport["payload"]
        if transport["to"] == "BILLET_STORE":
            melts.append(
                MeltInTransit(
                    melt_id=payload["melt_id"],
                    plan_id=payload["plan_id"],
                    alloy=payload["alloy"],
                    n_billets=len(payload["billets"]),
                    arrives_min=transport["t1"],
                )
            )
        else:
            lots.append(
                LotInTransit(
                    lot_id=transport["lot_id"],
                    stage=_STAGE_BY_BUFFER[transport["to"]],
                    arrives_min=transport["t1"],
                )
            )
    buffered: dict[str, str] = {}
    buffers = snapshot["buffers"]
    for buffer_name in ("AGE_IN", "SAW_IN", "PACK_IN"):
        for lot in buffers[buffer_name]:
            buffered[lot["lot_id"]] = _STAGE_BY_BUFFER[buffer_name]
    return PlanningState(
        run_id=snapshot["run_id"],
        seq=snapshot["seq"],
        now=snapshot["t"],
        machines=tuple(machines),
        billet_store={alloy: len(billets) for alloy, billets in snapshot["billet_store"].items()},
        melts_in_transit=tuple(melts),
        lots_in_transit=tuple(lots),
        lots_buffered=buffered,
        started_op_ids=frozenset(snapshot["started_op_ids"]),
        finished_op_ids=frozenset(snapshot["finished_op_ids"]),
    )
