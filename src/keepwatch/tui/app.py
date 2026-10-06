"""The Textual app: it renders a Monitor and derives nothing of its own. Read-only, like the Monitor."""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, RichLog, Static

from keepwatch.tui import monitor as view
from keepwatch.tui.monitor import Monitor, Status, WatchRow

COLUMNS = ("WATCH", "STATE", "COND", "FAIL", "LAST", "NEXT", "OBS", "NOTE")
NARROW = 80  # below this, NOTE goes; below NARROWEST, OBS follows
NARROWEST = 68


def line_style(line: str) -> str:
    """Emphasis for a rendered record: the same idea as `keepwatch logs`' level styles, found in the text."""
    if "FAILED" in line or "OFFLINE" in line or "keepwatch internal error" in line:
        return "red"
    if "timed out" in line.lower() or "error" in line.lower():
        return "yellow"
    return ""


class KeepwatchApp(App[None]):
    """A read-only, live view of the service's watches and their recent actions. Quit with q or Ctrl-C."""

    TITLE = "keepwatch"
    CSS = """
    #banner { height: auto; max-height: 5; padding: 0 1; background: $panel; }
    #watches { height: 45%; padding: 0 1; }
    #pane-header { height: 1; padding: 0 1; }
    #pane { height: 1fr; padding: 0 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("j", "next_watch", "Next", show=False),
        Binding("k", "previous_watch", "Previous", show=False),
        Binding("a", "toggle_all", "all watches"),
        Binding("v", "toggle_verbose", "verbose"),
        Binding("space", "toggle_pause", "pause"),
        Binding("r", "rescan", "rescan"),
    ]

    def __init__(self, monitor: Monitor) -> None:
        super().__init__()
        self.monitor = monitor
        self.status: Status | None = None
        self.banner = ""
        self.header = ""  # the pane header line (tests read this)
        self.selected: str | None = None
        self.table_columns: tuple[str, ...] = ()
        self.show_all = False
        self.verbose = False
        self.paused = False
        self.pending = 0
        self.pane_lines: list[str] = []  # what the pane currently shows (tests read this)
        self._names: list[str] = []
        self._rendered_version = 0
        self._paused_at = 0

    # -- layout --------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="banner")
        yield DataTable(id="watches", cursor_type="row", zebra_stripes=False, show_cursor=True)
        yield Static(id="pane-header")
        yield RichLog(id="pane", markup=False, highlight=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._set_columns(self._columns_for(self.size.width))
        self.monitor.rescan()
        self.monitor.refresh()
        self.update_status()
        self.update_records()
        self.set_interval(1.0, self.update_status)
        self.set_interval(0.25, self.update_records)
        self.set_interval(5.0, self.monitor.rescan)
        self.query_one("#watches", DataTable).focus()

    def on_resize(self) -> None:
        self._set_columns(self._columns_for(self.size.width))

    def _columns_for(self, width: int) -> tuple[str, ...]:
        if width < NARROWEST:
            return ("WATCH", "STATE", "COND", "LAST", "NEXT")
        if width < NARROW:
            return ("WATCH", "STATE", "COND", "FAIL", "LAST", "NEXT")
        return COLUMNS

    def _set_columns(self, columns: tuple[str, ...]) -> None:
        if columns == self.table_columns:
            return
        table = self.query_one("#watches", DataTable)
        table.clear(columns=True)
        for column in columns:
            table.add_column(column, key=column)
        self.table_columns = columns
        self._names = []

    # -- data in -------------------------------------------------------------

    def update_status(self) -> None:
        self.status = self.monitor.status()
        self.banner = view.banner_text(self.status, time.time())
        for problem in self.status.problems[:3]:
            self.banner += f"\n! {problem}"
        if len(self.status.problems) > 3:
            self.banner += f"\n! … {len(self.status.problems) - 3} more problems"
        self.query_one("#banner", Static).update(self.banner)
        self._fill_table()
        self._update_header()

    def update_records(self) -> None:
        version = self.monitor.version()
        if version == self._rendered_version:
            return  # nothing new: never redraw for nothing
        if self.paused:
            self.pending = version - self._paused_at
            self._update_header()
            return
        self._redraw()

    def _fill_table(self) -> None:
        if self.status is None:
            return
        table = self.query_one("#watches", DataTable)
        names = [row.name for row in self.status.watches]
        if names != self._names:
            table.clear()
            for row in self.status.watches:
                table.add_row(*self._cells(row), key=row.name)
            self._names = names
            if self.selected not in names:
                self.selected = names[0] if names else None
        else:
            for row in self.status.watches:
                for column, cell in zip(self.table_columns, self._cells(row), strict=True):
                    table.update_cell(row.name, column, cell)

    def _cells(self, row: WatchRow) -> list[Text]:
        now = time.time()
        stale = bool(self.status and self.status.stale)
        values = {
            "WATCH": row.name,
            "STATE": row.state,
            "COND": view.condition_text(row),
            "FAIL": "—" if row.failures is None else str(row.failures),
            "LAST": view.last_text(row, now),
            "NEXT": view.next_text(row, now, stale=stale),
            "OBS": view.observers_text(row),
            "NOTE": row.note,
        }
        return [Text(values[column], style=view.state_style(row.state) if column == "STATE" else "")
                for column in self.table_columns]

    def _row_for(self, name: str | None) -> WatchRow | None:
        if name is None or self.status is None:
            return None
        for row in self.status.watches:
            if row.name == name:
                return row
        return None

    def _selected_name(self) -> str | None:
        return None if self.show_all else self.selected

    # -- the pane ------------------------------------------------------------

    def _redraw(self, *, full: bool = False) -> None:
        """Write the pane: only the new records, unless the filter changed (`full`)."""
        pane = self.query_one("#pane", RichLog)
        lines = self.monitor.records(self._selected_name(), verbose=self.verbose)
        fresh = len(lines) if full else self.monitor.version() - self._rendered_version
        if fresh < 0 or fresh > len(lines):  # the window slid: what was drawn is no longer in the deque
            pane.clear()
            fresh = len(lines)
        for line in lines[-fresh:] if fresh else []:
            pane.write(Text(line, style=line_style(line)))
        self.pane_lines = lines
        self._rendered_version = self.monitor.version()

    def _update_header(self) -> None:
        row = self._row_for(self._selected_name())
        in_flight = None
        hooks: list[str] = []
        if row is not None:
            in_flight = self.monitor.in_flight().get(row.name)
            if in_flight is not None:
                hooks = self.monitor.hooks_done(row.name, in_flight.poll_id)
        header = view.pane_header_text(row, in_flight=in_flight, hooks=hooks, now=time.time(), paused_new=self.pending)
        self.header = header
        self.query_one("#pane-header", Static).update(header)

    def _show_selected(self) -> None:
        self._update_header()
        self._redraw(full=True)

    # -- keys ----------------------------------------------------------------

    def action_next_watch(self) -> None:
        table = self.query_one("#watches", DataTable)
        if table.row_count:
            table.move_cursor(row=(table.cursor_row + 1) % table.row_count)

    def action_previous_watch(self) -> None:
        table = self.query_one("#watches", DataTable)
        if table.row_count:
            table.move_cursor(row=(table.cursor_row - 1) % table.row_count)

    def action_toggle_all(self) -> None:
        self.show_all = not self.show_all
        self._show_selected()

    def action_toggle_verbose(self) -> None:
        self.verbose = not self.verbose
        self._show_selected()

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self._paused_at = self.monitor.version()
            self.pending = 0
        else:
            self.pending = 0
            self._redraw(full=True)
        self._update_header()

    def action_rescan(self) -> None:
        self.monitor.rescan()
        self.monitor.refresh()
        self.update_status()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self.selected = str(event.row_key.value)
        self._show_selected()
