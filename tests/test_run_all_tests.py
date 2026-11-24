import sys
from types import SimpleNamespace

import run_all_tests


def test_runner_defaults_to_four_loadfile_workers_and_preserves_pytest_args(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(sys, "argv", ["run_all_tests.py", "-q", "--maxfail=1"])
    monkeypatch.setattr(run_all_tests.subprocess, "run", fake_run)

    assert run_all_tests.main() == 7
    command, kwargs = commands[0]
    assert command[4:] == ["-n", "4", "--dist", "loadfile", "-q", "--maxfail=1"]
    assert kwargs["cwd"] == run_all_tests.Path(__file__).resolve().parent.parent


def test_runner_forwards_n_zero_to_override_default(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(sys, "argv", ["run_all_tests.py", "-n", "0"])
    monkeypatch.setattr(run_all_tests.subprocess, "run", fake_run)

    assert run_all_tests.main() == 0
    assert commands[0][-6:] == ["-n", "4", "--dist", "loadfile", "-n", "0"]
