import ast
import importlib.util
from pathlib import Path

from remote_watcher_selftest import WatcherTests  # noqa: F401  (pytest collects it from this module)

SOURCE = Path(__file__).resolve().parents[1] / "src" / "keepwatch" / "remote_watcher.py"
SELFTEST = Path(__file__).resolve().with_name("remote_watcher_selftest.py")
FORBIDDEN = (
    "dataclass",
    "capture_output",
    "text=True",
    "__future__",
    "time.time_ns(",  # st_mtime_ns (3.3+) is fine
    "time.monotonic_ns(",
    "fromisoformat",
    "removeprefix",
    "removesuffix",
    "shlex.join",
    "cached_property",
    "breakpoint(",
    "asyncio.run",
    "math.prod",
    ":=",
)


def test_the_watcher_is_ascii():
    SOURCE.read_text(encoding="ascii")


def test_the_watcher_imports_nothing_keepwatch_could_shadow():
    tree = ast.parse(SOURCE.read_text(encoding="ascii"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    ours = {path.stem for path in SOURCE.parent.iterdir() if path.suffix == ".py" or path.is_dir()}
    assert imported & ours == set()


def test_the_watcher_and_its_tests_parse_as_old_python():
    # (3, 7) is the oldest grammar newer Pythons can check; 3.7 added no syntax over 3.6. CI also runs 3.6 itself.
    for path in (SOURCE, SELFTEST):
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 7))


def test_the_watcher_and_its_tests_avoid_newer_features():
    for path in (SOURCE, SELFTEST):
        text = path.read_text(encoding="utf-8")
        assert [word for word in FORBIDDEN if word in text] == [], path.name


def test_the_watchers_defaults_match_keepwatch():
    from keepwatch import observers
    from keepwatch.config import ObserverConfig

    spec = importlib.util.spec_from_file_location("keepwatch_remote_watcher_copy", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    defaults = ObserverConfig(name="r", kind="remote_files")
    assert module.DEFAULTS["ignore"] == list(defaults.ignore)
    assert (module.DEFAULTS["settle"], module.DEFAULTS["interval"], module.DEFAULTS["heartbeat"]) == (
        defaults.settle,
        defaults.interval,
        defaults.heartbeat,
    )
    assert module.DEFAULTS["rescan"] == observers.FILES_RESCAN
    assert (module.SETTLE_STEP, module.MIN_RESCAN) == (observers.FILES_SETTLE_STEP, observers.FILES_MIN_RESCAN)
