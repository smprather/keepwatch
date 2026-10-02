from pathlib import Path

import pytest

from keepwatch.config import ConfigError, load_watch_config

PULL = """
recipe = "pull"
[settings]
remote = "me@linux1"
remote_dir = "/data/out"
local_dir = "staging"
pattern = "*.tar.gz"
"""
PUSH = """
recipe = "push"
[settings]
local_dir = "staging"
dest = "me@linux2:incoming/"
password_env = "RELAY_PASSWORD"
"""


def load(make_watch, text, files=None):
    return load_watch_config(make_watch("w", config=text, files=files))


def problems(make_watch, text, files=None):
    with pytest.raises(ConfigError) as info:
        load(make_watch, text, files)
    return [str(problem) for problem in info.value.problems]


def test_pull_recipe_settings_and_observer(make_watch):
    config = load(make_watch, PULL)
    assert config.recipe == "pull"
    settings = config.settings
    assert settings["local_dir"] == str(config.watch_dir / "staging")
    assert (settings["pattern"], settings["settle"], settings["checksum"], settings["remote_python"]) == (
        "*.tar.gz",
        10.0,
        True,
        "auto",
    )
    assert settings["ignore"] == [".*", "*.tmp", "*.part", "*~"] and settings["port"] is None
    [observer] = config.observers.values()
    assert (observer.name, observer.kind, observer.remote, observer.dir) == ("remote", "remote_files", "me@linux1", "/data/out")
    assert (observer.pattern, observer.skip_ledger, observer.checksum) == ("*.tar.gz", "pulled", True)


def test_pull_known_hosts_and_identity_reach_the_observer(make_watch):
    config = load(make_watch, PULL + "known_hosts = 'kh'\nidentity = '~/.ssh/id_relay'\nport = 2222\n")
    observer = config.observers["remote"]
    assert observer.identity == Path.home() / ".ssh" / "id_relay" and observer.port == 2222
    assert observer.ssh_options == ("-o", f'UserKnownHostsFile="{(config.watch_dir / "kh").as_posix()}"')


def test_push_recipe_settings_and_observer(make_watch):
    config = load(make_watch, PUSH)
    settings = config.settings
    assert (settings["dest"], settings["password_env"], settings["protocol"]) == ("me@linux2:incoming/", "RELAY_PASSWORD", "scp")
    assert (settings["marker"], settings["after"], settings["keep_for"], settings["reachable_timeout"]) == ("sha256", "delete", 7 * 86400.0, 5.0)
    assert (settings["reachable_host"], settings["reachable_port"]) == (None, None)
    [observer] = config.observers.values()
    assert (observer.name, observer.kind, observer.path) == ("local", "files", config.watch_dir / "staging")


def test_unknown_recipe(make_watch):
    [problem] = problems(make_watch, 'recipe = "relay"\n')
    assert "'recipe' must be one of pull, push, got 'relay'" in problem


def test_missing_and_unknown_settings(make_watch):
    found = problems(make_watch, 'recipe = "pull"\n[settings]\nremote = "h"\nlocaldir = "x"\n')
    assert any("unknown key 'localdir' in [settings] (did you mean 'local_dir'?)" in p for p in found)
    assert any("needs 'remote_dir' in [settings]" in p for p in found)
    assert any("needs 'local_dir' in [settings]" in p for p in found)


def test_invalid_setting_values(make_watch):
    found = problems(make_watch, PUSH + 'after = "shred"\nmarker = "md5"\nprotocol = "ftp"\nport = 70000\n')
    assert len(found) == 4


def test_push_dest_must_be_remote(make_watch):
    [problem] = problems(make_watch, 'recipe = "push"\n[settings]\nlocal_dir = "s"\ndest = "/tmp/out"\n')
    assert "'dest' must be a remote directory" in problem


def test_archive_needs_archive_dir(make_watch):
    [problem] = problems(make_watch, PUSH + 'after = "archive"\n')
    assert "after = \"archive\" needs 'archive_dir'" in problem


def test_recipe_watches_have_no_hooks_of_their_own(make_watch):
    found = problems(make_watch, PULL + "[hooks]\ncheck = ['true']\n[observe.x]\nkind = 'files'\npath = 'in'\n",
                     files={"watch.py": "def check(ctx):\n    return True\n"})
    assert any("must not have [hooks]" in p for p in found)
    assert any("must not have [observe.*] tables" in p for p in found)
    assert any("must not have watch.py" in p for p in found)


def test_watches_without_a_recipe_are_unchanged(make_watch):
    config = load(make_watch, "[settings]\nanything = 1\n")
    assert config.recipe is None and config.settings == {"anything": 1}


def test_a_malformed_dest_is_a_config_error(make_watch):
    [problem] = problems(make_watch, 'recipe = "push"\n[settings]\nlocal_dir = "s"\ndest = "scp://host"\n')
    assert "'dest':" in problem and "scp://[user@]host[:port]/path" in problem


def test_archive_dir_must_differ_from_local_dir(make_watch):
    [problem] = problems(make_watch, PUSH + 'after = "archive"\narchive_dir = "staging"\n')
    assert "'archive_dir' must not be 'local_dir'" in problem


def test_pull_on_conflict_default_and_choices(make_watch):
    assert load(make_watch, PULL).settings["on_conflict"] == "rename"


def test_pull_on_conflict_must_be_a_choice(make_watch):
    [problem] = problems(make_watch, PULL + 'on_conflict = "keep"\n')
    assert "'on_conflict' must be one of skip-identical, rename, overwrite" in problem


def test_recipe_ssh_options_must_start_with_an_option(make_watch):
    [problem] = problems(make_watch, PUSH + 'ssh_options = ["ProxyJump=bastion"]\n')
    assert "'ssh_options' are scp arguments" in problem and "['-o', 'ProxyJump=bastion']" in problem


def test_delete_remote_needs_checksum(make_watch):
    [problem] = problems(make_watch, PULL + "delete_remote = true\nchecksum = false\n")
    assert "delete_remote = true needs checksum = true" in problem


def test_delete_remote_defaults(make_watch):
    settings = load(make_watch, PULL).settings
    assert (settings["delete_remote"], settings["delete_retry"]) == (False, 600.0)
