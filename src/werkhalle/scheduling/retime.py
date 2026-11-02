"""Left-justified forward pass over fixed machine sequences, and carrying an old plan over to a new problem.

The dispatcher left-shifts whatever it is given, so times that a solver left slack in are fiction. Every candidate
goes through `retime` before it is validated or scored.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from time import perf_counter

from ..domain.catalog import KINDS, setup_minutes
from .model import Assignment, Op, Problem, Schedule, Unit


def machine_start_state(problem: Problem) -> dict[str, tuple[int, str | None]]:
    """Per machine, when pending work can begin and the class mounted then. A frozen op's end occupies the machine."""
    state = {m.id: (max(problem.now, m.available_from), m.initial_setup_class) for m in problem.machines}
    latest_frozen: dict[str, int] = {}
    for op in problem.ops:
        frozen = op.frozen
        if frozen is None or frozen.machine not in state:
            continue
        if frozen.end >= latest_frozen.get(frozen.machine, frozen.end):
            latest_frozen[frozen.machine] = frozen.end
            ready, _ = state[frozen.machine]
            state[frozen.machine] = (max(ready, frozen.end), op.setup_class)
    return state


def frozen_assignments(problem: Problem) -> list[Assignment]:
    return [
        Assignment(op.id, op.frozen.machine, op.frozen.start, op.frozen.end, 0, op.frozen.batch_id)
        for op in problem.ops
        if op.frozen is not None
    ]


def forward_pass(problem: Problem, sequences: Mapping[str, list[Unit]]) -> tuple[list[Assignment], bool]:
    """Place each unit as early as its machine, predecessors, release and downtimes allow.

    The second return value is False when sequences and predecessors form a cycle, which only inconsistent input can
    cause: the unplaced ops are then left out.
    """
    start_state = machine_start_state(problem)
    assignments = frozen_assignments(problem)
    ends = {a.op_id: a.end for a in assignments}
    sequenced = {op_id for units in sequences.values() for unit in units for op_id in unit.op_ids}
    heads = dict.fromkeys(sequences, 0)
    tails = {machine: start_state[machine] for machine in sequences}
    downtimes = {
        machine: sorted((d.start, d.end) for d in problem.machine(machine).downtimes) for machine in sequences
    }

    progress = True
    while progress:
        progress = False
        for machine, units in sequences.items():
            position = heads[machine]
            tail_end, tail_class = tails[machine]
            while position < len(units):
                unit = units[position]
                ops = [problem.op(op_id) for op_id in unit.op_ids]
                ready = problem.now
                blocked = False
                for op in ops:
                    ready = max(ready, op.release)
                    for pred in op.preds:
                        pred_end = ends.get(pred.op_id)
                        if pred_end is not None:
                            ready = max(ready, pred_end + pred.lag)
                        elif pred.op_id in sequenced:
                            blocked = True
                if blocked:
                    break
                head = ops[0]
                setup = setup_minutes(head.kind, tail_class, head.setup_class)
                duration = max(op.durations[machine] for op in ops)
                # StartOperation needs the material on site before SETUP begins, so a setup never runs ahead of it.
                start = max(ready, tail_end) + setup
                for down_start, down_end in downtimes[machine]:
                    if start - setup < down_end and start + duration > down_start:
                        start = down_end + setup
                for index, op in enumerate(ops):
                    end = start + op.durations[machine]
                    assignments.append(Assignment(op.id, machine, start, end, setup if index == 0 else 0, unit.batch_id))
                    ends[op.id] = end
                tail_end = start + duration
                tail_class = head.setup_class
                position += 1
                progress = True
            heads[machine] = position
            tails[machine] = (tail_end, tail_class)

    complete = all(heads[machine] == len(units) for machine, units in sequences.items())
    assignments.sort(key=lambda a: (a.start, a.machine, a.op_id))
    return assignments, complete


def _sequences_of(assignments: Iterable[Assignment]) -> dict[str, list[Unit]]:
    by_machine: dict[str, list[Assignment]] = {}
    for assignment in assignments:
        by_machine.setdefault(assignment.machine, []).append(assignment)
    sequences: dict[str, list[Unit]] = {}
    for machine, items in by_machine.items():
        items.sort(key=lambda a: (a.start, a.op_id))
        load_members: dict[str, list[str]] = {}
        order: list[tuple[list[str], str | None]] = []
        for assignment in items:
            if assignment.batch_id is None:
                order.append(([assignment.op_id], None))
            elif assignment.batch_id in load_members:
                load_members[assignment.batch_id].append(assignment.op_id)
            else:
                members = [assignment.op_id]
                load_members[assignment.batch_id] = members
                order.append((members, assignment.batch_id))
        sequences[machine] = [Unit(tuple(sorted(members)), batch_id) for members, batch_id in order]
    return sequences


def retime(problem: Problem, schedule: Schedule) -> Schedule:
    """Same machines, sequences and loads; every op as early as possible. Never makes any op end later."""
    if schedule.status == "no_solution":
        return schedule
    known_machines = {m.id for m in problem.machines}
    movable: list[Assignment] = []
    passthrough: list[Assignment] = []  # the forward pass cannot place these; left as they are for the validator
    for assignment in schedule.assignments:
        op = problem.op(assignment.op_id) if problem.has_op(assignment.op_id) else None
        if op is not None and op.frozen is not None:
            continue
        if op is not None and assignment.machine in known_machines and assignment.machine in op.durations:
            movable.append(assignment)
        else:
            passthrough.append(assignment)
    assignments, complete = forward_pass(problem, _sequences_of(movable))
    if not complete:
        return replace(
            schedule, status="no_solution", note="Maschinenfolge und Vorgänger bilden einen Zyklus, keine Terminierung."
        )
    return replace(schedule, assignments=tuple(assignments + passthrough))


def _stage(op: Op) -> int:
    return KINDS.index(op.kind)


def _respect_preds_on_machine(problem: Problem, units: list[Unit]) -> list[Unit]:
    """Move a unit behind any unit of the same machine it depends on; a melt chain can disagree with an old sequence."""
    if len(units) < 2:
        return units
    unit_of = {op_id: index for index, unit in enumerate(units) for op_id in unit.op_ids}
    needs = [
        {
            unit_of[pred.op_id]
            for op_id in unit.op_ids
            for pred in problem.op(op_id).preds
            if pred.op_id in unit_of and unit_of[pred.op_id] != index
        }
        for index, unit in enumerate(units)
    ]
    if not any(needs):
        return units
    ordered: list[Unit] = []
    done: set[int] = set()
    remaining = list(range(len(units)))
    while remaining:
        # Falls back to the head on a cycle, which forward_pass then reports.
        pick = next((index for index in remaining if needs[index] <= done), remaining[0])
        remaining.remove(pick)
        done.add(pick)
        ordered.append(units[pick])
    return ordered


def keep_schedule(problem: Problem, previous: Schedule) -> Schedule:
    """Carry the previous plan over to `problem`: its sequences retimed, gone ops dropped, new ops at machine tails."""
    started = perf_counter()
    frozen_batches = {op.frozen.batch_id for op in problem.ops if op.frozen is not None and op.frozen.batch_id}
    used_batches = frozen_batches | {a.batch_id for a in previous.assignments if a.batch_id}
    machines = {m.id: m for m in problem.machines}
    next_load = 1

    def mint() -> str:
        nonlocal next_load
        while True:
            batch_id = f"{problem.batch_prefix}-{next_load}"
            next_load += 1
            if batch_id not in used_batches:
                used_batches.add(batch_id)
                return batch_id

    sequences: dict[str, list[Unit]] = {machine: [] for machine in machines}
    placed: set[str] = set()
    open_loads: dict[tuple[str, str], int] = {}
    dropped = 0
    for assignment in sorted(previous.assignments, key=lambda a: (a.start, a.op_id)):
        if not problem.has_op(assignment.op_id):
            dropped += 1
            continue
        op = problem.op(assignment.op_id)
        machine = machines.get(assignment.machine)
        if op.frozen is not None or op.id in placed or machine is None:
            continue
        if assignment.machine not in op.durations or (machine.is_batch and op.size > machine.capacity):
            continue  # no longer possible there: treated as missing below
        placed.add(op.id)
        units = sequences[machine.id]
        if not machine.is_batch:
            units.append(Unit((op.id,)))
            continue
        # A pending op never joins a running load, so a load id still in use by a frozen op is retired for it.
        old_id = assignment.batch_id if assignment.batch_id not in frozen_batches else None
        index = open_loads.get((machine.id, old_id)) if old_id else None
        if index is not None:
            members = [problem.op(member) for member in units[index].op_ids]
            if (
                op.batch_family is not None
                and all(m.batch_family == op.batch_family for m in members)
                and all(m.durations[machine.id] == op.durations[machine.id] for m in members)
                and sum(m.size for m in members) + op.size <= machine.capacity
            ):
                units[index] = Unit(tuple(sorted(units[index].op_ids + (op.id,))), units[index].batch_id)
                continue
            old_id = None
        units.append(Unit((op.id,), old_id or mint()))
        if old_id:
            open_loads[(machine.id, old_id)] = len(units) - 1

    # Stage order, and problem order within a stage, keeps a melt chain in chain order at the tail.
    missing = sorted((op for op in problem.ops if op.frozen is None and op.id not in placed), key=_stage)
    appended = 0
    for op in missing:
        for machine_id in op.durations:
            machine = machines.get(machine_id)
            if machine is None or (machine.is_batch and op.size > machine.capacity):
                continue
            sequences[machine_id].append(Unit((op.id,), mint() if machine.is_batch else None))
            appended += 1
            break

    sequences = {machine: _respect_preds_on_machine(problem, units) for machine, units in sequences.items()}
    assignments, complete = forward_pass(problem, sequences)
    return Schedule(
        solver="keep",
        status="feasible" if complete else "no_solution",
        assignments=tuple(assignments),
        solve_seconds=perf_counter() - started,
        note=f"Bisheriger Plan: {appended} Vorgänge angehängt, {dropped} entfallen.",
    )
