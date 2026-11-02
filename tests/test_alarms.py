"""Alarm rows from Alarm* DataChanges. Delivery order is deliberately hostile: the server groups a publish by monitored item."""
from __future__ import annotations

import pytest

from werkhalle.mes.alarms import AlarmTracker
from werkhalle.mes.opcua_client import DataChange

BASE = 1_793_000_000.0


def change(name: str, value, ts: float, *, machine: str = "PR2", baseline: bool = False, received: float | None = None) -> DataChange:
    received_ts = ts + 0.05 if received is None else received
    return DataChange(
        node_id=f"ns=2;s=Werkhalle.{machine}.{name}",
        path=f"Werkhalle/Machines/{machine}/{name}",
        machine=machine,
        name=name,
        value=value,
        source_ts=ts,
        received_ts=received_ts,
        latency_ms=(received_ts - ts) * 1000.0,
        baseline=baseline,
    )


def raise_alarm(alarm_id: int, code: str, text: str, severity: int, ts: float, **kw) -> list[DataChange]:
    return [
        change("AlarmId", alarm_id, ts, **kw),
        change("AlarmActive", True, ts, **kw),
        change("AlarmCode", code, ts, **kw),
        change("AlarmText", text, ts, **kw),
        change("AlarmSeverity", severity, ts, **kw),
    ]


def sim_clock(wall_ts: float) -> float:
    """Factor 120: 2 sim minutes per wall second, with t=1000 at BASE."""
    return 1000.0 + (wall_ts - BASE) * 2.0


@pytest.fixture
def tracker() -> AlarmTracker:
    return AlarmTracker(sim_clock=sim_clock)


class TestRows:
    async def test_fault_row_has_the_api_shape(self, tracker):
        await tracker.apply_batch([*raise_alarm(3, "P-204", "Hauptzylinder undicht", 900, BASE),
                                   change("Status", "FAULT", BASE, received=BASE + 0.062)])
        row = tracker.active()[0]
        assert row["id"] == "PR2:3"
        assert row["machine"] == "PR2"
        assert row["alarm_id"] == 3
        assert row["code"] == "P-204"
        assert row["text"] == "Hauptzylinder undicht"
        assert row["severity"] == 900
        assert row["t"] == pytest.approx(1000.0)
        assert row["detected_wall_ts"] == pytest.approx(BASE + 0.062)
        assert row["latency_ms"] == pytest.approx(62.0)
        assert row["acked"] is False
        assert row["active"] is True
        assert row["cleared_t"] is None

    async def test_fault_timestamps_come_from_the_status_data_value(self, tracker):
        # The Alarm* values carry a slightly earlier timestamp; the failure chain measures from Status.
        status_ts = BASE + 0.0004
        await tracker.apply_batch([*raise_alarm(3, "P-204", "x", 900, BASE),
                                   change("Status", "FAULT", status_ts, received=status_ts + 0.1)])
        row = tracker.get("PR2:3")
        assert row["source_ts"] == status_ts
        assert row["latency_ms"] == pytest.approx(100.0)
        assert row["t"] == pytest.approx(sim_clock(status_ts), abs=1e-3)

    async def test_status_delivered_ahead_of_the_alarm_values_still_links(self, tracker):
        # What the server's grouping by monitored item really produces: Status first, because it changed first in this publish.
        batch = [change("Status", "RUN", BASE - 0.05),
                 change("Status", "FAULT", BASE + 0.0002),
                 *raise_alarm(3, "P-204", "x", 900, BASE)]
        await tracker.apply_batch(batch)
        row = tracker.get("PR2:3")
        assert row["code"] == "P-204"
        assert row["source_ts"] == BASE + 0.0002

    async def test_warning_keeps_its_own_timestamp_and_ignores_status(self, tracker):
        await tracker.apply_batch(raise_alarm(5, "P-W01", "Blocktemperatur", 500, BASE))
        row = tracker.get("PR2:5")
        assert row["severity"] == 500
        assert row["source_ts"] == BASE
        assert row["latency_ms"] == pytest.approx(50.0)

    async def test_clear_sets_cleared_time_and_leaves_active_list(self, tracker):
        await tracker.apply_batch(raise_alarm(5, "P-W01", "Blocktemperatur", 500, BASE))
        await tracker.apply_batch([change("AlarmActive", False, BASE + 30.0)])
        assert tracker.active() == []
        row = tracker.rows()[0]
        assert row["active"] is False
        assert row["cleared_t"] == pytest.approx(1060.0)
        assert row["cleared_ts"] == BASE + 30.0

    async def test_blanked_variables_after_a_clear_do_not_erase_the_row(self, tracker):
        await tracker.apply_batch(raise_alarm(5, "P-W01", "Blocktemperatur", 500, BASE))
        await tracker.apply_batch([change("AlarmActive", False, BASE + 30.0), change("AlarmCode", "", BASE + 30.0),
                                   change("AlarmText", "", BASE + 30.0), change("AlarmSeverity", 0, BASE + 30.0)])
        row = tracker.get("PR2:5")
        assert (row["code"], row["text"], row["severity"]) == ("P-W01", "Blocktemperatur", 500)

    async def test_second_alarm_with_unchanged_code_inherits_it(self, tracker):
        # The server reports only changes, so an identical code sends no notification for the new alarm.
        await tracker.apply_batch(raise_alarm(5, "P-W01", "Blocktemperatur", 500, BASE))
        await tracker.apply_batch([change("AlarmActive", False, BASE + 30.0)])
        await tracker.apply_batch([change("AlarmId", 6, BASE + 60.0), change("AlarmActive", True, BASE + 60.0)])
        row = tracker.get("PR2:6")
        assert (row["code"], row["text"], row["severity"], row["active"]) == ("P-W01", "Blocktemperatur", 500, True)

    async def test_rows_are_per_machine_with_same_alarm_id(self, tracker):
        await tracker.apply_batch(raise_alarm(1, "P-W01", "a", 500, BASE, machine="PR1"))
        await tracker.apply_batch(raise_alarm(1, "O-W01", "b", 500, BASE + 1.0, machine="OF1"))
        await tracker.apply_batch([change("AlarmActive", False, BASE + 2.0, machine="PR1")])
        assert tracker.get("PR1:1")["active"] is False
        assert tracker.get("OF1:1")["active"] is True

    async def test_ignores_other_variables(self, tracker):
        rows = await tracker.apply_batch([change("Temperature", 400.0, BASE), change("Status", "RUN", BASE)])
        assert rows == []
        assert tracker.rows() == []

    async def test_ordering_newest_first_and_limit(self, tracker):
        for i in range(1, 6):
            await tracker.apply_batch(raise_alarm(i, f"C{i}", "x", 500, BASE + i))
        assert [r["alarm_id"] for r in tracker.rows()] == [5, 4, 3, 2, 1]
        assert [r["alarm_id"] for r in tracker.rows(limit=2)] == [5, 4]
        assert [r["alarm_id"] for r in tracker.active()] == [5]


class TestIdentity:
    async def test_warning_clear_cannot_close_a_fault(self, tracker):
        await tracker.apply_batch(raise_alarm(3, "P-W01", "Warnung", 500, BASE))
        # The warning clears at t1, the fault is raised at t2, and both land in one publish. Grouped by monitored item, the clear
        # arrives after AlarmId=4. By arrival order it would close the fault.
        t1, t2 = BASE + 10.0, BASE + 10.05
        batch = [change("AlarmId", 4, t2),
                 change("AlarmActive", False, t1),
                 change("AlarmActive", True, t2),
                 change("AlarmCode", "P-204", t2),
                 change("AlarmText", "Hauptzylinder undicht", t2),
                 change("AlarmSeverity", 900, t2),
                 change("Status", "FAULT", t2)]
        await tracker.apply_batch(batch)
        warning, fault = tracker.get("PR2:3"), tracker.get("PR2:4")
        assert warning["active"] is False
        assert warning["cleared_ts"] == t1
        assert fault["active"] is True
        assert fault["cleared_t"] is None
        assert fault["code"] == "P-204"
        assert [r["id"] for r in tracker.active()] == ["PR2:4"]

    async def test_late_clear_in_a_later_batch_cannot_close_a_fault(self, tracker):
        await tracker.apply_batch(raise_alarm(3, "P-W01", "Warnung", 500, BASE))
        await tracker.apply_batch(raise_alarm(4, "P-204", "Fault", 900, BASE + 10.0))
        await tracker.apply_batch([change("AlarmActive", False, BASE + 5.0)])
        assert tracker.get("PR2:4")["active"] is True
        assert tracker.get("PR2:3")["active"] is False

    async def test_fault_replacing_a_warning_closes_it_without_touching_alarm_active(self, tracker):
        await tracker.apply_batch(raise_alarm(3, "P-W01", "Warnung", 500, BASE))
        # AlarmActive stays true, so the server sends no notification for it.
        await tracker.apply_batch([change("AlarmId", 4, BASE + 10.0), change("AlarmCode", "P-204", BASE + 10.0),
                                   change("AlarmText", "Hauptzylinder", BASE + 10.0), change("AlarmSeverity", 900, BASE + 10.0)])
        assert tracker.get("PR2:3")["active"] is False
        assert tracker.get("PR2:3")["cleared_ts"] == BASE + 10.0
        fault = tracker.get("PR2:4")
        assert fault["active"] is True
        assert fault["code"] == "P-204"
        # And the fault's clear closes only the fault.
        await tracker.apply_batch([change("AlarmActive", False, BASE + 60.0)])
        assert fault["active"] is True  # the row returned earlier is a copy
        assert tracker.get("PR2:4")["active"] is False
        assert tracker.get("PR2:3")["cleared_ts"] == BASE + 10.0

    async def test_two_alarms_in_one_publish_keep_their_own_values(self, tracker):
        t1, t2 = BASE, BASE + 0.05
        batch = [change("AlarmId", 3, t1), change("AlarmId", 4, t2),
                 change("AlarmActive", True, t1),
                 change("AlarmCode", "P-W01", t1), change("AlarmCode", "P-204", t2),
                 change("AlarmText", "Warnung", t1), change("AlarmText", "Fault", t2),
                 change("AlarmSeverity", 500, t1), change("AlarmSeverity", 900, t2)]
        await tracker.apply_batch(batch)
        warning, fault = tracker.get("PR2:3"), tracker.get("PR2:4")
        assert (warning["code"], warning["text"], warning["severity"]) == ("P-W01", "Warnung", 500)
        assert (fault["code"], fault["text"], fault["severity"]) == ("P-204", "Fault", 900)
        assert warning["active"] is False
        assert fault["active"] is True

    async def test_values_of_one_block_with_skewed_timestamps_stay_together(self, tracker):
        # A plant that stamps each write separately, AlarmId not first: the code is microseconds older than its id.
        await tracker.apply_batch([change("AlarmCode", "P-204", BASE),
                                   change("AlarmText", "x", BASE + 0.00001),
                                   change("AlarmId", 4, BASE + 0.00002),
                                   change("AlarmActive", True, BASE + 0.00003),
                                   change("AlarmSeverity", 900, BASE + 0.00004)])
        row = tracker.get("PR2:4")
        assert (row["code"], row["text"], row["severity"]) == ("P-204", "x", 900)


class TestBaseline:
    async def test_active_alarm_seen_first_in_a_baseline_has_no_latency(self, tracker):
        await tracker.apply_batch(raise_alarm(7, "P-201", "Hydraulikdruck", 900, BASE - 500.0, baseline=True))
        row = tracker.get("PR2:7")
        assert row["active"] is True
        assert row["latency_ms"] is None
        assert row["source_ts"] == BASE - 500.0

    async def test_baseline_with_no_alarm_creates_nothing(self, tracker):
        await tracker.apply_batch([change("AlarmId", 0, BASE, baseline=True), change("AlarmActive", False, BASE, baseline=True),
                                   change("AlarmCode", "", BASE, baseline=True), change("AlarmSeverity", 0, BASE, baseline=True)])
        assert tracker.rows() == []

    async def test_reconnect_baseline_does_not_duplicate_or_relink(self, tracker):
        await tracker.apply_batch([*raise_alarm(3, "P-204", "x", 900, BASE), change("Status", "FAULT", BASE)])
        before = tracker.get("PR2:3")
        await tracker.apply_batch([*raise_alarm(3, "P-204", "x", 900, BASE, baseline=True, received=BASE + 100.0),
                                   change("Status", "FAULT", BASE, baseline=True, received=BASE + 100.0)])
        assert len(tracker.rows()) == 1
        assert tracker.get("PR2:3") == before

    async def test_clear_during_a_disconnect_is_found_in_the_baseline(self, tracker):
        await tracker.apply_batch(raise_alarm(3, "P-W01", "x", 500, BASE))
        await tracker.apply_batch([change("AlarmId", 3, BASE, baseline=True),
                                   change("AlarmActive", False, BASE + 40.0, baseline=True)])
        row = tracker.get("PR2:3")
        assert row["active"] is False
        assert row["cleared_ts"] == BASE + 40.0

    async def test_alarm_replaced_during_a_disconnect_closes_the_old_one(self, tracker):
        await tracker.apply_batch(raise_alarm(3, "P-W01", "x", 500, BASE))
        await tracker.apply_batch(raise_alarm(5, "P-204", "y", 900, BASE + 40.0, baseline=True))
        assert tracker.get("PR2:3")["active"] is False
        assert tracker.get("PR2:5")["active"] is True
        assert tracker.get("PR2:5")["latency_ms"] is None

    async def test_baseline_status_does_not_overwrite_timestamps(self, tracker):
        await tracker.apply_batch(raise_alarm(7, "P-201", "x", 900, BASE, baseline=True))
        await tracker.apply_batch([change("Status", "FAULT", BASE + 5.0, baseline=True)])
        assert tracker.get("PR2:7")["source_ts"] == BASE


class TestSideEffects:
    async def test_on_change_runs_once_per_changed_row(self):
        seen = []
        tracker = AlarmTracker(sim_clock=sim_clock, on_change=seen.append)
        await tracker.apply_batch(raise_alarm(3, "P-204", "x", 900, BASE))
        assert [r["id"] for r in seen] == ["PR2:3"]
        assert seen[0]["code"] == "P-204"
        await tracker.apply_batch([change("Temperature", 1.0, BASE)])
        assert len(seen) == 1
        await tracker.apply_batch([change("AlarmActive", False, BASE + 1.0)])
        assert len(seen) == 2
        assert seen[1]["active"] is False

    async def test_async_on_change_is_awaited(self):
        seen = []

        async def on_change(row):
            seen.append(row["id"])

        tracker = AlarmTracker(on_change=on_change)
        await tracker.apply_batch(raise_alarm(3, "P-204", "x", 900, BASE))
        assert seen == ["PR2:3"]

    async def test_failing_on_change_does_not_break_the_tracker(self):
        def boom(_row):
            raise RuntimeError("kaputt")

        tracker = AlarmTracker(on_change=boom)
        rows = await tracker.apply_batch(raise_alarm(3, "P-204", "x", 900, BASE))
        assert rows[0]["id"] == "PR2:3"

    async def test_mark_acked(self, tracker):
        await tracker.apply_batch(raise_alarm(3, "P-204", "x", 900, BASE))
        assert tracker.mark_acked("PR2:3")["acked"] is True
        assert tracker.get("PR2:3")["acked"] is True
        assert tracker.active()[0]["acked"] is True
        assert tracker.mark_acked("PR2:99") is None
        assert tracker.get("PR2:99") is None

    async def test_reset_forgets_everything(self, tracker):
        await tracker.apply_batch(raise_alarm(3, "P-204", "x", 900, BASE))
        tracker.reset()
        assert tracker.rows() == []
        # The same id in a new run is a new row.
        await tracker.apply_batch(raise_alarm(3, "P-201", "y", 900, BASE + 1.0))
        assert tracker.get("PR2:3")["code"] == "P-201"

    async def test_row_limit_prunes_oldest_cleared_only(self):
        tracker = AlarmTracker(row_limit=3)
        await tracker.apply_batch(raise_alarm(1, "P-204", "stays", 900, BASE, machine="PR1"))
        for i in range(1, 6):
            await tracker.apply_batch(raise_alarm(i, "S-W01", "x", 500, BASE + i, machine="SAE"))
        assert len(tracker.rows()) == 3
        assert tracker.get("PR1:1") is not None  # still active
        assert tracker.get("SAE:5") is not None
