"""The spawn process pools of PLAN section 8: `replan` and `twin` for the failure path, `manual` for what an operator asks for.

Manual solves and explicit twin runs, Monte Carlo included, may take minutes. They run in their own low-priority pool, so a fault
never queues behind them.

This module is what a pool worker imports first, so it stays light. Solvers and the twin are imported by the job functions
inside the worker, never here: the event loop process must not pay for ortools.

A worker that outlives the MES would hold its CPU for the rest of a solve and, on Windows, its files. So each worker runs a
thread that waits for its parent and ends the process with it.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial
from typing import Any

logger = logging.getLogger(__name__)

PLANT_MODULE = "werkhalle.plant"
REPLAN_WORKERS = 2


def twin_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 2))


def _watch_parent() -> None:
    parent = multiprocessing.parent_process()
    if parent is None:
        return
    parent.join()
    os._exit(3)


def worker_init() -> None:
    """Pool initializer: die with the parent."""
    threading.Thread(target=_watch_parent, name="watch-parent", daemon=True).start()


def worker_init_low_priority() -> None:
    """Pool initializer of the `manual` pool: die with the parent, and leave the CPU to the failure path."""
    worker_init()
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        below_normal = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(wintypes.HANDLE(-1), below_normal)


def manual_workers() -> int:
    return max(1, min(2, (os.cpu_count() or 2) - 2))


def worker_ping() -> tuple[int, bool]:
    """Forces a worker to exist. Returns its pid and whether the plant module got into it."""
    return os.getpid(), PLANT_MODULE in sys.modules


def run_job(job: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, bool, float]:
    """Runs in the worker. Reports along with the result whether the plant module is loaded here and the compute time, without any queue wait."""
    started = time.perf_counter()
    result = job(*args, **kwargs)
    return result, PLANT_MODULE in sys.modules, time.perf_counter() - started


class ManagedPool:
    """A spawn executor that is rebuilt once when a worker dies, and says so."""

    def __init__(self, name: str, workers: int, *, on_retry: Callable[[str], None] | None = None, low_priority: bool = False) -> None:
        self.name = name
        self.workers = workers
        self._initializer = worker_init_low_priority if low_priority else worker_init
        self.plant_module_loaded = False
        self._on_retry = on_retry
        self._executor = self._new_executor()

    def _new_executor(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=self._initializer,
        )

    async def warm_up(self) -> None:
        """Start the workers now, so the first real solve is not delayed by spawning them."""
        loop = asyncio.get_running_loop()
        results = await asyncio.gather(
            *(loop.run_in_executor(self._executor, worker_ping) for _ in range(self.workers)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, tuple) and result[1]:
                self.plant_module_loaded = True

    async def run(self, job: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a job in the pool. On `BrokenProcessPool` the pool is rebuilt and the job retried once; a second failure raises."""
        result, _ = await self.run_timed(job, *args, **kwargs)
        return result

    async def run_timed(self, job: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, float]:
        """`run`, and how long the job computed in the worker, leaving out the wait for a free worker."""
        call = partial(run_job, job, *args, **kwargs)
        loop = asyncio.get_running_loop()
        executor = self._executor
        try:
            result, loaded, seconds = await loop.run_in_executor(executor, call)
        except BrokenProcessPool:
            if executor is self._executor:
                self._rebuild()
            message = f"Rechenprozess im Pool {self.name} abgestürzt, Pool neu aufgebaut, Berechnung wird wiederholt."
            logger.warning(message)
            if self._on_retry is not None:
                self._on_retry(message)
            result, loaded, seconds = await loop.run_in_executor(self._executor, call)
        self.plant_module_loaded = self.plant_module_loaded or loaded
        return result, seconds

    def _rebuild(self) -> None:
        old = self._executor
        self._executor = self._new_executor()
        old.shutdown(wait=False, cancel_futures=True)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["BrokenProcessPool", "ManagedPool", "REPLAN_WORKERS", "manual_workers", "run_job", "twin_workers", "worker_init",
           "worker_init_low_priority", "worker_ping"]
