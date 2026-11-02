"""CP-SAT model: a circuit on every machine, parallel join and next arcs on furnaces, setups priced on every arc.

Node order on a machine is op id order, so the first member of a load is the one that carries its setup. That is the
same convention `retime` uses, which is what lets the hint assign every single variable, arc literals included.
ortools is imported inside the functions: `execution` imports `scheduling.model` and should not pay for it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any

from ..domain.catalog import setup_minutes
from .decoder import decode_best_rule, decode_edd
from .metrics import compute_metrics
from .model import Assignment, Op, Problem, Schedule
from .retime import frozen_assignments, keep_schedule, machine_start_state, retime
from .validate import validate_schedule


@dataclass
class _Arc:
    tail: int  # op index, -1 for the depot
    head: int
    literal: Any
    join: bool = False


@dataclass
class _MachineModel:
    machine_id: str
    nodes: list[int]  # op indexes in node order
    arcs: list[_Arc] = field(default_factory=list)
    self_loops: dict[int, Any] = field(default_factory=dict)
    empty: Any = None


@dataclass
class _Built:
    model: Any
    ops: list[Op]
    start: list[Any]
    end: list[Any]
    setup: list[Any]
    load_setup: dict[int, Any]  # furnace ops: the setup of the load they are in, which a joining op's setup is not
    presence: dict[tuple[int, str], Any]  # (op, machine) -> literal, only for ops with a real choice
    eligible: list[dict[str, int]]
    machines: list[_MachineModel]
    downtime_literals: list[tuple[Any, int, int]]  # literal "op ends before the downtime", op, downtime start
    completions: list[tuple[Any, Any, list[int], int, int]]  # completion var, tardiness var, end ops, fixed end, due
    makespan: Any
    makespan_ops: list[int]
    makespan_fixed: int


def _setup(op: Op, from_class: str | None) -> int:
    return setup_minutes(op.kind, from_class, op.setup_class)


def _earliest_starts(problem: Problem, ops: list[Op], eligible: list[dict[str, int]], bases: dict[str, int]) -> list[int]:
    index = {op.id: i for i, op in enumerate(ops)}
    frozen_end = {op.id: op.frozen.end for op in problem.ops if op.frozen is not None}
    memo: dict[int, int] = {}

    def earliest(i: int) -> int:
        if i in memo:
            return memo[i]
        op = ops[i]
        value = max(problem.now, op.release, min(bases[m] for m in eligible[i]))
        for pred in op.preds:
            if pred.op_id in frozen_end:
                value = max(value, frozen_end[pred.op_id] + pred.lag)
            elif pred.op_id in index:
                p = index[pred.op_id]
                value = max(value, earliest(p) + min(eligible[p].values()) + pred.lag)
        memo[i] = value
        return value

    return [earliest(i) for i in range(len(ops))]


def _build_model(problem: Problem, hint: Schedule | None) -> _Built:
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    machines = {m.id: m for m in problem.machines}
    start_state = machine_start_state(problem)
    bases = {machine: state[0] for machine, state in start_state.items()}
    ops = sorted((op for op in problem.ops if op.frozen is None), key=lambda op: op.id)
    index = {op.id: i for i, op in enumerate(ops)}
    eligible = [
        {
            m: d
            for m, d in op.durations.items()
            if m in machines and (not machines[m].is_batch or op.size <= machines[m].capacity)
        }
        for op in ops
    ]

    classes_on: dict[str, set[str | None]] = {m: {state[1]} for m, state in start_state.items()}
    for i, op in enumerate(ops):
        for m in eligible[i]:
            classes_on[m].add(op.setup_class)
    max_setup = [max(_setup(op, c) for m in eligible[i] for c in classes_on[m]) for i, op in enumerate(ops)]

    earliest = _earliest_starts(problem, ops, eligible, bases)
    frozen_end = {op.id: op.frozen.end for op in problem.ops if op.frozen is not None}
    down_end = max([d.end for m in problem.machines for d in m.downtimes], default=problem.now)
    # A plan that runs one op after the other once everything else is over always exists within this bound.
    horizon = max(
        problem.now, down_end, *bases.values(), *(op.release for op in ops), *(earliest[i] for i in range(len(ops)))
    )
    for i, op in enumerate(ops):
        horizon += max(eligible[i].values()) + max_setup[i] + max((p.lag for p in op.preds), default=0)
    if hint is not None:
        metrics = compute_metrics(problem, hint)
        hint_end = max(a.end for a in hint.assignments)
        weights = problem.weights
        if weights.makespan > 0:
            # Past the last due date every further minute costs makespan and the lightest order's tardiness together,
            # so a plan ending later than this is worse than the hint. A spare melt at the end costs makespan only.
            lightest = min((o.weight for o in problem.orders), default=0) * weights.tardiness
            followed = {pred.op_id for op in problem.ops for pred in op.preds}
            if any(op.order_id is None for op in ops if op.id not in followed):
                lightest = 0
            latest_due = max((o.due for o in problem.orders), default=problem.now)
            bound = (metrics.objective + lightest * latest_due + weights.makespan * problem.now) // (lightest + weights.makespan)
            horizon = min(horizon, max(bound, problem.now + 1))
        horizon = max(horizon, hint_end)

    start: list[Any] = []
    end: list[Any] = []
    setup: list[Any] = []
    presence: dict[tuple[int, str], Any] = {}
    for i, op in enumerate(ops):
        shortest = min(eligible[i].values())
        start.append(model.new_int_var(earliest[i], max(earliest[i], horizon - shortest), f"s{i}"))
        end.append(model.new_int_var(earliest[i] + shortest, max(earliest[i] + shortest, horizon), f"e{i}"))
        setup.append(model.new_int_var(0, max_setup[i], f"u{i}"))
        if len(eligible[i]) == 1:
            model.add(end[i] == start[i] + next(iter(eligible[i].values())))
        else:
            literals = []
            for m, d in eligible[i].items():
                literal = model.new_bool_var(f"x{i}_{m}")
                presence[(i, m)] = literal
                literals.append(literal)
                model.add(end[i] == start[i] + d).only_enforce_if(literal)
            model.add_exactly_one(literals)

    # Material has to be there when the setup begins, since StartOperation needs it before SETUP. A load goes in as
    # one, so a joining op has to meet the load's setup, not its own 0. Lower bounds are enough here: nothing gains
    # from a load_setup larger than the setup it stands for.
    load_setup: dict[int, Any] = {}
    for i in range(len(ops)):
        if any(machines[m].is_batch and ops[i].batch_family is not None for m in eligible[i]):
            load_setup[i] = model.new_int_var(0, max_setup[i], f"ls{i}")
            model.add(load_setup[i] >= setup[i])
    for i, op in enumerate(ops):
        begins = start[i] - load_setup.get(i, setup[i])
        ready = op.release
        for pred in op.preds:
            if pred.op_id in index:
                model.add(begins >= end[index[pred.op_id]] + pred.lag)
            elif pred.op_id in frozen_end:
                ready = max(ready, frozen_end[pred.op_id] + pred.lag)
        if ready > problem.now:
            model.add(begins >= ready)

    setup_terms: list[list[tuple[Any, int]]] = [[] for _ in ops]
    machine_models: list[_MachineModel] = []
    downtime_literals: list[tuple[Any, int, int]] = []
    for machine in problem.machines:
        nodes = [i for i in range(len(ops)) if machine.id in eligible[i]]
        if not nodes:
            continue
        built = _MachineModel(machine.id, nodes)
        machine_models.append(built)
        base, mounted = start_state[machine.id]
        node_of = {i: k + 1 for k, i in enumerate(nodes)}
        arcs: list[tuple[int, int, Any]] = []
        for i in nodes:
            literal = presence.get((i, machine.id))
            if literal is not None:
                built.self_loops[i] = literal
                arcs.append((node_of[i], node_of[i], literal.Not()))
            first = model.new_bool_var(f"f{i}_{machine.id}")
            last = model.new_bool_var(f"l{i}_{machine.id}")
            arcs.append((0, node_of[i], first))
            arcs.append((node_of[i], 0, last))
            built.arcs.append(_Arc(-1, i, first))
            built.arcs.append(_Arc(i, -1, last))
            minutes = _setup(ops[i], mounted)
            model.add(start[i] >= base + minutes).only_enforce_if(first)
            if minutes:
                setup_terms[i].append((first, minutes))
        for i in nodes:
            for j in nodes:
                if i == j:
                    continue
                literal = model.new_bool_var(f"n{i}_{j}_{machine.id}")
                arcs.append((node_of[i], node_of[j], literal))
                built.arcs.append(_Arc(i, j, literal))
                minutes = _setup(ops[j], ops[i].setup_class)
                model.add(start[j] >= end[i] + minutes).only_enforce_if(literal)
                if minutes:
                    setup_terms[j].append((literal, minutes))
        if machine.is_batch:
            for position, i in enumerate(nodes):
                for j in nodes[position + 1 :]:
                    family = ops[i].batch_family
                    if family is None or family != ops[j].batch_family:
                        continue
                    if eligible[i][machine.id] != eligible[j][machine.id]:
                        continue
                    literal = model.new_bool_var(f"j{i}_{j}_{machine.id}")
                    arcs.append((node_of[i], node_of[j], literal))
                    built.arcs.append(_Arc(i, j, literal, join=True))
                    model.add(start[j] == start[i]).only_enforce_if(literal)
                    model.add(load_setup[j] >= load_setup[i]).only_enforce_if(literal)
        if all(i in built.self_loops for i in nodes):
            built.empty = model.new_bool_var(f"empty_{machine.id}")
            arcs.append((0, 0, built.empty))
        model.add_circuit(arcs)

        intervals = []
        for i in nodes:
            d = eligible[i][machine.id]
            literal = presence.get((i, machine.id))
            if literal is None:
                intervals.append(model.new_fixed_size_interval_var(start[i], d, f"iv{i}_{machine.id}"))
            else:
                intervals.append(model.new_optional_fixed_size_interval_var(start[i], d, literal, f"iv{i}_{machine.id}"))
        if machine.is_batch:
            sizes = [ops[i].size for i in nodes]
            if sum(sizes) > machine.capacity:
                model.add_cumulative(intervals, sizes, machine.capacity)
        else:
            model.add_no_overlap(intervals)

        for downtime in sorted(machine.downtimes, key=lambda d: d.start):
            for i in nodes:
                d = eligible[i][machine.id]
                if earliest[i] - max_setup[i] >= downtime.end:
                    continue  # cannot reach this downtime at all
                present = presence.get((i, machine.id))
                enforce = [present] if present is not None else []
                if earliest[i] + d > downtime.start:
                    # Too late to finish before it, so the op, setup included, has to come after.
                    model.add(start[i] - setup[i] >= downtime.end).only_enforce_if(*enforce)
                    continue
                before = model.new_bool_var(f"b{i}_{machine.id}_{downtime.start}")
                downtime_literals.append((before, i, downtime.start))
                model.add(end[i] <= downtime.start).only_enforce_if(before, *enforce)
                model.add(start[i] - setup[i] >= downtime.end).only_enforce_if(before.Not(), *enforce)

    for i in range(len(ops)):
        model.add(setup[i] == sum(literal * minutes for literal, minutes in setup_terms[i]))

    successors_in_order: set[int] = set()
    has_successor: set[int] = set()
    for i, op in enumerate(ops):
        for pred in op.preds:
            if pred.op_id in index:
                has_successor.add(index[pred.op_id])
                if ops[index[pred.op_id]].order_id == op.order_id:
                    successors_in_order.add(index[pred.op_id])

    fixed: dict[str, int] = {o.id: o.completed_lots_end for o in problem.orders if o.completed_lots_end is not None}
    for op in problem.ops:
        if op.frozen is not None and op.order_id is not None:
            fixed[op.order_id] = max(fixed.get(op.order_id, op.frozen.end), op.frozen.end)

    objective_terms: list[Any] = []
    constant = 0
    weights = problem.weights
    completions: list[tuple[Any, Any, list[int], int, int]] = []
    for order in problem.orders:
        order_ends = [i for i, op in enumerate(ops) if op.order_id == order.id and i not in successors_in_order]
        base = fixed.get(order.id)
        if not order_ends:
            if base is not None:
                constant += weights.tardiness * order.weight * max(0, base - order.due)
            continue
        lowest = max([base if base is not None else 0, *(earliest[i] for i in order_ends)])
        highest = max(horizon, lowest)
        completion = model.new_int_var(lowest, highest, f"c_{order.id}")
        tardiness = model.new_int_var(0, max(0, highest - order.due), f"t_{order.id}")
        for i in order_ends:
            model.add(completion >= end[i])
        model.add(tardiness >= completion - order.due)
        completions.append((completion, tardiness, order_ends, base if base is not None else -1, order.due))
        objective_terms.append(weights.tardiness * order.weight * tardiness)

    makespan_ops = [i for i in range(len(ops)) if i not in has_successor]
    makespan_fixed = max([problem.now, *frozen_end.values()])
    makespan = model.new_int_var(0, max(horizon, makespan_fixed) - problem.now, "makespan")
    model.add(makespan >= makespan_fixed - problem.now)
    for i in makespan_ops:
        model.add(makespan >= end[i] - problem.now)
    objective_terms.append(weights.makespan * makespan)
    objective_terms.append(weights.setup * sum(setup))
    model.minimize(sum(objective_terms) + constant)

    return _Built(
        model=model,
        ops=ops,
        start=start,
        end=end,
        setup=setup,
        load_setup=load_setup,
        presence=presence,
        eligible=eligible,
        machines=machine_models,
        downtime_literals=downtime_literals,
        completions=completions,
        makespan=makespan,
        makespan_ops=makespan_ops,
        makespan_fixed=makespan_fixed,
    )


def _add_complete_hint(built: _Built, problem: Problem, hint: Schedule) -> None:
    """Hint every variable of the model. Partial hints made CP-SAT 80 to 190 times worse at 32 to 48 lots."""
    model = built.model
    by_op = {a.op_id: a for a in hint.assignments}
    index = {op.id: i for i, op in enumerate(built.ops)}
    start_state = machine_start_state(problem)
    setup_value = [0] * len(built.ops)
    load_setup_value = [0] * len(built.ops)
    machine_of: dict[int, str] = {}

    for machine in built.machines:
        on_machine = sorted(
            (by_op[built.ops[i].id] for i in machine.nodes if by_op[built.ops[i].id].machine == machine.machine_id),
            key=lambda a: (a.start, a.op_id),
        )
        # Walk the machine's path: depot, then units in order, members of a load in node order, then back to the depot.
        taken: set[tuple[int, int, bool]] = set()
        previous = -1
        previous_class = start_state[machine.machine_id][1]
        previous_batch = None
        for a in on_machine:
            i = index[a.op_id]
            machine_of[i] = machine.machine_id
            joins = a.batch_id is not None and a.batch_id == previous_batch
            taken.add((previous, i, joins))
            if joins:
                load_setup_value[i] = load_setup_value[previous]
            else:
                setup_value[i] = load_setup_value[i] = _setup(built.ops[i], previous_class)
            previous = i
            previous_class = built.ops[i].setup_class
            previous_batch = a.batch_id
        taken.add((previous, -1, False))
        for arc in machine.arcs:
            model.add_hint(arc.literal, 1 if (arc.tail, arc.head, arc.join) in taken else 0)
        if machine.empty is not None:
            model.add_hint(machine.empty, 1 if not on_machine else 0)

    for i, op in enumerate(built.ops):
        a = by_op[op.id]
        model.add_hint(built.start[i], a.start)
        model.add_hint(built.end[i], a.end)
        model.add_hint(built.setup[i], setup_value[i])
        if i in built.load_setup:
            model.add_hint(built.load_setup[i], load_setup_value[i])
    for (i, machine_id), literal in built.presence.items():
        model.add_hint(literal, 1 if machine_of[i] == machine_id else 0)
    for literal, i, down_start in built.downtime_literals:
        model.add_hint(literal, 1 if by_op[built.ops[i].id].end <= down_start else 0)
    for completion, tardiness, order_ends, base, due in built.completions:
        value = max([base, *(by_op[built.ops[i].id].end for i in order_ends)])
        model.add_hint(completion, value)
        model.add_hint(tardiness, max(0, value - due))
    latest = max([built.makespan_fixed, *(by_op[built.ops[i].id].end for i in built.makespan_ops)])
    model.add_hint(built.makespan, latest - problem.now)


def _extract(built: _Built, problem: Problem, solver: Any) -> list[Assignment]:
    assignments = frozen_assignments(problem)
    taken = {a.batch_id for a in assignments if a.batch_id}
    machine_of: dict[int, str] = {}
    for i in range(len(built.ops)):
        if len(built.eligible[i]) == 1:
            machine_of[i] = next(iter(built.eligible[i]))
    for (i, machine_id), literal in built.presence.items():
        if solver.boolean_value(literal):
            machine_of[i] = machine_id
    starts = [solver.value(var) for var in built.start]

    # Follow the join arcs to find the loads.
    load_of: dict[int, int] = {}
    batch_machines = {m.id for m in problem.machines if m.is_batch}
    next_join: dict[int, int] = {}
    joined: set[int] = set()
    for machine in built.machines:
        if machine.machine_id in batch_machines:
            for arc in machine.arcs:
                if arc.join and solver.boolean_value(arc.literal):
                    next_join[arc.tail] = arc.head
                    joined.add(arc.head)
    heads = [i for i in range(len(built.ops)) if machine_of[i] in batch_machines and i not in joined]
    number = 0
    batch_ids: dict[int, str] = {}
    for head in sorted(heads, key=lambda i: (starts[i], machine_of[i], i)):
        number += 1
        while f"{problem.batch_prefix}-{number}" in taken:
            number += 1
        batch_ids[head] = f"{problem.batch_prefix}-{number}"
        member = head
        while member is not None:
            load_of[member] = head
            member = next_join.get(member)

    for i, op in enumerate(built.ops):
        assignments.append(
            Assignment(
                op.id,
                machine_of[i],
                starts[i],
                solver.value(built.end[i]),
                solver.value(built.setup[i]),
                batch_ids[load_of[i]] if i in load_of else None,
            )
        )
    return assignments


def _usable_hint(problem: Problem, hint: Schedule | None) -> tuple[Schedule, str]:
    """The best of the EDD decode, the best rule decode and the caller's hint, carried over to this problem if it still validates."""
    best = decode_edd(problem)
    rule = decode_best_rule(problem)
    if compute_metrics(problem, rule).objective < compute_metrics(problem, best).objective and not validate_schedule(problem, rule):
        best = rule
    note = ""
    if hint is not None:
        carried = keep_schedule(problem, hint)
        if carried.status != "no_solution" and not validate_schedule(problem, carried):
            if compute_metrics(problem, carried).objective <= compute_metrics(problem, best).objective:
                best = carried
        else:
            note = "Der übergebene Hinweis war nicht gültig und wurde durch die beste Startregel ersetzt."
    return best, note


def solve_cpsat(
    problem: Problem, time_limit_s: float = 10, seed: int = 1, workers: int = 8, hint: Schedule | None = None
) -> Schedule:
    """Solve to `time_limit_s`. Never raises on timeout; returns status optimal, feasible or no_solution."""
    from ortools.sat.python import cp_model

    started = perf_counter()
    if not any(op.frozen is None for op in problem.ops):
        schedule = Schedule(solver="cpsat", status="optimal", assignments=tuple(frozen_assignments(problem)))
        objective = compute_metrics(problem, schedule).objective
        return replace(
            schedule,
            solve_seconds=perf_counter() - started,
            best_bound=float(objective),
            solver_objective=objective,
            note="Nichts zu planen.",
        )
    machine_ids = {m.id for m in problem.machines}
    for op in problem.ops:
        fits = any(
            m in machine_ids and (not problem.machine(m).is_batch or op.size <= problem.machine(m).capacity)
            for m in op.durations
        )
        if op.frozen is None and not fits:
            return Schedule(
                solver="cpsat",
                status="no_solution",
                assignments=(),
                solve_seconds=perf_counter() - started,
                note=f"{op.id} hat keine geeignete Maschine.",
            )

    fallback, note = _usable_hint(problem, hint)
    fallback_objective = compute_metrics(problem, fallback).objective
    built = _build_model(problem, fallback)
    _add_complete_hint(built, problem, fallback)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.05, time_limit_s - (perf_counter() - started))
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = seed
    # Default presolve probes for over 3 s at 25 lots and left nothing of a 3 s budget; the bound barely suffers.
    solver.parameters.cp_model_probing_level = 0
    solver.parameters.max_presolve_iterations = 1
    # The arc constraints give the LP nothing to work with; primal progress here comes from LNS, which by default gets
    # only what the six full-problem workers leave over, 2 threads of 8.
    solver.parameters.linearization_level = 0
    solver.parameters.num_full_subsolvers = min(2, max(1, workers - 2))
    status = solver.solve(built.model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        solver_objective = round(solver.objective_value)
        raw = Schedule(solver="cpsat", status="feasible", assignments=tuple(_extract(built, problem, solver)))
        schedule = retime(problem, raw)
        objective = compute_metrics(problem, schedule).objective
        label = "optimal" if status == cp_model.OPTIMAL else "feasible"
        if objective <= fallback_objective:
            if objective != solver_objective:
                note = f"{note} Nachterminierung verbessert das Ziel von {solver_objective} auf {objective}.".strip()
            return replace(
                schedule,
                status=label,
                solve_seconds=perf_counter() - started,
                best_bound=float(solver.best_objective_bound),
                note=note,
                solver_objective=solver_objective,
            )
        note = f"{note} CP-SAT blieb bei {objective} über der Startlösung mit {fallback_objective}.".strip()
    else:
        note = f"{note} CP-SAT fand in der Zeit keine eigene Lösung ({solver.status_name(status)}).".strip()

    # The start solution is a valid plan. Better to return it, saying so, than nothing.
    best_bound = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        best_bound = float(solver.best_objective_bound)
    if validate_schedule(problem, fallback):
        return Schedule(
            solver="cpsat", status="no_solution", assignments=(), solve_seconds=perf_counter() - started, note=note
        )
    return replace(
        fallback,
        solver="cpsat",
        status="feasible",
        solve_seconds=perf_counter() - started,
        best_bound=best_bound,
        note=f"{note} Startlösung übernommen.".strip(),
        solver_objective=fallback_objective,
    )
