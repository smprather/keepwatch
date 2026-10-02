"""The keepwatch command line (rich-click).

On a terminal, help and output use Rich formatting. When stdout is not a
terminal (how agents run commands) or NO_COLOR is set, output is plain: no
color and no box-drawing characters.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import rich_click as click
from rich.markdown import Markdown

from keepwatch import __version__, installcheck, platform, systemd, transfer, winsched
from keepwatch.config import (
    ConfigError,
    Discovery,
    GlobalConfig,
    WatchConfig,
    WatchNotFound,
    discover_watches,
    find_watch,
    is_valid_watch_name,
    load_global_config,
    load_watch_config,
)
from keepwatch.control import ControlError, disable_watch, enable_watch, rename_watch, require_known
from keepwatch.ctx import CommandFailed
from keepwatch.durations import DurationError, parse_duration
from keepwatch.locks import LockBusy, hold_lock
from keepwatch.logquery import LogFilter, follow_log, parse_when, select_records
from keepwatch.logstore import LogWriter, QueueSink, fan_out, level_filter, make_record
from keepwatch.observers import build_observer, stop_all
from keepwatch.offline import iso_time
from keepwatch.output import ConsolePrinter, make_console, plain_output
from keepwatch.paths import PathError, Paths, ensure_private_dir, remove_stale_process_dirs, resolve_paths
from keepwatch.pollengine import Fake, PollEngine, PollReport, parse_fakes
from keepwatch.reference import UnknownTopic, render_all, render_topic, topic_index
from keepwatch.runner import Runner
from keepwatch.service import Service
from keepwatch.state import initial_state
from keepwatch.statusview import collect_status, format_status, service_running
from keepwatch.templates import AGENTS_MD, CLAUDE_MD, GLOBAL_CONFIG, TEMPLATES, render
from keepwatch.validation import validate_watches

COMMAND_GROUPS = {
    "keepwatch": [
        {"name": "Run", "commands": ["run", "stop"]},
        {"name": "Develop", "commands": ["new", "validate", "poll", "observe"]},
        {"name": "Inspect", "commands": ["status", "logs"]},
        {"name": "Control", "commands": ["enable", "disable", "rename"]},
        {"name": "Setup", "commands": ["init", "install", "uninstall"]},
        {"name": "Hook tools", "commands": ["kit"]},
        {"name": "Reference", "commands": ["docs"]},
    ]
}
_PLAIN_BOXES = {
    "style_commands_panel_box": "SIMPLE_HEAD",
    "style_options_panel_box": "SIMPLE_HEAD",
    "style_errors_panel_box": "SIMPLE_HEAD",
}
_LOG_FILE: Path | None = None


def help_config(plain: bool) -> click.RichHelpConfiguration:
    if plain:
        return click.RichHelpConfiguration(
            command_groups=COMMAND_GROUPS,
            text_markup=None,
            color_system=None,
            **_PLAIN_BOXES,
        )
    return click.RichHelpConfiguration(command_groups=COMMAND_GROUPS, text_markup="markdown")


@dataclass
class App:
    paths: Paths
    config_path: Path

    def load_global(self) -> GlobalConfig:
        return load_global_config(self.config_path, self.paths)


class DurationType(click.ParamType):
    """A duration such as 30s, 5m, 1h30m or a number of seconds."""

    name = "duration"

    def convert(self, value, param, ctx):
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return parse_duration(value)
        except DurationError as exc:
            self.fail(str(exc), param, ctx)


DURATION = DurationType()


def _fail(message: str) -> NoReturn:
    if sys.stderr is not None:
        make_console(stderr=True).print(message)
    elif _LOG_FILE is not None:
        # pythonw (Windows logon task): there is no console, so leave the reason in the log.
        try:
            LogWriter(_LOG_FILE, max_bytes=10_000_000, backups=10).write(
                make_record("cli.error", level="CRITICAL", error=message, argv=sys.argv[1:])
            )
        except OSError:
            pass
    raise SystemExit(1)


def _load_one(app: App, name: str) -> tuple[GlobalConfig, WatchConfig]:
    try:
        global_config = app.load_global()
        watch_dir = find_watch(discover_watches(global_config), name)
        watch = load_watch_config(watch_dir, global_config.defaults)
    except ConfigError as exc:
        _fail("\n".join(str(problem) for problem in exc.problems))
    except WatchNotFound as exc:
        _fail(str(exc))
    return global_config, watch


@contextmanager
def _process_dir(paths: Paths) -> Iterator[int]:
    """Prepare the private runtime dir for this process and remove it afterwards."""
    pid = os.getpid()
    try:
        ensure_private_dir(paths.runtime)
        remove_stale_process_dirs(paths)
    except PathError as exc:
        _fail(str(exc))
    try:
        yield pid
    finally:
        shutil.rmtree(paths.process_dir(pid), ignore_errors=True)


@click.group(name="keepwatch", context_settings={"help_option_names": ["-h", "--help"]})
@click.rich_config(help_config=help_config(plain_output()))
@click.version_option(__version__, prog_name="keepwatch")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    envvar="KEEPWATCH_CONFIG",
    help="Global config file. Default: $XDG_CONFIG_HOME/keepwatch/config.toml. Env: KEEPWATCH_CONFIG.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None) -> None:
    """Poll conditions and run actions.

    Each watch is a directory holding a config.toml plus the code for its check and actions.

    Writing or fixing a watch? Read `keepwatch docs agent` first, or `keepwatch docs --all` for the complete reference.
    """
    global _LOG_FILE
    paths = resolve_paths()
    _LOG_FILE = paths.log_file
    ctx.obj = App(paths=paths, config_path=config_path or paths.config_file)


@cli.command()
@click.argument("name")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Run the real check but only report which actions would run (they are assumed to succeed). "
    "With --fake, nothing runs at all.",
)
@click.option(
    "--fake",
    "fake_spec",
    metavar="OUTCOMES",
    help="Skip the check and feed these outcomes, one poll each, e.g. true,true,false,timeout. "
    "Valid: true, false, unknown, timeout, error. Actions run for real unless --dry-run.",
)
@click.option(
    "--payload",
    "payload_json",
    metavar="JSON",
    help='With --fake: the payload handed to the actions, e.g. \'["a.tar.gz"]\'.',
)
@click.option(
    "--events",
    "events_json",
    metavar="JSON",
    help='Observer events for the hooks (ctx.events, KEEPWATCH_EVENTS_FILE): a JSON array of objects, e.g. '
    '\'[{"event": "file", "name": "a.tar.gz"}]\'. Each gets "observer": "manual" and "received" unless it has '
    "them. Every poll of this run gets the same events. Default: none (a manual poll runs no observers).",
)
@click.option(
    "--initial-condition",
    type=click.BOOL,
    default=None,
    help="Condition before the first poll (true or false). Default: the watch's initial_condition.",
)
@click.option("-v", "--verbose", is_flag=True, help="Also print captured output and payloads of successful hooks.")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON document (polls and all log records) at the end.")
@click.pass_obj
def poll(
    app: App,
    name: str,
    dry_run: bool,
    fake_spec: str | None,
    payload_json: str | None,
    events_json: str | None,
    initial_condition: bool | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """Run one poll of watch NAME now, printing everything as it happens.

    Every record also goes to the log file. A manual poll shares the watch's persistent data (ledgers)
    with the service but not its condition or failure count, and holds the watch's lock so the service
    and a manual poll never run the same watch at once.

    Observers do not run during a manual poll: hooks see the events given with --events, or none.

    Exit status: 0 if no poll failed, 1 if a poll failed or the watch could not be loaded, 2 for bad usage.
    """
    fakes = None
    if fake_spec is not None:
        try:
            fakes = parse_fakes(fake_spec)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--fake") from None
    payload = None
    if payload_json is not None:
        if fakes is None:
            raise click.UsageError("--payload only applies together with --fake")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"not valid JSON: {exc}", param_hint="--payload") from None
    events: list[dict] = []
    if events_json is not None:
        try:
            parsed = json.loads(events_json)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"not valid JSON: {exc}", param_hint="--events") from None
        if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
            raise click.BadParameter("must be a JSON array of objects", param_hint="--events")
        received = iso_time(time.time())
        events = [{"observer": "manual", "received": received, **item} for item in parsed]
    global_config, watch = _load_one(app, name)
    records: list[dict] = []
    writer = LogWriter(app.paths.log_file, max_bytes=global_config.log.max_bytes, backups=global_config.log.backups)
    display = records.append if as_json else ConsolePrinter(make_console(), verbose=verbose)
    reports: list[PollReport] = []
    with _process_dir(app.paths) as pid:
        engine = PollEngine(
            runner=Runner(),
            paths=app.paths,
            global_config=global_config,
            sink=fan_out(writer.write, display),
            pid=pid,
        )
        state = initial_state(watch.initial_condition if initial_condition is None else initial_condition)

        def waiting() -> None:
            make_console(stderr=True).print(f"{name} is being polled by another keepwatch process; waiting…")

        with hold_lock(app.paths.watch_lock(watch.name), on_wait=waiting):
            for outcome in fakes or [None]:
                fake = None if outcome is None else Fake(outcome, payload)
                report = engine.poll(watch, state, fake=fake, dry_run=dry_run, events=events)
                reports.append(report)
                state = report.after
    if as_json:
        click.echo(json.dumps({"polls": [r.to_dict() for r in reports], "records": records}, indent=2, default=str))
    raise SystemExit(1 if any(report.failed for report in reports) else 0)


@cli.command()
@click.argument("name")
@click.argument("observer", required=False)
@click.option(
    "--for",
    "duration",
    type=DURATION,
    metavar="DURATION",
    help='Stop after this long, e.g. "30s" or "5m". Default: run until interrupted (Ctrl-C).',
)
@click.option("--count", type=int, metavar="N", help="Stop after N events.")
@click.pass_obj
def observe(app: App, name: str, observer: str | None, duration: float | None, count: int | None) -> None:
    """Run watch NAME's observers (or only OBSERVER) in the foreground and print their events.

    Each event is printed to stdout as one JSON line, exactly as hooks receive it in ctx.events. Observer
    records (started, stopped, restarting, stderr output) go to stderr; nothing is written to the log file
    and the watch is not polled. It runs its own copies of the observers, so it does not disturb a running
    service. Agents: always pass --for or --count, since there is no Ctrl-C.

    Exit status: 0 when stopped by --for, --count or Ctrl-C; 1 if the watch cannot be loaded, has no
    observers or has no observer named OBSERVER; 2 for bad usage.
    """
    seconds = duration
    if count is not None and count < 1:
        raise click.BadParameter("must be at least 1", param_hint="--count")
    global_config, watch = _load_one(app, name)
    if not watch.observers:
        _fail(f"watch '{name}' has no observers; add an [observe.<name>] table (see: keepwatch docs observers)")
    if observer is not None and observer not in watch.observers:
        _fail(f"watch '{name}' has no observer '{observer}'; its observers: {', '.join(watch.observers)}")
    done = threading.Event()
    lock = threading.Lock()
    seen = 0

    def deliver(event: dict) -> None:
        nonlocal seen
        with lock:
            if done.is_set():
                return
            click.echo(json.dumps(event, ensure_ascii=False, default=str))
            seen += 1
            if count is not None and seen >= count:
                done.set()

    printer = level_filter(ConsolePrinter(make_console(stderr=True)), "INFO")
    running = [
        build_observer(
            watch,
            watch.observers[key],
            deliver=deliver,
            sink=printer,
            environment=global_config.environment,
            data_dir=app.paths.watch_data_dir(watch.name),
        )
        for key in ([observer] if observer else list(watch.observers))
    ]
    for item in running:
        item.start()
    deadline = None if seconds is None else time.monotonic() + seconds
    try:
        while not done.wait(0.5):  # short waits: Ctrl-C cannot interrupt an endless wait on Windows
            if deadline is not None and time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_all(running)


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--json", "as_json", is_flag=True, help="Print the result as one JSON document.")
@click.pass_obj
def validate(app: App, names: tuple[str, ...], as_json: bool) -> None:
    """Check watches for config and code problems, reporting every problem at once.

    Checks each watch's config.toml (types, unknown keys, with line numbers), that it has a check, that no
    hook is defined twice, that command hooks' programs exist, and that watch.py imports cleanly (in a
    worker process, exactly as a poll loads it). With no NAMES, checks every watch.

    Exit status: 0 if everything is valid, 1 if any problem was found.
    """
    try:
        global_config = app.load_global()
    except ConfigError as exc:
        _fail("\n".join(str(problem) for problem in exc.problems))
    discovery = discover_watches(global_config)
    with _process_dir(app.paths) as pid:
        general, checks = validate_watches(global_config, discovery, list(names), Runner(), app.paths, pid)
    ok = not general and all(check.ok for check in checks)
    if as_json:
        document = {"ok": ok, "problems": general, "watches": [check.to_dict() for check in checks]}
        click.echo(json.dumps(document, indent=2))
    else:
        console = make_console()
        for problem in general:
            console.print(f"problem  {problem}")
        for check in checks:
            console.print(f"{'ok' if check.ok else 'FAIL':<4} {check.name}")
            for problem in check.problems:
                console.print(f"     {problem}")
        if not checks and not general:
            console.print("no watches found")
    raise SystemExit(0 if ok else 1)


@cli.command(name="run")
@click.option("--watch", "only", multiple=True, metavar="NAME", help="Run only this watch (repeatable). For development.")
@click.option("-v", "--verbose", is_flag=True, help="Print every record, including captured output.")
@click.option("-q", "--quiet", is_flag=True, help="Print only warnings and errors.")
@click.pass_obj
def run_service(app: App, only: tuple[str, ...], verbose: bool, quiet: bool) -> None:
    """Run the service in the foreground: every watch, polled forever, with config changes picked up live.

    This is what the login service runs. Stop it with Ctrl-C or SIGTERM: running hooks are terminated and the
    service exits once their polls end. Only one service runs at a time.

    Terminal output: on a terminal, one line per event (only warnings and errors with -q; captured output too
    with -v). When stdout is not a terminal (for example under systemd), only warnings and errors are printed
    unless -v is given. The log file always gets every record; read it with `keepwatch logs`.

    Exit status: 0 after a clean stop, 1 if another service is running or the global config is broken.
    """
    paths = app.paths
    try:
        ensure_private_dir(paths.runtime)
        remove_stale_process_dirs(paths)
    except PathError as exc:
        _fail(str(exc))
    with ExitStack() as stack:
        try:
            stack.enter_context(hold_lock(paths.service_lock, blocking=False))
        except LockBusy:
            _fail("another keepwatch service is already running; see: keepwatch status")
        locked_at = time.time()
        try:
            global_config = app.load_global()
        except ConfigError as exc:
            _fail("\n".join(str(problem) for problem in exc.problems))
        if quiet:
            minimum = "WARNING"
        elif verbose:
            minimum = "DEBUG"
        elif plain_output():
            minimum = "WARNING"
        else:
            minimum = "INFO"
        writer = LogWriter(paths.log_file, max_bytes=global_config.log.max_bytes, backups=global_config.log.backups)
        if sys.stdout is None:  # pythonw: no console, the log file has everything
            sink = QueueSink(writer.write)
        else:
            printer = level_filter(ConsolePrinter(make_console(), verbose=verbose), minimum)
            sink = QueueSink(fan_out(writer.write, printer))
        stack.callback(sink.close)
        stack.callback(shutil.rmtree, paths.process_dir(os.getpid()), True)
        stop = threading.Event()

        def request_stop(signum: int, frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        service = Service(paths=paths, config_path=app.config_path, sink=sink, only=set(only),
                          stale_before=locked_at)
        try:
            service.run(stop)
        except ConfigError as exc:
            _fail("\n".join(str(problem) for problem in exc.problems))
    raise SystemExit(0)


@cli.command()
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, help="Print the status as one JSON document.")
@click.pass_obj
def status(app: App, name: str | None, as_json: bool) -> None:
    """Show whether the service runs and each watch's state.

    Per watch: online, offline (with the reason and last failure), parked (enabled = false) or idle (the
    service is not running), the TRUE/FALSE condition, consecutive failures, the last poll and the next one.
    Also lists config problems and orphaned state directories (state of a watch that no longer exists).
    The service refreshes this every reload_interval.

    Exit status: 0, or 1 if NAME is not a known watch.
    """
    try:
        discovery = discover_watches(app.load_global())
    except ConfigError as exc:
        _fail("\n".join(str(problem) for problem in exc.problems))
    names = []
    if name is not None:
        if name not in discovery.watches and not app.paths.watch_state_dir(name).is_dir():
            try:
                find_watch(discovery, name)
            except WatchNotFound as exc:
                _fail(str(exc))
        names = [name]
    document = collect_status(app.paths, discovery, names)
    if as_json:
        click.echo(json.dumps(document, indent=2, default=str))
        return
    console = make_console()
    for line in format_status(document, time.time()):
        console.print(line)


@cli.command()
@click.argument("name", required=False)
@click.option("--since", metavar="WHEN", help='Records at or after WHEN: a duration ago ("1h", "2d") or a time ("2026-09-30", "2026-09-30T14:00").')
@click.option("--until", metavar="WHEN", help="Records at or before WHEN (same forms as --since).")
@click.option("--level", type=click.Choice(["debug", "info", "warning", "error", "critical"], case_sensitive=False),
              help="Minimum level.")
@click.option("--event", "events", multiple=True, metavar="EVENT",
              help="Only this event or event family (repeatable): hook.end, or hook for every hook.* event.")
@click.option("--failed", is_flag=True, help="Only failures: ERROR or worse, failed hooks, failed polls and failed alerts.")
@click.option("--poll", "poll_id", metavar="ID", help="Only records of this poll (a prefix of the poll ID is enough).")
@click.option("-n", "--limit", type=int, default=200, show_default=True, help="Show at most the last N matching records; 0 shows all.")
@click.option("-f", "--follow", is_flag=True, help="Then keep printing new matching records until interrupted.")
@click.option("-v", "--verbose", is_flag=True, help="Show captured output and payloads for every record.")
@click.option("--json", "as_json", is_flag=True, help="Print each record as one JSON line (the log file's own format).")
@click.pass_obj
def logs(
    app: App,
    name: str | None,
    since: str | None,
    until: str | None,
    level: str | None,
    events: tuple[str, ...],
    failed: bool,
    poll_id: str | None,
    limit: int,
    follow: bool,
    verbose: bool,
    as_json: bool,
) -> None:
    """Search the log (including rotated files), optionally restricted to watch NAME.

    Every poll's records share a poll ID: `keepwatch logs --poll <id> -v` shows one poll from start to finish,
    with every command's output. Records are printed oldest first.

    Exit status: 0, or 2 for bad usage.
    """
    try:
        since_time = parse_when(since) if since else None
        until_time = parse_when(until) if until else None
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from None
    log_filter = LogFilter(
        watch=name,
        since=since_time,
        until=until_time,
        min_level=level.upper() if level else None,
        events=events,
        failed=failed,
        poll_id=poll_id,
    )
    printer = ConsolePrinter(make_console(), verbose=verbose)

    def show(record: dict) -> None:
        if as_json:
            click.echo(json.dumps(record, ensure_ascii=False, default=str))
        else:
            printer(record)

    for record in select_records(app.paths.log_file, log_filter, limit):
        show(record)
    if follow:
        try:
            follow_log(app.paths.log_file, lambda r: show(r) if log_filter.matches(r) else None, stop=lambda: False)
        except KeyboardInterrupt:
            pass


def _control_setup(app: App) -> Discovery:
    try:
        ensure_private_dir(app.paths.runtime)
        return discover_watches(app.load_global())
    except ConfigError as exc:
        _fail("\n".join(str(problem) for problem in exc.problems))
    except PathError as exc:
        _fail(str(exc))


@cli.command()
@click.argument("name")
@click.pass_obj
def enable(app: App, name: str) -> None:
    """Bring an offline watch back online (deletes its offline.json).

    A running service notices within a second, resets the failure count and polls the watch at once.

    Exit status: 0, or 1 if NAME is unknown.
    """
    discovery = _control_setup(app)
    try:
        require_known(app.paths, discovery, name)
    except ControlError as exc:
        _fail(str(exc))
    if enable_watch(app.paths, name):
        click.echo(f"{name} enabled; a running service polls it within a second")
    else:
        click.echo(f"{name} was not offline; nothing to do")


@cli.command()
@click.argument("name")
@click.pass_obj
def disable(app: App, name: str) -> None:
    """Take a watch offline until `keepwatch enable` (writes offline.json, survives restarts).

    A running service stops polling it within a second; a poll already running finishes first.

    Exit status: 0, or 1 if NAME is unknown.
    """
    discovery = _control_setup(app)
    try:
        require_known(app.paths, discovery, name)
    except ControlError as exc:
        _fail(str(exc))
    if disable_watch(app.paths, name, time.time()):
        click.echo(f"{name} disabled; run `keepwatch enable {name}` to bring it back")
    else:
        click.echo(f"{name} is already disabled")


@cli.command()
@click.argument("old")
@click.argument("new")
@click.pass_obj
def rename(app: App, old: str, new: str) -> None:
    """Rename watch OLD to NEW, moving its persistent state (ledgers, offline marker) with it.

    Renaming the directory by hand would leave the state behind, and the watch would start with empty data
    (a watch that sends files would send them all again). A running service sees OLD disappear and NEW appear
    at its next tick; NEW starts from its initial_condition.

    Exit status: 0 on success, 1 if refused (unknown OLD, NEW taken or invalid, or OLD being polled).
    """
    discovery = _control_setup(app)
    try:
        new_dir, new_state = rename_watch(app.paths, discovery, old, new)
    except ControlError as exc:
        _fail(str(exc))
    click.echo(f"renamed {old} to {new}: {new_dir}" + (f" (state: {new_state})" if new_state.exists() else ""))


@cli.command()
@click.argument("name")
@click.option("--template", type=click.Choice(sorted(TEMPLATES)), default="python", show_default=True,
              help="python: watch.py with check() and on_true(). shell: check.sh and on_true.sh. "
              "expect: a shell check and an Expect action. powershell: check.ps1 and on_true.ps1 (Windows).")
@click.option("--dir", "base", type=click.Path(path_type=Path, file_okay=False),
              help="Watches directory to create it in. Default: the first entry of watch_dirs.")
@click.pass_obj
def new(app: App, name: str, template: str, base: Path | None) -> None:
    """Create watch NAME from a commented template, ready to validate and poll.

    The template's comments point at the reference topics for every part. Next steps are printed.

    Exit status: 0, or 1 if NAME is not a valid watch name or already exists.
    """
    if not is_valid_watch_name(name):
        _fail(f"'{name}' is not a valid watch name: use letters, digits, '_', '.' and '-', starting with a letter or digit")
    if base is None:
        try:
            base = app.load_global().watch_dirs[0]
        except ConfigError as exc:
            _fail("\n".join(str(problem) for problem in exc.problems))
    watch_dir = base / name
    if watch_dir.exists():
        _fail(f"{watch_dir} already exists; choose another name or edit it directly")
    watch_dir.mkdir(parents=True)
    for relative, content in render(template, name).items():
        path = watch_dir / relative
        path.write_text(content, encoding="utf-8")
        if content.startswith("#!"):
            path.chmod(0o755)
    click.echo(f"created {watch_dir} from the {template} template")
    click.echo(f"next: edit it, then run `keepwatch validate {name}` and `keepwatch poll {name} --dry-run`")


@cli.command()
@click.pass_obj
def init(app: App) -> None:
    """Create the global config file, the default watches directory, and AGENTS.md/CLAUDE.md for agents.

    Never overwrites anything: each path is reported as created or exists.

    Exit status: 0.
    """
    config_path = app.config_path
    if config_path.exists():
        click.echo(f"exists  {config_path}")
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(GLOBAL_CONFIG, encoding="utf-8")
        click.echo(f"created {config_path}")
    watches = app.paths.default_watches_dir
    if watches.is_dir():
        click.echo(f"exists  {watches}")
    else:
        watches.mkdir(parents=True)
        click.echo(f"created {watches}")
    for filename, content in (("AGENTS.md", AGENTS_MD), ("CLAUDE.md", CLAUDE_MD)):
        path = watches / filename
        if path.exists():
            click.echo(f"exists  {path}")
        else:
            path.write_text(content, encoding="utf-8")
            click.echo(f"created {path}")
    click.echo("next: keepwatch new <name>, then keepwatch install to start the service at login")


def _systemctl_or_fail(*args: str) -> None:
    try:
        completed = systemd.systemctl(*args)
    except OSError as exc:
        _fail(f"cannot run systemctl: {exc}")
    if completed.returncode != 0:
        _fail(f"systemctl --user {' '.join(args)} failed ({completed.returncode}): {completed.stderr.strip()}")


def _on_windows() -> bool:
    return platform.IS_WINDOWS


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no details"


def _install_windows(app: App, dry_run: bool) -> None:
    custom_config = app.config_path.resolve() if app.config_path != app.paths.config_file else None
    executable = winsched.pythonw()
    arguments = winsched.run_arguments(custom_config)
    register = winsched.register_script(executable, arguments)
    shortcut = winsched.shortcut_script(executable, arguments)
    if dry_run:
        click.echo("would run in PowerShell:\n")
        click.echo(register)
        click.echo("\nand, if Task Scheduler refuses, create a Startup-folder shortcut:\n")
        click.echo(shortcut)
        return
    result = winsched.run_powershell(register)
    if result.returncode == 0:
        started = winsched.run_powershell(winsched.start_script())
        if started.returncode == 0:
            click.echo(f"registered Task Scheduler task '{winsched.TASK_NAME}' (at logon, restarted on failure) and started it")
        else:
            click.echo(
                f"registered Task Scheduler task '{winsched.TASK_NAME}', but could not start it now "
                f"({_last_line(started.stderr)}); it will start at the next logon"
            )
    else:
        click.echo(f"Task Scheduler refused ({_last_line(result.stderr)}); using a Startup-folder shortcut instead")
        fallback = winsched.run_powershell(shortcut)
        if fallback.returncode != 0:
            _fail(f"could not create the Startup-folder shortcut either: {_last_line(fallback.stderr)}")
        click.echo("created a Startup-folder shortcut (keepwatch.lnk) and started keepwatch")
    click.echo("check it with: keepwatch status")


def _install_problem(force: bool) -> str | None:
    """Why the login service would break, or None (see keepwatch.installcheck)."""
    argv = installcheck.service_check_argv()
    if not force:
        caches, temp = installcheck.uv_cache_dirs(os.environ), Path(tempfile.gettempdir())
        reason = installcheck.transient_reason(Path(sys.prefix), cache_dirs=caches, temp_dir=temp)
        reason = reason or installcheck.transient_reason(
            Path(argv[0]), cache_dirs=caches, temp_dir=temp, subject="the program the login service would run"
        )
        if reason is not None:
            return (
                f"{reason}, and the login service would stop working when it disappears. Install keepwatch for good "
                "with `uv tool install keepwatch` and run `keepwatch install` from there (or pass --force)."
            )
    problem = installcheck.verify_command(argv, os.environ)
    if problem is not None:
        return f"the login service would run a command that does not work: {problem}"
    return None


@cli.command()
@click.option("--dry-run", is_flag=True, help="Print the unit file and the commands without changing anything.")
@click.option(
    "--force",
    is_flag=True,
    help="Install even when this keepwatch runs from a temporary environment (uv's cache, uvx, the temp directory).",
)
@click.pass_obj
def install(app: App, dry_run: bool, force: bool) -> None:
    """Start keepwatch at login: a systemd user service on Linux, a Task Scheduler logon task on Windows.

    On Windows it registers a Task Scheduler task "keepwatch" that runs pythonw.exe -m keepwatch run at logon
    (no console window, restarted on failure); if policy forbids that, it creates a Startup-folder shortcut instead.

    Writes $XDG_CONFIG_HOME/systemd/user/keepwatch.service with this keepwatch's absolute path and the
    current PATH (so hooks find the same programs as your shell), then runs `systemctl --user daemon-reload`
    and `systemctl --user enable --now keepwatch.service`. Re-running it updates the unit.

    The service does not see your shell's ssh-agent unless its socket is set in [environment] of the
    global config; see: keepwatch docs environment.

    Before changing anything it checks the command the service will run: it must not live in a temporary
    environment (uv's cache, where uvx puts it, or the temp directory; --force skips this check) and it must
    report this keepwatch's version. keepwatch never looks for another Python on PATH.

    Exit status: 0, or 1 if a check fails or systemctl (Task Scheduler) fails.
    """
    problem = _install_problem(force)
    if problem is not None:
        _fail(problem)
    if _on_windows():
        _install_windows(app, dry_run)
        return
    unit_path = systemd.unit_dir(app.paths) / systemd.UNIT_NAME
    custom_config = app.config_path.resolve() if app.config_path != app.paths.config_file else None
    text = systemd.unit_text(systemd.find_executable(), os.environ.get("PATH", ""), custom_config)
    commands = ["systemctl --user daemon-reload", f"systemctl --user enable --now {systemd.UNIT_NAME}"]
    if dry_run:
        click.echo(f"would write {unit_path}:\n")
        click.echo(text)
        click.echo("would run:\n  " + "\n  ".join(commands))
        return
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(text, encoding="utf-8")
    click.echo(f"wrote {unit_path}")
    _systemctl_or_fail("daemon-reload")
    _systemctl_or_fail("enable", "--now", systemd.UNIT_NAME)
    click.echo(f"ran: {'; '.join(commands)}")
    click.echo("check it with: systemctl --user status keepwatch   and   keepwatch status")
    if os.environ.get("SSH_AUTH_SOCK"):
        click.echo(
            "warning: SSH_AUTH_SOCK is set in this shell, but the service will not see that ssh-agent. "
            "Hooks that use ssh/scp need a key without a passphrase, or a stable agent socket set as "
            "SSH_AUTH_SOCK in [environment] of the global config; see: keepwatch docs environment"
        )


@cli.command()
@click.pass_obj
def uninstall(app: App) -> None:
    """Stop and remove what keepwatch install set up (systemd user service, or Windows logon task/shortcut).

    Watches, state and logs are left alone.

    Exit status: 0, or 1 if systemctl daemon-reload fails.
    """
    if _on_windows():
        if service_running(app.paths) and not _request_stop(app.paths, 30.0):
            click.echo("warning: the running service did not stop within 30s")
        result = winsched.run_powershell(winsched.unregister_script())
        if result.returncode != 0:
            _fail(f"could not remove the logon task: {_last_line(result.stderr)}")
        click.echo("removed the logon task and the Startup-folder shortcut (whichever existed)")
        click.echo("keepwatch will no longer start at logon")
        return
    unit_path = systemd.unit_dir(app.paths) / systemd.UNIT_NAME
    try:
        systemd.systemctl("disable", "--now", systemd.UNIT_NAME)
    except OSError as exc:
        _fail(f"cannot run systemctl: {exc}")
    if unit_path.exists():
        unit_path.unlink()
        click.echo(f"removed {unit_path}")
    else:
        click.echo(f"{unit_path} was not installed")
    _systemctl_or_fail("daemon-reload")
    click.echo("keepwatch will no longer start at login")


@cli.command()
@click.argument("topic", required=False)
@click.option("--all", "show_all", is_flag=True, help="Print every topic, in reading order.")
def docs(topic: str | None, show_all: bool) -> None:
    """Print the reference as Markdown. With no TOPIC, list the topics.

    When stdout is not a terminal (agents, pipes), the raw Markdown is printed unchanged; on a terminal it is
    rendered. Start with `keepwatch docs agent`.

    Exit status: 0, or 1 for an unknown topic.
    """
    if show_all:
        text = render_all()
    elif topic is None:
        text = topic_index()
    else:
        try:
            text = render_topic(topic)
        except UnknownTopic as exc:
            _fail(str(exc))
    if plain_output():
        click.echo(text)
    else:
        make_console().print(Markdown(text))


def _request_stop(paths: Paths, timeout: float) -> bool:
    """Ask a running service to stop; True once it has (its lock is free)."""
    paths.stop_request.parent.mkdir(parents=True, exist_ok=True)
    paths.stop_request.write_text(str(time.time()), encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not service_running(paths):
            return True
        time.sleep(0.2)
    return False


@cli.command()
@click.option("--timeout", type=float, default=30.0, show_default=True, help="Seconds to wait for the service to stop.")
@click.pass_obj
def stop(app: App, timeout: float) -> None:
    """Ask the running service to stop, and wait until it has. Works on every OS (the way to stop it on Windows).

    Running hooks are terminated and the service exits once their polls end.

    Exit status: 0 when the service has stopped, 1 if none is running or it did not stop in time.
    """
    if not service_running(app.paths):
        _fail("no keepwatch service is running")
    if _request_stop(app.paths, timeout):
        click.echo("keepwatch service stopped")
    else:
        _fail(f"the service did not stop within {timeout:g}s; see: keepwatch logs --since 5m")


def _scp_options(function):
    """The scp options every `keepwatch kit` transfer accepts."""
    decorators = [
        click.option("--protocol", type=click.Choice(transfer.PROTOCOLS), default="scp", show_default=True,
                     help="scp: the classic protocol, which scp-only servers accept (adds -O on OpenSSH 9+). "
                     "sftp: needs OpenSSH 9+, and is required for two remote endpoints."),
        click.option("--password-env", metavar="VAR",
                     help="Name of the environment variable holding the password (given to scp through SSH_ASKPASS; "
                     "never pass the password itself). Default: key authentication only (BatchMode)."),
        click.option("--identity", type=click.Path(dir_okay=False), help="Private key file for ssh -i (only this key is offered)."),
        click.option("--known-hosts", type=click.Path(dir_okay=False),
                     help="known_hosts file to check the host key against. Unknown host keys are always refused."),
        click.option("--port", type=click.IntRange(1, 65535), help="ssh port for user@host:path endpoints (scp -P)."),
        click.option("--ssh-option", "ssh_options", multiple=True, metavar="OPTION",
                     help='An ssh option passed as -o OPTION, e.g. "ProxyJump=bastion" (repeatable).'),
        click.option("--timeout", type=DURATION, metavar="DURATION", help='Give up after this long, e.g. "10m". Default: no limit (a command hook\'s own timeout still stops it).'),
    ]
    for decorator in reversed(decorators):
        function = decorator(function)
    return function


def _scp(protocol, password_env, identity, known_hosts, port, ssh_options, timeout) -> transfer.ScpOptions:
    extra = tuple(item for option in ssh_options for item in ("-o", option))
    try:
        return transfer.ScpOptions(
            protocol=protocol,
            password_env=password_env,
            identity=identity,
            known_hosts=known_hosts,
            port=port,
            ssh_options=extra,
            timeout=timeout,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None


def _kit_run(action) -> None:
    if os.environ.get("KEEPWATCH_HOOK") == "check":
        _fail("a check must not transfer files; do it in an action (keepwatch kit tcp-open is fine in a check)")
    try:
        result = action()
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    except (CommandFailed, transfer.TransferFailed) as exc:
        _fail(str(exc))
    except OSError as exc:
        _fail(f"{exc.strerror or exc}: {exc.filename}" if exc.filename else str(exc))
    if result is not None:
        click.echo(str(result))


@cli.group()
def kit() -> None:
    """Tools for command hooks: scp transfers and reachability checks (what Python hooks get as ctx.transfer).

    Each subcommand prints its result on stdout and, on failure, the reason on stderr. Endpoints are local
    paths, user@host:path or scp://user@host:port/path; host keys must already be known. Exit status: 0 on
    success, 1 on failure, 2 for bad usage. See: keepwatch docs transfers.
    """


@kit.command(name="copy")
@click.argument("source")
@click.argument("destination")
@_scp_options
def kit_copy(source: str, destination: str, **scp) -> None:
    """Copy one file from SOURCE to DESTINATION with scp (at least one of them remote). Prints nothing.

    Exit status: 0, 1 if scp failed, 2 for bad usage.
    """
    options = _scp(**scp)
    _kit_run(lambda: transfer.copy(source, destination, options=options))


@kit.command(name="pull")
@click.argument("remote")
@click.argument("local_dir", type=click.Path(file_okay=False))
@click.option("--size", type=click.IntRange(0), help="Fail unless the pulled file has exactly this many bytes.")
@click.option("--sha256", metavar="HEX", help="Fail unless the pulled file has this sha256; also lets an identical existing file count as done.")
@click.option("--on-conflict", type=click.Choice(transfer.CONFLICTS), default="skip-identical", show_default=True,
              help="When LOCAL_DIR already has the name: skip-identical (no-op if --sha256 matches, else fail), "
              "rename (NAME-1.ext), overwrite.")
@_scp_options
def kit_pull(remote: str, local_dir: str, size: int | None, sha256: str | None, on_conflict: str, **scp) -> None:
    """Copy REMOTE into LOCAL_DIR through a hidden .NAME.part file, verify it, then rename it. Prints the final path.

    Exit status: 0, 1 if the copy or a check failed (nothing is left behind), 2 for bad usage.
    """
    options = _scp(**scp)
    _kit_run(lambda: transfer.pull(remote, local_dir, size=size, sha256=sha256, on_conflict=on_conflict, options=options))


@kit.command(name="push")
@click.argument("path", type=click.Path(dir_okay=False))
@click.argument("remote_dir")
@click.option("--marker", type=click.Choice(transfer.MARKERS), default="sha256", show_default=True,
              help="After the file, upload NAME.sha256 (sha256sum format) as a completion marker, or none.")
@_scp_options
def kit_push(path: str, remote_dir: str, marker: str, **scp) -> None:
    """Upload PATH into REMOTE_DIR (user@host:dir), then its .sha256 marker. Prints the remote path.

    Exit status: 0, 1 if scp failed, 2 for bad usage.
    """
    options = _scp(**scp)
    _kit_run(lambda: transfer.push(path, remote_dir, marker=marker, options=options))


@kit.command(name="tcp-open")
@click.argument("host")
@click.option("--port", type=click.IntRange(1, 65535), default=22, show_default=True, help="TCP port to try.")
@click.option("--timeout", type=DURATION, default="5s", show_default=True, metavar="DURATION", help="How long to wait for the connection.")
def kit_tcp_open(host: str, port: int, timeout: float) -> None:
    """Check whether HOST accepts TCP connections on --port. Prints open or closed.

    Exit status: 0 if open, 1 if closed or unreachable, 2 for bad usage.
    """
    opened = transfer.tcp_open(host, port, timeout)
    click.echo("open" if opened else "closed")
    raise SystemExit(0 if opened else 1)


def main() -> None:
    cli()
