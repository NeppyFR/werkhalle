"""Serial decoder shared by `decode_edd` and annealing. Pure Python, written for tens of thousands of decodes.

A candidate is a lot-priority list, an optional machine choice per op and a wait gene per lot. Decoding goes stage by
stage (casts, press, age, saw, pack), so that when a later lot joins and thereby delays a furnace load, none of that
load's downstream ops have been placed yet. Within a stage, ops are taken in priority order and placed at the earliest
position their machine allows, gaps included.

The wait gene does not idle the furnace: it extends how long a load accepts latecomers. If nobody joins, nothing moves.
So the decode needs no separate left shift, and its objective already is close to what `retime` will produce.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field

from ..domain.catalog import AGE, CAST, PACK, PRESS, SAW, setup_minutes
from .model import Assignment, Op, Problem, Schedule
from .retime import frozen_assignments, machine_start_state, retime

WAIT_GENES = (0, 30, 60, 120, 180, 240)
_RULE_BUCKETS = (1, 120, 240, 360, 480, 720, 1 << 61)  # minutes; the last puts every lot in one bucket
AUTO = -1  # machine gene: take whichever eligible machine finishes the op first

_STAGE_RANK = {PRESS: 1, AGE: 2, SAW: 3, PACK: 4}


class Compiled:
    """Everything about a problem that a decode only reads, as flat integer-indexed lists."""

    def __init__(self, problem: Problem) -> None:
        self.problem = problem
        self.ops: list[Op] = [op for op in problem.ops if op.frozen is None]
        self.n_ops = len(self.ops)
        index = {op.id: i for i, op in enumerate(self.ops)}
        frozen_end = {op.id: op.frozen.end for op in problem.ops if op.frozen is not None}

        start_state = machine_start_state(problem)
        self.machines = list(problem.machines)
        self.n_machines = len(self.machines)
        self.machine_range = range(self.n_machines)
        machine_index = {m.id: i for i, m in enumerate(self.machines)}
        self.is_batch = [m.is_batch for m in self.machines]
        self.batch_machines = [i for i, m in enumerate(self.machines) if m.is_batch]
        self.capacity = [m.capacity for m in self.machines]
        self.base = [start_state[m.id][0] for m in self.machines]
        self.downtimes = [tuple(sorted((d.start, d.end) for d in m.downtimes)) for m in self.machines]

        # Class 0 is "nothing mounted". setup[to][from] is the setup when changing to class `to`.
        class_of: dict[str | None, int] = {None: 0}
        kind_of: dict[int, str] = {}
        for op in self.ops:
            code = class_of.setdefault(op.setup_class, len(class_of))
            kind_of[code] = op.kind
        self.init_class = [class_of.setdefault(start_state[m.id][1], len(class_of)) for m in self.machines]
        names = list(class_of)
        self.setup = [
            [setup_minutes(kind_of[to], names[frm], names[to]) if to in kind_of else 0 for frm in range(len(names))]
            for to in range(len(names))
        ]
        self.op_class = [class_of[op.setup_class] for op in self.ops]

        family_of: dict[str, int] = {}
        self.family = [
            family_of.setdefault(op.batch_family, len(family_of)) if op.batch_family is not None else -1
            for op in self.ops
        ]
        self.size = [op.size for op in self.ops]

        self.options: list[tuple[tuple[int, int], ...]] = []
        for op in self.ops:
            options = []
            for machine_id, minutes in op.durations.items():
                m = machine_index.get(machine_id)
                if m is not None and (not self.is_batch[m] or op.size <= self.capacity[m]):
                    options.append((m, minutes))
            self.options.append(tuple(options))
        self.placeable = all(self.options)
        self.batch_op = [bool(opts) and self.is_batch[opts[0][0]] for opts in self.options]

        self.static_ready = []
        self.dynamic_preds: list[tuple[tuple[int, int], ...]] = []
        for op in self.ops:
            ready = max(problem.now, op.release)
            dynamic = []
            for pred in op.preds:
                if pred.op_id in frozen_end:
                    ready = max(ready, frozen_end[pred.op_id] + pred.lag)
                elif pred.op_id in index:
                    dynamic.append((index[pred.op_id], pred.lag))
            self.static_ready.append(ready)
            self.dynamic_preds.append(tuple(dynamic))

        self._group_ops()
        self._order_rows(frozen_end)

    def _group_ops(self) -> None:
        """Lots become groups. Casts stay outside: each is placed when the first lot needing it comes up."""
        group_of: dict[str, int] = {}
        members: list[list[int]] = []
        self.casts = [i for i, op in enumerate(self.ops) if op.kind == CAST and self.options[i]]
        for i, op in enumerate(self.ops):
            if op.kind == CAST or not self.options[i]:
                continue
            key = op.lot_id if op.lot_id is not None else f"#{op.id}"
            group = group_of.setdefault(key, len(group_of))
            if group == len(members):
                members.append([])
            members[group].append(i)
        self.n_groups = len(members)
        self.group_members = members
        due = {order.id: order.due for order in self.problem.orders}
        self.group_key = [
            (
                min(due.get(self.ops[i].order_id, 1 << 60) for i in ms),
                self.ops[ms[0]].lot_id or self.ops[ms[0]].id,
            )
            for ms in members
        ]

        # A stage holds at most one op per group. The catalogue route gives one stage per kind; anything unusual in a
        # hand-built problem (two ops of one kind in a lot, a pred within a kind) just moves to a later stage.
        level = [_STAGE_RANK.get(op.kind, 0) for op in self.ops]
        for _ in range(self.n_ops):
            changed = False
            for i in range(self.n_ops):
                if self.ops[i].kind == CAST:
                    continue
                for p, _lag in self.dynamic_preds[i]:
                    if self.ops[p].kind != CAST and level[p] >= level[i]:
                        level[i] = level[p] + 1
                        changed = True
            if not changed:
                break
        slots: dict[int, list[int]] = {}
        for group, ms in enumerate(members):
            for i in sorted(ms, key=lambda i: level[i]):
                while level[i] in slots and slots[level[i]][group] >= 0:
                    level[i] += 1
                slots.setdefault(level[i], [-1] * self.n_groups)[group] = i
        self.stages = [slots[lv] for lv in sorted(slots)]

        # Casts in dependency order; each group lists the casts it transitively needs.
        topo: list[int] = []
        seen: set[int] = set()
        castable = set(self.casts)

        def visit(c: int) -> None:
            if c in seen:
                return
            seen.add(c)
            for p, _lag in self.dynamic_preds[c]:
                if p in castable:
                    visit(p)
            topo.append(c)

        for c in self.casts:
            visit(c)
        self.casts = topo
        rank = {c: r for r, c in enumerate(topo)}

        def closure(i: int, into: set[int]) -> None:
            for p, _lag in self.dynamic_preds[i]:
                if p in rank and p not in into:
                    into.add(p)
                    closure(p, into)

        self.cast_deps: list[tuple[int, ...]] = []
        for ms in members:
            needed: set[int] = set()
            for i in ms:
                closure(i, needed)
            self.cast_deps.append(tuple(sorted(needed, key=rank.__getitem__)))

    def _order_rows(self, frozen_end: dict[str, int]) -> None:
        problem = self.problem
        followed_in_order: set[int] = set()
        followed: set[int] = set()
        for i in range(self.n_ops):
            for p, _lag in self.dynamic_preds[i]:
                followed.add(p)
                if self.ops[p].order_id == self.ops[i].order_id:
                    followed_in_order.add(p)
        self.sinks = [i for i in range(self.n_ops) if i not in followed]
        self.frozen_latest = max([problem.now, *frozen_end.values()])

        fixed: dict[str, int] = {}
        for order in problem.orders:
            if order.completed_lots_end is not None:
                fixed[order.id] = order.completed_lots_end
        for op in problem.ops:
            if op.frozen is not None and op.order_id is not None:
                fixed[op.order_id] = max(fixed.get(op.order_id, op.frozen.end), op.frozen.end)
        rows = []
        for order in problem.orders:
            ends = tuple(
                i for i in range(self.n_ops) if self.ops[i].order_id == order.id and i not in followed_in_order
            )
            base = fixed.get(order.id, -1)
            if ends or base >= 0:
                rows.append((order.due, order.weight, base, ends))
        self.order_rows = rows
        self.w_tardiness = problem.weights.tardiness
        self.w_makespan = problem.weights.makespan
        self.w_setup = problem.weights.setup

    def edd_priority(self) -> list[int]:
        return sorted(range(self.n_groups), key=self.group_key.__getitem__)

    def machine_genes(self) -> list[int]:
        return [AUTO] * self.n_ops

    def wait_genes(self) -> list[int]:
        return [0] * self.n_groups


@dataclass
class Decoded:
    """One placed candidate. `units[m]` lists op-index groups in machine order, parallel to starts and ends."""

    objective: int
    starts: list[list[int]] = field(default_factory=list)
    ends: list[list[int]] = field(default_factory=list)
    classes: list[list[int]] = field(default_factory=list)
    units: list[list[list[int]]] = field(default_factory=list)


def _clear_downtimes(start: int, setup: int, duration: int, downtimes) -> int:
    for down_start, down_end in downtimes:
        if start - setup < down_end and start + duration > down_start:
            start = down_end + setup
    return start


def _position(starts, ends, classes, gap, base, init, table, op_class, duration, ready, downtimes):
    """Earliest position on a machine as (start, index, setup): a gap if one fits, else the tail."""
    row = table[op_class]
    n = len(ends)
    if not n:
        setup = row[init]
        start = (base if base > ready else ready) + setup
    else:
        tail = ends[-1]
        if ready < tail and duration <= gap:
            # A unit starting before ready + duration cannot have this op finish ahead of it.
            k = bisect_left(starts, ready + duration)
            while k < n:
                if k:
                    prev_end = ends[k - 1]
                    prev_class = classes[k - 1]
                else:
                    prev_end = base
                    prev_class = init
                setup = row[prev_class]
                start = (prev_end if prev_end > ready else ready) + setup
                next_start = starts[k]
                if start + duration <= next_start:
                    if downtimes:
                        start = _clear_downtimes(start, setup, duration, downtimes)
                    # The next unit's setup must not begin any earlier: its material may not be there, nor the machine.
                    next_row = table[classes[k]]
                    next_setup = next_row[op_class]
                    if start + duration + next_setup <= next_start and next_setup <= next_row[prev_class]:
                        return start, k, setup
                k += 1
        setup = row[classes[-1]]
        start = (tail if tail > ready else ready) + setup
    if downtimes:
        start = _clear_downtimes(start, setup, duration, downtimes)
    return start, n, setup


def _place_cast(c: Compiled, i: int, machines, gaps, done) -> None:
    ready = c.static_ready[i]
    for p, lag in c.dynamic_preds[i]:
        value = done[p] + lag
        if value > ready:
            ready = value
    m, duration = c.options[i][0]
    starts, ends, classes, units = machines[m][:4]
    start, k, _setup = _position(
        starts, ends, classes, gaps[m], c.base[m], c.init_class[m], c.setup, c.op_class[i], duration, ready, c.downtimes[m]
    )
    if k == len(ends):
        idle = start - (ends[-1] if k else c.base[m])
        if idle > gaps[m]:
            gaps[m] = idle
    starts.insert(k, start)
    ends.insert(k, start + duration)
    classes.insert(k, c.op_class[i])
    units.insert(k, [i])
    done[i] = start + duration


def decode(c: Compiled, priority: list[int], machine_gene: list[int], wait: list[int], keep: bool = False):
    """Place one candidate and return its objective, or the full `Decoded` when `keep` is set."""
    # Per machine: starts, ends, classes, units, and for furnaces per load its family, baskets and join deadline.
    machines = [([], [], [], [], [], [], []) for _ in c.machine_range]
    # Largest idle stretch seen per machine, so that a machine without any gap costs no gap search.
    gaps = [0] * c.n_machines
    done = [-1] * c.n_ops

    base = c.base
    init = c.init_class
    table = c.setup
    downtimes = c.downtimes
    options = c.options
    op_class = c.op_class
    static_ready = c.static_ready
    preds = c.dynamic_preds
    batch_op = c.batch_op
    batch_machines = c.batch_machines
    capacity = c.capacity
    position = _position

    for group in priority:
        for i in c.cast_deps[group]:
            if done[i] < 0:
                _place_cast(c, i, machines, gaps, done)
    for i in c.casts:
        if done[i] < 0:
            _place_cast(c, i, machines, gaps, done)

    for stage in c.stages:
        any_load = False
        for group in priority:
            i = stage[group]
            if i < 0:
                continue
            opts = options[i]
            gene = machine_gene[i]
            if gene >= 0:
                opts = (opts[gene],)
            ready = static_ready[i]
            for p, lag in preds[i]:
                value = done[p] + lag
                if value > ready:
                    ready = value
            cls = op_class[i]

            if not batch_op[i]:
                if len(opts) == 1:
                    m, duration = opts[0]
                    starts, ends, classes, units = machines[m][:4]
                    if ends and (ready >= ends[-1] or duration > gaps[m]):
                        # The common case, with no gap search: straight onto the tail.
                        tail = ends[-1]
                        setup = table[cls][classes[-1]]
                        start = (tail if tail > ready else ready) + setup
                        if downtimes[m]:
                            start = _clear_downtimes(start, setup, duration, downtimes[m])
                        k = len(ends)
                    else:
                        start, k, setup = position(
                            starts, ends, classes, gaps[m], base[m], init[m], table, cls, duration, ready, downtimes[m]
                        )
                else:
                    best_finish = None
                    best_setup = 0
                    for m2, duration2 in opts:
                        state = machines[m2]
                        start2, k2, setup2 = position(
                            state[0], state[1], state[2], gaps[m2], base[m2], init[m2], table, cls, duration2, ready,
                            downtimes[m2],
                        )
                        finish2 = start2 + duration2
                        if best_finish is None or finish2 < best_finish or (finish2 == best_finish and setup2 < best_setup):
                            best_finish, best_setup = finish2, setup2
                            m, duration, start, k = m2, duration2, start2, k2
                    starts, ends, classes, units = machines[m][:4]
                end = start + duration
                done[i] = end
                if k == len(ends):
                    idle = start - (ends[-1] if k else base[m])
                    if idle > gaps[m]:
                        gaps[m] = idle
                    starts.append(start)
                    ends.append(end)
                    classes.append(cls)
                    units.append([i])
                else:
                    starts.insert(k, start)
                    ends.insert(k, end)
                    classes.insert(k, cls)
                    units.insert(k, [i])
                continue

            # Furnace: join a compatible load on the chosen machine if its wait gene still allows it, else open one.
            any_load = True
            family = c.family[i]
            size = c.size[i]
            best = None
            for m, duration in opts:
                starts, ends, classes, _units, fams, sizes, holds = machines[m]
                joined = None
                if family >= 0:
                    room = capacity[m] - size
                    for k in range(len(starts)):
                        if fams[k] == family and ready <= holds[k] and sizes[k] <= room and ends[k] - starts[k] == duration:
                            # A load is put in as one before its setup, so the latecomer has to be there by then.
                            load_setup = table[classes[k]][classes[k - 1] if k else init[m]]
                            load_start = ready + load_setup
                            if load_start <= starts[k]:
                                joined = (ends[k], 0, 0, m, k, starts[k], duration)
                                break
                            # The latecomer delays the load. Only possible if what follows stays where it is.
                            load_end = load_start + duration
                            if k + 1 < len(starts) and load_end + table[classes[k + 1]][classes[k]] > starts[k + 1]:
                                continue
                            if downtimes[m] and _clear_downtimes(load_start, load_setup, duration, downtimes[m]) != load_start:
                                continue
                            joined = (load_end, 0, 0, m, k, load_start, duration)
                            break
                if joined is not None:
                    candidate = joined
                else:
                    start, k, setup = position(
                        starts, ends, classes, gaps[m], base[m], init[m], table, cls, duration, ready, downtimes[m]
                    )
                    candidate = (start + duration, setup, 1, m, k, start, duration)
                if best is None or candidate[:3] < best[:3]:
                    best = candidate
            finish, _setup, opens, m, k, start, duration = best
            starts, ends, classes, units, fams, sizes, holds = machines[m]
            if opens:
                if k == len(ends):
                    idle = start - (ends[-1] if k else base[m])
                    if idle > gaps[m]:
                        gaps[m] = idle
                starts.insert(k, start)
                ends.insert(k, finish)
                classes.insert(k, cls)
                units.insert(k, [i])
                fams.insert(k, family)
                sizes.insert(k, size)
                hold = ready + wait[group]
                holds.insert(k, hold if hold > start else start)
            else:
                # Delaying a load widens the stretch before it.
                idle = start - (ends[k - 1] if k else base[m])
                if idle > gaps[m]:
                    gaps[m] = idle
                starts[k] = start
                ends[k] = finish
                sizes[k] += size
                units[k].append(i)

        if any_load:
            for m in batch_machines:
                state = machines[m]
                ends = state[1]
                units = state[3]
                for k in range(len(units)):
                    end = ends[k]
                    for i in units[k]:
                        done[i] = end

    # Objective, with exactly the terms and weights of compute_metrics.
    latest = c.frozen_latest
    for i in c.sinks:
        if done[i] > latest:
            latest = done[i]
    weighted = 0
    for due, weight, fixed_end, order_ends in c.order_rows:
        completion = fixed_end
        for i in order_ends:
            if done[i] > completion:
                completion = done[i]
        if completion > due:
            weighted += weight * (completion - due)
    setup_total = 0
    for m in c.machine_range:
        previous = init[m]
        for cls in machines[m][2]:
            setup_total += table[cls][previous]
            previous = cls
    objective = c.w_tardiness * weighted + c.w_makespan * (latest - c.problem.now) + c.w_setup * setup_total
    if keep:
        return Decoded(
            objective,
            [state[0] for state in machines],
            [state[1] for state in machines],
            [state[2] for state in machines],
            [state[3] for state in machines],
        )
    return objective


def assignments_of(c: Compiled, decoded: Decoded) -> list[Assignment]:
    """Turn a decode into assignments. Load ids are minted in start order and never collide with a frozen load's."""
    problem = c.problem
    assignments = frozen_assignments(problem)
    taken = {a.batch_id for a in assignments if a.batch_id}
    loads: list[tuple[int, int, int]] = []
    for m in c.batch_machines:
        loads.extend((decoded.starts[m][k], m, k) for k in range(len(decoded.units[m])))
    load_ids: dict[tuple[int, int], str] = {}
    number = 0
    for _, m, k in sorted(loads):
        number += 1
        while f"{problem.batch_prefix}-{number}" in taken:
            number += 1
        load_ids[(m, k)] = f"{problem.batch_prefix}-{number}"

    for m, machine in enumerate(c.machines):
        previous = c.init_class[m]
        for k, unit in enumerate(decoded.units[m]):
            cls = decoded.classes[m][k]
            setup = c.setup[cls][previous]
            previous = cls
            start = decoded.starts[m][k]
            for position, i in enumerate(sorted(unit, key=lambda i: c.ops[i].id)):
                op = c.ops[i]
                assignments.append(
                    Assignment(
                        op.id,
                        machine.id,
                        start,
                        start + op.durations[machine.id],
                        setup if position == 0 else 0,
                        load_ids.get((m, k)),
                    )
                )
    return assignments


def decode_schedule(c: Compiled, priority: list[int], machine_gene: list[int], wait: list[int], solver: str) -> Schedule:
    """Decode a candidate and retime it: this is the schedule the candidate really stands for."""
    decoded = decode(c, priority, machine_gene, wait, keep=True)
    status = "feasible" if c.placeable else "no_solution"
    return retime(c.problem, Schedule(solver=solver, status=status, assignments=tuple(assignments_of(c, decoded))))


def best_rule_candidate(c: Compiled) -> tuple[list[int], list[int]]:
    """Priorities and wait genes of the best dispatch rule. This is the start plan of both solvers.

    A rule is a due date or slack order, optionally in time buckets sorted by press class or recipe, with one hold time
    for all loads. About 200 decodes, 50 ms at most at 28 lots. The bare EDD decode starts each load as soon as its first
    lot is there, which wastes furnace capacity, and after a fault it presses lots that could wait: on real seed book
    states it came out 1.2 to 2.7 times worse than the best rule.
    """
    genes = c.machine_genes()
    best = (c.edd_priority(), c.wait_genes())
    best_value = decode(c, best[0], genes, best[1])

    def press_class(group: int) -> str:
        return c.ops[c.group_members[group][0]].setup_class or ""

    def recipe(group: int) -> str:
        return next((c.ops[i].batch_family for i in c.group_members[group] if c.ops[i].batch_family), "")

    def due(group: int) -> int:
        return c.group_key[group][0]

    def slack(group: int) -> int:
        return due(group) - sum(min(minutes for _, minutes in c.options[i]) for i in c.group_members[group])

    holds = WAIT_GENES if any(c.batch_op) else (0,)
    tried = set()
    for urgency in (due, slack):
        for bucket in _RULE_BUCKETS:
            for tie in (
                lambda g: (),
                lambda g: (press_class(g),),
                lambda g: (recipe(g),),
                lambda g: (press_class(g), recipe(g)),
                lambda g: (recipe(g), press_class(g)),
            ):
                priority = sorted(
                    range(c.n_groups), key=lambda g: (urgency(g) // bucket, *tie(g), urgency(g), c.group_key[g])
                )
                if tuple(priority) in tried:
                    continue
                tried.add(tuple(priority))
                for hold in holds:
                    wait = [hold] * c.n_groups
                    value = decode(c, priority, genes, wait)
                    if value < best_value:
                        best, best_value = (priority, wait), value
    return best


def decode_best_rule(problem: Problem) -> Schedule:
    c = Compiled(problem)
    priority, wait = best_rule_candidate(c)
    return decode_schedule(c, priority, c.machine_genes(), wait, "edd")


def decode_edd(problem: Problem) -> Schedule:
    """Single decode with lots in order of due date, each on whichever machine finishes it first."""
    c = Compiled(problem)
    return decode_schedule(c, c.edd_priority(), c.machine_genes(), c.wait_genes(), "edd")
