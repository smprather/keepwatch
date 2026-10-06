"""The Textual app, through its own test pilot (headless; no terminal needed)."""

import asyncio
import time

from textual.widgets import DataTable

from keepwatch.locks import hold_lock
from keepwatch.logstore import LogWriter, make_record
from keepwatch.offline import OfflineMarker, iso_time, write_offline
from keepwatch.paths import ensure_private_dir, write_json_atomic
from keepwatch.tui.app import KeepwatchApp
from keepwatch.tui.monitor import Monitor

WATCH_CONFIG = '[hooks]\ncheck = ["true"]\n'


def last_poll(at, outcome="true", failed=False):
    return {"poll_id": "p1", "at": iso_time(at), "outcome": outcome, "reason": None, "failed": failed, "trial": False,
            "results": []}


def entry(**fields):
    data = {
        "description": "test watch",
        "watch_dir": "/tmp/watches/a",
        "enabled": True,
        "interval": 30.0,
        "condition": True,
        "pending_edge": None,
        "failures": 0,
        "offline": None,
        "last_poll": last_poll(time.time() - 12),
        "next_poll": iso_time(time.time() + 18),
        "config_error": None,
        "pending_events": 0,
        "observers": {"local": {"kind": "files", "running": True}},
    }
    data.update(fields)
    return data


def status_document(watches=None, invalid=None, updated=None, pid=4711):
    return {
        "service": {"running": True, "pid": pid, "version": "2026.10.6", "started": iso_time(time.time() - 900),
                    "updated": iso_time(time.time() if updated is None else updated), "config": "x",
                    "config_error": None, "only": []},
        "watches": watches or {},
        "invalid_watches": invalid or {},
        "problems": [],
    }


def publish(paths, document):
    write_json_atomic(paths.status_file, document)


def running(paths):
    """Hold the service lock, so the monitor sees a live service while the app runs."""
    ensure_private_dir(paths.runtime)
    return hold_lock(paths.service_lock)


def write_records(paths, *records):
    writer = LogWriter(paths.log_file, max_bytes=1_000_000, backups=1)
    for record in records:
        writer.write(record)


def loaded(paths, **kwargs):
    monitor = Monitor(paths, paths.config_file, **kwargs)
    monitor.start()
    monitor.stop()
    return monitor


def run_app(monitor, check, *, size=(120, 40)):
    """Run the app headless and hand it to `check` (an async function) with the pilot."""

    async def main():
        app = KeepwatchApp(monitor)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await check(app, pilot)

    asyncio.run(main())


def rows(app):
    table = app.query_one("#watches", DataTable)
    return [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]


def cell(app, row_index, column):
    return rows(app)[row_index][app.table_columns.index(column)]


def test_the_table_lists_every_watch_with_its_state(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    make_watch("b", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry(), "b": entry(enabled=False, condition=False)}))
    monitor = Monitor(xdg, xdg.config_file)

    async def check(app, pilot):
        assert len(rows(app)) == 2
        assert cell(app, 0, "WATCH") == "a" and cell(app, 0, "STATE") == "online"
        assert cell(app, 1, "STATE") == "parked"
        assert cell(app, 0, "COND") == "TRUE" and cell(app, 0, "FAIL") == "0"
        assert cell(app, 0, "OBS") == "1/1"
        assert "service running (pid 4711, 2026.10.6)" in app.banner

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check)


def test_a_stopped_service_is_shown_stale_and_counts_nothing_down(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry(next_poll=iso_time(time.time() + 30))}, updated=time.time() - 7200))
    monitor = Monitor(xdg, xdg.config_file)

    async def check(app, pilot):
        assert "NOT running" in app.banner and "(stale)" in app.banner
        assert cell(app, 0, "STATE") == "idle"
        assert cell(app, 0, "NEXT") == "—"
        assert cell(app, 0, "LAST").startswith("true ")  # last known, with its age

    run_app(monitor, check)


def test_selecting_a_watch_shows_only_its_records_then_all(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    make_watch("b", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry(), "b": entry()}))
    write_records(xdg,
                  make_record("hook.end", watch="a", poll_id="pa", hook="on_true", status="ok", duration=1.0,
                              target="a.py:on_true"),
                  make_record("hook.end", watch="b", poll_id="pb", hook="on_true", status="ok", duration=1.0,
                              target="b.py:on_true"))
    monitor = loaded(xdg)

    async def check(app, pilot):
        assert len(app.pane_lines) == 1 and "a.py:on_true" in app.pane_lines[0]
        await pilot.press("j")
        await pilot.pause()
        assert app.selected == "b"
        assert app.pane_lines and all("b.py:on_true" in line for line in app.pane_lines)
        await pilot.press("a")
        await pilot.pause()
        assert app.show_all is True and len(app.pane_lines) == 2

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check)


def test_v_toggles_verbose_output(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry()}))
    write_records(xdg, make_record("hook.end", watch="a", poll_id="p1", hook="on_true", status="ok", duration=1.0,
                                   target="a.py:on_true", stdout="the whole output\n"))
    monitor = loaded(xdg)

    async def check(app, pilot):
        assert "the whole output" not in "\n".join(app.pane_lines)
        await pilot.press("v")
        await pilot.pause()
        assert app.verbose is True and "the whole output" in "\n".join(app.pane_lines)

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check)


def test_space_pauses_the_pane_while_records_keep_buffering(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry()}))
    write_records(xdg, make_record("hook.end", watch="a", poll_id="p1", hook="on_true", status="ok", duration=1.0,
                                   target="a.py:on_true"))
    monitor = loaded(xdg)

    async def check(app, pilot):
        await pilot.press("space")
        await pilot.pause()
        assert app.paused is True
        before = list(app.pane_lines)
        monitor._ingest(make_record("hook.end", watch="a", poll_id="p2", hook="on_true", status="ok", duration=1.0,
                                    target="a.py:on_true"))
        app.update_records()
        assert app.pane_lines == before and app.pending == 1
        await pilot.press("space")
        await pilot.pause()
        assert app.paused is False and app.pending == 0 and len(app.pane_lines) == len(before) + 1

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check)


def test_broken_watches_get_rows_with_their_labels(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    make_watch("b", config=WATCH_CONFIG)
    make_watch("broken", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry(), "b": entry(enabled=False)},
                                 invalid={"broken": "config.toml:4: unknown key 'x'"}))
    write_offline(xdg, "a", OfflineMarker(reason="5 consecutive failed polls", since=iso_time(time.time() - 100),
                                          by_user=True))
    (xdg.state_home / "watches" / "gone").mkdir(parents=True)
    monitor = Monitor(xdg, xdg.config_file)

    async def check(app, pilot):
        states = {cell(app, index, "WATCH"): cell(app, index, "STATE") for index in range(len(rows(app)))}
        assert states == {"a": "offline", "b": "parked", "broken": "invalid", "gone": "orphaned"}
        notes = {cell(app, index, "WATCH"): cell(app, index, "NOTE") for index in range(len(rows(app)))}
        assert notes["a"] == "offline by user"
        assert notes["broken"].startswith("invalid: config.toml:4")
        assert "rename" in notes["gone"]

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check)


def test_r_rescans_and_finds_a_new_watch(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry()}))
    monitor = loaded(xdg)

    async def check(app, pilot):
        assert len(rows(app)) == 1
        make_watch("late", config=WATCH_CONFIG)
        await pilot.press("r")
        await pilot.pause()
        assert [row[0] for row in rows(app)] == ["a", "late"]

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check)


def test_the_screen_drops_columns_when_it_is_narrow(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry()}))
    monitor = Monitor(xdg, xdg.config_file)

    async def check(app, pilot):
        assert app.table_columns == ("WATCH", "STATE", "COND", "LAST", "NEXT")
        assert len(rows(app)[0]) == 5

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check, size=(60, 30))


def test_an_in_flight_poll_is_named_in_the_pane_header(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry(last_poll=last_poll(time.time() - 300), next_poll=None)}))
    write_records(xdg,
                  make_record("poll.start", watch="a", poll_id="p9", condition=True),
                  make_record("hook.end", watch="a", poll_id="p9", hook="on_check", status="ok", duration=0.1,
                              target="watch.py:check"))
    monitor = loaded(xdg)

    async def check(app, pilot):
        assert cell(app, 0, "STATE") == "polling"
        assert "p9 running" in app.header
        assert "1 hook done (on_check)" in app.header

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check)


def test_the_service_view_of_an_in_flight_hook_is_shown(xdg, make_watch):
    """status.json's own in_flight and transfer win over the log's guess (issue 3)."""
    make_watch("a", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry(
        in_flight={"hook": "on_true", "poll_id": "p1", "target": "recipe pull:on_true",
                   "started_at": iso_time(time.time() - 90)},
        transfer={"name": "big.bin", "part": "/stage/.big.bin.part", "size": 4096, "part_size": 3072,
                  "started_at": iso_time(time.time() - 90)},
    )}))
    monitor = Monitor(xdg, xdg.config_file)

    async def check(app, pilot):
        assert cell(app, 0, "STATE") == "polling"
        assert "recipe pull:on_true running 1m30s" in app.header
        assert "pulling big.bin 75%" in app.header

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check)


def test_q_quits_the_app(xdg, make_watch):
    make_watch("a", config=WATCH_CONFIG)
    publish(xdg, status_document(watches={"a": entry()}))
    monitor = Monitor(xdg, xdg.config_file)

    async def check(app, pilot):
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running is False

    with running(xdg):
        monitor.refresh()
        run_app(monitor, check)
