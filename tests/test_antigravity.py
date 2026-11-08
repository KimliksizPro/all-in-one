from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aio import antigravity


class FakeProcess:
    def __init__(self, pid, name, exe, cmdline):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "exe": exe, "cmdline": cmdline}

    def name(self):
        raise AssertionError("process_iter must prefetch identity attributes")

    def exe(self):
        raise AssertionError("process_iter must prefetch identity attributes")

    def cmdline(self):
        raise AssertionError("process_iter must prefetch identity attributes")


@pytest.fixture
def discovery(monkeypatch):
    clock = [100.0]
    processes = []
    scans = []

    def process_iter(attrs):
        scans.append(attrs)
        return iter(processes)

    monkeypatch.setattr(antigravity.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(antigravity.psutil, "process_iter", process_iter)
    antigravity._invalidate_discovery()
    yield clock, processes, scans
    antigravity._invalidate_discovery()


@pytest.mark.parametrize("directory", ["/tmp/aio-probe-test", "/tmp/aio-login-test", r"C:\aio-probe-test", r"C:\aio-login-test"])
def test_isolated_language_servers_are_not_application_processes(directory, discovery):
    _, processes, _ = discovery
    proc = FakeProcess(101, "language_server", str(antigravity.LS_BINARY), [
        "language_server", "--override_ide_name", "antigravity", "--user_data_dir", directory,
    ])
    processes.append(proc)
    assert not antigravity._is_ls_process(proc)
    assert not antigravity._is_app_process(proc)
    assert antigravity.app_processes() == []
    assert antigravity.ls_processes() == []
    assert antigravity.running() is False


def test_unrelated_language_server_is_not_an_antigravity_stop_target(discovery):
    _, processes, _ = discovery
    other = FakeProcess(109, "language_server", "/opt/OtherEditor/language_server", ["language_server"])
    processes.append(other)
    assert not antigravity.running()
    assert antigravity.app_processes() == []
    assert antigravity.ls_processes() == []


def test_app_info_and_running_share_prefetched_discovery(discovery, monkeypatch):
    clock, processes, scans = discovery
    app = FakeProcess(102, "antigravity", "/opt/Antigravity/antigravity", ["antigravity"])
    server = FakeProcess(103, "language_server", "/mock/language_server", ["language_server"])
    processes.extend([app, server])
    monkeypatch.setattr(antigravity, "installed_binary", lambda: Path("/mock/antigravity"))
    monkeypatch.setattr(antigravity, "installed_ls_binary", lambda: Path("/mock/language_server"))
    monkeypatch.setattr(antigravity, "version", lambda: "test")
    info = antigravity.app_info()
    assert info.installed and info.running and info.pids == [102, 103]
    assert antigravity.running()
    assert antigravity.app_processes() == [app]
    returned = antigravity.ls_processes()
    returned.clear()
    assert antigravity.ls_processes() == [server]
    assert scans == [["pid", "name", "exe", "cmdline"]]
    clock[0] += antigravity._DISCOVERY_TTL_SECONDS + 0.01
    processes.clear()
    assert not antigravity.running()
    assert len(scans) == 2


def test_absence_is_not_cached_before_keyring_swap(discovery):
    _, processes, scans = discovery
    assert not antigravity.running()
    processes.append(FakeProcess(104, "antigravity", "/opt/Antigravity/antigravity", []))
    assert antigravity.running()
    assert len(scans) == 2


def test_start_invalidates_discovery_without_launching_real_process(discovery, monkeypatch):
    _, processes, scans = discovery
    processes.append(FakeProcess(105, "antigravity", "/opt/Antigravity/antigravity", []))
    assert antigravity.running()
    monkeypatch.setattr(antigravity, "installed_binary", lambda: Path("/mock/antigravity"))
    monkeypatch.setattr(antigravity.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=106))
    assert antigravity.start() == 106
    assert antigravity.running()
    assert len(scans) == 2


def test_failed_start_also_invalidates_discovery(discovery, monkeypatch):
    _, processes, scans = discovery
    processes.append(FakeProcess(107, "antigravity", "/opt/Antigravity/antigravity", []))
    assert antigravity.running()
    monkeypatch.setattr(antigravity, "installed_binary", lambda: None)
    with pytest.raises(antigravity.AppError):
        antigravity.start()
    processes.clear()
    assert not antigravity.running()
    assert len(scans) == 2


def test_stop_uses_fresh_discovery_and_invalidates_afterwards(discovery):
    _, processes, scans = discovery
    processes.append(FakeProcess(108, "antigravity", "/opt/Antigravity/antigravity", []))
    assert antigravity.running()
    # A cached, exited PID must not become a termination target.
    processes.clear()
    result = antigravity.stop()
    assert result.stopped == result.remaining == []
    assert not antigravity.running()
    assert len(scans) == 3


def test_missing_path_lookup_never_accepts_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(antigravity.shutil, "which", lambda name: None)
    monkeypatch.setattr(antigravity, "APP_BINARY", tmp_path / "missing")
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    monkeypatch.setattr(antigravity.os, "access", lambda path, mode: True)
    assert antigravity.installed_binary() is None


def test_install_candidate_must_be_an_executable_file(tmp_path, monkeypatch):
    candidate = tmp_path / "antigravity"
    candidate.mkdir()
    monkeypatch.setattr(antigravity.shutil, "which", lambda name: str(candidate))
    monkeypatch.setattr(antigravity, "APP_BINARY", tmp_path / "missing")
    monkeypatch.setattr(antigravity.os, "access", lambda path, mode: path == candidate)
    assert antigravity.installed_binary() is None
    candidate.rmdir()
    candidate.write_text("test executable")
    assert antigravity.installed_binary() == candidate
