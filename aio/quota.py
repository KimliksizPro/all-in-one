"""Kota orkestrasyonu: profil başına önbellek, canlı RPC ve arka plan işi."""
from __future__ import annotations

import functools
import random
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from . import antigravity, storage
from .accounts import Accounts
from .keyring import KEYRING_LOCK, KeyringError, SecretService, TokenFormatError, decode_token
from .locks import keyring_file_lock
from .probe import LanguageServerProbe, ProbeError, UserQuota, parse_cli_usage, parse_user_status
from .util import now_iso, parse_iso, redact, utc_now

CACHE_TTL_SECONDS = 300
CACHE_TTL_ERROR_SECONDS = 60
CACHE_VERSION = 2

# Sunucuya nazik davran: aynı hesap için art arda istek atma, hatada geri çekil.
MIN_FETCH_INTERVAL_SECONDS = 20          # manuel "yenile" bile bu süre içinde yeniden sormaz
BACKOFF_BASE_SECONDS = 120               # hata sonrası otomatik yeniden deneme: 2, 4, 8 ... dk
BACKOFF_MAX_SECONDS = 1800
RATE_LIMIT_COOLDOWN_SECONDS = 900        # 429 / RESOURCE_EXHAUSTED benzeri yanıtlar
NEEDS_LOGIN_COOLDOWN_SECONDS = 3600      # geçersiz oturumu otomatik zorlama
ACCOUNT_PAUSE_SECONDS = (3.0, 8.0)       # toplu taramada hesaplar arası rastgele bekleme
_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "resource_exhausted", "too many", "quota exceeded")


def _jitter(seconds: float, spread: float = 0.2) -> float:
    return seconds * random.uniform(1.0 - spread, 1.0 + spread)


def _copy_quota(quota: UserQuota) -> UserQuota:
    return replace(quota, groups=[replace(group, models=list(group.models)) for group in quota.groups])


def _cache_signature(name: str) -> tuple | None:
    path = storage.quota_cache_path(name)
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)



def _keyring_serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with KEYRING_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _external_serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with keyring_file_lock(blocking=True):
            return fn(*args, **kwargs)
    return wrapper


class QuotaError(RuntimeError):
    def __init__(self, message: str, *, needs_stop: bool = False):
        super().__init__(message)
        self.needs_stop = needs_stop


@dataclass
class RefreshJob:
    running: bool = False
    total: int = 0
    done: int = 0
    current: str | None = None
    errors: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "total": self.total,
            "done": self.done,
            "current": self.current,
            "errors": list(self.errors),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


class QuotaService:
    def __init__(
        self,
        accounts: Accounts,
        keyring: SecretService | None = None,
        probe: LanguageServerProbe | None = None,
        app_module=antigravity,
    ):
        self.accounts = accounts
        self.keyring = keyring or accounts.keyring
        self.app = app_module
        self._probe_factory = probe or LanguageServerProbe()
        self._lock = threading.RLock()
        self._cache: dict[str, UserQuota] = {}
        self._cache_signatures: dict[str, tuple | None] = {}
        self._job = RefreshJob()
        self._job_thread: threading.Thread | None = None
        # Otomatik (force=False) denemeler için hata geri çekilmesi; bellekte tutulur.
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self.on_result: Callable[[str, UserQuota], None] | None = None
        accounts.validator = self.validate_live

    # ---------- önbellek ----------

    def _load_cache_file(self, name: str) -> UserQuota | None:
        raw = storage.load_quota_cache(name)
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return None
        q = raw.get("quota")
        if not isinstance(q, dict):
            return None
        try:
            return UserQuota.from_dict(q)
        except (TypeError, ValueError, OverflowError):
            return None

    def get_cached(self, name: str) -> UserQuota | None:
        with self._lock:
            signature = _cache_signature(name)
            if name not in self._cache_signatures or self._cache_signatures[name] != signature:
                self._cache.pop(name, None)
                self._cache_signatures.pop(name, None)
                if signature is not None:
                    quota = self._load_cache_file(name)
                    # A concurrent writer/deletion must be checked again next time.
                    if _cache_signature(name) == signature:
                        self._cache_signatures[name] = signature
                        if quota is not None:
                            self._cache[name] = quota
                    else:
                        return None
            quota = self._cache.get(name)
            if quota is None:
                return None
            quota = _copy_quota(quota)
            fetched = parse_iso(quota.fetched_at)
            if fetched:
                age = (utc_now() - fetched).total_seconds()
                ttl = CACHE_TTL_ERROR_SECONDS if not quota.ok else CACHE_TTL_SECONDS
                quota.stale = quota.stale or age > ttl
            return quota

    def _save_cache(self, name: str, quota: UserQuota) -> None:
        with self._lock:
            retained = _copy_quota(quota)
            self._cache[name] = retained
            # A successful atomic write changes file identity. Reload it once
            # before trusting a signature, so concurrent external writers win.
            before = _cache_signature(name)
            self._cache_signatures[name] = before
            storage.save_quota_cache(
                name,
                {
                    "version": CACHE_VERSION,
                    "email": retained.email,
                    "saved_at": now_iso(),
                    "quota": retained.to_dict(),
                },
            )
            if _cache_signature(name) != before:
                self._cache_signatures.pop(name, None)

    def _previous(self, name: str) -> UserQuota | None:
        return self.get_cached(name)

    def drop(self, name: str) -> None:
        with self._lock:
            self._cache.pop(name, None)
            self._cache_signatures.pop(name, None)

    def cached_map(self) -> dict[str, UserQuota]:
        state = self.accounts.state()
        out: dict[str, UserQuota] = {}
        for name in state.profiles:
            q = self.get_cached(name)
            if q is not None:
                out[name] = q
        return out

    # ---------- canlı RPC ----------

    def _uses_cli(self) -> bool:
        # Ayrı language_server yok (yalnızca `agy` CLI kurulu): /usage komutunu kullan.
        return hasattr(self.app, "fetch_usage_cli") and not self.app.installed_ls_binary()

    def _probe_cli(self) -> UserQuota:
        try:
            live = self.keyring.read_live()
        except (KeyringError, TokenFormatError) as exc:
            raise ProbeError(f"Canlı keyring okunamadı: {redact(exc)}") from exc
        try:
            data = self.app.fetch_usage_cli()
        except antigravity.AppError as exc:
            raise ProbeError(str(exc)) from exc
        return parse_cli_usage(data, email=live.email if live else None)

    def _probe_current(self, *, source: str = "probe") -> UserQuota:
        if self._uses_cli():
            quota = self._probe_cli()
        elif self.app.running():
            data = antigravity.fetch_user_status_from_running()
            if data:
                quota = parse_user_status(data, source="running")
            elif getattr(self.app, "running_ls_info", lambda: True)() is None:
                # CLI (agy) açık ama HTTP language_server'ı dışarı açmıyor: keyring'e
                # dokunmadan izole bir probe ile oku.
                quota = self._probe_factory.probe()
            else:
                raise ProbeError("Antigravity açık ancak language_server kota yanıtı vermedi. Birkaç saniye sonra tekrar deneyin.")
        else:
            quota = self._probe_factory.probe()
        if not quota.ok:
            raise ProbeError(quota.error or "Kota okunamadı.")
        return quota

    @_keyring_serialized
    def _sync_live_to_profile(self, quota: UserQuota) -> None:
        try:
            live = self.keyring.read_live()
        except (KeyringError, TokenFormatError):
            return
        if not live:
            return
        if quota.email and live.email and live.email.strip().lower() != quota.email.strip().lower():
            return
        state = self.accounts.state()
        meta = self.accounts._match_by_email(state, live.email)
        if meta:
            try:
                self.keyring.write_vault_raw(meta.name, live.raw)
            except KeyringError:
                pass

    def _probe_with_live_keyring(self) -> UserQuota:
        quota = self._probe_current()
        self._sync_live_to_profile(quota)
        return quota

    # ---------- istek sıklığı / geri çekilme ----------

    def _record_success(self, name: str) -> None:
        with self._lock:
            self._failures.pop(name, None)
            self._cooldown_until.pop(name, None)

    def _record_failure(self, name: str, error: str, *, needs_login: bool = False) -> None:
        text = (error or "").lower()
        with self._lock:
            count = self._failures.get(name, 0) + 1
            self._failures[name] = count
            wait = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (count - 1))
            if any(marker in text for marker in _RATE_LIMIT_MARKERS):
                wait = max(wait, RATE_LIMIT_COOLDOWN_SECONDS)
            if needs_login:
                wait = max(wait, NEEDS_LOGIN_COOLDOWN_SECONDS)
            self._cooldown_until[name] = time.monotonic() + _jitter(wait)

    def _in_cooldown(self, name: str) -> bool:
        with self._lock:
            return time.monotonic() < self._cooldown_until.get(name, 0.0)

    @staticmethod
    def _fetched_recently(quota: UserQuota | None) -> bool:
        if quota is None or not quota.ok:
            return False
        fetched = parse_iso(quota.fetched_at)
        return bool(fetched and (utc_now() - fetched).total_seconds() < MIN_FETCH_INTERVAL_SECONDS)

    @staticmethod
    def _pause_between_accounts() -> None:
        low, high = ACCOUNT_PAUSE_SECONDS
        if high > 0:
            time.sleep(random.uniform(low, high))

    # ---------- tek profil ----------

    @_keyring_serialized
    @_external_serialized
    def refresh_one(
        self,
        name: str,
        *,
        force: bool = False,
        allow_swap: bool = True,
        raise_errors: bool = False,
    ) -> UserQuota:
        state = self.accounts.state()
        meta = state.profiles.get(name)
        if not meta:
            raise QuotaError(f"'{name}' profili bulunamadı.")
        cached = self.get_cached(name)
        if cached and not force and not cached.stale and cached.ok:
            return cached
        if cached and self._fetched_recently(cached):
            return cached
        if cached and not force and self._in_cooldown(name):
            return cached

        target_raw = None
        try:
            target_raw = self.keyring.read_vault_raw(name)
        except KeyringError as exc:
            quota = self._error_quota(name, meta.email, f"Token kasası okunamadı: {redact(exc)}")
            self._save_cache(name, quota)
            if raise_errors:
                raise QuotaError(quota.error or "Kota okunamadı.")
            return quota
        if not target_raw:
            quota = self._error_quota(name, meta.email, "Kayıtlı token bulunamadı; yeniden yakalayın.")
            self._save_cache(name, quota)
            if raise_errors:
                raise QuotaError(quota.error or "Kota okunamadı.")
            return quota
        try:
            decode_token(target_raw)
        except TokenFormatError as exc:
            quota = self._error_quota(name, meta.email, f"Token okunamadı: {exc}")
            self._save_cache(name, quota)
            if raise_errors:
                raise QuotaError(quota.error or "Kota okunamadı.")
            return quota

        live = None
        try:
            live = self.keyring.read_live()
        except (KeyringError, TokenFormatError):
            live = None
        live_matches = bool(
            live and live.email and meta.email and live.email.strip().lower() == meta.email.strip().lower()
        )

        quota: UserQuota | None = None
        try:
            if live_matches:
                quota = self._probe_with_live_keyring()
            else:
                if self.app.running():
                    raise QuotaError(
                        "Antigravity açıkken yalnızca aktif hesabın kotası okunabilir. "
                        "Diğer hesaplar için Antigravity'yi kapatın.",
                        needs_stop=True,
                    )
                if not allow_swap:
                    raise QuotaError(
                        "Bu hesap şu an canlı keyring'de değil; kota okumak için keyring geçişi gerekiyor."
                    )
                quota = self._probe_with_swap(name, target_raw)
            if quota.email and meta.email and quota.email.strip().lower() != meta.email.strip().lower():
                raise QuotaError(
                    f"Kota yanıtındaki hesap beklenenden farklı: {quota.email} != {meta.email}."
                )
            quota.stale = False
            quota.source = "live"
            self._record_success(name)
            self._save_cache(name, quota)
            self.accounts.update_quota_meta(name, quota)
            if self.on_result:
                try:
                    self.on_result(name, quota)
                except Exception:
                    pass
            return quota
        except QuotaError as exc:
            quota = self._error_quota(name, meta.email, str(exc), needs_login=False)
            self._save_cache(name, quota)
            if self.on_result:
                try:
                    self.on_result(name, quota)
                except Exception:
                    pass
            if raise_errors or exc.needs_stop:
                raise
            return quota
        except (ProbeError, KeyringError, OSError, ValueError) as exc:
            text = redact(exc)
            needs_login = any(
                token in text.lower()
                for token in ("login", "oturum", "unauth", "401", "invalid_grant", "revoked")
            )
            quota = self._error_quota(name, meta.email, text, needs_login=needs_login)
            self._record_failure(name, text, needs_login=needs_login)
            self._save_cache(name, quota)
            self.accounts.mark_needs_login(name, needs_login)
            if self.on_result:
                try:
                    self.on_result(name, quota)
                except Exception:
                    pass
            if raise_errors:
                raise QuotaError(text) from exc
            return quota

    def _probe_with_swap(self, name: str, target_raw: str) -> UserQuota:
        try:
            before = self.keyring.read_live_raw()
        except KeyringError as exc:
            raise QuotaError(f"Canlı keyring okunamadı: {redact(exc)}") from exc
        if self.app.running():
            raise QuotaError(
                "Antigravity tarama sırasında açıldı; keyring değiştirilmedi. Toplu taramayı tekrar başlatın.",
                needs_stop=True,
            )
        try:
            self.keyring.write_live_raw(target_raw)
            quota = self._probe_current(source="probe")
            try:
                after = self.keyring.read_live()
            except (KeyringError, TokenFormatError):
                after = None
            if after and after.email and quota.email and after.email.strip().lower() == quota.email.strip().lower():
                self.keyring.write_vault_raw(name, after.raw)
            return quota
        finally:
            try:
                if before:
                    self.keyring.write_live_raw(before)
                else:
                    self.keyring.clear("gemini", "antigravity")
            except KeyringError:
                pass

    def _error_quota(self, name: str, email: str | None, error: str, *, needs_login: bool = False) -> UserQuota:
        prev = self._previous(name)
        if prev is not None and prev.groups:
            prev.ok = False
            prev.error = error
            prev.needs_login = needs_login or prev.needs_login
            prev.stale = True
            prev.source = "cache"
            prev.fetched_at = prev.fetched_at or now_iso()
            return prev
        return UserQuota(
            ok=False,
            email=email,
            error=error,
            needs_login=needs_login,
            fetched_at=now_iso(),
            source="error",
        )

    # ---------- canlı doğrulama (switch sonrası) ----------

    @_keyring_serialized
    @_external_serialized
    def validate_live(self, profile_name: str) -> UserQuota:
        """Switch sonrası canlı keyring'deki hesabı doğrula ve önbelleğe yaz."""
        quota = self._probe_with_live_keyring()
        self._save_cache(profile_name, quota)
        self.accounts.update_quota_meta(profile_name, quota)
        return quota

    # ---------- toplu ----------

    def refresh_active(self, *, force: bool = False) -> UserQuota | None:
        # bootstrap canlı token'ı ve aktif profili kendi kilidi altında senkronize eder.
        with KEYRING_LOCK:
            state = self.accounts.bootstrap()
            target = state.active if state.active in state.profiles else None
        if target:
            return self.refresh_one(target, force=force, allow_swap=True)
        return None

    def refresh_all(self, *, force: bool = True) -> list[UserQuota]:
        state = self.accounts.state()
        names = [p.name for p in state.ordered_profiles()]
        if not names:
            return []
        if self.app.running() and len(names) > 1:
            raise QuotaError(
                "Tüm hesapların kotasını okumak için Antigravity kapatılmalı. "
                "Uygulama açıkken yalnızca aktif hesap okunabilir.",
                needs_stop=True,
            )
        results: list[UserQuota] = []
        for index, name in enumerate(names):
            if index:
                self._pause_between_accounts()
            try:
                results.append(self.refresh_one(name, force=force, allow_swap=True))
            except QuotaError as exc:
                results.append(self._error_quota(name, state.profiles[name].email, redact(exc)))
                if exc.needs_stop:
                    break
            except Exception as exc:
                results.append(self._error_quota(name, state.profiles[name].email, redact(exc)))
        return results

    def start_refresh_all(self) -> dict:
        with self._lock:
            if self._job.running:
                return self._job.to_dict()
            if not self.accounts.state().profiles:
                raise QuotaError("Kayıtlı hesap yok.")
            self._job = RefreshJob(
                running=True,
                total=len(self.accounts.state().profiles),
                done=0,
                started_at=now_iso(),
            )
            thread = threading.Thread(target=self._run_job, name="aio-quota-refresh", daemon=True)
            self._job_thread = thread
            thread.start()
            return self._job.to_dict()

    def _run_job(self) -> None:
        try:
            state = self.accounts.state()
            names = [p.name for p in state.ordered_profiles()]
            with self._lock:
                self._job.total = len(names)
            if self.app.running() and len(names) > 1:
                raise QuotaError(
                    "Antigravity açık. Tüm kotaları okumak için önce uygulamayı kapatın.",
                    needs_stop=True,
                )
            for i, name in enumerate(names, start=1):
                if i > 1:
                    self._pause_between_accounts()
                with self._lock:
                    self._job.current = name
                try:
                    quota = self.refresh_one(name, force=True, allow_swap=True)
                    if not quota.ok:
                        with self._lock:
                            self._job.errors.append(f"{name}: {redact(quota.error or 'Kota okunamadı.')}")
                except QuotaError as exc:
                    with self._lock:
                        self._job.errors.append(f"{name}: {redact(exc)}")
                    if exc.needs_stop:
                        break
                except Exception as exc:
                    with self._lock:
                        self._job.errors.append(f"{name}: {redact(exc)}")
                finally:
                    with self._lock:
                        self._job.done = i
            with self._lock:
                self._job.running = False
                self._job.current = None
                self._job.finished_at = now_iso()
        except Exception as exc:
            with self._lock:
                self._job.running = False
                self._job.current = None
                self._job.error = redact(exc)
                self._job.finished_at = now_iso()

    def job_state(self) -> dict:
        with self._lock:
            return self._job.to_dict()
