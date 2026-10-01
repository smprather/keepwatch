import json
import re
import time

from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.locks import hold_lock
from keepwatch.offline import OfflineMarker, write_offline
from keepwatch.paths import ensure_private_dir
from keepwatch.service import Service


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_status_without_a_service(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    write_offline(xdg, "b", OfflineMarker(reason="disabled by user", since="2026-09-30T10:00:00.000-05:00", by_user=True))
    (xdg.state_home / "watches" / "gone").mkdir(parents=True)
    result = run("status")
    assert result.exit_code == 0
    assert "service: not running" in result.output
    assert re.search(r"^a\s+idle", result.output, re.M)
    assert re.search(r"^b\s+offline", result.output, re.M)
    assert "offline since 2026-09-30T10:00:00.000-05:00: disabled by user" in result.output
    assert "orphaned state: gone" in result.output
    data = json.loads(run("status", "--json").output)
    assert data["service"]["running"] is False
    assert data["orphaned_state"] == ["gone"]
    assert data["watches"]["b"]["offline"]["by_user"] is True
    assert data["watches"]["a"]["known_to_service"] is False


def test_status_with_a_running_service(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    ensure_private_dir(xdg.runtime)
    service = Service(paths=xdg, config_path=xdg.config_file, sink=lambda record: None, start_threads=False)
    service.start()
    service.runners["a"].poll_once(time.time())
    service.tick()
    with hold_lock(xdg.service_lock):
        data = json.loads(run("status", "--json").output)
        text = run("status", "a").output
    assert data["service"]["running"] is True
    entry = data["watches"]["a"]
    assert entry["condition"] is True
    assert entry["last_poll"]["outcome"] == "true"
    assert re.search(r"^a\s+online\s+condition TRUE\s+failures 0\s+last true", text, re.M)
    assert "service: running" in text


def test_status_for_an_unknown_watch(xdg, make_watch):
    make_watch("psg-export", config='[hooks]\ncheck = ["true"]\n')
    result = run("status", "psg-exprot")
    assert result.exit_code == 1
    assert "did you mean 'psg-export'" in result.output
