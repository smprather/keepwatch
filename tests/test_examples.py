import json
import shutil

import pytest
from click.testing import CliRunner

from keepwatch.cli import cli
from keepwatch.reference import example_dirs, render_topic


def run(*args):
    return CliRunner().invoke(cli, list(args))


def install(xdg, name):
    source = next(d for d in example_dirs() if d.name == name)
    target = xdg.default_watches_dir / name
    shutil.copytree(source, target)
    return target


def test_examples_are_in_the_docs():
    text = render_topic("examples")
    assert [d.name for d in example_dirs()] == ["disk-space-ps", "greet-once", "psg-export", "site-down"]
    for directory in example_dirs():
        for path in directory.iterdir():
            if path.is_file():
                assert f"{directory.name}/{path.name}" in text


def test_psg_export_example_validates_and_polls(xdg):
    install(xdg, "psg-export")
    assert run("validate", "psg-export").exit_code == 0
    data = json.loads(run("poll", "psg-export", "--dry-run", "--json").output)
    assert data["polls"][0]["outcome"] == "false"


@pytest.mark.posix_only
def test_site_down_example_validates_and_polls(xdg):
    install(xdg, "site-down")
    assert run("validate", "site-down").exit_code == 0
    data = json.loads(run("poll", "site-down", "--fake", "true,false", "--json").output)
    assert [p["results"][0]["status"] for p in data["polls"]] == ["ok", "ok"]


@pytest.mark.skipif(shutil.which("expect") is None, reason="expect is not installed")
def test_expect_example_runs_once(xdg):
    install(xdg, "greet-once")
    assert run("validate", "greet-once").exit_code == 0
    first = json.loads(run("poll", "greet-once", "--json").output)["polls"][0]
    assert first["outcome"] == "true" and first["results"][0]["status"] == "ok"
    second = json.loads(run("poll", "greet-once", "--json").output)["polls"][0]
    assert second["outcome"] == "false"


@pytest.mark.windows_only
def test_disk_space_example_runs_on_windows(xdg):
    install(xdg, "disk-space-ps")
    assert run("validate", "disk-space-ps").exit_code == 0
    data = json.loads(run("poll", "disk-space-ps", "--fake", "true,false", "--json").output)
    assert [p["results"][0]["status"] for p in data["polls"]] == ["ok", "ok"]
    real = json.loads(run("poll", "disk-space-ps", "--json").output)["polls"][0]
    assert real["outcome"] in ("true", "false")
