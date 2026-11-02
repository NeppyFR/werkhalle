"""The MES's only way to the plant: one OPC UA session, one subscription, one event pump (PLAN section 9).

What has to stay true here, and why:

- `datachange_notification` is a plain `def` that only does `put_nowait`. asyncua spawns one task per notification and runs them
  in order, so a plain function cannot be overtaken. An `async def` that suspended could be.
- Monitored items use a queue larger than 1 on every node. With a queue of 1, a burst RUN, FAULT, RUN within one publish cycle
  would reach the MES as RUN only, and the fault would never be seen.
- One consumer task dispatches. A publish reaches the queue as one uninterrupted run of `put_nowait` calls, and the consumer
  drains the queue before dispatching, so a batch callback sees a whole publish at once. That matters because the server groups a
  publish by monitored item: a Status change can arrive ahead of the Alarm values written before it.
- `GetEvents` is single-flight and pulled until `last_seq >= newest EventSeq seen`, so bursts cannot duplicate or reorder events.
- `RunId` is compared on every (re)connect. Until that comparison is done, no notification of the new connection is dispatched, so
  a new run's values can never land in state belonging to the old run.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from asyncua import Client, Node, ua
from asyncua.client.ua_client import UaClientState

logger = logging.getLogger(__name__)

PUBLISH_INTERVAL_MS = 100
MONITORED_QUEUE_SIZE = 1000
EVENT_BATCH = 500
CHANGE_RING = 500
METHOD_RING = 200
SHORT_STRING = 120
METHOD_TEXT = 300
STALE_AFTER_S = 3.0
BASELINE_WINDOW_S = 2.0
RATE_WINDOW_S = 3
SESSION_TIMEOUT_MS = 600_000  # what the asyncua server grants anyway; asking for more only logs a warning

DataChangeCallback = Callable[["DataChange"], Awaitable[None] | None]
BatchCallback = Callable[[list["DataChange"]], Awaitable[None] | None]


class PlantNotConnected(ConnectionError):
    """A call was attempted while the OPC UA connection is down. The plant has not seen the call."""


@dataclass(slots=True)
class DataChange:
    """One delivered notification.

    `machine` is None for `Plant/` variables. `source_ts` is the SourceTimestamp in Unix seconds, `received_ts` the local wall clock
    when the notification arrived. `baseline` marks the first notification of a node after (re)subscribing: it carries the current
    value, not necessarily a change. `value` is complete; only the rings and the node table shorten it.
    """

    node_id: str
    path: str
    machine: str | None
    name: str
    value: Any
    source_ts: float
    received_ts: float
    latency_ms: float
    baseline: bool
    type: str = ""


@dataclass(slots=True)
class _NodeInfo:
    node: Node
    node_id: str
    path: str
    machine: str | None
    name: str
    type: str = ""
    value: Any = None
    truncated: bool = False
    source_ts: float | None = None
    changes: int = 0


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _shorten(value: Any) -> tuple[Any, bool]:
    """Large strings shrink to 120 characters: the Material JSON changes constantly and must not bloat the rings."""
    value = _json_safe(value)
    if isinstance(value, str) and len(value) > SHORT_STRING:
        return value[:SHORT_STRING], True
    return value, False


def _clip(text: str) -> str:
    return text if len(text) <= METHOD_TEXT else f"{text[:METHOD_TEXT]}… ({len(text)} Zeichen)"


async def _safely(callback: Callable[..., Any], *args: Any) -> None:
    """Run a sync or async callback. One failing consumer must not stop the dispatch loop for the others."""
    try:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("callback %r failed", callback)


class _SubscriptionHandler:
    """Receives notifications from asyncua. Does nothing but queue them."""

    def __init__(self, owner: "PlantClient") -> None:
        self._owner = owner

    def datachange_notification(self, node: Node, val: Any, data: Any) -> None:
        value = data.monitored_item.Value
        self._owner._queue.put_nowait(
            (self._owner._generation, node.nodeid, val, value.SourceTimestamp, value.Value, time.time())
        )

    def status_change_notification(self, status: Any) -> None:
        logger.warning("subscription status change: %s", status)


class PlantClient:
    """Connection to the plant.

    Typical use, so the trace store can be opened for the right run before the first event is pumped:

        client = PlantClient("opc.tcp://127.0.0.1:4840/werkhalle/")
        await client.connect()                     # retries until the plant answers; client.run_id is known now
        client.set_last_event_seq(trace.last_seq)  # optional, to resume after an MES restart
        client.add_batch_callback(...)             # register callbacks, see the add_* methods
        await client.start()                       # subscribes and starts the tasks
        ...
        await client.stop()

    All callbacks may be sync or async, run on the event loop, and run one at a time per kind in registration order. A slow one delays
    its kind only; nothing is lost, because the queue is unbounded. Exceptions in callbacks are logged and swallowed.

    Method calls raise `PlantNotConnected` when the connection is down, and pass through asyncua errors or `asyncio.TimeoutError` on
    failure. Only a returned `{"ok": false, ...}` is a definite refusal; after an exception the outcome at the plant is unknown.
    """

    def __init__(self, url: str, *, timeout: float = 10.0, watchdog_intervall: float = 5.0,
                 stale_after_s: float = STALE_AFTER_S, connect_retry_s: float = 0.5,
                 reconnect_max_delay: float = 5.0) -> None:
        self.url = url
        self._timeout = timeout
        self._watchdog_intervall = watchdog_intervall
        self._stale_after_s = stale_after_s
        self._connect_retry_s = connect_retry_s
        self._reconnect_max_delay = reconnect_max_delay

        self._client: Client | None = None
        self._subscription: Any = None
        self._handler = _SubscriptionHandler(self)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._started = False

        self._run_id: str | None = None
        self._connected = False
        self._ready = asyncio.Event()
        self._stale = False
        self._loss_reason = ""

        # A generation is one stretch of connection. Notifications are stamped with it, so the consumer can hold back those of a
        # connection whose RunId is unverified, and drop leftovers of a previous run.
        self._generation = 0
        self._released_generation = 0
        self._discard_below_generation = 0
        self._released = asyncio.Event()
        self._baseline_pending: set[str] = set()
        self._baseline_deadline = 0.0

        self.namespace_uri = ""
        self._by_nodeid: dict[ua.NodeId, _NodeInfo] = {}
        self._plant_node: Node | None = None
        self._plant_vars: dict[str, Node] = {}
        self._plant_methods: dict[str, ua.NodeId] = {}
        self._machine_nodes: dict[str, Node] = {}
        self._machine_methods: dict[str, dict[str, ua.NodeId]] = {}

        self._changes: deque[dict] = deque(maxlen=CHANGE_RING)
        self._calls: deque[dict] = deque(maxlen=METHOD_RING)
        self._rate: deque[list[int]] = deque(maxlen=RATE_WINDOW_S + 2)
        self.datachanges_total = 0

        self._last_seq = 0
        self._newest_seq = 0
        self._run_epoch = 0
        self.events_total = 0
        self.get_events_calls = 0
        self._pump_wake = asyncio.Event()
        self._last_heartbeat = 0.0

        self._batch_callbacks: list[BatchCallback] = []
        self._change_callbacks: list[DataChangeCallback] = []
        self._event_callbacks: list[Callable[[dict], Any]] = []
        self._gap_callbacks: list[Callable[[int, int], Any]] = []
        self._connection_callbacks: list[Callable[[bool, str], Any]] = []
        self._stale_callbacks: list[Callable[[bool], Any]] = []
        self._run_changed_callbacks: list[Callable[[str | None, str], Any]] = []

    # --- callback registration ---

    def add_batch_callback(self, callback: BatchCallback) -> None:
        """`callback(changes: list[DataChange])`, once per drained queue, which is one whole publish in normal operation."""
        self._batch_callbacks.append(callback)

    def add_datachange_callback(self, callback: DataChangeCallback) -> None:
        """`callback(change: DataChange)` for every notification in delivery order, after the batch callbacks saw its batch."""
        self._change_callbacks.append(callback)

    def add_event_callback(self, callback: Callable[[dict], Any]) -> None:
        """`callback(event: dict)` for every trace event, exactly once and in seq order."""
        self._event_callbacks.append(callback)

    def add_gap_callback(self, callback: Callable[[int, int], Any]) -> None:
        """`callback(last_seq, oldest_seq)` when the plant's ring no longer holds the events after `last_seq`."""
        self._gap_callbacks.append(callback)

    def add_connection_callback(self, callback: Callable[[bool, str], Any]) -> None:
        """`callback(connected, reason)` on every change of the connection, including the first connect."""
        self._connection_callbacks.append(callback)

    def add_stale_callback(self, callback: Callable[[bool], Any]) -> None:
        """`callback(stale)` when the Heartbeat stops arriving, and again when it returns."""
        self._stale_callbacks.append(callback)

    def add_run_changed_callback(self, callback: Callable[[str | None, str], Any]) -> None:
        """`callback(old_run_id, new_run_id)` when a reconnect finds a different RunId. Not called on the first connect.

        It runs before any notification or event of the new run is dispatched, so it is the place for the hard reset.
        Method calls already work inside it.
        """
        self._run_changed_callbacks.append(callback)

    # --- lifecycle ---

    @property
    def run_id(self) -> str | None:
        """The plant's RunId, known after `connect`."""
        return self._run_id

    @property
    def connected(self) -> bool:
        """True while the session is up and the RunId has been verified."""
        return self._connected

    @property
    def stale(self) -> bool:
        """True when no Heartbeat has arrived for `stale_after_s`, also while disconnected."""
        return self._stale

    @property
    def last_event_seq(self) -> int:
        """Seq of the last trace event dispatched."""
        return self._last_seq

    @property
    def newest_event_seq(self) -> int:
        """Highest EventSeq seen."""
        return self._newest_seq

    def machine_ids(self) -> list[str]:
        """Machine ids found by browsing, in browse order."""
        return list(self._machine_nodes)

    def set_last_event_seq(self, seq: int) -> None:
        """Resume the event pump after `seq`. Call before `start`."""
        self._last_seq = seq

    async def connect(self, timeout: float | None = None) -> None:
        """Connect and browse, retrying until the plant answers. Raises `TimeoutError` after `timeout` seconds if one is given."""
        if self._client is not None:
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            client = self._new_client()
            try:
                await client.connect(
                    auto_reconnect=True,
                    reconnect_max_delay=self._reconnect_max_delay,
                    reconnect_request_timeout=self._timeout,
                )
            except (OSError, asyncio.TimeoutError, ua.UaError) as exc:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"OPC-UA-Server {self.url} nicht erreichbar: {exc!r}") from exc
                await asyncio.sleep(self._connect_retry_s)
                continue
            try:
                self._client = client
                await self._browse()
                self._run_id = await self._read_plant_value("RunId")
            except BaseException:
                self._client = None
                await self._disconnect_quietly(client)
                raise
            logger.info("connected to %s, run %s, machines %s", self.url, self._run_id, self.machine_ids())
            return

    async def start(self, timeout: float | None = None) -> None:
        """Connect if needed, subscribe to every variable and start the tasks. Returns once the subscription exists."""
        if self._started:
            return
        await self.connect(timeout)
        self._started = True
        self._stopping = False
        self._released_generation = self._generation
        self._queue = asyncio.Queue()
        self._tasks = [
            asyncio.create_task(self._consume_loop(), name="opcua-consume"),
            asyncio.create_task(self._pump_loop(), name="opcua-events"),
            asyncio.create_task(self._heartbeat_loop(), name="opcua-heartbeat"),
        ]
        await self._subscribe_all()
        self._connected = True
        self._ready.set()
        self._last_heartbeat = time.monotonic()
        self._tasks.append(asyncio.create_task(self._supervise(), name="opcua-supervise"))
        for callback in self._connection_callbacks:
            await _safely(callback, True, "")

    async def stop(self) -> None:
        """Stop all tasks and close the session. Safe to call more than once."""
        self._stopping = True
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        client, self._client = self._client, None
        self._connected = False
        self._started = False
        self._ready.clear()
        if client is not None:
            await self._disconnect_quietly(client)

    def _new_client(self) -> Client:
        client = Client(self.url, timeout=self._timeout, watchdog_intervall=self._watchdog_intervall)
        client.name = "Werkhalle MES"
        client.description = "Werkhalle MES"
        client.application_uri = "urn:werkhalle:mes"
        client.session_timeout = SESSION_TIMEOUT_MS
        client.connection_lost_callback = self._on_connection_lost
        return client

    @staticmethod
    async def _disconnect_quietly(client: Client) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("disconnect failed", exc_info=True)

    # --- browsing and subscribing ---

    async def _browse(self) -> None:
        """Discover plant variables, machines and methods from the address space. Nothing about them is hard-coded."""
        client = self._client
        root = await self._child(client.nodes.objects, "Werkhalle")
        plant = await self._child(root, "Plant")
        machines = await self._child(root, "Machines")
        namespaces = await client.get_namespace_array()
        self.namespace_uri = namespaces[root.nodeid.NamespaceIndex]

        by_nodeid: dict[ua.NodeId, _NodeInfo] = {}
        plant_vars, plant_methods = await self._read_object(plant, "Werkhalle/Plant", None, by_nodeid)
        machine_nodes: dict[str, Node] = {}
        machine_methods: dict[str, dict[str, ua.NodeId]] = {}
        for description in await machines.get_children_descriptions():
            if description.NodeClass != ua.NodeClass.Object:
                continue
            machine_id = description.BrowseName.Name
            node = client.get_node(_plain_nodeid(description.NodeId))
            machine_nodes[machine_id] = node
            _, methods = await self._read_object(node, f"Werkhalle/Machines/{machine_id}", machine_id, by_nodeid)
            machine_methods[machine_id] = methods

        self._plant_node = plant
        self._plant_vars = plant_vars
        self._plant_methods = plant_methods
        self._machine_nodes = machine_nodes
        self._machine_methods = machine_methods
        self._by_nodeid = by_nodeid

    async def _child(self, parent: Node, name: str) -> Node:
        for description in await parent.get_children_descriptions():
            if description.BrowseName.Name == name:
                return self._client.get_node(_plain_nodeid(description.NodeId))
        raise ua.UaError(f"Adressraum: {name} unter {parent.nodeid.to_string()} nicht gefunden")

    async def _read_object(self, parent: Node, path: str, machine: str | None,
                           by_nodeid: dict[ua.NodeId, _NodeInfo]) -> tuple[dict[str, Node], dict[str, ua.NodeId]]:
        variables: dict[str, Node] = {}
        methods: dict[str, ua.NodeId] = {}
        for description in await parent.get_children_descriptions():
            name = description.BrowseName.Name
            nodeid = _plain_nodeid(description.NodeId)
            if description.NodeClass == ua.NodeClass.Variable:
                node = self._client.get_node(nodeid)
                variables[name] = node
                by_nodeid[nodeid] = _NodeInfo(node=node, node_id=nodeid.to_string(), path=f"{path}/{name}",
                                              machine=machine, name=name)
            elif description.NodeClass == ua.NodeClass.Method:
                methods[name] = nodeid
        return variables, methods

    async def _subscribe_all(self) -> None:
        self._baseline_pending = {info.node_id for info in self._by_nodeid.values()}
        self._baseline_deadline = time.monotonic() + 60.0
        self._subscription = await self._client.create_subscription(PUBLISH_INTERVAL_MS, self._handler)
        nodes = [info.node for info in self._by_nodeid.values()]
        results = await self._subscription.subscribe_data_change(nodes, queuesize=MONITORED_QUEUE_SIZE, sampling_interval=0)
        for node, result in zip(nodes, results):
            if isinstance(result, ua.StatusCode):
                logger.error("could not monitor %s: %s", node.nodeid.to_string(), result)
        self._baseline_deadline = time.monotonic() + BASELINE_WINDOW_S

    async def _read_plant_value(self, name: str) -> Any:
        return await self._plant_vars[name].read_value()

    # --- connection supervision ---

    async def _on_connection_lost(self, exc: Exception) -> None:
        await self._mark_lost(repr(exc))

    async def _mark_lost(self, reason: str) -> None:
        if not self._connected or self._stopping:
            return
        self._connected = False
        self._ready.clear()
        self._generation += 1
        self._baseline_pending = {info.node_id for info in self._by_nodeid.values()}
        logger.warning("connection to plant lost: %s", reason)
        for callback in self._connection_callbacks:
            await _safely(callback, False, reason)

    async def _supervise(self) -> None:
        """Turn asyncua's state changes into our connection story, one at a time and in order."""
        client = self._client
        async with client.subscribe_state() as states:
            while True:
                if client.state is UaClientState.CONNECTED:
                    if not self._connected:
                        await self._on_reconnected()
                elif self._connected:
                    await self._mark_lost(client.state.value)
                await states.next_change()

    async def _on_reconnected(self) -> None:
        """Compare the RunId. Same: resume, first notifications are a baseline. Different: hard reset before anything is dispatched."""
        # If the connection drops again during this, the new generation must stay held back until its own RunId check.
        generation = self._generation
        run_id = await self._read_run_id_while_connected()
        if run_id is None:
            return  # lost again; the next CONNECTED brings us back here
        old = self._run_id
        changed = run_id != old
        if changed:
            self._discard_below_generation = self._generation
            self._run_id = run_id
            self._run_epoch += 1
            self._last_seq = 0
            self._newest_seq = 0
            await self._rebuild_for_new_run()
        self._baseline_deadline = time.monotonic() + BASELINE_WINDOW_S
        self._connected = True
        self._ready.set()
        if changed:
            logger.warning("plant run changed: %s -> %s", old, run_id)
            for callback in self._run_changed_callbacks:
                await _safely(callback, old, run_id)
        for callback in self._connection_callbacks:
            await _safely(callback, True, "")
        self._released_generation = generation
        self._released.set()
        await self._kick_pump()

    async def _read_run_id_while_connected(self) -> str | None:
        client = self._client
        while client.state is UaClientState.CONNECTED and not self._stopping:
            try:
                return await self._read_plant_value("RunId")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("reading RunId after reconnect failed: %r", exc)
                await asyncio.sleep(0.2)
        return None

    async def _rebuild_for_new_run(self) -> None:
        """A new run may have a different address space. Only a different one needs a new subscription."""
        old_ids = {info.node_id for info in self._by_nodeid.values()}
        try:
            await self._browse()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("browsing the new run failed, keeping the old node table")
            return
        self._changes.clear()
        self._rate.clear()
        new_ids = {info.node_id for info in self._by_nodeid.values()}
        self._baseline_pending = set(new_ids)
        if new_ids == old_ids:
            return
        logger.warning("address space of the new run differs, subscribing again")
        old_subscription = self._subscription
        try:
            if old_subscription is not None:
                await old_subscription.delete()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("deleting the old subscription failed", exc_info=True)
        await self._subscribe_all()

    async def _kick_pump(self) -> None:
        # A transferred subscription sends no initial values, so the EventSeq has to be read once.
        try:
            self._note_event_seq(int(await self._read_plant_value("EventSeq")))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("reading EventSeq failed: %r", exc)

    async def _heartbeat_loop(self) -> None:
        interval = min(0.25, self._stale_after_s / 4)
        while True:
            await asyncio.sleep(interval)
            if not self._last_heartbeat:
                continue
            stale = time.monotonic() - self._last_heartbeat > self._stale_after_s
            if stale != self._stale:
                self._stale = stale
                logger.warning("plant heartbeat %s", "stopped" if stale else "back")
                for callback in self._stale_callbacks:
                    await _safely(callback, stale)

    # --- dispatching data changes ---

    async def _consume_loop(self) -> None:
        queue = self._queue
        while True:
            items = [await queue.get()]
            while not queue.empty():
                items.append(queue.get_nowait())
            batch: list[DataChange] = []
            for item in items:
                generation = item[0]
                if generation > self._released_generation and generation >= self._discard_below_generation:
                    # Whatever came before belongs to the old connection and may go out now; this one waits for the RunId check.
                    await self._dispatch(batch)
                    batch = []
                    await self._wait_released(generation)
                if generation < self._discard_below_generation:
                    continue
                change = self._record(item)
                if change is not None:
                    batch.append(change)
            await self._dispatch(batch)

    async def _wait_released(self, generation: int) -> None:
        while generation > self._released_generation and generation >= self._discard_below_generation:
            self._released.clear()
            await self._released.wait()

    async def _dispatch(self, batch: list[DataChange]) -> None:
        if not batch:
            return
        for callback in self._batch_callbacks:
            await _safely(callback, batch)
        for change in batch:
            for callback in self._change_callbacks:
                await _safely(callback, change)

    def _record(self, item: tuple) -> DataChange | None:
        generation, nodeid, value, source_dt, variant, received = item
        info = self._by_nodeid.get(nodeid)
        if info is None:
            return None
        source_ts = source_dt.timestamp() if isinstance(source_dt, datetime) else received
        latency_ms = max((received - source_ts) * 1000.0, 0.0)

        baseline = False
        if generation == self._generation and self._baseline_pending:
            if time.monotonic() > self._baseline_deadline and generation == self._released_generation:
                # A transferred subscription sends no initial values, so a quiet node would otherwise keep its flag for hours
                # and its next real change would be mistaken for a baseline.
                self._baseline_pending.clear()
            elif info.node_id in self._baseline_pending:
                self._baseline_pending.discard(info.node_id)
                baseline = True

        variant_type = getattr(getattr(variant, "VariantType", None), "name", "")
        shown, truncated = _shorten(value)
        info.type = variant_type or info.type
        info.value = shown
        info.truncated = truncated
        info.source_ts = source_ts
        info.changes += 1

        entry = {
            "node_id": info.node_id,
            "path": info.path,
            "value": shown,
            "source_ts": source_ts,
            "received_wall_ts": received,
            "latency_ms": round(latency_ms, 1),
        }
        if truncated:
            entry["truncated"] = True
        self._changes.append(entry)
        self.datachanges_total += 1
        self._count_rate(received)

        if info.machine is None:
            if info.name == "Heartbeat":
                self._last_heartbeat = time.monotonic()
            elif info.name == "EventSeq":
                self._note_event_seq(int(value or 0))

        return DataChange(
            node_id=info.node_id, path=info.path, machine=info.machine, name=info.name, value=value, source_ts=source_ts,
            received_ts=received, latency_ms=latency_ms, baseline=baseline, type=info.type,
        )

    def _count_rate(self, received: float) -> None:
        second = int(received)
        if self._rate and self._rate[-1][0] == second:
            self._rate[-1][1] += 1
        else:
            self._rate.append([second, 1])

    # --- the event pump ---

    def _note_event_seq(self, seq: int) -> None:
        if seq > self._newest_seq:
            self._newest_seq = seq
        if self._newest_seq > self._last_seq:
            self._pump_wake.set()

    async def _pump_loop(self) -> None:
        """The only caller of GetEvents, which is what makes it single-flight."""
        while True:
            await self._pump_wake.wait()
            self._pump_wake.clear()
            while self._last_seq < self._newest_seq:
                await self._ready.wait()
                epoch = self._run_epoch
                after = self._last_seq
                try:
                    payload = await self._get_events(after)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.info("GetEvents failed: %r", exc)
                    await asyncio.sleep(0.5)
                    continue
                if epoch != self._run_epoch:
                    continue  # the answer belongs to the previous run
                await self._deliver_events(payload, after, epoch)

    async def _get_events(self, after: int) -> dict:
        method = self._plant_methods["GetEvents"]
        self.get_events_calls += 1
        raw = await self._plant_node.call_method(
            method,
            ua.Variant(int(after), ua.VariantType.UInt64),
            ua.Variant(EVENT_BATCH, ua.VariantType.UInt32),
        )
        return json.loads(raw)

    async def _deliver_events(self, payload: dict, after: int, epoch: int) -> None:
        events = payload.get("events") or []
        oldest = int(payload.get("oldest_seq") or 0)
        newest = int(payload.get("newest_seq") or 0)
        if events and oldest > after + 1:
            logger.warning("event gap: wanted seq after %d, the plant's ring starts at %d", after, oldest)
            for callback in self._gap_callbacks:
                await _safely(callback, after, oldest)
        for event in events:
            if epoch != self._run_epoch:
                return
            seq = event["seq"]
            if seq <= self._last_seq:
                continue
            self._last_seq = seq
            self.events_total += 1
            for callback in self._event_callbacks:
                await _safely(callback, event)
        if events:
            self._newest_seq = max(self._newest_seq, newest)
            return
        # Nothing after last_seq: the server's own newest beats the DataChange we saw, so this cannot turn into a hot loop.
        self._newest_seq = newest
        if self._last_seq < newest:
            await asyncio.sleep(0.2)

    # --- calling the plant ---

    def _ensure_connected(self) -> None:
        if not self._connected or self._client is None:
            raise PlantNotConnected("Verbindung zur Anlage getrennt")

    async def _logged(self, method: str, node: str, args: str, call: Callable[[], Awaitable[Any]]) -> Any:
        wall_ts = time.time()
        started = time.perf_counter()
        try:
            self._ensure_connected()
            result = await call()
        except Exception as exc:
            self._log_call(wall_ts, method, node, args, f"FEHLER: {exc!r}", started)
            raise
        self._log_call(wall_ts, method, node, args, result if isinstance(result, str) else json.dumps(result), started)
        return result

    def _log_call(self, wall_ts: float, method: str, node: str, args: str, result: str, started: float) -> None:
        self._calls.append({
            "wall_ts": wall_ts,
            "method": method,
            "node": node,
            "args": _clip(args),
            "result": _clip(result),
            "ms": round((time.perf_counter() - started) * 1000.0, 2),
        })

    async def _call_machine(self, machine: str, method: str, args_text: str, *args: ua.Variant) -> dict:
        node = self._machine_nodes[machine]
        nodeid = self._machine_methods[machine][method]
        raw = await self._logged(method, machine, args_text, lambda: node.call_method(nodeid, *args))
        return json.loads(raw)

    async def start_operation(self, machine: str, command: dict) -> dict:
        """`StartOperation` on a machine. Returns `{ok, reason}`. Fills in `run_id` when the command has none."""
        command = {**command, "run_id": command.get("run_id") or self._run_id}
        text = json.dumps(command, ensure_ascii=False)
        return await self._call_machine(machine, "StartOperation", text, ua.Variant(text, ua.VariantType.String))

    async def inject_fault(self, machine: str, code: str = "", repair_min: float = 0.0) -> dict:
        """`InjectFault`. Returns `{ok, reason}`. "" and 0 select the machine's default code and repair time."""
        return await self._call_machine(
            machine, "InjectFault", json.dumps({"code": code, "repairMin": repair_min}, ensure_ascii=False),
            ua.Variant(code, ua.VariantType.String),
            ua.Variant(float(repair_min), ua.VariantType.Double),
        )

    async def repair(self, machine: str) -> dict:
        """`Repair`. Returns `{ok, reason}`."""
        return await self._call_machine(machine, "Repair", "")

    async def ack_alarm(self, machine: str, alarm_id: int) -> dict:
        """`AckAlarm`. Returns `{ok, reason}`."""
        return await self._call_machine(
            machine, "AckAlarm", json.dumps({"alarmId": alarm_id}), ua.Variant(int(alarm_id), ua.VariantType.UInt64),
        )

    async def get_state_snapshot(self) -> dict:
        """`GetStateSnapshot`, parsed. For forks and planning only, never to refresh the shadow."""
        method = self._plant_methods["GetStateSnapshot"]
        raw = await self._logged("GetStateSnapshot", "Plant", "", lambda: self._plant_node.call_method(method))
        return json.loads(raw)

    async def set_time_factor(self, factor: float) -> None:
        """Write `Plant/TimeFactor` as a Double. An invalid value is reverted by the plant, and the shadow shows that."""
        node = self._plant_vars["TimeFactor"]
        value = ua.Variant(float(factor), ua.VariantType.Double)
        await self._logged("Write TimeFactor", "Plant", str(float(factor)), lambda: self._write(node, value))

    async def set_random_failures(self, enabled: bool) -> None:
        """Write `Plant/RandomFailures` as a Boolean."""
        node = self._plant_vars["RandomFailures"]
        value = ua.Variant(bool(enabled), ua.VariantType.Boolean)
        await self._logged("Write RandomFailures", "Plant", str(bool(enabled)), lambda: self._write(node, value))

    @staticmethod
    async def _write(node: Node, value: ua.Variant) -> str:
        await node.write_value(value)
        return "ok"

    # --- monitor data for /api/opcua/* and the state counters ---

    def per_second(self) -> float:
        """DataChanges per second, averaged over the last few complete seconds."""
        now = int(time.time())
        recent = [count for second, count in self._rate if now - RATE_WINDOW_S <= second < now]
        return round(sum(recent) / RATE_WINDOW_S, 1)

    def counters(self) -> dict:
        """The `counters` object of `/api/state`."""
        return {
            "datachanges_total": self.datachanges_total,
            "datachanges_per_s": self.per_second(),
            "events_total": self.events_total,
            "last_event_seq": self._last_seq,
        }

    def nodes_table(self) -> dict:
        """`GET /api/opcua/nodes`. Large strings are cut to 120 characters and marked `truncated`."""
        nodes = []
        for info in self._by_nodeid.values():
            row = {
                "node_id": info.node_id,
                "path": info.path,
                "machine": info.machine,
                "name": info.name,
                "type": info.type,
                "value": info.value,
                "source_ts": info.source_ts,
                "changes": info.changes,
            }
            if info.truncated:
                row["truncated"] = True
            nodes.append(row)
        return {"endpoint": self.url, "namespace": self.namespace_uri, "nodes": nodes}

    def recent_changes(self, limit: int = 200) -> dict:
        """`GET /api/opcua/changes`: newest first, from the ring of 500."""
        rows = list(self._changes)[-limit:] if limit > 0 else []
        rows.reverse()
        return {"changes": rows, "per_second": self.per_second()}

    def method_calls(self, limit: int = 200) -> dict:
        """`GET /api/opcua/methods`: the ring of the last 200 calls and writes, newest first. GetEvents is not logged here."""
        rows = list(self._calls)[-limit:] if limit > 0 else []
        rows.reverse()
        return {"calls": rows}


def _plain_nodeid(nodeid: ua.NodeId) -> ua.NodeId:
    """Browse results carry ExpandedNodeIds. Notifications carry the id we subscribed with, so keys must be plain NodeIds."""
    return ua.NodeId(nodeid.Identifier, nodeid.NamespaceIndex, nodeid.NodeIdType)


__all__ = ["DataChange", "PlantClient", "PlantNotConnected"]
