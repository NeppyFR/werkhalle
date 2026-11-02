"""What the process pools run. Every function here is top level and takes and returns plain picklable data.

Importing this module does not load ortools: the CP-SAT solver imports it inside the solve.
"""
from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence

from ..domain.orders import Lot
from ..scheduling.model import OrderInfo, Plan, Schedule
from ..simcore import planning_state_from_snapshot
from .planning import CandidateSet, finalize_candidates, prepare_problem, solve_all, solve_candidate
from .twin import evaluate_candidate_set, evaluate_scenario


def cpsat_workers() -> int:
    return max(2, min(8, (os.cpu_count() or 4) - 4))


def solve_stage(snapshot: Mapping, open_lots: Sequence[Lot], orders: Sequence[OrderInfo], active_plan: Plan | None, *, revision: int,
                melt_counter: int, solver_limit_s: float, seed: int, source: str,
                hint: Schedule | None = None) -> tuple[CandidateSet, float]:
    """The candidates for the next plan from one snapshot, each retimed, validated and scored. `hint` warm-starts CP-SAT.

    Runs in the `replan` pool. Returns the set and the wall clock when the solvers finished, for the failure chain.
    """
    state = planning_state_from_snapshot(snapshot)
    build = prepare_problem(state, open_lots, orders, active_plan, revision=revision, melt_counter=melt_counter)
    schedules = solve_all(
        build.problem,
        active_plan.schedule if active_plan is not None else None,
        limit=solver_limit_s,
        seed=seed,
        workers=cpsat_workers(),
        hint=hint,
    )
    candidate_set = finalize_candidates(build, schedules, revision=revision, lots=open_lots, created_min=state.now, source=source)
    return candidate_set, time.time()


def keep_stage(snapshot: Mapping, open_lots: Sequence[Lot], orders: Sequence[OrderInfo], active_plan: Plan, *, revision: int,
               melt_counter: int, source: str = "rolling") -> CandidateSet:
    """The active plan with new lots and their melts at the machine tails: no solver, retimed and validated like any candidate.

    The load ids get a prefix of their own, as a proposal computed meanwhile for this revision mints B-r<revision> ids.
    """
    state = planning_state_from_snapshot(snapshot)
    build = prepare_problem(state, open_lots, orders, active_plan, revision=revision, melt_counter=melt_counter,
                            batch_prefix=f"B-r{revision}a")
    schedules = {"keep": solve_candidate(build.problem, "keep", 0.0, 1, active_plan.schedule)}
    return finalize_candidates(build, schedules, revision=revision, lots=open_lots, created_min=state.now, source=source)


def twin_stage(snapshot: Mapping, candidate_set: CandidateSet, *, trigger: Mapping | None, based_on_revision: int | None,
               revision: int) -> dict:
    """Runs `keep` against every candidate on forks of the snapshot. In the `twin` pool. Proposal fields of docs/API.md, plus plan data."""
    return evaluate_candidate_set(snapshot, candidate_set, trigger=trigger, based_on_revision=based_on_revision, revision=revision)


def scenario_stage(snapshot: Mapping, active_plan: Plan | None, open_lots: Sequence[Lot], orders: Sequence[OrderInfo],
                   scenario: Sequence[Mapping], *, revision: int, melt_counter: int, solver_limit_s: float, seed: int,
                   monte_carlo: int) -> dict:
    """An explicit twin run, in the `twin` pool: variants A, B and C of docs/API.md."""
    return evaluate_scenario(snapshot, active_plan, open_lots, orders, scenario, revision=revision, melt_counter=melt_counter,
                             solver_limit_s=solver_limit_s, seed=seed, monte_carlo=monte_carlo, workers=cpsat_workers())
