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
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import rich_click as click

from keepwatch import __version__
from keepwatch.config import (
    ConfigError,
    GlobalConfig,
    WatchConfig,
    WatchNotFound,
    discover_watches,
    find_watch,
    load_global_config,
    load_watch_config,
)
from keepwatch.locks import LockBusy, hold_lock
from keepwatch.logstore import LogWriter, QueueSink, fan_out, level_filter
from keepwatch.output import ConsolePrinter, make_console, plain_output
from keepwatch.paths import PathError, Paths, ensure_private_dir, remove_stale_process_dirs, resolve_paths
from keepwatch.pollengine import Fake, PollEngine, PollReport, parse_fakes
from keepwatch.runner import Runner
from keepwatch.service import Service
from keepwatch.state import initial_state
from keepwatch.validation import validate_watches

COMMAND_GROUPS = {
    "keepwatch": [
        {"name": "Run", "commands": ["run"]},
        {"name": "Develop", "commands": ["validate", "poll"]},
        {"name": "Inspect", "commands": ["status", "logs"]},
        {"name": "Control", "commands": ["enable", "disable", "rename"]},
    ]
}
_PLAIN_BOXES = {
    "style_commands_panel_box": "SIMPLE_HEAD",
    "style_options_panel_box": "SIMPLE_HEAD",
    "style_errors_panel_box": "SIMPLE_HEAD",
}


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


def _fail(message: str) -> NoReturn:
    make_console(stderr=True).print(message)
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
    """
    paths = resolve_paths()
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
    initial_condition: bool | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """Run one poll of watch NAME now, printing everything as it happens.

    Every record also goes to the log file. A manual poll shares the watch's persistent data (ledgers)
    with the service but not its condition or failure count, and holds the watch's lock so the service
    and a manual poll never run the same watch at once.

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
                report = engine.poll(watch, state, fake=fake, dry_run=dry_run)
                reports.append(report)
                state = report.after
    if as_json:
        click.echo(json.dumps({"polls": [r.to_dict() for r in reports], "records": records}, indent=2, default=str))
    raise SystemExit(1 if any(report.failed for report in reports) else 0)


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
        printer = level_filter(ConsolePrinter(make_console(), verbose=verbose), minimum)
        sink = QueueSink(fan_out(writer.write, printer))
        stack.callback(sink.close)
        stack.callback(shutil.rmtree, paths.process_dir(os.getpid()), True)
        stop = threading.Event()

        def request_stop(signum: int, frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        service = Service(paths=paths, config_path=app.config_path, sink=sink, only=set(only))
        try:
            service.run(stop)
        except ConfigError as exc:
            _fail("\n".join(str(problem) for problem in exc.problems))
    raise SystemExit(0)


def main() -> None:
    cli()
