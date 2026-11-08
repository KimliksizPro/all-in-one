"""Hesap ekleme akışı: doğrudan Google OAuth + Antigravity keyring formatı.

Callback sunucusu bizim sürecimizde olduğu için Antigravity/LS kapansa bile
giriş tamamlanabilir. Token canlı keyring'e değil, yeni profilin kasasına
yazılır; mevcut canlı hesap değişmez.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import antigravity
from .accounts import AccountError, Accounts, NoLiveAccountError
from .google_oauth import GoogleOAuthFlow
from .keyring import KEYRING_LOCK, KeyringError, TokenFormatError
from .locks import keyring_file_lock
from .util import now_iso, redact

LOGIN_TIMEOUT = 300.0
POLL_INTERVAL = 1.2


@dataclass
class LoginState:
    running: bool = False
    status: str = "idle"  # idle | starting | waiting | success | success_same | error | timeout
    message: str = ""
    before_email: str | None = None
    before_profile: str | None = None
    captured_profile: str | None = None
    captured_email: str | None = None
    auth_url: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "status": self.status,
            "message": self.message,
            "before_email": self.before_email,
            "before_profile": self.before_profile,
            "captured_profile": self.captured_profile,
            "captured_email": self.captured_email,
            "auth_url": self.auth_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class LoginManager:
    def __init__(
        self,
        accounts: Accounts,
        *,
        app_module=antigravity,
        login_fn: Callable[[float], dict] | None = None,
        wait_ls_fn: Callable[[float], object] | None = None,
        oauth_factory: Callable[[], GoogleOAuthFlow] | None = None,
        timeout: float = LOGIN_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
    ):
        self.accounts = accounts
        self.app = app_module
        # Sadece test/legacy: çalışan uygulamanın LS Login RPC'si.
        self.login_fn = login_fn
        self.wait_ls_fn = wait_ls_fn or antigravity.wait_for_ls
        self.oauth_factory = oauth_factory or (lambda: GoogleOAuthFlow(timeout=timeout))
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._lock = threading.RLock()
        self._state = LoginState()
        self._rpc_done = False
        self._rpc_data: dict | None = None
        self._rpc_error: str | None = None
        self._thread: threading.Thread | None = None

    # ---------- durum ----------

    def state(self) -> dict:
        with self._lock:
            return self._state.to_dict()

    def is_running(self) -> bool:
        with self._lock:
            return self._state.running

    def _set(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)

    def _finish(self, status: str, message: str, *, error: str | None = None,
                captured_profile: str | None = None, captured_email: str | None = None) -> None:
        with self._lock:
            self._state.running = False
            self._state.status = status
            self._state.message = message
            self._state.error = error
            self._state.captured_profile = captured_profile
            self._state.captured_email = captured_email
            self._state.auth_url = None
            self._state.finished_at = now_iso()

    def reset(self) -> dict:
        with self._lock:
            if self._state.running:
                raise AccountError("Giriş akışı sürüyor; bitmesini bekleyin.")
            self._state = LoginState()
            return self._state.to_dict()

    def start(self) -> dict:
        with self._lock:
            if self._state.running:
                return self._state.to_dict()
            self._state = LoginState(
                running=True,
                status="starting",
                message="Giriş hazırlanıyor…",
                started_at=now_iso(),
            )
            self._rpc_done = False
            self._rpc_data = None
            self._rpc_error = None
            thread = threading.Thread(target=self._run, name="aio-login", daemon=True)
            self._thread = thread
            thread.start()
            return self._state.to_dict()

    # ---------- ortak hazırlık ----------

    def _capture_before(self) -> tuple[str | None, str | None]:
        with KEYRING_LOCK, keyring_file_lock(blocking=True):
            self.accounts.bootstrap()
            try:
                live = self.accounts.keyring.read_live()
            except (KeyringError, TokenFormatError):
                live = None
            before_email = live.email if live and live.email else None
            before_profile = None
            if before_email:
                state = self.accounts.state()
                meta = self.accounts._match_by_email(state, before_email)
                if meta is None:
                    try:
                        result = self.accounts.capture_live_auto()
                        before_profile = result.profile.name
                    except (AccountError, NoLiveAccountError):
                        before_profile = None
                else:
                    before_profile = meta.name
            self._set(before_email=before_email, before_profile=before_profile)
            return before_email, before_profile

    def _run(self) -> None:
        try:
            if self.login_fn is not None:
                self._run_app_based()
            else:
                self._run_oauth()
        except Exception as exc:
            self._finish("error", "Giriş başlatılamadı.", error=redact(exc))

    # ---------- üretim: doğrudan OAuth ----------

    def _run_oauth(self) -> None:
        flow: GoogleOAuthFlow | None = None
        try:
            before_email, _before_profile = self._capture_before()
            self._set(status="starting", message="Tarayıcı hazırlanıyor…")
            flow = self.oauth_factory()
            auth_url = flow.start()
            self._set(
                status="waiting",
                message="Tarayıcıda eklemek istediğin Google hesabını seç ve girişi tamamla.",
                auth_url=auth_url,
            )
            result = flow.wait()
            capture = self.accounts.add_token(result.email, result.raw_secret, label=None)
            same = bool(before_email and result.email.strip().lower() == before_email.strip().lower())
            self._finish(
                "success" if not same else "success_same",
                (f"Hesap güncellendi: {result.email}" if same
                 else f"Hesap eklendi: {result.email}. Canlı hesap değişmedi; listeden geçebilirsin."),
                captured_profile=capture.profile.name,
                captured_email=capture.profile.email,
            )
        except Exception as exc:
            self._finish("error", "Giriş tamamlanamadı.", error=redact(exc))
        finally:
            if flow is not None:
                flow.stop()

    # ---------- test/legacy: çalışan uygulama LS'i ----------

    def _run_app_based(self) -> None:
        if not self.app.running():
            self._set(status="starting", message="Antigravity başlatılıyor…")
            self.app.start()
        info = self.wait_ls_fn(35.0)
        if not info:
            self._finish("error", "Antigravity language_server hazır olmadı.", error="language_server bulunamadı.")
            return
        before_email, _before_profile = self._capture_before()
        self._set(status="waiting", message="Tarayıcıda Google hesabını seç ve girişi tamamla.")
        rpc_thread = threading.Thread(target=self._call_login, name="aio-login-rpc", daemon=True)
        rpc_thread.start()
        self._watch(before_email)

    def _call_login(self) -> None:
        try:
            data = self.login_fn(self.timeout + 30.0)  # type: ignore[misc]
            with self._lock:
                self._rpc_data = data if isinstance(data, dict) else {}
        except Exception as exc:
            with self._lock:
                self._rpc_error = redact(exc)
        finally:
            with self._lock:
                self._rpc_done = True

    # ---------- app-based izleme (test) ----------

    def _live_email(self) -> str | None:
        try:
            with KEYRING_LOCK:
                with keyring_file_lock(blocking=False) as acquired:
                    if not acquired:
                        return None
                    live = self.accounts.keyring.read_live()
                    return live.email if live and live.email else None
        except (KeyringError, TokenFormatError):
            return None

    def _watch(self, before_email: str | None) -> None:
        deadline = time.monotonic() + self.timeout
        before_profile = self.state().get("before_profile")
        same_auth_since: float | None = None
        while time.monotonic() < deadline:
            live_email = self._live_email()
            if live_email and (not before_email or live_email.strip().lower() != before_email.strip().lower()):
                try:
                    result = self.accounts.capture_live_auto()
                except (AccountError, NoLiveAccountError, KeyringError) as exc:
                    self._finish("error", "Yeni hesap yakalanamadı.", error=redact(exc))
                    return
                self._finish(
                    "success",
                    (f"Hesap güncellendi: {result.profile.email}" if result.already
                     else f"Hesap eklendi: {result.profile.email}"),
                    captured_profile=result.profile.name,
                    captured_email=result.profile.email,
                )
                return

            with self._lock:
                done = self._rpc_done
                error = self._rpc_error
                data = self._rpc_data
            if done and error:
                self._finish("error", "Antigravity girişi başlatılamadı.", error=error)
                return
            if done and data:
                auth = data.get("authResult") if isinstance(data, dict) else None
                if isinstance(auth, dict) and auth.get("hasValidAuth"):
                    current = self._live_email()
                    if current and before_email and current.strip().lower() == before_email.strip().lower():
                        if same_auth_since is None:
                            same_auth_since = time.monotonic()
                        elif time.monotonic() - same_auth_since >= 2.5:
                            self._finish(
                                "success_same",
                                "Aynı hesap seçildi. Yeni hesap için tekrar deneyip 'Başka hesap kullan' seçin.",
                                captured_profile=before_profile,
                                captured_email=current,
                            )
                            return
                        time.sleep(self.poll_interval)
                        continue
                    if current:
                        try:
                            result = self.accounts.capture_live_auto()
                        except (AccountError, NoLiveAccountError, KeyringError) as exc:
                            self._finish("error", "Hesap yakalanamadı.", error=redact(exc))
                            return
                        self._finish(
                            "success",
                            (f"Hesap güncellendi: {result.profile.email}" if result.already
                             else f"Hesap eklendi: {result.profile.email}"),
                            captured_profile=result.profile.name,
                            captured_email=result.profile.email,
                        )
                        return
            time.sleep(self.poll_interval)
        self._finish("timeout", "Giriş zaman aşımına uğradı. Tarayıcı penceresini kapatıp tekrar deneyin.")
