"""Simulated annealing over the serial decoder: lot priorities, machine choices and furnace wait genes.

Geometric cooling, split into cycles: at the end of each the temperature is reheated and the search restarts from the
best candidate so far. The first cycle starts from the best rule decode, the same start plan CP-SAT gets.
"""
from __future__ import annotations

import math
import random
from dataclasses import replace
from time import perf_counter

from .decoder import WAIT_GENES, Compiled, best_rule_candidate, decode, decode_schedule
from .metrics import compute_metrics
from .model import Problem, Schedule
from .retime import frozen_assignments

_CYCLES = 4
_REHEAT = 0.5
_LOWEST_REHEAT = 0.05
_FINAL_TEMPERATURE_RATIO = 0.002
_CALIBRATION_MOVES = 40


def solve_anneal(problem: Problem, time_limit_s: float = 10, seed: int = 1, *, max_iterations: int | None = None) -> Schedule:
    """Anneal until `time_limit_s`, or `max_iterations` decodes if given, which makes a run repeat exactly by seed."""
    started = perf_counter()
    deadline = started + time_limit_s
    c = Compiled(problem)
    if c.n_ops == 0:
        return Schedule(
            solver="anneal",
            status="optimal",
            assignments=tuple(frozen_assignments(problem)),
            solve_seconds=perf_counter() - started,
            iterations=0,
            note="Nichts zu planen.",
        )
    if not c.placeable:
        return Schedule(
            solver="anneal",
            status="no_solution",
            assignments=(),
            solve_seconds=perf_counter() - started,
            iterations=0,
            note="Mindestens ein Vorgang hat keine geeignete Maschine.",
        )

    rng = random.Random(seed)
    rand = rng.random
    randrange = rng.randrange

    priority, wait = best_rule_candidate(c)
    priority, wait = list(priority), list(wait)
    machine_gene = c.machine_genes()
    choice_ops = [i for i in range(c.n_ops) if len(c.options[i]) > 1]
    wait_groups = [g for g in range(c.n_groups) if any(c.batch_op[i] for i in c.group_members[g])]
    n_groups = c.n_groups

    # Each move returns what undoing it needs.
    def apply_move():
        while True:
            roll = rand()
            if roll < 0.45 and n_groups > 1:
                a = randrange(n_groups)
                b = randrange(n_groups - 1)
                if b >= a:
                    b += 1
                if rand() < 0.5:
                    priority[a], priority[b] = priority[b], priority[a]
                    return (0, a, b)
                priority.insert(b, priority.pop(a))
                return (1, a, b)
            if roll < 0.70 and choice_ops:
                i = choice_ops[randrange(len(choice_ops))]
                previous = machine_gene[i]
                allele = randrange(-1, len(c.options[i]) - 1)  # the machine indexes and AUTO, minus the current value
                if allele >= previous:
                    allele += 1
                machine_gene[i] = allele
                return (2, i, previous)
            if roll >= 0.70 and wait_groups:
                g = wait_groups[randrange(len(wait_groups))]
                previous = wait[g]
                allele = WAIT_GENES[randrange(len(WAIT_GENES))]
                if allele == previous:
                    allele = WAIT_GENES[(WAIT_GENES.index(allele) + 1) % len(WAIT_GENES)]
                wait[g] = allele
                return (3, g, previous)

    def undo_move(move) -> None:
        kind, x, y = move
        if kind == 0:
            priority[x], priority[y] = priority[y], priority[x]
        elif kind == 1:
            priority.insert(x, priority.pop(y))
        elif kind == 2:
            machine_gene[x] = y
        else:
            wait[x] = y

    current = decode(c, priority, machine_gene, wait)
    best = current
    best_candidate = (priority[:], machine_gene[:], wait[:])
    start_plans = (best_candidate, (c.edd_priority(), machine_gene[:], c.wait_genes()))
    iterations = 1

    has_moves = n_groups > 1 or bool(choice_ops) or bool(wait_groups)
    limit = max_iterations if max_iterations is not None else 1 << 62

    # Calibration: typical worsening sets the start temperature, decode speed sets the cooling rate.
    worsenings = []
    calibration_start = perf_counter()
    calibrated = 0
    while has_moves and calibrated < _CALIBRATION_MOVES and iterations < limit and perf_counter() < deadline:
        move = apply_move()
        value = decode(c, priority, machine_gene, wait)
        iterations += 1
        calibrated += 1
        if value > current:
            worsenings.append(value - current)
        if value < best:
            best = value
            best_candidate = (priority[:], machine_gene[:], wait[:])
        undo_move(move)
    seconds_per_decode = (perf_counter() - calibration_start) / max(1, calibrated)

    if max_iterations is not None:
        planned = max(1, max_iterations - iterations)
    else:
        planned = max(1, int(max(0.0, deadline - perf_counter()) / max(seconds_per_decode, 1e-6)))
    cycle_length = max(1, planned // _CYCLES)
    start_temperature = max(1.0, sum(worsenings) / len(worsenings)) if worsenings else 1.0
    cooling = _FINAL_TEMPERATURE_RATIO ** (1.0 / cycle_length)
    temperature = start_temperature
    in_cycle = 0
    reheats = 0

    while has_moves and iterations < limit:
        if iterations & 15 == 0 and perf_counter() >= deadline:
            break
        move = apply_move()
        value = decode(c, priority, machine_gene, wait)
        iterations += 1
        delta = value - current
        if delta <= 0 or rand() < math.exp(-delta / temperature):
            current = value
            if value < best:
                best = value
                best_candidate = (priority[:], machine_gene[:], wait[:])
        else:
            undo_move(move)
        temperature *= cooling
        in_cycle += 1
        if in_cycle >= cycle_length:
            in_cycle = 0
            priority[:], machine_gene[:], wait[:] = best_candidate[0], best_candidate[1], best_candidate[2]
            current = best
            if reheats + 1 < _CYCLES:
                reheats += 1
                if max_iterations is None:
                    # A stall during calibration misjudges the speed, so each cycle plans from the rate actually reached.
                    now = perf_counter()
                    decodes_left = iterations / max(now - started, 1e-6) * max(0.0, deadline - now)
                    cycle_length = max(1, int(decodes_left) // (_CYCLES - reheats))
                    cooling = _FINAL_TEMPERATURE_RATIO ** (1.0 / cycle_length)
                temperature = start_temperature * max(_REHEAT**reheats, _LOWEST_REHEAT)
            else:
                # The last cycle came in ahead of the deadline: stay cold around the best plan. A short last cycle has
                # a steep cooling rate, and letting that run on would drive the temperature to zero.
                cooling = 1.0

    schedule = decode_schedule(c, *best_candidate, "anneal")
    # Fitness comes from the decode, and retime can improve a candidate unevenly, so the start plans get a last look.
    for start_plan in start_plans:
        if start_plan is not best_candidate:
            other = decode_schedule(c, *start_plan, "anneal")
            if compute_metrics(problem, other).objective < compute_metrics(problem, schedule).objective:
                schedule = other
    return replace(
        schedule,
        status="feasible",
        solve_seconds=perf_counter() - started,
        iterations=iterations,
        note=f"{iterations} Decodierungen, {reheats} Wiederaufheizungen.",
    )

