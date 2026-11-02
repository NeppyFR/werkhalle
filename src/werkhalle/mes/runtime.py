"""The MES process: one asyncio loop that owns all state and glues the parts together.

Nothing here blocks the loop. Solvers and the twin run in process pools, disk writes on the database's writer thread. State is only
touched on the loop, so a function without an `await` is atomic, which is what makes accept and activate safe without locks.

The rules that matter, and where they come from:

- The plant is reached only over OPC UA. A machine is FAULT here because a DataChange said so, never because the UI asked.
- `GetStateSnapshot` feeds forks and plans. It never refreshes the shadow.
- Ops are recorded as commanded before the call and cleared only on a definite refusal. After an exception the outcome is unknown; the
  same command is sent again later, and the plant's duplicate protection makes that safe.
- A plan failing validation is never applied. Candidates are validated when created, against the problem they were built for.
- Every background job compares `self._epoch` when it comes back, so nothing computed for an old run lands in a new one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import sys
import time
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
from typing import Any

from ..domain import catalog
from ..domain.catalog import PACK, PRESS
from ..domain.orders import Lot, Order
from ..execution import Dispatcher, Head, Tracker, billet_shortfall, machine_heads, rebase, stalled
from ..execution.dispatcher import WAITING_NO_WORK
from ..scheduling.metrics import compute_metrics
from ..scheduling.model import Plan, Schedule
from ..simcore import planning_state_from_snapshot
from . import jobs
from .alarms import AlarmTracker
from .db import RunDb, open_run_db
from .oee import OeeTracker, WINDOWS
from .opcua_client import PlantClient, PlantNotConnected
from .orderbook import MAX_FINISHED_ROWS, OrderBook, OrderRecord, base_row, effective_finish
from .planning import SOLVER_LABELS, assignments_json, downtimes_json, plan_from_result
from .pools import ManagedPool, REPLAN_WORKERS, manual_workers, twin_workers
from .seed import SUPPLY_BELOW_LOTS, next_supply, seed_orders
from .shadow import FaultEvent, Shadow, TREND_SAMPLES
from .trace import TraceStore
from .twin import RUSH_WEIGHT, api_scenario_items, warm_start_schedule
from .validation import MAX_MINUTES, OrderRuleError, is_minutes, order_fields

logger = logging.getLogger("werkhalle.mes")

VERSION = "1"
TIME_FACTORS = (0, 60, 120, 240, 480)

TICK_S = 0.5
STATE_PERIOD_S = 0.25
STATE_LIMIT_BYTES = 11_900
WS_SEND_TIMEOUT_S = 2.0
WS_QUEUE_SIZE = 256
LOG_RING = 500
EVENT_RING = 4000

STARTUP_LIMIT_S = 3.0
ROLLING_LIMIT_S = 3.0
PIPELINE_OVERHEAD_S = 1.5
TWIN_ALLOWANCE_S = 2.0
WAITING_NO_PLAN = "Kein Plan"
WAITING_DECISION = "Entscheidung offen"
ROLL_RETRY_S = 5.0
STALL_CONFIRM_S = 3.0
STALL_REPROPOSE_S = 60.0
PROPOSAL_TTL_MIN = 480.0
INJECT_MATCH_S = 30.0
UNKNOWN_RETRY_S = 2.0
REFUSAL_BACKOFF_S = 1.0
PROPOSAL_HISTORY = 20
CANDIDATE_SETS_KEPT = 8
TWIN_RUNS_KEPT = 20
TWIN_FRAMES_KEPT = 5
ENERGY_TREND_STEP_MIN = 30.0
ENERGY_TREND_MAX = 120
ALARMS_IN_STATE = 8
MAX_MONTE_CARLO = 200

LOG_LEVELS = {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}
_ALARM_ROW = re.compile(r"^(?P<machine>[A-Za-z0-9_]+):(?P<alarm>\d+)$")
_EMPTY_OEE = {"oee": None, "oee_iso": None, "availability": None, "performance": None, "quality": None}
_EMPTY_MATERIAL = {
    "t": 0.0,
    "transports": [],
    "buffers": {"BILLET_STORE": {}, "AGE_IN": [], "SAW_IN": [], "PACK_IN": [],
                "SHIP": {"packages": 0, "pieces": 0, "kg": 0.0, "lots_total": 0, "last_lots": []}},
    "in_machine": {},
}
_STATE_METRICS = ("makespan", "total_tardiness", "late_orders", "setups", "setup_minutes", "objective")
_STATUS_WORDS = {"accepted": "übernommen", "superseded": "ersetzt", "expired": "abgelaufen", "rejected": "verworfen"}
_SOURCE_WORDS = {"startup": "Startplan", "rolling": "Rollierend", "proposal": "Vorschlag", "manual": "Manuell", "scenario": "Zwilling"}


class ApiError(Exception):
    """Becomes `{"error": code, "message": message, **extra}` with the given HTTP status."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


class WsClient:
    """One browser. Discrete messages queue up, bounded; state is a latest-wins slot, so a slow client cannot build a backlog."""

    def __init__(self) -> None:
        self.latest: str | None = None
        self.queue: deque[str] = deque(maxlen=WS_QUEUE_SIZE)
        self.wake = asyncio.Event()


@dataclass
class Proposal:
    """A re-plan offered to the operator. See PLAN section 8 for the lifecycle."""

    id: str
    trigger: dict
    chain: dict
    based_on_revision: int
    status: str = "computing"
    status_reason: str = ""
    keep: dict | None = None
    candidates: dict[str, dict] = field(default_factory=dict)
    recommended: str | None = None
    beats_keep: bool | None = None
    summary: str = ""
    delta: dict | None = None
    result: dict | None = None  # what the twin stage returned, including the plan data of the candidates
    assumed_faults: frozenset[str] = frozenset()
    open_lot_ids: frozenset[str] = frozenset()
    warm_start: Schedule | None = None
    pending_update: dict | None = None  # what changed since the fork; a background re-solve is on its way
    refresh_gen: int = 0
    replan_from: float | None = None  # a re-solve keeps the fault's real chain, so its duration needs its own start

    @property
    def is_live(self) -> bool:
        return self.status in ("computing", "open")


def _strip_assignments(candidate: dict | None) -> dict | None:
    if candidate is None:
        return None
    return {k: v for k, v in candidate.items() if k != "assignments"}


def _finite(data: Any) -> Any:
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {key: _finite(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_finite(value) for value in data]
    return data


def _compact(data: Any) -> str:
    """Strict JSON, always: one bad float must not take the state or the proposal list down for the rest of the run."""
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except ValueError:
        logger.warning("non-finite number in an answer, sent as null")
        return json.dumps(_finite(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _de(value: float, digits: int = 0) -> str:
    """A number the way the German log texts write it: 1.234,5."""
    return f"{value:,.{digits}f}".replace(",", "\0").replace(".", ",").replace("\0", ".")


class Runtime:
    """Everything the MES knows and does. Built once; `start` and `stop` bracket the process."""

    def __init__(self, *, plant_url: str, data_dir: str, seed: int = 42, solver_limit_s: float = 8.0, auto_supply: bool = True,
                 client: PlantClient | None = None) -> None:
        self.plant_url = plant_url
        self.data_dir = data_dir
        self.seed = seed
        self.solver_limit_s = float(solver_limit_s)
        self.auto_supply = auto_supply

        self.client = client if client is not None else PlantClient(plant_url)
        self.shadow = Shadow(on_fault=self._on_fault)
        self.alarms = AlarmTracker(sim_clock=self.shadow.sim_time_at, on_change=self._on_alarm_row)
        self.oee = OeeTracker()
        self.db: RunDb | None = None
        self.trace: TraceStore | None = None

        self.replan_pool: ManagedPool | None = None
        self.twin_pool: ManagedPool | None = None
        self.manual_pool: ManagedPool | None = None

        self._clients: set[WsClient] = set()
        self._tasks: set[asyncio.Task] = set()
        self._started_loops = False
        self._stopping = False
        self._epoch = 0
        self._dispatch_wake = asyncio.Event()
        self._pipeline_wake = asyncio.Event()
        self._log_ring: deque[dict] = deque(maxlen=LOG_RING)
        self._trend_samples = TREND_SAMPLES
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        self.tracker: Tracker | None = None
        self.plan: Plan | None = None
        self.revision = 0
        self.melt_counter = 1
        self.book = OrderBook()
        self._bootstrapped = False
        self._metrics = None
        self._planned_machine: dict[str, str] = {}
        self._op_machine: dict[str, str] = {}
        self._supply_rng = random.Random(self.seed + 1)

        self._recent_events: deque[dict] = deque(maxlen=EVENT_RING)
        self._unconsumed_events: list[dict] = []
        self._resync_task: asyncio.Task | None = None

        self.dispatcher = Dispatcher(backoff=REFUSAL_BACKOFF_S)
        self._in_flight: dict[str, asyncio.Task] = {}
        self._unknown: dict[str, tuple[tuple[str, ...], float]] = {}

        self._heads: dict[str, Head] = {}
        self._last_status: dict[str, str] = {}
        self.stalled_now = False
        self._stall_since: float | None = None
        self._stall_alarm_id: str | None = None
        self._last_stall_proposal = 0.0
        self._last_material_proposal = 0.0
        self._mes_alarms: dict[str, dict] = {}
        self._mes_alarm_no = 0

        self._rolling: asyncio.Task | None = None
        self._roll_not_before = 0.0

        self._injected: dict[str, float] = {}
        self._fault_chains: dict[str, dict] = {}
        self._revision_base: dict[int, int] = {}
        self._proposals: dict[str, Proposal] = {}
        self._open: Proposal | None = None
        self._history: deque[str] = deque(maxlen=PROPOSAL_HISTORY)
        self._proposal_no = 0
        self._last_pipeline_s: float | None = None

        self._candidate_sets: dict[str, dict] = {}
        self._candidates_no = 0
        self._twin_runs: dict[str, dict] = {}
        self._twin_no = 0

        self._energy_points: list[tuple[float, dict[str, float]]] = []
        self._energy_last: tuple[float, dict[str, float]] | None = None
        self._energy_step = ENERGY_TREND_STEP_MIN

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start everything in the background. The HTTP server is up before the plant is, so `/api/health` can say `ok:false`."""
        self.replan_pool = ManagedPool("replan", REPLAN_WORKERS, on_retry=lambda text: self._log("warn", "plan", text))
        self.twin_pool = ManagedPool("twin", twin_workers(), on_retry=lambda text: self._log("warn", "twin", text))
        self.manual_pool = ManagedPool("manual", manual_workers(), low_priority=True, on_retry=lambda text: self._log("warn", "plan", text))
        self._spawn(self._warm_pools(), name="warm-pools")
        self._spawn(self._connect_and_run(), name="mes-connect")

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.client.stop()
        except Exception:
            logger.exception("stopping the OPC UA client failed")
        for pool in (self.replan_pool, self.twin_pool, self.manual_pool):
            if pool is not None:
                pool.shutdown()
        if self.db is not None:
            db, self.db = self.db, None
            self.trace = None
            await asyncio.to_thread(db.close)

    def _spawn(self, coro: Coroutine, *, name: str = "") -> asyncio.Task:
        task = asyncio.create_task(self._guarded(coro), name=name or None)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _guarded(self, coro: Coroutine) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background task failed")
            self._log("error", "mes", "Interner Fehler in einer Hintergrundaufgabe, siehe Protokolldatei.")

    async def _warm_pools(self) -> None:
        await asyncio.gather(self.replan_pool.warm_up(), self.twin_pool.warm_up(), self.manual_pool.warm_up())

    async def _connect_and_run(self) -> None:
        await self.client.connect()
        await self._open_run(self.client.run_id)
        self.client.set_last_event_seq(self.trace.last_seq)
        self.shadow.attach(self.client, alarms=self.alarms)
        self.client.add_batch_callback(self._on_batch)
        self.client.add_event_callback(self._on_event)
        self.client.add_gap_callback(self._on_gap)
        self.client.add_connection_callback(self._on_connection)
        self.client.add_stale_callback(self._on_stale)
        self.client.add_run_changed_callback(self._on_run_changed)
        await self.client.start()
        if not self._started_loops:
            self._started_loops = True
            self._spawn(self._tick_loop(), name="mes-tick")
            self._spawn(self._dispatch_loop(), name="mes-dispatch")
            self._spawn(self._pipeline_loop(), name="mes-pipeline")
            self._spawn(self._state_loop(), name="mes-state")
        await self._start_run(self._epoch)

    async def _open_run(self, run_id: str) -> None:
        self.db = await asyncio.to_thread(open_run_db, self.data_dir, run_id)
        self.trace = await asyncio.to_thread(TraceStore, self.db)
        pending, self._unconsumed_events = self._unconsumed_events, []
        for event in pending:
            self.trace.consume(event)

    async def _start_run(self, epoch: int) -> None:
        """First snapshot of a run: seed the tracker and the order book. The next tick asks for the start-up plan."""
        snapshot = await self._snapshot_with_retry(epoch)
        if snapshot is None or epoch != self._epoch:
            return
        tracker = Tracker.from_state(planning_state_from_snapshot(snapshot))
        for event in self._recent_events:
            tracker.apply(event)
        self.tracker = tracker
        await self._load_or_seed_orders(snapshot["t"])
        if epoch != self._epoch:
            return
        self._bootstrapped = True
        self._log("info", "mes", f"Lauf {snapshot['run_id']} übernommen, {len(self.book.orders)} Aufträge, {len(self.book.lots)} Lose.")

    async def _snapshot_with_retry(self, epoch: int) -> dict | None:
        while epoch == self._epoch and not self._stopping:
            try:
                return await self.client.get_state_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("snapshot failed: %r", exc)
                await asyncio.sleep(0.5)
        return None

    async def _load_or_seed_orders(self, now: float) -> None:
        """An MES restarted within a run finds its book in the run's database. Only a fresh run gets seeded."""
        db = self.db
        orders, lots, melt_counter, history = await asyncio.to_thread(self._read_book, db)
        if orders:
            by_order: dict[str, list[Lot]] = {}
            for row in lots:
                by_order.setdefault(row["order_id"], []).append(
                    Lot(id=row["id"], order_id=row["order_id"], product_id=row["product_id"], n_billets=row["n_billets"]))
            for row in orders:
                order = Order(row["id"], row["product_id"], row["qty_bars"], row["due_min"], row["weight"], row["created_min"])
                self.book.add(order, by_order.get(row["id"], ()))
            self.melt_counter = max(self.melt_counter, melt_counter)
            for event in history:
                self._note_event_history(event)
            for row in orders:
                if row.get("completed_min") is not None:
                    self.book.mark_finished(self.book.orders[row["id"]], row["completed_min"])
            self._log("info", "mes", f"Auftragsbuch aus der Datenbank wiederhergestellt ({len(orders)} Aufträge).")
            return
        for seeded in seed_orders(self.seed, now=now):
            self._register(self.book.add(seeded.order, seeded.lots))

    @staticmethod
    def _read_book(db: RunDb) -> tuple[list[dict], list[dict], int, list[dict]]:
        history = [json.loads(row["data"]) for row in db.query(
            "SELECT data FROM events WHERE type IN ('op_started','op_finished') ORDER BY seq")]
        return db.load_orders(), db.load_lots(), int(db.get_meta("melt_counter", 1)), history

    def _register(self, record: OrderRecord) -> None:
        """A new order enters the trace store and the database."""
        order = record.order
        self.trace.register_order(order.id, order.product_id, order.qty_bars, order.due_min, order.created_min)
        self.db.save_order(base_row(record))
        for lot in record.lots:
            self.trace.register_lot(lot.id, order.id, lot.product_id, lot.n_billets, order.created_min)
            self.db.save_lot(lot.to_dict())

    # ------------------------------------------------------------------ OPC UA callbacks

    def _on_connection(self, connected: bool, reason: str) -> None:
        if connected:
            self._log("info", "opcua", "Verbindung zur Anlage steht.")
            # What was on its way when the line dropped may be lost. A repeat is refused harmlessly as ALREADY_STARTED.
            self.dispatcher.forget_unconfirmed()
            self._unknown.clear()
            self._dispatch_wake.set()
        else:
            self._log("warn", "opcua", "Verbindung getrennt, Dispatcher pausiert.")
            logger.warning("connection lost: %s", reason)

    def _on_stale(self, stale: bool) -> None:
        self._log("warn" if stale else "info", "opcua", "Heartbeat der Anlage bleibt aus, Daten sind veraltet." if stale else "Heartbeat der Anlage ist zurück.")

    def _on_batch(self, changes: list) -> None:
        """Runs after the shadow applied the same batch."""
        for change in changes:
            if change.name != "Status" or change.machine is None:
                continue
            previous = self._last_status.get(change.machine)
            self._last_status[change.machine] = change.value
            if previous == "FAULT" and change.value != "FAULT":
                self._on_machine_recovered(change.machine)
            self._dispatch_wake.set()

    def _on_event(self, event: dict) -> None:
        self._recent_events.append(event)
        if self.trace is not None:
            self.trace.consume(event)
        else:
            self._unconsumed_events.append(event)
        tracker = self.tracker
        if tracker is not None:
            tracker.apply(event)
            if tracker.resync_needed:
                self._schedule_resync()
        self._note_event_history(event)
        kind = event.get("type")
        if kind == "op_finished" and event.get("kind") == PACK:
            self._on_pack_finished(event)
        elif kind == "op_started" and event.get("kind") == PRESS:
            self._broadcast_lots(event.get("lot_ids") or ())
        self._dispatch_wake.set()

    def _note_event_history(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "op_started":
            machine = event.get("machine")
            for op_id in event.get("op_ids") or ():
                self._op_machine[op_id] = machine
        elif kind == "op_finished" and event.get("kind") == PACK:
            for lot_id in event.get("lot_ids") or ():
                self.book.note_lot_finished(lot_id, event["t"])

    def _on_gap(self, last_seq: int, oldest_seq: int) -> None:
        self._log("error", "opcua", f"Ereignislücke: nach Ereignis {last_seq} kennt die Anlage erst wieder {oldest_seq}. Rückverfolgung ist unvollständig.")
        self._schedule_resync()

    def _schedule_resync(self) -> None:
        if self._resync_task is None or self._resync_task.done():
            self._resync_task = self._spawn(self._resync_tracker(self._epoch), name="mes-resync")

    async def _resync_tracker(self, epoch: int) -> None:
        snapshot = await self._snapshot_with_retry(epoch)
        if snapshot is None or epoch != self._epoch:
            return
        tracker = Tracker.from_state(planning_state_from_snapshot(snapshot))
        for event in self._recent_events:
            tracker.apply(event)
        self.tracker = tracker
        self._log("warn", "mes", "Ausführungsstand aus einem frischen Zustandsabbild neu aufgebaut.")

    async def _on_run_changed(self, old: str | None, new: str) -> None:
        """Hard reset. The client runs this before any data of the new run reaches us."""
        self._epoch += 1
        old_db, self.db = self.db, None
        self.trace = None
        for task in self._in_flight.values():
            task.cancel()
        self._reset_run_state()
        self.oee.reset()
        self._trend_samples = TREND_SAMPLES
        self._log_ring.clear()
        if old_db is not None:
            await asyncio.to_thread(old_db.close)
        await self._open_run(new)
        self._log("warn", "mes", f"Die Anlage wurde neu gestartet (Lauf {new} statt {old}). Das MES beginnt von vorn.")
        self._broadcast("plan", {"revision": 0})
        self._spawn(self._start_run(self._epoch), name="mes-start-run")

    def _on_alarm_row(self, row: dict) -> None:
        if self.db is not None:
            self.db.save_alarm(row)
        self._broadcast("alarm", row)

    # ------------------------------------------------------------------ failure path

    def _on_fault(self, event: FaultEvent) -> None:
        """Runs inside the OPC UA consumer: only decide and hand over, the pipeline does the work."""
        sent = self._injected.pop(event.machine, None)
        if sent is not None and (event.source_ts - sent > INJECT_MATCH_S):
            sent = None
        latency = None if event.baseline else round(event.latency_ms, 1)
        shown = "beim Wiederverbinden erkannt" if event.baseline else f"per OPC-UA-DataChange erkannt ({_de(event.latency_ms)} ms)"
        self._log("warn", "opcua", f"{event.machine}: Störung {event.code} {shown}, geschätzte Reparatur {_de(event.est_repair_min)} min.")
        chain = {"injected_wall_ts": sent, "source_ts": event.source_ts, "detected_wall_ts": event.received_ts, "latency_ms": latency}
        self._fault_chains[event.machine] = chain
        if not self._bootstrapped or self.plan is None:
            return
        if self.shadow.status(event.machine) != "FAULT":
            self._log("info", "plan", f"{event.machine}: Störung schon wieder behoben, keine Neuplanung.")
            return
        factor = self.shadow.time_factor
        pipeline_s = self._pipeline_estimate_s()
        # A fault someone broke on purpose through this MES always gets its answer, however short: the operator asked for the chain.
        if sent is None and event.est_repair_min < pipeline_s * factor / 60.0:
            self._log("info", "plan", f"{event.machine}: {event.code} zu kurz für Neuplanung ({_de(event.est_repair_min)} min geschätzt, Rechnung dauert {_de(pipeline_s, 1)} s bei Faktor {_de(factor)}).")
            return
        if self._open is not None:
            self._mark_pending(self._open, event.machine, event.code, event.text)
            return
        self._new_proposal(trigger=self._fault_trigger(event.machine, event.code, event.text, event.est_repair_min, round(event.t, 2)),
                           chain=chain, supersede_reason="")

    def _pipeline_estimate_s(self) -> float:
        """Own compute time of the last pipeline. It leaves queue wait out, and has a cap, because a slow one must not stick: a skip never measures."""
        cap = self.solver_limit_s + PIPELINE_OVERHEAD_S + TWIN_ALLOWANCE_S
        if self._last_pipeline_s is None:
            return self.solver_limit_s + PIPELINE_OVERHEAD_S
        return min(self._last_pipeline_s, cap)

    def _fault_trigger(self, machine: str, code: str, text: str, estimate: float, t: float) -> dict:
        return {"kind": "fault", "machine": machine, "code": code, "text": text, "est_repair_min": estimate, "t": t,
                "also": self._other_faults(machine)}

    def _other_faults(self, main: str | None, among: frozenset[str] | None = None) -> list[dict]:
        """Every other fault a proposal assumes, for `trigger.also`."""
        now = self.shadow.sim_time_at()
        rows = []
        for machine in self.shadow.machine_ids():
            if machine == main or self.shadow.status(machine) != "FAULT" or (among is not None and machine not in among):
                continue
            values = self.shadow.values(machine)
            since = self.shadow.status_since(machine)
            elapsed = max(now - since, 0.0) if since is not None else 0.0
            rows.append({"machine": machine, "code": values.get("AlarmCode") or "", "text": values.get("AlarmText") or "",
                         "remaining_min": round(max(float(values.get("EstimatedRepairMin") or 0.0) - elapsed, 0.0), 1)})
        return rows

    def _chain_of(self, machine: str | None) -> dict:
        """The first four links of a machine's failure chain, so a re-solve never reads as a fault nobody can account for."""
        chain = self._fault_chains.get(machine or "")
        if chain is not None:
            return dict(chain)
        row = next((r for r in self.alarms.active() if r["machine"] == machine), None)
        if row is not None:
            return {"injected_wall_ts": None, "source_ts": row["source_ts"], "detected_wall_ts": row["detected_wall_ts"],
                    "latency_ms": row["latency_ms"]}
        return {"injected_wall_ts": None, "source_ts": None, "detected_wall_ts": time.time(), "latency_ms": None}

    def _mark_pending(self, proposal: Proposal, machine: str | None, code: str, text: str) -> None:
        """Something changed that the open proposal does not know. It stays acceptable, and the pipeline re-solves in the background."""
        proposal.pending_update = {"machine": machine, "code": code, "text": text}
        proposal.refresh_gen += 1
        self._broadcast("proposal", self._proposal_json(proposal, assignments=False))
        self._pipeline_wake.set()

    def _flag_doomed_accept(self, proposal: Proposal) -> None:
        """New lots can need melts this result does not hold. Say so while the re-solve runs, rather than offer an accept that can only answer 409."""
        note = proposal.pending_update
        solver = proposal.recommended
        candidate = proposal.candidates.get(solver) if solver else None
        if proposal.status != "open" or note is None or not candidate or not candidate.get("valid"):
            return
        try:
            rebased = self._rebase(plan_from_result(proposal.result, solver, source="proposal", source_proposal_id=proposal.id))
        except Exception:
            # Only a hint for the panel, and it runs inside the order path. Accept does the real check.
            logger.warning("accept check for %s failed", proposal.id, exc_info=True)
            return
        blocked = rebased.reason if rebased.plan is None else None
        if blocked == note.get("accept_blocked"):
            return
        rest = {key: value for key, value in note.items() if key != "accept_blocked"}
        proposal.pending_update = {**rest, "accept_blocked": blocked} if blocked else rest
        self._broadcast("proposal", self._proposal_json(proposal, assignments=False))

    def _on_machine_recovered(self, machine: str) -> None:
        self._fault_chains.pop(machine, None)
        proposal = self._open
        if proposal is None or proposal.trigger.get("kind") == "scenario":
            return
        pending = proposal.pending_update
        also = proposal.trigger.get("also") or []
        still_down = [row for row in also if row.get("machine") != machine]
        if len(still_down) != len(also):
            proposal.trigger = {**proposal.trigger, "also": still_down}
        if pending is not None and pending.get("machine") == machine:
            proposal.pending_update = None
            proposal.refresh_gen += 1
        if len(still_down) != len(also) or pending is not proposal.pending_update:
            self._broadcast("proposal", self._proposal_json(proposal, assignments=False))
        # A secondary fault clearing changes little: its downtime in the plan is over early. Only the main trigger makes a proposal pointless.
        if proposal.trigger.get("kind") != "fault" or proposal.trigger.get("machine") != machine:
            return
        self._close_proposal(proposal, "superseded", f"{machine} läuft wieder, der Vorschlag ging von einer Störung aus.")
        self._replan_remaining_faults()

    def _replan_remaining_faults(self) -> None:
        """Another machine is still down: the operator still needs an answer for the longest such fault."""
        limit = self._pipeline_estimate_s() * self.shadow.time_factor / 60.0
        estimates = {
            machine: float(self.shadow.values(machine).get("EstimatedRepairMin") or 0.0)
            for machine in self.shadow.machine_ids()
            if self.shadow.status(machine) == "FAULT"
        }
        wanted = {machine: estimate for machine, estimate in estimates.items() if estimate >= limit}
        if wanted:
            self._propose_for_fault(max(wanted, key=wanted.get))

    def _propose_for_fault(self, machine: str) -> Proposal:
        values = self.shadow.values(machine)
        return self._new_proposal(
            trigger=self._fault_trigger(machine, values.get("AlarmCode") or "", values.get("AlarmText") or "",
                                        float(values.get("EstimatedRepairMin") or 0.0), round(self.shadow.sim_time_at(), 2)),
            chain=self._chain_of(machine),
            supersede_reason="",
            replan_from=time.time(),
        )

    def _new_proposal(self, *, trigger: dict, chain: dict, supersede_reason: str, warm_start: Schedule | None = None,
                      replan_from: float | None = None) -> Proposal:
        if self._open is not None:
            self._close_proposal(self._open, "superseded", supersede_reason or "Ein neuer Vorschlag ersetzt diesen.")
        proposal = self._make_proposal(trigger, chain, warm_start, replan_from)
        self._proposals[proposal.id] = proposal
        self._open = proposal
        self._broadcast("proposal", self._proposal_json(proposal, assignments=False))
        self._pipeline_wake.set()
        return proposal

    def _make_proposal(self, trigger: dict, chain: dict, warm_start: Schedule | None, replan_from: float | None) -> Proposal:
        self._proposal_no += 1
        full_chain = {"injected_wall_ts": None, "source_ts": None, "detected_wall_ts": None, "latency_ms": None,
                      "forked_wall_ts": None, "solved_wall_ts": None, "twin_wall_ts": None, "ready_wall_ts": None,
                      "replan_seconds": None, "snapshot_seq": None, **chain}
        return Proposal(id=f"pr-{self._proposal_no}", trigger={"also": [], **trigger}, chain=full_chain, based_on_revision=self.revision,
                        warm_start=warm_start, replan_from=replan_from)

    def _close_proposal(self, proposal: Proposal, status: str, reason: str) -> None:
        proposal.status = status
        proposal.status_reason = reason
        # A closed proposal is history: its plans and rows go, or twenty of them would hold megabytes for nothing.
        proposal.result = None
        proposal.warm_start = None
        proposal.keep = _strip_assignments(proposal.keep)
        proposal.candidates = {k: _strip_assignments(v) for k, v in proposal.candidates.items()}
        if self._open is proposal:
            self._open = None
        self._history.appendleft(proposal.id)
        self._prune_proposals()
        self._broadcast("proposal", self._proposal_json(proposal, assignments=False))
        self._log("warn" if status in ("superseded", "expired") else "info", "plan", f"Vorschlag {proposal.id}: {_STATUS_WORDS.get(status, status)}. {reason}".strip())

    def _prune_proposals(self) -> None:
        keep = set(self._history)
        if self._open is not None:
            keep.add(self._open.id)
        for proposal_id in [p for p in self._proposals if p not in keep]:
            del self._proposals[proposal_id]

    async def _pipeline_loop(self) -> None:
        """The single in-flight pipeline. A proposal that replaced the running one waits here until that job came back, then re-forks.

        An open proposal with a pending update is re-solved here too, in the background, and replaced when the result is ready.
        """
        while True:
            await self._pipeline_wake.wait()
            self._pipeline_wake.clear()
            proposal = self._open
            if proposal is None:
                continue
            try:
                if proposal.status == "computing":
                    await self._run_pipeline(proposal, self._epoch)
                elif proposal.status == "open" and proposal.pending_update is not None:
                    await self._refresh_proposal(proposal, self._epoch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("pipeline failed")
                if proposal.status == "computing":
                    self._close_proposal(proposal, "expired", f"Die Neuplanung ist fehlgeschlagen: {exc!r}")
                elif proposal.is_live:
                    # The old result stands and can still be accepted. Trying again at once would only spin.
                    proposal.pending_update = None
                    self._log("warn", "plan", f"Vorschlag {proposal.id}: Aktualisierung im Hintergrund fehlgeschlagen.")
            current = self._open
            if current is not None and (current.status == "computing" or current.pending_update is not None):
                self._pipeline_wake.set()

    async def _refresh_proposal(self, old: Proposal, epoch: int) -> None:
        """Re-solve with everything that is down now. Only a finished result replaces `old`, in one step, so the accept button stays."""
        generation = old.refresh_gen
        trigger = {**old.trigger, "t": round(self.shadow.sim_time_at(), 2)}
        if trigger.get("kind") == "fault":
            chain = self._chain_of(trigger.get("machine"))
        else:
            chain = {key: old.chain.get(key) for key in ("injected_wall_ts", "source_ts", "detected_wall_ts", "latency_ms")}
        successor = self._make_proposal(trigger, chain, old.warm_start, time.time())
        landed = await self._run_pipeline(successor, epoch, stands_for=old)
        if self._open is not old:
            return  # accepted, rejected or overtaken meanwhile: this result is never applied
        if not landed:
            old.pending_update = None  # nothing left to plan; left set, the pipeline would ask again at once
            return
        if old.pending_update is None:
            return
        note = old.pending_update
        self._close_proposal(old, "superseded", f"Neu berechnet: {successor.id} berücksichtigt {note['text'] or 'die neue Lage'}.")
        self._proposals[successor.id] = successor
        self._open = successor
        successor.status = "open"
        if old.refresh_gen != generation:
            successor.pending_update = {key: value for key, value in note.items() if key != "accept_blocked"}
            successor.refresh_gen = 1
            self._flag_doomed_accept(successor)
        self._broadcast("proposal", self._proposal_json(successor, assignments=False))
        self._log("info", "plan", f"Vorschlag {successor.id} ersetzt {old.id} nach {_de(successor.chain['replan_seconds'], 1)} s: {successor.summary}")

    async def _run_pipeline(self, proposal: Proposal, epoch: int, *, stands_for: Proposal | None = None) -> bool:
        """Fork, solve, twin. `stands_for` is the open proposal a background re-solve would replace. Returns whether a result landed."""
        anchor = stands_for if stands_for is not None else proposal
        snapshot = await self.client.get_state_snapshot()
        chain = proposal.chain
        chain["forked_wall_ts"] = time.time()
        chain["snapshot_seq"] = snapshot["seq"]
        if epoch != self._epoch or anchor is not self._open:
            return False
        self._log("info", "plan", f"Zustand abgezweigt (Ereignis {snapshot['seq']}), Neuplanung läuft für {proposal.id}.")
        if stands_for is None:
            self._broadcast("proposal", self._proposal_json(proposal, assignments=False))
        open_lots = self.book.open_lots_of_snapshot(snapshot["finished_op_ids"])
        if not open_lots:
            if stands_for is None:
                self._close_proposal(proposal, "expired", "Es gibt keine offenen Lose mehr, die neu zu planen wären.")
            return False
        proposal.assumed_faults = frozenset(m for m, data in snapshot["machines"].items() if data["status"] == "FAULT")
        proposal.open_lot_ids = frozenset(lot.id for lot in open_lots)
        proposal.trigger = {**proposal.trigger, "also": self._other_faults(proposal.trigger.get("machine"), proposal.assumed_faults)}
        pending = anchor.pending_update if stands_for is None else None
        if pending is not None and pending.get("machine") in proposal.assumed_faults:
            proposal.pending_update = None  # the fault came in before this fork, so this very run knows it
        based_on = self.revision
        (candidate_set, solved_ts), solve_seconds = await self.replan_pool.run_timed(
            jobs.solve_stage, snapshot, open_lots, self.book.order_infos(open_lots), self.plan,
            revision=based_on + 1, melt_counter=self.melt_counter, solver_limit_s=self.solver_limit_s,
            seed=self.seed + self._proposal_no, source="proposal", hint=proposal.warm_start,
        )
        if epoch != self._epoch or anchor is not self._open:
            return False
        chain["solved_wall_ts"] = solved_ts
        if stands_for is None:
            self._broadcast("proposal", self._proposal_json(proposal, assignments=False))
        result, twin_seconds = await self.twin_pool.run_timed(
            jobs.twin_stage, snapshot, candidate_set, trigger=proposal.trigger, based_on_revision=based_on, revision=based_on + 1,
        )
        if epoch != self._epoch or anchor is not self._open or (stands_for is None and proposal.status != "computing"):
            return False
        now = time.time()
        chain["twin_wall_ts"] = result["twin_wall_ts"]
        chain["ready_wall_ts"] = now
        if proposal.replan_from is not None:
            started = proposal.replan_from
        else:
            started = chain["detected_wall_ts"] if chain["detected_wall_ts"] is not None else chain["forked_wall_ts"]
        chain["replan_seconds"] = round(now - started, 2)
        self._last_pipeline_s = solve_seconds + twin_seconds + PIPELINE_OVERHEAD_S
        proposal.based_on_revision = based_on
        proposal.keep = result["keep"]
        proposal.candidates = result["candidates"]
        proposal.recommended = result["recommended"]
        proposal.beats_keep = result["beats_keep"]
        proposal.summary = result["summary"]
        proposal.delta = result["delta"]
        proposal.result = result
        if stands_for is not None:
            return True
        proposal.status = "open"
        self._broadcast("proposal", self._proposal_json(proposal, assignments=False))
        self._log("info", "plan", f"Vorschlag {proposal.id} bereit nach {_de(chain['replan_seconds'], 1)} s: {proposal.summary}")
        return True

    def _absorb_melt_counter(self, value: int) -> None:
        # Advances with the plan that gets applied, never backwards: a melt id in an active plan keeps its meaning.
        if value > self.melt_counter:
            self.melt_counter = value
            if self.db is not None:
                self.db.set_meta("melt_counter", value)

    def accept_proposal(self, proposal_id: str, solver: str | None) -> dict:
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ApiError(404, "not_found", "Vorschlag nicht gefunden.")
        if proposal.status == "computing":
            raise ApiError(409, "noch_nicht_fertig", "Der Vorschlag wird noch berechnet.", new_proposal_id=proposal.id)
        if proposal.status != "open":
            raise self._stale_proposal(proposal)
        solver = solver or proposal.recommended
        candidate = proposal.candidates.get(solver)
        if solver == "keep" or candidate is None:
            raise ApiError(422, "invalid", "Dieser Kandidat kann nicht übernommen werden.")
        if not candidate.get("valid"):
            raise ApiError(422, "invalid", "Dieser Kandidat hat die Prüfung nicht bestanden.")
        # The stamp of the candidate: the plan it was computed against and the machine it was computed for. Melts appended since need no
        # check of their own: an id names alloy and number, so an equal id is the same melt, and the rebase counts the billets.
        if self._revision_base.get(proposal.based_on_revision, proposal.based_on_revision) != self._revision_base.get(self.revision, self.revision):
            raise self._stale_proposal(proposal, "Der aktive Plan wurde seit der Berechnung geändert.")
        main = proposal.trigger.get("machine")
        if proposal.trigger.get("kind") == "fault" and main in proposal.assumed_faults and self.shadow.status(main) != "FAULT":
            raise self._stale_proposal(proposal, f"{main} läuft wieder, der Vorschlag ging von einer Störung aus.")
        plan = plan_from_result(proposal.result, solver, source="proposal", source_proposal_id=proposal.id)
        rebased = self._rebase(plan)
        if rebased.plan is None:
            logger.info("accept of %s refused by rebase: %s", proposal.id, rebased.reason)
            raise self._stale_proposal(proposal, rebased.reason)
        self._absorb_melt_counter(proposal.result["melt_counter"])
        label = _SOURCE_WORDS["scenario"] if proposal.trigger.get("kind") == "scenario" else None
        self._activate(rebased.plan, source="proposal", source_proposal_id=proposal.id, label=label)
        revision = self.revision
        pending = proposal.pending_update
        self._close_proposal(proposal, "accepted", f"Übernommen als Revision {revision} ({SOLVER_LABELS.get(solver, solver)}).")
        self._answer_pending_fault(pending)
        return {"revision": revision}

    def _answer_pending_fault(self, pending: dict | None) -> None:
        """The plan just decided on does not know this fault. A new proposal does, and it is only ever applied by the operator."""
        machine = pending.get("machine") if pending is not None else None
        if machine is not None and self._open is None and self.shadow.status(machine) == "FAULT":
            self._propose_for_fault(machine)

    def _stale_proposal(self, proposal: Proposal, reason: str = "Die Anlage hat sich seit der Berechnung weiterbewegt.") -> ApiError:
        """A 409 with the new proposal's id. The re-solve starts here, from a fresh snapshot, so the operator is not left empty-handed."""
        if proposal.is_live:
            self._close_proposal(proposal, "superseded", reason)
        else:
            reason = proposal.status_reason or reason
        new_id = self._open.id if self._open is not None else None
        if new_id is None and proposal.status in ("superseded", "expired"):
            trigger = proposal.trigger
            if trigger.get("kind") != "fault" or self.shadow.status(trigger.get("machine")) == "FAULT":
                chain = proposal.chain
                fresh = self._new_proposal(
                    trigger={**trigger, "t": round(self.shadow.sim_time_at(), 2)},
                    chain={"injected_wall_ts": chain.get("injected_wall_ts"), "source_ts": chain.get("source_ts"),
                           "detected_wall_ts": chain.get("detected_wall_ts") or time.time(), "latency_ms": chain.get("latency_ms")},
                    supersede_reason="",
                    replan_from=time.time(),
                )
                new_id = fresh.id
        return ApiError(409, "veraltet", f"Der Vorschlag ist veraltet: {reason}", new_proposal_id=new_id)

    def reject_proposal(self, proposal_id: str) -> dict:
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ApiError(404, "not_found", "Vorschlag nicht gefunden.")
        if proposal.is_live:
            pending = proposal.pending_update
            self._close_proposal(proposal, "rejected", "Vom Bediener verworfen.")
            self._answer_pending_fault(pending)
        return {"ok": True}

    def _rebase(self, plan: Plan):
        open_lots = self.book.open_lots(self.tracker.op_state)
        return rebase(plan, self.tracker, open_lots, new_orders=self.book.order_infos(open_lots), now=self.shadow.sim_time_at())

    # ------------------------------------------------------------------ plans

    def _activate(self, plan: Plan, *, source: str, source_proposal_id: str | None = None, label: str | None = None,
                  appended: bool = False) -> None:
        """The one place a plan becomes active. Callers have validated it and rebased it onto the current state.

        `appended` means the same plan with new lots at its tails: an open proposal computed against the one before stays acceptable.
        """
        if appended:
            self._revision_base[self.revision + 1] = self._revision_base.get(self.revision, self.revision)
        else:
            self._revision_base.clear()
        self.revision += 1
        plan = replace(plan, revision=self.revision, source=source, source_proposal_id=source_proposal_id,
                       created_min=round(self.shadow.sim_time_at(), 2))
        self.plan = plan
        self._metrics = compute_metrics(plan.problem, plan.schedule)
        self._planned_machine = {a.op_id: a.machine for a in plan.schedule.assignments}
        self._stall_since = None
        self._broadcast("plan", {"revision": self.revision, "solver": plan.schedule.solver, "source": source,
                                 "source_proposal_id": source_proposal_id})
        self._log("info", "plan", f"Plan Revision {self.revision} aktiv ({SOLVER_LABELS.get(plan.schedule.solver, plan.schedule.solver)}, Quelle {label or _SOURCE_WORDS.get(source, source)}).")
        self._dispatch_wake.set()

    async def _roll(self, source: str, epoch: int) -> None:
        """Solve from a fresh snapshot and apply on its own, unless a proposal came up meanwhile: then `_append_to_plan` takes over."""
        try:
            snapshot = await self.client.get_state_snapshot()
            if epoch != self._epoch:
                return
            open_lots = self.book.open_lots_of_snapshot(snapshot["finished_op_ids"])
            if not open_lots:
                return
            limit = min(self.solver_limit_s, STARTUP_LIMIT_S if source == "startup" else ROLLING_LIMIT_S)
            based_on = self.revision
            candidate_set, _ = await self.replan_pool.run(
                jobs.solve_stage, snapshot, open_lots, self.book.order_infos(open_lots), self.plan,
                revision=based_on + 1, melt_counter=self.melt_counter, solver_limit_s=limit,
                seed=self.seed + based_on, source=source,
            )
            if epoch != self._epoch:
                return
            if self._open is not None:
                self._log("info", "plan", "Fortschreibung zurückgestellt, ein Vorschlag ist offen.")
                return
            if based_on != self.revision:
                # Someone applied a plan meanwhile. Its load ids carry the same revision as ours, so this result must not follow it.
                return
            valid = [c for c in candidate_set.candidates.values() if c.valid]
            if not valid:
                self._log("error", "plan", "Fortschreibung: kein Kandidat hat die Prüfung bestanden.")
                self._roll_not_before = time.monotonic() + ROLL_RETRY_S
                return
            best = min(valid, key=lambda c: (c.metrics.objective, c.solver == "keep"))
            rebased = self._rebase(best.plan)
            if rebased.plan is None:
                self._log("info", "plan", f"Fortschreibung veraltet ({rebased.reason}), wird neu berechnet.")
                self._roll_not_before = time.monotonic() + 0.5
                return
            self._absorb_melt_counter(candidate_set.melt_counter)
            self._activate(rebased.plan, source=source)
        except asyncio.CancelledError:
            raise
        except PlantNotConnected:
            self._roll_not_before = time.monotonic() + ROLL_RETRY_S
        except Exception as exc:
            logger.exception("rolling replan failed")
            self._log("error", "plan", f"Fortschreibung fehlgeschlagen: {exc!r}")
            self._roll_not_before = time.monotonic() + ROLL_RETRY_S
        finally:
            if epoch == self._epoch:
                self._rolling = None

    async def _append_to_plan(self, epoch: int) -> None:
        """New lots while a proposal is undecided: at the tails of the active plan, with their melts, so the plant keeps working on them."""
        try:
            snapshot = await self.client.get_state_snapshot()
            if epoch != self._epoch or self.plan is None:
                return
            open_lots = self.book.open_lots_of_snapshot(snapshot["finished_op_ids"])
            if not open_lots:
                return
            based_on = self.revision
            candidate_set = await self.replan_pool.run(
                jobs.keep_stage, snapshot, open_lots, self.book.order_infos(open_lots), self.plan,
                revision=based_on + 1, melt_counter=self.melt_counter,
            )
            if epoch != self._epoch or based_on != self.revision or self._open is None:
                return
            keep = candidate_set.candidates["keep"]
            if not keep.valid:
                self._log("warn", "plan", f"Neue Lose: der fortgeschriebene Plan besteht die Prüfung nicht. {keep.note}".strip())
                self._roll_not_before = time.monotonic() + ROLL_RETRY_S
                return
            rebased = self._rebase(keep.plan)
            if rebased.plan is None:
                self._log("info", "plan", f"Neue Lose: Plan veraltet ({rebased.reason}), wird neu angehängt.")
                self._roll_not_before = time.monotonic() + 0.5
                return
            self._absorb_melt_counter(candidate_set.melt_counter)
            self._activate(rebased.plan, source="rolling", appended=True)
            proposal = self._open
            if proposal.open_lot_ids and proposal.pending_update is None and {lot.id for lot in open_lots} - proposal.open_lot_ids:
                self._mark_pending(proposal, None, "", "neue Lose")
            self._flag_doomed_accept(proposal)
        except asyncio.CancelledError:
            raise
        except PlantNotConnected:
            self._roll_not_before = time.monotonic() + ROLL_RETRY_S
        except Exception as exc:
            logger.exception("appending lots failed")
            self._log("error", "plan", f"Neue Lose konnten nicht angehängt werden: {exc!r}")
            self._roll_not_before = time.monotonic() + ROLL_RETRY_S
        finally:
            if epoch == self._epoch:
                self._rolling = None

    def _maybe_roll(self) -> None:
        if not self._bootstrapped or self._rolling is not None or not self.client.connected:
            return
        if time.monotonic() < self._roll_not_before:
            return
        if self.plan is None:
            if self.book.lots and self._open is None:
                self._rolling = self._spawn(self._roll("startup", self._epoch), name="mes-roll")
            return
        if self.book.missing_from(self.plan.problem.has_op, self.tracker.op_state):
            if self._open is None:
                self._rolling = self._spawn(self._roll("rolling", self._epoch), name="mes-roll")
            else:
                self._rolling = self._spawn(self._append_to_plan(self._epoch), name="mes-append")

    def start_solve(self, body: dict) -> dict:
        self._require_ready()
        self._require_connected()
        limit = _bounded_float(body.get("time_limit_s"), default=self.solver_limit_s, low=0.5, high=120.0, name="time_limit_s")
        seed = _seed_of(body.get("seed"))
        self._candidates_no += 1
        entry_id = f"cs-{self._candidates_no}"
        entry = {
            "id": entry_id, "status": "computing", "created_min": round(self.shadow.sim_time_at(), 2), "time_limit_s": limit,
            "lots": 0, "ops": 0, "candidates": {}, "recommended": None, "note": "",
            "_revision": self.revision, "_result": None,
        }
        self._candidate_sets[entry_id] = entry
        _evict(self._candidate_sets, CANDIDATE_SETS_KEPT)
        self._spawn(self._solve(entry, limit, seed, self._epoch), name=f"mes-{entry_id}")
        return {"id": entry_id, "status": "computing"}

    async def _solve(self, entry: dict, limit: float, seed: int, epoch: int) -> None:
        try:
            snapshot = await self.client.get_state_snapshot()
            open_lots = self.book.open_lots_of_snapshot(snapshot["finished_op_ids"])
            if not open_lots:
                raise ValueError("Es gibt keine offenen Lose.")
            based_on = self.revision
            entry["_revision"] = based_on
            entry["lots"] = len(open_lots)
            candidate_set, _ = await self.manual_pool.run(
                jobs.solve_stage, snapshot, open_lots, self.book.order_infos(open_lots), self.plan,
                revision=based_on + 1, melt_counter=self.melt_counter, solver_limit_s=limit, seed=seed, source="manual",
            )
            result = await self.manual_pool.run(jobs.twin_stage, snapshot, candidate_set, trigger=None, based_on_revision=based_on,
                                                revision=based_on + 1)
            if epoch != self._epoch:
                return
            entry["ops"] = len(candidate_set.problem.ops)
            entry["candidates"] = {**result["candidates"], **({"keep": result["keep"]} if result["keep"] is not None else {})}
            entry["recommended"] = result["recommended"]
            entry["_result"] = result
            entry["status"] = "ready"
            self._log("info", "plan", f"Kandidaten {entry['id']} berechnet, empfohlen: {SOLVER_LABELS.get(result['recommended'], 'keiner')}.")
        except asyncio.CancelledError:
            raise
        except PlantNotConnected as exc:
            entry["status"] = "failed"
            entry["note"] = str(exc)
            self._log("warn", "plan", f"Kandidaten {entry['id']}: {exc}")
        except Exception as exc:
            logger.exception("manual solve failed")
            entry["status"] = "failed"
            entry["note"] = str(exc)
            self._log("error", "plan", f"Kandidaten {entry['id']}: Berechnung fehlgeschlagen: {exc}")

    def candidates_json(self, entry_id: str) -> dict:
        entry = self._candidate_sets.get(entry_id)
        if entry is None:
            raise ApiError(404, "not_found", "Kandidatensatz nicht gefunden.")
        return {**{k: v for k, v in entry.items() if not k.startswith("_")}, "stale": entry["_revision"] != self.revision}

    def apply_candidates(self, body: dict) -> dict:
        self._require_ready()
        entry = self._candidate_sets.get(_optional_text(body, "candidates_id"))
        if entry is None or entry["status"] != "ready":
            raise ApiError(404, "not_found", "Kandidatensatz nicht gefunden oder noch nicht fertig.")
        solver = _optional_text(body, "solver") or entry.get("recommended")
        candidate = entry["candidates"].get(solver)
        if candidate is None or not candidate.get("valid"):
            raise ApiError(422, "invalid", "Dieser Kandidat hat die Prüfung nicht bestanden.")
        if entry["_revision"] != self.revision:
            raise ApiError(409, "veraltet", "Der Plan hat sich seit der Berechnung geändert. Bitte neu berechnen.")
        rebased = self._rebase(plan_from_result(entry["_result"], solver, source="manual"))
        if rebased.plan is None:
            raise ApiError(409, "veraltet", f"Die Anlage hat sich seit der Berechnung weiterbewegt ({rebased.reason}). Bitte neu berechnen.")
        self._absorb_melt_counter(entry["_result"]["melt_counter"])
        self._activate(rebased.plan, source="manual")
        if self._open is not None:
            self._close_proposal(self._open, "superseded", f"Plan wurde von Hand auf Revision {self.revision} geändert.")
        return {"revision": self.revision}

    # ------------------------------------------------------------------ twin

    def start_twin(self, body: dict) -> dict:
        self._require_ready()
        self._require_connected()
        if self.plan is None:
            raise ApiError(409, "kein_plan", "Es gibt noch keinen aktiven Plan.")
        scenario = body.get("scenario") or []
        if not isinstance(scenario, list):
            raise ApiError(400, "bad_scenario", "Das Szenario muss eine Liste sein.")
        try:
            scenario = api_scenario_items(scenario)
        except OrderRuleError as exc:
            raise ApiError(400, "bad_scenario", exc.message, **exc.extra) from exc
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise ApiError(400, "bad_scenario", str(exc)) from exc
        monte_carlo = _bounded_int(body.get("monte_carlo"), default=0, low=0, high=MAX_MONTE_CARLO, name="monte_carlo")
        seed = _seed_of(body.get("seed"))
        limit = _bounded_float(body.get("time_limit_s"), default=min(self.solver_limit_s, 6.0), low=0.5, high=120.0, name="time_limit_s")
        self._twin_no += 1
        run_id = f"tw-{self._twin_no}"
        entry = {
            "id": run_id, "status": "computing", "created_min": round(self.shadow.sim_time_at(), 2), "forked_seq": None,
            "scenario": scenario, "note": "", "variants": {}, "candidates": {}, "recommended": None,
            "delta_b_vs_a": {}, "delta_c_vs_b": {}, "monte_carlo": {"n": monte_carlo}, "applicable": False,
            "_revision": self.revision,
        }
        self._twin_runs[run_id] = entry
        _evict(self._twin_runs, TWIN_RUNS_KEPT)
        self._spawn(self._run_twin(entry, scenario, monte_carlo, seed, limit, self._epoch), name=f"mes-{run_id}")
        return {"id": run_id, "status": "computing"}

    async def _run_twin(self, entry: dict, scenario: list, monte_carlo: int, seed: int, limit: float, epoch: int) -> None:
        try:
            snapshot = await self.client.get_state_snapshot()
            entry["forked_seq"] = snapshot["seq"]
            entry["_revision"] = self.revision
            open_lots = self.book.open_lots_of_snapshot(snapshot["finished_op_ids"])
            result = await self.manual_pool.run(
                jobs.scenario_stage, snapshot, self.plan, open_lots, self.book.order_infos(open_lots), scenario,
                revision=self.revision + 1, melt_counter=self.melt_counter, solver_limit_s=limit, seed=seed, monte_carlo=monte_carlo,
            )
            if epoch != self._epoch:
                return
            entry.update(result)
            entry["status"] = "ready"
            self._log("info", "twin", f"Zwillingslauf {entry['id']} fertig. {result.get('note', '')}".strip())
        except asyncio.CancelledError:
            raise
        except PlantNotConnected as exc:
            entry["status"] = "failed"
            entry["note"] = str(exc)
            self._log("warn", "twin", f"Zwillingslauf {entry['id']}: {exc}")
        except Exception as exc:
            logger.exception("twin run failed")
            entry["status"] = "failed"
            entry["note"] = str(exc)
            self._log("error", "twin", f"Zwillingslauf {entry['id']} fehlgeschlagen: {exc}")
        else:
            self._prune_twin_frames()

    def _prune_twin_frames(self) -> None:
        ready = [e for e in self._twin_runs.values() if e["status"] == "ready"]
        for entry in ready[:-TWIN_FRAMES_KEPT]:
            for variant in entry.get("variants", {}).values():
                if variant is not None:  # variant C is null when no new plan is valid
                    variant["frames"] = []

    def twin_json(self, run_id: str) -> dict:
        entry = self._twin_runs.get(run_id)
        if entry is None:
            raise ApiError(404, "not_found", "Zwillingslauf nicht gefunden.")
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    def apply_twin(self, run_id: str, body: dict) -> dict:
        """The two-step path of PLAN section 8: real orders first, then a normal proposal from live state, without hypothetical downtimes."""
        self._require_ready()
        self._require_connected()
        entry = self._twin_runs.get(run_id)
        if entry is None or entry["status"] != "ready":
            raise ApiError(404, "not_found", "Zwillingslauf nicht gefunden oder noch nicht fertig.")
        if not entry.get("applicable"):
            raise ApiError(409, "veraltet", "Dieser Lauf kann nicht mehr übernommen werden.")
        solver = _optional_text(body, "solver") or entry.get("recommended")
        candidate = entry["candidates"].get(solver)
        if candidate is None or not candidate.get("valid"):
            raise ApiError(422, "invalid", "Dieser Kandidat hat die Prüfung nicht bestanden.")
        rush_items = [item for item in entry["scenario"] if item["type"] == "rush_order"]
        # All or nothing: half a scenario must not be booked, and a refused run must stay applicable.
        for item in rush_items:
            try:
                order_fields(item["product_id"], item["qty_bars"], item["due_in_min"])
            except OrderRuleError as exc:
                raise ApiError(400, exc.code, exc.message, **exc.extra) from exc
        # Applying a scenario twice would book its rush orders twice.
        entry["applicable"] = False
        lot_ids: dict[str, str] = {}
        for item, described in zip(rush_items, entry.get("rush_orders") or ()):
            record = self._create_order(item["product_id"], int(item["qty_bars"]), float(item["due_in_min"]), weight=RUSH_WEIGHT)
            lot_ids.update(zip(described["lot_ids"], (lot.id for lot in record.lots)))
        now = round(self.shadow.sim_time_at(), 2)
        proposal = self._new_proposal(
            trigger={"kind": "scenario", "machine": None, "code": "", "text": "Szenario aus dem Zwilling", "est_repair_min": 0, "t": now},
            chain={"detected_wall_ts": time.time()},
            supersede_reason="Neuer Vorschlag aus dem Zwilling.",
            warm_start=warm_start_schedule(entry, solver, lot_ids),
        )
        self._log("info", "twin", f"Zwillingslauf {run_id}: Szenario wird als Vorschlag {proposal.id} neu gerechnet.")
        return {"proposal_id": proposal.id}

    # ------------------------------------------------------------------ orders

    def create_order(self, body: dict) -> dict:
        self._require_ready()
        try:
            qty, due_in = order_fields(body.get("product_id"), body.get("qty_bars"), body.get("due_in_min"))
        except OrderRuleError as exc:
            raise ApiError(400, exc.code, exc.message, **exc.extra) from exc
        rush = body.get("rush", False)
        if rush is not None and not isinstance(rush, bool):
            raise ApiError(400, "bad_order", "rush muss wahr oder falsch sein.")
        record = self._create_order(body["product_id"], qty, due_in, weight=RUSH_WEIGHT if rush else 1)
        if self._open is not None:
            # An undecided proposal never idles the plant: the lots go onto the active plan now, and the proposal gets refreshed.
            self._mark_pending(self._open, None, "", f"neuer Auftrag {record.order.id}")
            self._flag_doomed_accept(self._open)
            self._roll_not_before = 0.0
            self._maybe_roll()
        return {"order": self._order_row(record), "replan": self._open.id if self._open is not None else None}

    def _create_order(self, product_id: str, qty: int, due_in: float, weight: int) -> OrderRecord:
        """The normal order path, also for what a twin scenario books for real. The caller has run `order_fields`."""
        now = round(self.shadow.sim_time_at(), 2)
        record = self.book.create(product_id, qty, math.ceil(now + due_in), weight, now)
        self._register(record)
        self._log("info", "mes", f"Auftrag {record.order.id} angelegt ({product_id}, {qty} Stangen, {len(record.lots)} Lose).")
        self._broadcast("order", self._order_row(record))
        return record

    def _op_state(self, op_id: str) -> str:
        return self.tracker.op_state(op_id) if self.tracker is not None else "pending"

    def _order_row(self, record: OrderRecord) -> dict:
        planned = None
        if self._metrics is not None:
            planned = self._metrics.order_completion.get(record.order.id)
        return self.book.order_row(
            record, op_state=self._op_state, delivered=self.trace.delivered_pieces(record.order.id) if self.trace else 0,
            planned_completion=planned, op_machine=self._op_machine, planned_machine=self._planned_machine,
            now=round(self.shadow.sim_time_at(), 2),
        )

    def _on_pack_finished(self, event: dict) -> None:
        orders = {self.book.lots[lot_id].order_id for lot_id in event.get("lot_ids") or () if lot_id in self.book.lots}
        for order_id in orders:
            record = self.book.orders[order_id]
            if self.book.all_lots_finished(record) and record.completed_min is None:
                self.book.mark_finished(record, round(event["t"], 2))
                self.db.save_order(base_row(record))
                delivered = self.trace.delivered_pieces(order_id)
                short = delivered < record.order.qty_bars
                self._log("warn" if short else "info", "mes", f"Auftrag {order_id} abgeschlossen: {delivered} von {record.order.qty_bars} Stück" + (" (Unterlieferung)." if short else "."))
            self._broadcast("order", self._order_row(record))

    def _broadcast_lots(self, lot_ids) -> None:
        for order_id in {self.book.lots[lot_id].order_id for lot_id in lot_ids if lot_id in self.book.lots}:
            self._broadcast("order", self._order_row(self.book.orders[order_id]))

    def orders_json(self) -> dict:
        """Newest open first, then what finished last."""
        open_rows = [self._order_row(record) for record in self.book.open_orders()]
        open_rows.sort(key=lambda r: (-r["created_min"], r["id"]))
        return {"orders": open_rows + [self._order_row(record) for record in self.book.finished_orders()]}

    def _auto_supply(self) -> None:
        if not self.auto_supply or not self._bootstrapped or self.tracker is None:
            return
        # An undecided proposal must not idle the plant: `_maybe_roll` puts these lots on the active plan and the proposal gets refreshed.
        open_lots = self.book.open_lots(self.tracker.op_state)
        if len(open_lots) >= SUPPLY_BELOW_LOTS:
            return
        created = next_supply(self._supply_rng, now=round(self.shadow.sim_time_at(), 2), open_lots=open_lots,
                              next_order_no=self.book.next_order_no, next_lot_no=self.book.next_lot_no)
        for seeded in created:
            record = self.book.add(seeded.order, seeded.lots)
            self._register(record)
            self._broadcast("order", self._order_row(record))
        if created:
            self._log("info", "mes", f"Auftragsnachschub: {len(created)} neue Aufträge.")

    # ------------------------------------------------------------------ execution

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._dispatch_wake.wait(), TICK_S)
            except asyncio.TimeoutError:
                pass
            self._dispatch_wake.clear()
            try:
                self._dispatch_once()
            except Exception:
                logger.exception("dispatch failed")
            # Events come in bursts. A short pause lets a burst finish before next_commands looks at the state again.
            await asyncio.sleep(0.02)

    def _dispatch_paused(self) -> bool:
        return self.plan is None or self.tracker is None or not self.client.connected or self.client.stale

    def _dispatch_once(self) -> None:
        if self._dispatch_paused():
            return
        self.dispatcher.run_id = self.client.run_id or ""
        now = time.monotonic()
        for machine, (op_ids, retry_at) in list(self._unknown.items()):
            if now >= retry_at:
                # Lets next_commands send it again. The plant's duplicate protection answers ALREADY_STARTED if the first call got through.
                del self._unknown[machine]
                self.dispatcher.commanded.difference_update(op_ids)
        views = {
            machine: self.shadow.values(machine)
            for machine in self.shadow.machine_ids()
            if machine not in self._in_flight and machine not in self._unknown
        }
        for command in self.dispatcher.poll(self.plan, self.tracker, views, now):
            machine = command["machine"]
            self._in_flight[machine] = self._spawn(self._send_command(machine, command, self._epoch), name=f"cmd-{machine}")

    async def _send_command(self, machine: str, command: dict, epoch: int) -> None:
        op_ids = tuple(command["op_ids"])
        try:
            result = await self.client.start_operation(machine, command)
        except asyncio.CancelledError:
            raise
        except PlantNotConnected:
            if epoch == self._epoch:
                self.dispatcher.commanded.difference_update(op_ids)  # the plant never saw this one
            return
        except Exception as exc:
            if epoch == self._epoch:
                logger.warning("StartOperation %s on %s: outcome unknown: %r", op_ids, machine, exc)
                self._log("warn", "opcua", f"{machine}: Antwort auf StartOperation fehlt, wird wiederholt.")
                self._unknown[machine] = (op_ids, time.monotonic() + UNKNOWN_RETRY_S)
            return
        finally:
            if epoch == self._epoch and self._in_flight.get(machine) is asyncio.current_task():
                del self._in_flight[machine]
        if epoch != self._epoch:
            return
        self.dispatcher.on_result(self.plan, self.tracker, command, result, time.monotonic())
        if not result.get("ok"):
            self._on_refusal(machine, command, op_ids, result.get("reason") or "")
        self._dispatch_wake.set()

    def _on_refusal(self, machine: str, command: dict, op_ids: tuple[str, ...], reason: str) -> None:
        if reason == "ALREADY_STARTED":
            return
        self._log("warn" if reason in ("NOT_ELIGIBLE", "BAD_COMMAND") else "info", "opcua", f"{machine}: Anlage lehnt {', '.join(op_ids)} ab ({reason}).")
        if self.dispatcher.replan_needed:
            self.dispatcher.replan_needed = False
            self._propose_for_material(machine, command)

    def _propose_for_material(self, machine: str, command: dict) -> None:
        """A NO_MATERIAL refusal with no planned supply for the alloy: PLAN section 7 makes that a re-plan trigger."""
        if self._open is not None or time.monotonic() - self._last_material_proposal < STALL_REPROPOSE_S:
            return
        self._last_material_proposal = time.monotonic()
        shortfall = billet_shortfall(self.plan, self.tracker)
        text = ", ".join(f"{alloy}: {count}" for alloy, count in sorted(shortfall.items()))
        self._log("warn", "plan", f"{machine}: Bolzen fehlen ({text}) und keine Schmelze ist geplant. Neuplanung wird vorgeschlagen.")
        self._new_proposal(
            trigger={"kind": "no_material", "machine": machine, "code": "", "text": f"Bolzen fehlen ({text})",
                     "est_repair_min": 0, "t": round(self.shadow.sim_time_at(), 2)},
            chain={"detected_wall_ts": time.time()},
            supersede_reason="",
        )

    # ------------------------------------------------------------------ periodic work

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self._tick()
            except Exception:
                logger.exception("tick failed")

    def _tick(self) -> None:
        if self.tracker is None:
            return
        self._update_waits()
        self._sample_oee()
        self._sample_energy()
        self._check_stall()
        self._expire_proposal()
        self._auto_supply()
        self._maybe_roll()

    def _update_waits(self) -> None:
        views = {machine: self.shadow.values(machine) for machine in self.shadow.machine_ids()}
        heads = machine_heads(self.plan, self.tracker, views, self.dispatcher.commanded)
        # Idle with lots no plan knows yet is a planning loss, not a lack of orders.
        if self.plan is None:
            unplanned = self._bootstrapped and self.book.lots_open > 0
        else:
            unplanned = bool(self.book.missing_from(self.plan.problem.has_op, self.tracker.op_state))
        if unplanned:
            heads = {machine: replace(head, waiting_on=WAITING_NO_PLAN) if head.waiting_on == WAITING_NO_WORK else head
                     for machine, head in heads.items()}
        elif self._open is not None and self.book.lots_open > 0:
            # The work exists, but the active plan holds it elsewhere until the operator decides: not a lack of orders.
            heads = {machine: replace(head, waiting_on=WAITING_DECISION) if head.waiting_on == WAITING_NO_WORK else head
                     for machine, head in heads.items()}
        self._heads = heads

    def _sample_oee(self) -> None:
        t = self.shadow.sim_time_at()
        for machine in self.shadow.machine_ids():
            head = self._heads.get(machine)
            self.oee.update(machine, t, self.shadow.counters(machine), status=self.shadow.status(machine) or "",
                            waiting_on=head.waiting_on if head is not None else None)

    def _sample_energy(self) -> None:
        t = self.shadow.sim_time_at()
        energies = {m: float(self.shadow.values(m).get("EnergyKWh") or 0.0) for m in self.shadow.machine_ids()}
        if self._energy_last is None:
            self._energy_last = (t, energies)
            return
        t0, previous = self._energy_last
        if t - t0 < self._energy_step:
            return
        # Mean power over the step, from the energy counter: instant power at the sampling moment would alias with the machine cycles.
        kw = {m: round(max(energies[m] - previous.get(m, 0.0), 0.0) * 60.0 / (t - t0), 1) for m in energies}
        self._energy_points.append((round(t, 1), kw))
        self._energy_last = (t, energies)
        if len(self._energy_points) > ENERGY_TREND_MAX:
            self._energy_points = self._energy_points[1::2]
            self._energy_step *= 2

    def _check_stall(self) -> None:
        if self.plan is None or self._dispatch_paused() or self.shadow.time_factor <= 0.0:
            self._stall_since = None
            return
        machines = {m: self.shadow.values(m) for m in self.shadow.machine_ids()}
        transports = bool(self.shadow.material.get("transports"))
        busy_here = self._in_flight or self._unknown or self.dispatcher.commanded
        if busy_here or not stalled(self.plan, self.tracker, machines, transports):
            self._stall_since = None
            if self.stalled_now:
                self.stalled_now = False
                self._clear_mes_alarm()
            return
        now = time.monotonic()
        if self._stall_since is None:
            self._stall_since = now
            return
        if now - self._stall_since < STALL_CONFIRM_S or self.stalled_now:
            if self.stalled_now and self._open is None and now - self._last_stall_proposal > STALL_REPROPOSE_S:
                self._propose_for_stall()
            return
        self.stalled_now = True
        self._raise_mes_alarm("MES-STALL", "Plan steht still: nichts läuft, nichts unterwegs, aber der Plan ist nicht fertig.")
        self._propose_for_stall()

    def _propose_for_stall(self) -> None:
        self._last_stall_proposal = time.monotonic()
        if self._open is not None:
            return
        self._log("error", "plan", "Der Plan steht still. Eine Neuplanung wird vorgeschlagen.")
        self._new_proposal(
            trigger={"kind": "stall", "machine": None, "code": "MES-STALL", "text": "Plan steht still", "est_repair_min": 0,
                     "t": round(self.shadow.sim_time_at(), 2)},
            chain={"detected_wall_ts": time.time()},
            supersede_reason="",
        )

    def _raise_mes_alarm(self, code: str, text: str) -> None:
        self._mes_alarm_no += 1
        now = time.time()
        row = {
            "id": f"MES:{self._mes_alarm_no}", "machine": "MES", "alarm_id": self._mes_alarm_no, "code": code, "text": text,
            "severity": 900, "t": round(self.shadow.sim_time_at(), 3), "source_ts": now, "detected_wall_ts": now, "latency_ms": None,
            "active": True, "acked": False, "cleared_t": None, "cleared_ts": None,
        }
        self._mes_alarms[row["id"]] = row
        self._stall_alarm_id = row["id"]
        self._on_alarm_row(row)

    def _clear_mes_alarm(self) -> None:
        row = self._mes_alarms.get(self._stall_alarm_id or "")
        if row is None or not row["active"]:
            return
        row["active"] = False
        row["cleared_t"] = round(self.shadow.sim_time_at(), 3)
        row["cleared_ts"] = time.time()
        self._on_alarm_row(row)

    def _expire_proposal(self) -> None:
        proposal = self._open
        if proposal is None or proposal.status != "open":
            return
        now = self.shadow.sim_time_at()
        trigger = proposal.trigger
        if now - float(trigger.get("t") or 0.0) <= PROPOSAL_TTL_MIN:
            return
        if trigger.get("kind") == "fault" and self.shadow.status(trigger.get("machine")) == "FAULT":
            # The machine is still down, so the operator still needs an answer: re-solve against today's state, and the clock starts over.
            proposal.trigger = {**trigger, "t": round(now, 2)}
            if proposal.pending_update is None:
                self._mark_pending(proposal, None, "", "die aktuelle Lage")
            return
        self._close_proposal(proposal, "expired", f"Nicht binnen {_de(PROPOSAL_TTL_MIN)} min entschieden.")

    # ------------------------------------------------------------------ REST actions on the plant

    async def inject_fault(self, machine: str, body: dict) -> dict:
        """Sends InjectFault and nothing else. The machine turns FAULT here only through the DataChange."""
        self._require_machine(machine)
        code = body.get("code") or ""
        repair = body.get("repair_min", 0)
        if not isinstance(code, str):
            raise ApiError(400, "BAD_COMMAND", "Der Störcode muss ein Text sein.")
        if not is_minutes(repair):
            raise ApiError(400, "BAD_COMMAND", f"Die Reparaturdauer muss eine Zahl von 0 bis {_de(MAX_MINUTES)} Minuten sein.")
        try:
            catalog.fault_spec(machine, code or None)
        except KeyError:
            raise ApiError(400, "BAD_COMMAND", f"{machine} kennt den Störcode {code} nicht.") from None
        sent = time.time()
        # Before the call: the DataChange can overtake the method's reply. A repeat, a double click for instance, must not touch the
        # first call's entry, and its refusal must not erase it.
        earlier = self._injected.get(machine)
        ours = earlier is None or sent - earlier > INJECT_MATCH_S
        if ours:
            self._injected[machine] = sent
        try:
            result = await self.client.inject_fault(machine, code, float(repair))
        except Exception:
            if ours:
                self._injected.pop(machine, None)
            raise
        if not result.get("ok"):
            if ours:
                self._injected.pop(machine, None)
            reason = result.get("reason") or "refused"
            if reason == "ALREADY_FAULTED":
                raise ApiError(409, reason, f"{machine} ist bereits gestört.")
            raise ApiError(400, reason, f"Die Anlage lehnt die Störung ab ({reason}).")
        self._log("info", "mes", f"{machine}: InjectFault per OPC UA gesendet ({code or 'Standardcode'}).")
        return {"ok": True, "sent_wall_ts": sent}

    async def repair(self, machine: str) -> dict:
        self._require_machine(machine)
        result = await self.client.repair(machine)
        if not result.get("ok"):
            reason = result.get("reason") or "refused"
            if reason == "NOT_FAULTED":
                raise ApiError(409, reason, f"{machine} ist nicht gestört.")
            raise ApiError(400, reason, f"Die Anlage lehnt die Reparatur ab ({reason}).")
        self._log("info", "mes", f"{machine}: Repair per OPC UA gesendet.")
        return {"ok": True}

    async def ack_alarm(self, row_id: str) -> dict:
        row = self._mes_alarms.get(row_id)
        if row is not None:
            row["acked"] = True
            self._on_alarm_row(row)
            return {"ok": True}
        match = _ALARM_ROW.match(row_id)
        if match is None or self.alarms.get(row_id) is None:
            raise ApiError(404, "not_found", "Alarm nicht gefunden.")
        result = await self.client.ack_alarm(match["machine"], int(match["alarm"]))
        if not result.get("ok"):
            reason = result.get("reason") or "refused"
            raise ApiError(409, reason, "Die Anlage kennt diesen Alarm nicht mehr als aktuell." if reason == "STALE_ALARM" else f"Die Anlage lehnt ab ({reason}).")
        updated = self.alarms.mark_acked(row_id)
        if updated is not None:
            self._on_alarm_row(updated)
        return {"ok": True}

    async def apply_settings(self, body: dict) -> dict:
        factor = body.get("time_factor")
        if "time_factor" in body and (isinstance(factor, bool) or not isinstance(factor, (int, float)) or factor not in TIME_FACTORS):
            raise ApiError(400, "bad_time_factor", f"Ungültiger Zeitfaktor {factor}, erlaubt sind {list(TIME_FACTORS)}.")
        for key in ("random_failures", "auto_supply"):
            if key in body and not isinstance(body[key], bool):
                raise ApiError(400, "bad_request", f"{key} muss wahr oder falsch sein.")
        if "time_factor" in body:
            await self.client.set_time_factor(float(factor))
            self._log("info", "mes", f"Zeitfaktor {factor} gesetzt.")
        if "random_failures" in body:
            await self.client.set_random_failures(body["random_failures"])
            self._log("info", "mes", f"Zufallsstörungen {'an' if body['random_failures'] else 'aus'}.")
        if "auto_supply" in body:
            self.auto_supply = body["auto_supply"]
            self._log("info", "mes", f"Auftragsnachschub {'an' if self.auto_supply else 'aus'}.")
        return {"ok": True}

    def _require_machine(self, machine: str) -> None:
        if machine not in catalog.MACHINES:
            raise ApiError(404, "not_found", "Maschine nicht gefunden.")

    def _require_ready(self) -> None:
        if not self._bootstrapped:
            raise ApiError(503, "not_ready", "Das MES ist noch nicht bereit.")

    def _require_connected(self) -> None:
        if not self.client.connected:
            raise PlantNotConnected("Die Verbindung zur Anlage ist getrennt.")

    def require_trace(self) -> TraceStore:
        trace = self.trace
        if trace is None:
            raise ApiError(503, "not_ready", "Das MES ist noch nicht bereit.")
        return trace

    # ------------------------------------------------------------------ logging and WebSocket

    def _log(self, level: str, source: str, text: str) -> None:
        row = {"t": round(self.shadow.sim_time_at(), 2), "wall_ts": time.time(), "level": level, "source": source, "text": text}
        self._log_ring.appendleft(row)
        logger.log(LOG_LEVELS.get(level, logging.INFO), "[%s] %s", source, text)
        if self.db is not None:
            self.db.append_log(row)
        self._broadcast("log", row)

    def _broadcast(self, kind: str, data: Any) -> None:
        if not self._clients:
            return
        message = _compact({"type": kind, "data": data})
        for client in self._clients:
            client.queue.append(message)
            client.wake.set()

    def add_client(self, client: WsClient) -> None:
        self._clients.add(client)

    def remove_client(self, client: WsClient) -> None:
        self._clients.discard(client)

    async def _state_loop(self) -> None:
        while True:
            await asyncio.sleep(STATE_PERIOD_S)
            if not self._clients:
                continue
            try:
                message = self.state_message()
            except Exception:
                logger.exception("building the state failed")
                continue
            for client in self._clients:
                client.latest = message
                client.wake.set()

    # ------------------------------------------------------------------ JSON views

    def state_message(self) -> str:
        return f'{{"type":"state","data":{self.state_text()}}}'

    def state_text(self) -> str:
        """The state as compact JSON, never above the size limit: the trend length gives way when the rest takes up the room."""
        samples = self._trend_samples
        text = _compact(self.state_json(samples))
        size = len(text.encode("utf-8"))
        tries = 0
        while size > STATE_LIMIT_BYTES and samples > 0 and tries < 4:
            # Two trends per machine, at most 4 bytes per whole number plus comma.
            excess = size - STATE_LIMIT_BYTES
            cut = max(5, int(excess / (2 * 7 * 4)) + 2)
            samples = max(samples - cut, 0)
            text = _compact(self.state_json(samples))
            size = len(text.encode("utf-8"))
            tries += 1
        if size > STATE_LIMIT_BYTES:
            logger.warning("state message is %d bytes even without trends", size)
        elif size < STATE_LIMIT_BYTES - 1500 and samples < TREND_SAMPLES:
            samples = min(TREND_SAMPLES, samples + 10)
        self._trend_samples = samples
        return text

    def state_json(self, trend_samples: int = TREND_SAMPLES) -> dict:
        machines = self._in_browse_order(self.shadow.to_state_machines(trend_samples=trend_samples))
        for row in machines:
            head = self._heads.get(row["id"])
            row["next_op"] = head.next_op if head is not None else None
            row["waiting_on"] = head.waiting_on if head is not None else None
            row["oee"] = self.oee.state_summary(row["id"]) or dict(_EMPTY_OEE)
        sim = self.shadow.sim_state()
        sim["auto_supply"] = self.auto_supply
        material = dict(self.shadow.material) or dict(_EMPTY_MATERIAL)
        material["t"] = sim["t"]
        plan = None
        if self.plan is not None:
            metrics = self._metrics.to_dict()
            plan = {"revision": self.plan.revision, "solver": self.plan.schedule.solver, "source": self.plan.source,
                    "created_min": self.plan.created_min, "metrics": {k: metrics[k] for k in _STATE_METRICS}}
        return {
            "run_id": self.client.run_id,
            "connected": self.client.connected,
            "stale": self.client.stale,
            "sim": sim,
            "machines": machines,
            "material": material,
            "kpi": self._kpi(material),
            "plan": plan,
            "proposal": self._proposal_summary(),
            "alarms_active": self._active_alarms()[:ALARMS_IN_STATE],
            "stalled": self.stalled_now,
            "counters": self.client.counters(),
        }

    def _in_browse_order(self, rows: list[dict]) -> list[dict]:
        # The shadow lists machines as their first values arrived, which is not a promise the UI can lean on after a reconnect.
        order = {machine: index for index, machine in enumerate(self.client.machine_ids())}
        return sorted(rows, key=lambda row: order.get(row["id"], len(order)))

    def _kpi(self, material: dict) -> dict:
        totals = self.shadow.plant_totals()
        ship = material.get("buffers", {}).get("SHIP") or {}
        kg = float(ship.get("kg") or 0.0)
        energy = totals["energy_kwh"]
        orders_late = 0
        completion = self._metrics.order_completion if self._metrics is not None else {}
        open_orders = self.book.open_orders()
        now = round(self.shadow.sim_time_at(), 2)
        for record in open_orders:
            if effective_finish(record, completion.get(record.order.id), now) > record.order.due_min:
                orders_late += 1
        oee_plant = self.oee.plant("8h").oee_net
        return {
            "oee_plant": None if oee_plant is None else round(oee_plant, 4),
            "power_kw": totals["power_kw"],
            "energy_kwh": energy,
            "kwh_per_kg": round(energy / kg, 3) if kg > 0 else None,
            "packages": int(ship.get("packages") or 0),
            "kg_shipped": kg,
            "orders_open": len(open_orders),
            "orders_late": orders_late,
            "orders_late_total": orders_late + self.book.finished_late,
            "lots_open": self.book.lots_open,
            "lots_done": self.book.lots_done,
        }

    def _active_alarms(self) -> list[dict]:
        rows = self.alarms.active() + [dict(r) for r in self._mes_alarms.values() if r["active"]]
        rows.sort(key=lambda r: r["source_ts"], reverse=True)
        return rows

    def _proposal_summary(self) -> dict | None:
        proposal = self._open
        if proposal is None:
            return None
        return {"id": proposal.id, "status": proposal.status, "trigger": proposal.trigger, "recommended": proposal.recommended,
                "age_min": self._age_min(proposal), "pending_update": proposal.pending_update}

    def _age_min(self, proposal: Proposal) -> float:
        return round(max(self.shadow.sim_time_at() - float(proposal.trigger.get("t") or 0.0), 0.0), 1)

    def _proposal_json(self, proposal: Proposal, *, assignments: bool) -> dict:
        strip = (lambda c: c) if assignments else _strip_assignments
        return {
            "id": proposal.id,
            "status": proposal.status,
            "status_reason": proposal.status_reason,
            "pending_update": proposal.pending_update,
            "trigger": proposal.trigger,
            "chain": proposal.chain,
            "based_on_revision": proposal.based_on_revision,
            "age_min": self._age_min(proposal),
            "keep": strip(proposal.keep),
            "candidates": {k: strip(v) for k, v in proposal.candidates.items()},
            "recommended": proposal.recommended,
            "beats_keep": proposal.beats_keep,
            "summary": proposal.summary,
            "delta": proposal.delta,
        }

    def proposals_json(self) -> dict:
        history = [self._proposal_json(self._proposals[p], assignments=False) for p in self._history if p in self._proposals]
        current = self._proposal_json(self._open, assignments=True) if self._open is not None else None
        return {"open": current, "history": history}

    def plan_json(self) -> dict:
        now = round(self.shadow.sim_time_at(), 2)
        plan = self.plan
        if plan is None:
            return {"revision": 0, "solver": None, "source": None, "source_proposal_id": None, "created_min": None, "now": now,
                    "metrics": None, "assignments": [], "downtimes": [], "orders": []}
        return {
            "revision": plan.revision,
            "solver": plan.schedule.solver,
            "source": plan.source,
            "source_proposal_id": plan.source_proposal_id,
            "created_min": plan.created_min,
            "now": now,
            "metrics": self._metrics.to_dict(),
            "assignments": assignments_json(plan, self._op_state),
            "downtimes": downtimes_json(plan.problem),
            "orders": [{"id": o.id, "due": o.due, "weight": o.weight} for o in plan.problem.orders],
        }

    def catalog_json(self) -> dict:
        return {
            "machines": [{"id": m.id, "name": m.name, "kind": m.kind, "unit": m.unit, "capacity": m.capacity,
                          "faults": [{"code": f.code, "text": f.text, "repair_mean_min": f.repair_mean_min} for f in m.faults]}
                         for m in catalog.MACHINES.values()],
            "products": [{"id": p.id, "name": p.name, "alloy": p.alloy, "recipe": p.recipe, "die": p.die,
                          "presses": list(p.press_min_per_billet), "kg_per_bar": p.kg_per_bar, "bars_per_billet": p.bars_per_billet,
                          "min_lead_min": catalog.min_lead_min(p)} for p in catalog.PRODUCTS.values()],
            "recipes": [{"id": r.id, "temp_c": r.temp_c, "soak_min": r.soak_min} for r in catalog.RECIPES.values()],
            "time_factors": list(TIME_FACTORS),
        }

    def alarms_json(self, limit: int) -> dict:
        limit = max(0, min(limit, 2000))
        rows = self.alarms.rows(limit) + [dict(r) for r in self._mes_alarms.values()]
        rows.sort(key=lambda r: r["source_ts"], reverse=True)
        return {"alarms": rows[:limit]}

    def log_json(self, limit: int) -> dict:
        return {"entries": list(self._log_ring)[:max(0, limit)]}

    def oee_json(self, window: str) -> dict:
        if window not in WINDOWS:
            raise ApiError(400, "bad_window", f"Unbekanntes Fenster {window}, erlaubt sind start und 8h.")
        return self.oee.response(window, self._in_browse_order(self.shadow.machines_meta()))

    def energy_json(self) -> dict:
        totals = self.shadow.plant_totals()
        material = self.shadow.material
        kg = float(((material.get("buffers") or {}).get("SHIP") or {}).get("kg") or 0.0)
        machines = []
        energies = {m: float(self.shadow.values(m).get("EnergyKWh") or 0.0) for m in self.shadow.machine_ids()}
        total = sum(energies.values())
        for meta in self._in_browse_order(self.shadow.machines_meta()):
            values = self.shadow.values(meta["id"])
            energy = energies[meta["id"]]
            count = int(values.get("TotalCount") or 0)
            machines.append({
                "id": meta["id"], "name": meta["name"], "energy_kwh": round(energy, 1),
                "power_kw": round(float(values.get("PowerKW") or 0.0), 1),
                "share": round(energy / total, 3) if total > 0 else 0.0,
                "kwh_per_unit": round(energy / count, 3) if count else None, "unit": meta["unit"],
            })
        points = self._energy_points
        kw: dict[str, list[float]] = {m: [p[1].get(m, 0.0) for p in points] for m in energies}
        kw["total"] = [round(sum(p[1].values()), 1) for p in points]
        return {
            "t": round(self.shadow.sim_time_at(), 2),
            "total_kwh": totals["energy_kwh"],
            "power_kw": totals["power_kw"],
            "kwh_per_kg": round(totals["energy_kwh"] / kg, 3) if kg > 0 else None,
            "machines": machines,
            "trend": {"t": [p[0] for p in points], "kw": kw},
            "by_order": self._energy_by_order(),
        }

    def _energy_by_order(self) -> list[dict]:
        """Open orders and the last finished ones, like `GET /api/orders`, so the answer does not grow with the length of a run."""
        if self.trace is None:
            return []
        finished = [record.order.id for record in self.book.finished_orders()]
        return self.trace.energy_by_order([record.order.id for record in self.book.open_orders()] + finished, finished=frozenset(finished))

    def health(self) -> dict:
        totals = self.shadow.plant_totals()
        pools = [p for p in (self.replan_pool, self.twin_pool, self.manual_pool) if p is not None]
        return {
            "ok": self.client.connected,
            "connected": self.client.connected,
            "run_id": self.client.run_id,
            "plan_revision": self.revision,
            "plant_module_loaded": "werkhalle.plant" in sys.modules,
            "snapshot_calls": totals["snapshot_calls"],
            "pool_plant_module_loaded": any(p.plant_module_loaded for p in pools),
            "version": VERSION,
        }


def _seed_of(value: Any) -> int:
    if value is None:
        return 1
    if not isinstance(value, int) or isinstance(value, bool):
        raise ApiError(400, "bad_request", "seed muss eine ganze Zahl sein.")
    # CP-SAT takes a 32 bit seed, and the runtime adds to it.
    return value & 0x3FFFFFFF


def _optional_text(body: dict, key: str) -> str | None:
    value = body.get(key)
    if value is not None and not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{key} muss ein Text sein.")
    return value


def _evict(entries: dict[str, dict], keep: int) -> None:
    """Drop the oldest finished entries. An id just handed out and still computing stays, or its poll would get a 404."""
    excess = len(entries) - keep
    for entry_id in [key for key, entry in entries.items() if entry["status"] != "computing"][:max(excess, 0)]:
        del entries[entry_id]


def _bounded_int(value: Any, *, default: int, low: int, high: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ApiError(400, "bad_request", f"{name} muss eine ganze Zahl zwischen {low} und {high} sein.")
    return value


def _bounded_float(value: Any, *, default: float, low: float, high: float, name: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ApiError(400, "bad_request", f"{name} muss eine Zahl zwischen {low} und {high} sein.")
    return float(value)


def to_json(data: Any) -> str:
    return _compact(data)


__all__ = ["ApiError", "Runtime", "VERSION", "WS_SEND_TIMEOUT_S", "WsClient", "to_json"]
