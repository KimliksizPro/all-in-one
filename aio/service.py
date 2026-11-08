"""CLI, web sunucusu ve testlerin paylaştığı servis katmanı."""
from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import asdict
from typing import Any

from . import __version__, antigravity, storage
from .accounts import Accounts
from .alerts import AlertManager, AlertNotifier
from .keyring import KEYRING_LOCK, KeyringError, SecretService, TokenFormatError
from .locks import keyring_file_lock
from .login import LoginManager
from .probe import LanguageServerProbe, UserQuota
from .quota import QuotaError, QuotaService
from .util import human_percent, now_iso, redact


class Service:
    def __init__(
        self,
        *,
        keyring: SecretService | None = None,
        accounts: Accounts | None = None,
        quota: QuotaService | None = None,
        alerts: AlertManager | None = None,
        login: LoginManager | None = None,
        probe: LanguageServerProbe | None = None,
    ):
        storage.ensure_dirs()
        self.keyring = keyring or SecretService()
        self.accounts = accounts or Accounts(self.keyring)
        self.quota = quota or QuotaService(self.accounts, self.keyring, probe=probe)
        self.accounts.validator = self.quota.validate_live
        self.quota.on_result = self._on_quota_result
        self.alerts = alerts or AlertManager(
            notifier=AlertNotifier(),
            notifier_enabled=lambda: self.accounts.state().settings.desktop_notifications,
        )
        self.login = login or LoginManager(self.accounts)
        self._auth_lock = threading.Lock()
        self._auth_data: dict | None = None
        self._auth_fetched = 0.0
        self._auth_fetching = False
        self._live_cache: dict[str, Any] = {
            "available": False, "email": None, "account_name": None, "expiry": None,
            "expired": None, "known_profile": None, "matches_active": False,
            "error": None, "has_refresh_token": False, "auth_method": None, "cached": False,
        }

        self._live_lock = threading.Lock()
        self._live_observed = 0.0
        self._live_fetching = False
        self._live_attempted = 0.0
        self._bootstrapped = False
        self._auth_context: tuple | None = None

    def _on_quota_result(self, name: str, quota: UserQuota) -> None:
        self.evaluate_alerts(notify=True)

    # ---------- bootstrap ----------

    def bootstrap(self) -> storage.State:
        state = self.accounts.bootstrap()
        self.live_status(fresh=True, state=state)
        self._bootstrapped = True
        return state

    # ---------- uyarılar ----------

    def account_quota_pairs(self) -> list[tuple[str, str, UserQuota]]:
        state = self.accounts.state()
        pairs: list[tuple[str, str, UserQuota]] = []
        for meta in state.ordered_profiles():
            q = self.quota.get_cached(meta.name)
            if q is not None:
                pairs.append((meta.name, meta.label, q))
        return pairs

    def evaluate_alerts(self, *, notify: bool = True) -> tuple[list, list]:
        state = self.accounts.state()
        return self.alerts.evaluate(self.account_quota_pairs(), state.settings, notify=notify)

    # ---------- durum ----------

    def _invalidate_auth(self) -> None:
        with self._auth_lock:
            self._auth_context = None
            self._auth_data = None
            self._auth_fetched = 0.0

    def _refresh_auth_status(self, email: str | None, app: antigravity.AppInfo) -> dict | None:
        """Bir hesabın auth sonucunu başka hesap/süreç üzerinde gösterme."""
        context = ((email or "").casefold(), min(app.pids) if app.pids else None)
        now = time.monotonic()
        with self._auth_lock:
            if self._auth_context != context:
                self._auth_context = context
                self._auth_data = None
                self._auth_fetched = 0.0
            retained = self._auth_data
            if self._auth_fetching or now - self._auth_fetched < 15.0:
                return retained
            self._auth_fetching = True

        def _fetch() -> None:
            try:
                data = antigravity.fetch_auth_status(timeout=2.0)
            except Exception:
                data = None
            finally:
                with self._auth_lock:
                    if self._auth_context == context:
                        self._auth_data = data
                        self._auth_fetched = time.monotonic()
                    self._auth_fetching = False

        threading.Thread(target=_fetch, name="aio-auth-status", daemon=True).start()
        return retained

    @staticmethod
    def _summarize_auth(data: dict | None) -> dict | None:
        if not isinstance(data, dict) or not isinstance(data.get("authResult"), dict):
            return None
        ar = data["authResult"]
        ineligible = ar.get("ineligible")
        ineligible = ineligible if isinstance(ineligible, dict) else None
        valid = ar.get("hasValidAuth") is True
        verification = (ineligible or {}).get("verificationUrl")
        if verification:
            from urllib.parse import urlsplit

            try:
                parsed = urlsplit(verification)
                if parsed.scheme != "https" or parsed.hostname != "accounts.google.com" or parsed.username:
                    verification = None
            except (ValueError, TypeError):
                verification = None
        return {
            "eligible": False if ineligible is not None else True if valid else None,
            "ineligible": ineligible is not None,
            "has_valid_auth": valid,
            "message": ar.get("uiMessage") or (ineligible or {}).get("uiMessage"),
            "verification_url": verification,
        }

    def _remember_live(self, status: dict) -> dict:
        status["cached"] = False
        with self._live_lock:
            self._live_cache = dict(status)
            self._live_observed = time.monotonic()
        return status

    def live_status(self, *, fresh: bool = True, state: storage.State | None = None) -> dict:
        if fresh:
            with KEYRING_LOCK, keyring_file_lock(blocking=True):
                return self._remember_live(self._live_status_unlocked(state))
        self._schedule_live_refresh()
        with self._live_lock:
            status = dict(self._live_cache)
            status["cached"] = self._live_fetching or time.monotonic() - self._live_observed >= 2.0
        return status

    def _schedule_live_refresh(self) -> None:
        with self._live_lock:
            now = time.monotonic()
            if self._live_fetching or now - max(self._live_observed, self._live_attempted) < 2.0:
                return
            self._live_attempted = now
            self._live_fetching = True

        def _observe() -> None:
            acquired = KEYRING_LOCK.acquire(blocking=False)
            try:
                if not acquired:
                    return
                with keyring_file_lock(blocking=False) as external:
                    if not external:
                        return
                    state = self.accounts.state()
                    status = self._live_status_unlocked(state)
                    # Seçim token'ı temsil eder; Google oturumunun elle değişimini de yakala.
                    match = status.get("known_profile")
                    if match and state.active != match:
                        state.active = match
                        self.accounts.save(state)
                        status["matches_active"] = True
                    self._remember_live(status)
            except Exception as exc:
                with self._live_lock:
                    self._live_cache = {**self._live_cache, "cached": True, "error": redact(exc)}
                    self._live_observed = time.monotonic()
            finally:
                if acquired:
                    KEYRING_LOCK.release()
                with self._live_lock:
                    self._live_fetching = False

        threading.Thread(target=_observe, name="aio-live-status", daemon=True).start()

    def _live_status_unlocked(self, state: storage.State | None = None) -> dict:
        result: dict[str, Any] = {
            "available": False,
            "email": None,
            "account_name": None,
            "expiry": None,
            "expired": None,
            "known_profile": None,
            "matches_active": False,
            "error": None,
        }
        try:
            live = self.keyring.read_live()
        except KeyringError as exc:
            result["error"] = redact(exc)
            return result
        except TokenFormatError as exc:
            result["error"] = f"Canlı oturum okunamadı: {exc}"
            return result
        if not live:
            return result

        result.update(
            {
                "available": True,
                "email": live.email,
                "account_name": live.data.get("name") if isinstance(live.data.get("name"), str) else None,
                "expiry": live.expiry.isoformat(timespec="seconds") if live.expiry else None,
                "expired": live.is_expired(),
                "has_refresh_token": bool(live.refresh_token),
                "auth_method": live.auth_method,
            }
        )
        state = state or self.accounts.state()
        match = self.accounts._match_by_email(state, live.email)
        if match:
            result["known_profile"] = match.name
            result["matches_active"] = state.active == match.name
        return result

    def snapshot(self) -> dict:
        if not self._bootstrapped:
            self.bootstrap()
        state = self.accounts.state()
        live = self.live_status(fresh=False)
        match = self.accounts._match_by_email(state, live.get("email"))
        live["known_profile"] = match.name if match else None
        live["matches_active"] = bool(match and state.active == match.name)
        if live.get("expiry"):
            from .util import parse_iso, utc_now

            expiry = parse_iso(live["expiry"])
            live["expired"] = expiry <= utc_now() if expiry else None
        keyring_busy = bool(live.get("cached"))
        app = antigravity.app_info()
        if app.running and live.get("email"):
            live["auth"] = self._summarize_auth(self._refresh_auth_status(live["email"], app))
        else:
            self._invalidate_auth()
            live["auth"] = None
        settings = state.settings
        accounts: list[dict] = []
        quotas_for_alerts: list[tuple[str, str, UserQuota]] = []
        for meta in state.ordered_profiles():
            quota = self.quota.get_cached(meta.name)
            quota_dict = quota.to_dict() if quota else None
            if quota is not None:
                quotas_for_alerts.append((meta.name, meta.label, quota))
            accounts.append(
                {
                    "name": meta.name,
                    "label": meta.label,
                    "email": meta.email,
                    "account_name": meta.account_name,
                    "plan_name": meta.plan_name,
                    "primary": meta.name == state.primary,
                    "active": meta.name == state.active,
                    "created_at": meta.created_at,
                    "last_used": meta.last_used,
                    "needs_login": meta.needs_login,
                    "quota": quota_dict,
                    "quota_stale": bool(quota.stale) if quota else True,
                }
            )
        alerts = self.alerts.current_alerts(quotas_for_alerts, settings)
        exhausted = sum(
            1
            for meta in accounts
            if (meta.get("quota") or {}).get("groups")
            and any(g.get("exhausted") for g in (meta["quota"].get("groups") or []))
        )
        warnings: list[str] = []
        if live.get("email") and not live.get("known_profile"):
            warnings.append(
                f"Canlı Antigravity hesabı ({live['email']}) henüz kayıtlı değil. 'Canlı hesabı yakala' ile ekleyin."
            )
        if live.get("email") and live.get("known_profile") and not live.get("matches_active"):
            warnings.append("Canlı keyring ile aktif profil farklı; durum yakında senkronize edilecek.")
        if app.running and len(accounts) > 1:
            warnings.append("Antigravity açıkken yalnızca aktif hesabın kotası canlı okunur.")
        return {
            "version": __version__,
            "generated_at": now_iso(),
            "app": app.to_dict(),
            "live": live,
            "active": state.active,
            "primary": state.primary,
            "settings": asdict(settings),
            "accounts": accounts,
            "alerts": [a.to_dict() for a in alerts],
            "counts": {
                "accounts": len(accounts),
                "alerts": len(alerts),
                "exhausted": exhausted,
            },
            "refresh": self.quota.job_state(),
            "login": self.login.state(),
            "keyring_busy": keyring_busy,
            "warnings": warnings,
            "storage": {
                "root": str(storage.root_dir()),
                "state": str(storage.state_path()),
                "keyring": self.keyring.available(),
            },
        }

    # ---------- işlemler ----------

    def capture(self, name: str, label: str | None = None, *, overwrite: bool = False, primary: bool = False):
        result = self.accounts.capture_current(name, label=label, overwrite=overwrite, make_primary=primary)
        self.live_status(fresh=True)
        self._invalidate_auth()
        self.evaluate_alerts(notify=False)
        return result

    def switch(self, name: str, *, stop: bool = False, launch: bool | None = None, validate: bool = True):
        result = self.accounts.switch(name, stop=stop, launch=launch, validate=validate)
        self.live_status(fresh=True)
        self._invalidate_auth()
        self.evaluate_alerts(notify=True)
        return result

    def rename(self, old: str, new: str):
        result = self.accounts.rename(old, new)
        self.quota.drop(old)
        storage.clear_quota_cache(old)
        self.evaluate_alerts(notify=False)
        return result

    def delete(self, name: str):
        self.accounts.delete(name)
        self.quota.drop(name)
        storage.clear_quota_cache(name)
        self.evaluate_alerts(notify=False)

    def set_primary(self, name: str):
        result = self.accounts.set_primary(name)
        self.evaluate_alerts(notify=False)
        return result

    def refresh_active(self, *, force: bool = True):
        if self.login.is_running():
            return None
        quota = self.quota.refresh_active(force=force)
        with self._live_lock:
            self._live_observed = 0.0
        self.evaluate_alerts(notify=True)
        return quota

    def start_login(self) -> dict:
        if self.quota.job_state().get("running"):
            raise QuotaError("Kota taraması sürüyor; bitmesini bekleyip tekrar deneyin.")
        return self.login.start()

    def reset_login(self) -> dict:
        return self.login.reset()

    def start_refresh_all(self) -> dict:
        if self.login.is_running():
            raise QuotaError("Antigravity hesap girişi sürüyor; önce girişin bitmesini bekleyin.")
        state = self.accounts.state()
        if antigravity.running() and len(state.profiles) > 1:
            raise QuotaError(
                "Tüm kotaları okumak için Antigravity kapatılmalı. "
                "Uygulama açıkken yalnızca aktif hesap okunabilir.",
                needs_stop=True,
            )
        job = self.quota.start_refresh_all()
        return job

    def refresh_one(self, name: str, *, force: bool = True):
        if self.login.is_running():
            raise QuotaError("Antigravity hesap girişi sürüyor; önce girişin bitmesini bekleyin.")
        quota = self.quota.refresh_one(name, force=force, allow_swap=True)
        with self._live_lock:
            self._live_observed = 0.0
        self.evaluate_alerts(notify=True)
        return quota

    def update_settings(self, updates: dict) -> storage.Settings:
        state = self.accounts.state()
        data = asdict(state.settings)
        for key in data:
            if key in updates:
                data[key] = updates[key]
        state.settings = storage.Settings.from_dict(data)
        self.accounts.save(state)
        return state.settings

    # ---------- doctor ----------

    def doctor(self, *, probe: bool = False) -> dict:
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str, hint: str | None = None) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail, "hint": hint})

        add("Python", True, f"{os.sys.version.split()[0]} ({os.sys.executable})")
        add("Veri dizini", storage.root_dir().exists(), str(storage.root_dir()))
        add("state dosyası", storage.state_path().is_file(), str(storage.state_path()))
        add("secret-tool", self.keyring.available(), str(shutil.which(self.keyring.command) or "bulunamadı"),
            None if self.keyring.available() else "KDE Wallet / GNOME Keyring (libsecret) kurun.")
        live = self.live_status()
        add("Canlı keyring", bool(live.get("available")), live.get("email") or (live.get("error") or "oturum yok"),
            None if live.get("available") else "Antigravity'de giriş yapın.")
        add("Antigravity", antigravity.installed(), str(antigravity.installed_binary() or "bulunamadı"))
        add("language_server", bool(antigravity.installed_ls_binary()),
            str(antigravity.installed_ls_binary() or antigravity.LS_BINARY))
        add("Antigravity durumu", True,
            "açık" if antigravity.running() else "kapalı",
            "Toplu kota yenilemesi için kapatın." if antigravity.running() else None)
        add("notify-send", shutil.which("notify-send") is not None,
            str(shutil.which("notify-send") or "bulunamadı"),
            None if shutil.which("notify-send") else "Masaüstü bildirimi için libnotify kurun.")
        state = self.accounts.state()
        add("Profil sayısı", bool(state.profiles), str(len(state.profiles)))
        add("Ana hesap", bool(state.primary), state.primary or "yok")
        add("Aktif hesap", bool(state.active), state.active or "yok")

        quota_info = None
        if probe:
            try:
                quota = self.quota.refresh_active(force=True)
                if quota:
                    quota_info = quota.to_dict()
                    add(
                        "Kota RPC",
                        quota.ok,
                        f"{quota.email or '?'} · {human_percent(quota.worst_remaining * 100 if quota.worst_remaining is not None else None)} · {len(quota.groups)} model",
                        quota.error,
                    )
                else:
                    add("Kota RPC", False, "Aktif profil yok.")
            except Exception as exc:
                add("Kota RPC", False, redact(exc))
        return {"checks": checks, "quota": quota_info, "snapshot": self.snapshot()}
