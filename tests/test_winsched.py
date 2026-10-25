from pathlib import Path

from watchglass import winsched


def test_quoting_doubles_single_quotes():
    script = winsched.register_script("C:/it's/pythonw.exe", "-m watchglass run")
    assert "'C:/it''s/pythonw.exe'" in script


def test_run_arguments():
    assert winsched.run_arguments(None) == "-m watchglass run"
    assert winsched.run_arguments(Path("C:/cfg/k.toml")) == f'-m watchglass --config "{Path("C:/cfg/k.toml")}" run'
