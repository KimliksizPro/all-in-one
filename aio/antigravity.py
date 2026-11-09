"""Linux Antigravity kurulumu, süreç kontrolü ve çalışan RPC keşfi."""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil

from . import storage
from .util import atomic_write_json, read_json, redact

LS_BINARY = Path("/opt/Antigravity/resources/bin/language_server")
APP_BINARY = Path("/opt/Antigravity/antigravity")
BINARY_NAMES = ("antigravity", "agy")
ENV_BINARY = "AIO_ANTIGRAVITY_BIN"
ENV_LS_BINARY = "AIO_ANTIGRAVITY_LS"
ENV_DATA_DIR = "AIO_ANTIGRAVITY_DATA"


def _config_home() -> Path:
    env = os.environ.get("XDG_CONFIG_HOME")
    return Path(env) if env and os.path.isabs(env) else Path.home() / ".config"


def _find_user_data_dir() -> Path:
    """Masaüstü uygulamasının kullanıcı verisi (Electron userData) dizini."""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    candidates = [
        _config_home() / "Antigravity",
        home / ".config" / "Antigravity",
        home / ".config" / "antigravity",
        home / ".var" / "app" / "com.google.Antigravity" / "config" / "Antigravity",
        home / "snap" / "antigravity" / "current" / ".config" / "Antigravity",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _find_gemini_data_dir(gemini_dir: Path) -> Path:
    # Masaüstü: ~/.gemini/antigravity, CLI (agy): ~/.gemini/antigravity-cli
    for name in ("antigravity", "antigravity-cli"):
        if (gemini_dir / name).is_dir():
            return gemini_dir / name
    return gemini_dir / "antigravity"


USER_DATA_DIR = _find_user_data_dir()
GEMINI_DIR = Path.home() / ".gemini"
ANTIGRAVITY_DATA_DIR = _find_gemini_data_dir(GEMINI_DIR)
APP_STORAGE = USER_DATA_DIR / "app_storage.json"
LS_LOG = USER_DATA_DIR / "logs" / "language_server.log"
MAIN_LOG = USER_DATA_DIR / "logs" / "main.log"
DEFAULT_VERSION = "2.29.0"

LS_SERVICE_PATH = "/exa.language_server_pb.LanguageServerService"
UNLEASH_PATH = f"{LS_SERVICE_PATH}/GetUnleashData"
USER_STATUS_PATH = f"{LS_SERVICE_PATH}/GetUserStatus"
AUTH_STATUS_PATH = f"{LS_SERVICE_PATH}/GetAuthStatus"
LOGIN_PATH = f"{LS_SERVICE_PATH}/Login"


class AppError(RuntimeError):
    pass


@dataclass
class AppInfo:
    installed: bool
    binary: str | None
    ls_binary: str | None
    version: str
    running: bool
    pids: list[int]
    user_data_dir: str
    data_dir: str

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "binary": self.binary,
            "ls_binary": self.ls_binary,
            "version": self.version,
            "running": self.running,
            "pids": self.pids,
            "user_data_dir": self.user_data_dir,
            "data_dir": self.data_dir,
        }


@dataclass
class LSInfo:
    pid: int
    csrf: str
    base_url: str


@dataclass
class StopResult:
    stopped: list[int]
    remaining: list[int]
    forced: bool = False

    def to_dict(self) -> dict:
        return {"stopped": self.stopped, "remaining": self.remaining, "forced": self.forced}


def _safe_exe(proc: psutil.Process) -> str:
    try:
        return proc.exe()
    except (psutil.Error, OSError):
        return ""


def _safe_cmdline(proc: psutil.Process) -> list[str]:
    try:
        return proc.cmdline() or []
    except (psutil.Error, OSError):
        return []


def _process_identity(proc: psutil.Process) -> tuple[str, str, str]:
    # process_iter prefetches these together; direct callers still work normally.
    info = getattr(proc, "info", {})
    if all(key in info for key in ("name", "exe", "cmdline")):
        return (info["name"] or "").lower(), info["exe"] or "", " ".join(info["cmdline"] or [])
    try:
        name = (proc.name() or "").lower()
    except (psutil.Error, OSError):
        name = ""
    return name, _safe_exe(proc), " ".join(_safe_cmdline(proc))


def _is_isolated_process(cmd: str) -> bool:
    # Isolated quota/login helpers must never become application stop targets.
    return any(marker in cmd for marker in ("/aio-probe-", "\\aio-probe-", "/aio-login-", "\\aio-login-"))


def _is_ls_identity(name: str, exe: str, cmd: str) -> bool:
    if _is_isolated_process(cmd):
        return False
    if exe and Path(exe).name == "language_server" and Path(exe).resolve() == (installed_ls_binary() or LS_BINARY).resolve():
        return True
    return name == "language_server" and (
        "--override_ide_name antigravity" in cmd or "--app_data_dir antigravity" in cmd
    )


def _is_ls_process(proc: psutil.Process) -> bool:
    return _is_ls_identity(*_process_identity(proc))


def _is_app_identity(name: str, exe: str, cmd: str) -> bool:
    if _is_isolated_process(cmd) or _is_ls_identity(name, exe, cmd):
        return False
    if exe and Path(exe).name.lower() in BINARY_NAMES:
        return True
    return name in BINARY_NAMES or (
        "/opt/Antigravity/antigravity" in cmd and "language_server" not in cmd
    )


def _is_app_process(proc: psutil.Process) -> bool:
    return _is_app_identity(*_process_identity(proc))


_DISCOVERY_TTL_SECONDS = 0.5
_discovery_lock = threading.RLock()
_discovery_cache: tuple[tuple[psutil.Process, ...], tuple[psutil.Process, ...]] | None = None
_discovery_at = 0.0


def _invalidate_discovery() -> None:
    global _discovery_cache
    with _discovery_lock:
        _discovery_cache = None


def _invalidates_discovery(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _invalidate_discovery()
        try:
            return fn(*args, **kwargs)
        finally:
            _invalidate_discovery()
    return wrapper


def _scan_processes() -> tuple[tuple[psutil.Process, ...], tuple[psutil.Process, ...]]:
    apps: list[psutil.Process] = []
    servers: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            identity = _process_identity(proc)
            if _is_ls_identity(*identity):
                servers.append(proc)
            elif _is_app_identity(*identity):
                apps.append(proc)
        except (psutil.Error, OSError):
            continue
    return tuple(apps), tuple(servers)


def _discover_processes() -> tuple[tuple[psutil.Process, ...], tuple[psutil.Process, ...]]:
    global _discovery_cache, _discovery_at
    with _discovery_lock:
        now = time.monotonic()
        if _discovery_cache is not None and now - _discovery_at < _DISCOVERY_TTL_SECONDS:
            return _discovery_cache
        result = _scan_processes()
        # Never cache absence: running() guards keyring swaps against newly opened apps.
        _discovery_cache = result if any(result) else None
        _discovery_at = now
        return result


def app_processes() -> list[psutil.Process]:
    return list(_discover_processes()[0])


def ls_processes() -> list[psutil.Process]:
    return list(_discover_processes()[1])


def _usable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _search_dirs() -> list[Path]:
    """Antigravity'nin kurulu olabileceği, ucuz ve bilinen dizinler."""
    home = Path.home()
    data_home = os.environ.get("XDG_DATA_HOME")
    data = Path(data_home) if data_home and os.path.isabs(data_home) else home / ".local" / "share"
    dirs = [
        Path("/opt/Antigravity"),
        Path("/opt/antigravity"),
        Path("/usr/lib/antigravity"),
        Path("/usr/lib/Antigravity"),
        Path("/usr/share/antigravity"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        home / ".local" / "bin",
        home / ".antigravity" / "bin",
        home / ".gemini" / "antigravity-cli" / "bin",
        data / "antigravity",
        home / "Applications" / "Antigravity",
        Path("/var/lib/flatpak/app/com.google.Antigravity/current/active/files"),
        home / ".local" / "share" / "flatpak" / "app" / "com.google.Antigravity" / "current" / "active" / "files",
        Path("/snap/antigravity/current"),
    ]
    # mise / asdf: .../installs/antigravity*/<sürüm|latest>/
    for manager in (data / "mise" / "installs", home / ".asdf" / "installs"):
        try:
            for tool in manager.glob("antigravity*"):
                dirs.extend(sorted(tool.glob("*"), reverse=True))
        except OSError:
            continue
    return dirs


def installed_binary() -> Path | None:
    candidates: list[Path] = []
    override = os.environ.get(ENV_BINARY)
    if override:
        candidates.append(Path(override).expanduser())
    for name in BINARY_NAMES:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    candidates += [APP_BINARY, Path("/usr/bin/antigravity")]
    for directory in _search_dirs():
        candidates.extend(directory / name for name in BINARY_NAMES)
    seen: set[Path] = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if _usable(p):
            return p
    return None


def installed_ls_binary() -> Path | None:
    override = os.environ.get(ENV_LS_BINARY)
    if override and _usable(Path(override).expanduser()):
        return Path(override).expanduser()
    if _usable(LS_BINARY):
        return LS_BINARY
    binary = installed_binary()
    if binary:
        resolved = Path(binary).resolve()
        for base in {resolved.parent, Path(binary).parent}:
            candidate = base / "resources" / "bin" / "language_server"
            if _usable(candidate):
                return candidate
            sibling = base / "language_server"
            if _usable(sibling):
                return sibling
    return None


def installed() -> bool:
    return installed_binary() is not None and installed_ls_binary() is not None


def version() -> str:
    """Antigravity uygulama sürümü; LS'ye override olarak verilir."""
    try:
        size = MAIN_LOG.stat().st_size
        with MAIN_LOG.open("rb") as fh:
            if size > 131072:
                fh.seek(-131072, os.SEEK_END)
            text = fh.read().decode("utf-8", "replace")
        matches = re.findall(r"Starting app \(v([0-9][0-9A-Za-z.\-]*)\)", text)
        if matches:
            return matches[-1]
    except OSError:
        pass
    return DEFAULT_VERSION


def running() -> bool:
    apps, servers = _discover_processes()
    return bool(apps or servers)


def app_info() -> AppInfo:
    binary = installed_binary()
    ls_binary = installed_ls_binary()
    apps, servers = _discover_processes()
    pids = sorted({p.pid for p in (*apps, *servers)})
    return AppInfo(
        installed=binary is not None and ls_binary is not None,
        binary=str(binary) if binary else None,
        ls_binary=str(ls_binary) if ls_binary else None,
        version=version(),
        running=bool(pids),
        pids=pids,
        user_data_dir=str(USER_DATA_DIR),
        data_dir=str(ANTIGRAVITY_DATA_DIR),
    )


def _parse_csrf(cmd: list[str]) -> str | None:
    for i, arg in enumerate(cmd):
        if arg == "--csrf_token" and i + 1 < len(cmd):
            return cmd[i + 1]
        if arg.startswith("--csrf_token="):
            return arg.split("=", 1)[1]
    return None


def _listen_ports(proc: psutil.Process) -> list[int]:
    ports: set[int] = set()
    try:
        conns = proc.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        conns = []
    for conn in conns:
        try:
            if conn.status == psutil.CONN_LISTEN and conn.laddr:
                ports.add(int(conn.laddr.port))
        except (AttributeError, TypeError, ValueError):
            continue
    if not ports:
        # Fallback: aynı kullanıcıya ait ls logundan son portları oku.
        ports.update(_ports_from_log())
    return sorted(ports)


_LOG_PORT_RE = re.compile(r"listening on (?:random|fixed) port at (\d+) for (HTTP|HTTPS)", re.I)


def _ports_from_log() -> set[int]:
    try:
        size = LS_LOG.stat().st_size
        with LS_LOG.open("rb") as fh:
            if size > 262144:
                fh.seek(-262144, os.SEEK_END)
            text = fh.read().decode("utf-8", "replace")
        return {int(m.group(1)) for m in _LOG_PORT_RE.finditer(text)}
    except (OSError, ValueError):
        return set()


def _try_base(port: int, csrf: str) -> str | None:
    headers = {
        "X-Codeium-Csrf-Token": csrf,
        "Connect-Protocol-Version": "1",
        "Content-Type": "application/json",
    }
    for scheme in ("http", "https"):
        base = f"{scheme}://127.0.0.1:{port}"
        try:
            resp = httpx.post(
                base + UNLEASH_PATH,
                json={"wrapper_data": {}},
                headers=headers,
                timeout=1.5,
                verify=False,
            )
            if resp.status_code == 200:
                return base
        except (httpx.HTTPError, ValueError, OSError):
            continue
    return None


def running_ls_info() -> LSInfo | None:
    """Çalışan Antigravity language_server'ının HTTP tabanını bul."""
    for proc in ls_processes():
        cmd = _safe_cmdline(proc)
        csrf = _parse_csrf(cmd)
        if not csrf:
            continue
        for port in _listen_ports(proc):
            base = _try_base(port, csrf)
            if base:
                return LSInfo(pid=proc.pid, csrf=csrf, base_url=base)
    return None


def _headers(csrf: str) -> dict[str, str]:
    return {
        "X-Codeium-Csrf-Token": csrf,
        "Connect-Protocol-Version": "1",
        "Content-Type": "application/json",
    }


def wait_for_ls(timeout: float = 30.0) -> LSInfo | None:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        info = running_ls_info()
        if info:
            return info
        time.sleep(0.5)
    return None


def _ls_post(path: str, body: dict, *, timeout: float = 20.0) -> dict:
    info = running_ls_info()
    if not info:
        raise AppError("Antigravity language_server çalışmıyor. Önce Antigravity'yi açın.")
    try:
        resp = httpx.post(
            info.base_url + path,
            json=body,
            headers=_headers(info.csrf),
            timeout=timeout,
            verify=False,
        )
    except httpx.HTTPError as exc:
        raise AppError(f"language_server isteği başarısız: {redact(exc)}") from exc
    if resp.status_code != 200:
        raise AppError(f"language_server HTTP {resp.status_code}: {redact(resp.text[:300])}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise AppError("language_server JSON yanıtı geçersiz.") from exc
    return data if isinstance(data, dict) else {}


def fetch_auth_status(timeout: float = 10.0) -> dict | None:
    try:
        return _ls_post(AUTH_STATUS_PATH, {}, timeout=timeout)
    except AppError:
        return None


def login_with_browser(timeout: float = 300.0) -> dict:
    """Antigravity'nin kendi OAuth tarayıcı akışını başlatır.

    RPC, kullanıcı girişi tamamlanana kadar açık kalabilir. Çağıran taraf bunu
    arka planda çalıştırmalı ve keyring değişimini ayrıca izlemelidir.
    """
    return _ls_post(
        LOGIN_PATH,
        {"isGcpTos": False, "additionalScopes": [], "enableBusinessLogin": False},
        timeout=timeout,
    )


def fetch_usage_cli(timeout: float = 45.0) -> dict:
    """CLI (`agy`) kurulumlarında kota: `agy --output-format json -p=/usage`."""
    binary = installed_binary()
    if not binary:
        raise AppError("Antigravity kurulu değil.")
    try:
        proc = subprocess.run(
            [str(binary), "--output-format", "json", "-p=/usage"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path.home()),
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError("agy /usage zaman aşımına uğradı.") from exc
    except OSError as exc:
        raise AppError(f"agy çalıştırılamadı: {redact(exc)}") from exc
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise AppError(f"agy /usage yanıtı JSON değil: {redact((proc.stderr or proc.stdout)[:200])}") from exc
    if not isinstance(data, dict) or data.get("status") != "SUCCESS":
        raise AppError(f"agy /usage başarısız: {redact(str(data)[:200])}")
    return data


def fetch_user_status_from_running(timeout: float = 20.0) -> dict | None:
    info = running_ls_info()
    if not info:
        return None
    body = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}}
    try:
        resp = httpx.post(
            info.base_url + USER_STATUS_PATH,
            json=body,
            headers=_headers(info.csrf),
            timeout=timeout,
            verify=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError, OSError):
        return None
    return None


@_invalidates_discovery
def stop(timeout: float = 12.0, force: bool = False) -> StopResult:
    """Antigravity ve ona ait language_server süreçlerini durdur."""
    apps, servers = _scan_processes()
    roots = (*apps, *servers)
    procs: dict[int, psutil.Process] = {}
    for root in roots:
        procs[root.pid] = root
        try:
            for child in root.children(recursive=True):
                procs[child.pid] = child
        except (psutil.Error, OSError):
            continue
    procs = {pid: p for pid, p in procs.items() if pid != os.getpid()}
    stopped: list[int] = []
    for proc in procs.values():
        try:
            proc.terminate()
            stopped.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    deadline = time.monotonic() + max(1.0, timeout)
    remaining: list[int] = []
    while time.monotonic() < deadline:
        remaining = []
        for pid in list(procs):
            try:
                proc = psutil.Process(pid)
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    remaining.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not remaining:
            return StopResult(stopped=stopped, remaining=[], forced=False)
        time.sleep(0.25)
    if force:
        for pid in remaining:
            try:
                psutil.Process(pid).kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(0.5)
        remaining = []
        for pid in list(procs):
            try:
                proc = psutil.Process(pid)
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    remaining.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return StopResult(stopped=stopped, remaining=remaining, forced=True)
    return StopResult(stopped=stopped, remaining=remaining, forced=False)


@_invalidates_discovery
def start() -> int:
    binary = installed_binary()
    if not binary:
        raise AppError("Antigravity kurulu değil (antigravity çalıştırılabilir dosyası bulunamadı).")
    env = os.environ.copy()
    try:
        proc = subprocess.Popen(
            [str(binary)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=str(Path.home()),
            env=env,
        )
    except OSError as exc:
        raise AppError(f"Antigravity başlatılamadı: {redact(exc)}") from exc
    return proc.pid


def wait_started(timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if running():
            return True
        time.sleep(0.4)
    return running()


def read_app_storage() -> dict:
    data = read_json(APP_STORAGE, {})
    return data if isinstance(data, dict) else {}


def update_last_login(email: str) -> Path | None:
    """Antigravity'nin UI'da gösterdiği son giriş adını güncelle.

    Skills/MCP/settings dosyalarına dokunulmaz; yalnızca uygulama kabuğunun
    `app_storage.json` anahtarı güncellenir ve öncesinde yedek alınır.
    """
    if not APP_STORAGE.is_file():
        return None
    data = read_app_storage()
    if not data:
        return None
    try:
        raw = APP_STORAGE.read_bytes()
        storage.backup_file(APP_STORAGE, "app-storage", raw)
    except OSError:
        pass
    data["jetski.onboarding.lastLoginUsername"] = email
    atomic_write_json(APP_STORAGE, data, mode=0o600)
    return APP_STORAGE


def log_tail(path: Path, limit: int = 40) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 262144:
                fh.seek(-262144, os.SEEK_END)
            text = fh.read().decode("utf-8", "replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines[-limit:])
    except OSError:
        return ""
