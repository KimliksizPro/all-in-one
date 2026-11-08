"""Hesap profilleri: canlı keyring'den yakalama, geçiş ve metadata."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import antigravity, storage
from .keyring import KEYRING_LOCK, KeyringError, SecretService, TokenBundle, TokenFormatError, decode_token
from .locks import keyring_file_lock
from .util import now_iso, safe_name


class AccountError(RuntimeError):
    pass


class AccountConflict(AccountError):
    def __init__(self, message: str, existing: str | None = None):
        super().__init__(message)
        self.existing = existing


class NoLiveAccountError(AccountError):
    pass


class AppRunningError(AccountError):
    pass


@dataclass
class CaptureResult:
    profile: storage.ProfileMeta
    already: bool
    message: str

    def to_dict(self) -> dict:
        return {
            "name": self.profile.name,
            "label": self.profile.label,
            "email": self.profile.email,
            "primary": self.profile.primary,
            "already": self.already,
            "message": self.message,
        }


@dataclass
class SwitchResult:
    profile: str
    email: str
    stopped: bool
    launched: bool
    warning: str | None = None

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "email": self.email,
            "stopped": self.stopped,
            "launched": self.launched,
            "warning": self.warning,
        }


class Accounts:
    def __init__(
        self,
        keyring: SecretService | None = None,
        app_module=antigravity,
        validator: Callable[[str], object] | None = None,
    ):
        self.keyring = keyring or SecretService()
        self.app = app_module
        self.validator = validator
        self._lock = threading.RLock()

    # ---------- temel ----------

    def state(self) -> storage.State:
        return storage.load_state()

    def save(self, state: storage.State) -> None:
        storage.save_state(state)

    def _match_by_email(self, state: storage.State, email: str | None) -> storage.ProfileMeta | None:
        if not email:
            return None
        needle = email.strip().lower()
        for profile in state.profiles.values():
            if profile.email.strip().lower() == needle:
                return profile
        return None

    def live(self, *, resolve_email: bool = False) -> TokenBundle | None:
        with KEYRING_LOCK:
            try:
                return self.keyring.read_live(resolve_email=resolve_email)
            except TokenFormatError as exc:
                raise NoLiveAccountError(f"Canlı Antigravity oturumu okunamadı: {exc}") from exc

    # ---------- bootstrap ----------

    def bootstrap(self) -> storage.State:
        """İlk çalıştırmada canlı hesabı `main` olarak yakala; token rotasyonunu senkronize et."""
        with KEYRING_LOCK, keyring_file_lock(blocking=True), self._lock:
            state = self.state()
            live = None
            try:
                live = self.keyring.read_live(resolve_email=True)
            except (KeyringError, TokenFormatError):
                live = None

            changed = False
            if not state.profiles and live and live.email:
                meta = self._create_meta(state, "main", live, label="Ana hesap", make_primary=True)
                state.profiles[meta.name] = meta
                self.keyring.write_vault_raw(meta.name, live.raw)
                state.active = meta.name
                state.primary = meta.name
                self._write_marker(meta)
                changed = True
            elif state.profiles and live and live.email:
                match = self._match_by_email(state, live.email)
                if match:
                    # IDE kendi token'ını yenilemiş olabilir; kasayı tazele.
                    try:
                        if self.keyring.read_vault_raw(match.name) != live.raw:
                            self.keyring.write_vault_raw(match.name, live.raw)
                    except KeyringError:
                        pass
                    if state.active != match.name:
                        active_meta = state.profiles.get(state.active or "")
                        if not active_meta or active_meta.email.strip().lower() != live.email.strip().lower():
                            state.active = match.name
                            changed = True
                    if state.primary is None:
                        state.primary = match.name
                        changed = True
            if state.profiles and state.primary is None:
                state.primary = state.active or next(iter(state.profiles))
                changed = True
            if changed:
                self.save(state)
            return state

    def _create_meta(
        self,
        state: storage.State,
        name: str,
        live: TokenBundle,
        *,
        label: str | None = None,
        make_primary: bool = False,
    ) -> storage.ProfileMeta:
        email = (live.email or "").strip()
        if not email:
            raise NoLiveAccountError(
                "Canlı Antigravity oturumunda e-posta bulunamadı. Antigravity'de tekrar giriş yapıp deneyin."
            )
        existing = self._match_by_email(state, email)
        if existing and existing.name != name:
            raise AccountConflict(
                f"Bu hesap zaten '{existing.name}' olarak kayıtlı ({email}).",
                existing=existing.name,
            )
        old = state.profiles.get(name)
        same_email = bool(old and old.email.strip().lower() == email.lower())
        if old and old.primary and not make_primary and not same_email:
            raise AccountError(
                f"'{name}' ana hesap. Üzerine yazmak için önce ana hesabı değiştirin veya --primary kullanın."
            )
        default_label = "Ana hesap" if (make_primary or name == "main") else name
        meta = storage.ProfileMeta(
            name=name,
            label=(label or (old.label if old else default_label)).strip() or default_label,
            email=email,
            created_at=old.created_at if old else now_iso(),
            last_used=old.last_used if old else now_iso(),
            primary=bool(make_primary or (old.primary if old else False)),
            account_name=live.data.get("name") if isinstance(live.data.get("name"), str) else old.account_name if old else None,
            needs_login=False,
        )
        return meta

    def _write_marker(self, meta: storage.ProfileMeta) -> None:
        storage.write_profile_marker(
            meta.name,
            {
                "name": meta.name,
                "label": meta.label,
                "email": meta.email,
                "created_at": meta.created_at,
                "primary": meta.primary,
            },
        )

    # ---------- yakalama ----------

    def capture_current(
        self,
        name: str,
        *,
        label: str | None = None,
        overwrite: bool = False,
        make_primary: bool = False,
    ) -> CaptureResult:
        name = safe_name(name, "profil adı")
        with KEYRING_LOCK, keyring_file_lock(blocking=True), self._lock:
            live = self.live(resolve_email=True)
            if not live or not live.email:
                raise NoLiveAccountError(
                    "Antigravity'de aktif bir oturum bulunamadı. Önce Antigravity'ye giriş yapın."
                )
            state = self.state()
            existing = self._match_by_email(state, live.email)
            if existing and existing.name != name:
                raise AccountConflict(
                    f"Bu hesap zaten '{existing.name}' olarak kayıtlı ({live.email}).",
                    existing=existing.name,
                )
            old = state.profiles.get(name)
            if old and not overwrite:
                if existing and existing.name == name:
                    raise AccountConflict(
                        f"'{name}' bu hesap için zaten kayıtlı. Yenilemek için --overwrite kullanın.",
                        existing=name,
                    )
                raise AccountConflict(
                    f"'{name}' profili zaten var. Üzerine yazmak için --overwrite kullanın.",
                    existing=name,
                )
            meta = self._create_meta(state, name, live, label=label, make_primary=make_primary)
            self.keyring.write_vault_raw(name, live.raw)
            state.profiles[name] = meta
            if make_primary:
                state.primary = name
            elif state.primary is None:
                state.primary = name
            state.active = name
            self.save(state)
            self._write_marker(meta)
            try:
                self.app.update_last_login(meta.email)
            except Exception:
                pass
            already = bool(existing and existing.name == name)
            return CaptureResult(
                profile=meta,
                already=already,
                message=(
                    f"'{name}' güncellendi ({meta.email})."
                    if already
                    else f"'{name}' kaydedildi ({meta.email})."
                ),
            )

    def capture_live_auto(self, label: str | None = None) -> CaptureResult:
        """Canlı keyring'deki hesabı kullanıcıdan isim istemeden profile kaydet."""
        with KEYRING_LOCK, keyring_file_lock(blocking=True), self._lock:
            live = self.live(resolve_email=True)
            if not live or not live.email:
                raise NoLiveAccountError("Canlı Antigravity oturumunda e-posta bulunamadı.")
            state = self.state()
            existing = self._match_by_email(state, live.email)
            if existing:
                self.keyring.write_vault_raw(existing.name, live.raw)
                state.active = existing.name
                existing.needs_login = False
                self.save(state)
                self._write_marker(existing)
                try:
                    self.app.update_last_login(existing.email)
                except Exception:
                    pass
                return CaptureResult(
                    profile=existing,
                    already=True,
                    message=f"'{existing.name}' güncellendi ({existing.email}).",
                )
            local = (live.email.split("@", 1)[0] or "hesap").lower()
            base = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in local).strip("-._") or "hesap"
            base = base[:48]
            name = base
            n = 2
            while name in state.profiles:
                name = f"{base}-{n}"
                n += 1
            meta = self._create_meta(
                state,
                name,
                live,
                label=(label or local or live.email),
                make_primary=False,
            )
            self.keyring.write_vault_raw(name, live.raw)
            state.profiles[name] = meta
            state.active = name
            if state.primary is None:
                state.primary = name
            self.save(state)
            self._write_marker(meta)
            try:
                self.app.update_last_login(meta.email)
            except Exception:
                pass
            return CaptureResult(
                profile=meta,
                already=False,
                message=f"'{name}' kaydedildi ({meta.email}).",
            )

    def add_token(self, email: str, raw: str, label: str | None = None) -> CaptureResult:
        """OAuth ile alınan token'ı profile yaz; canlı/aktif hesabı değiştirme."""
        email = (email or "").strip()
        if not email:
            raise NoLiveAccountError("OAuth hesabında e-posta yok.")
        try:
            token = decode_token(raw, resolve_email=True)
        except TokenFormatError as exc:
            raise AccountError(f"OAuth token'ı okunamadı: {exc}") from exc
        if token.email and token.email.strip().lower() != email.lower():
            raise AccountError(f"OAuth e-postası token ile uyuşmuyor: {token.email} != {email}.")
        with KEYRING_LOCK, keyring_file_lock(blocking=True), self._lock:
            state = self.state()
            existing = self._match_by_email(state, email)
            if existing:
                self.keyring.write_vault_raw(existing.name, raw)
                existing.needs_login = False
                if existing.label == existing.email:
                    existing.label = existing.email.split("@", 1)[0] or existing.name
                self.save(state)
                self._write_marker(existing)
                return CaptureResult(
                    profile=existing,
                    already=True,
                    message=f"'{existing.name}' güncellendi ({existing.email}).",
                )
            local = (email.split("@", 1)[0] or "hesap").lower()
            base = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in local).strip("-._") or "hesap"
            base = base[:48]
            name = base
            n = 2
            while name in state.profiles:
                name = f"{base}-{n}"
                n += 1
            meta = self._create_meta(
                state,
                name,
                token,
                label=(label or local or email),
                make_primary=False,
            )
            self.keyring.write_vault_raw(name, raw)
            state.profiles[name] = meta
            if state.primary is None:
                state.primary = name
            if state.active is None:
                state.active = name
            self.save(state)
            self._write_marker(meta)
            return CaptureResult(
                profile=meta,
                already=False,
                message=f"'{name}' eklendi ({meta.email}).",
            )

    # ---------- geçiş ----------

    def _find_live_profile(self, state: storage.State, live: TokenBundle | None) -> storage.ProfileMeta | None:
        return self._match_by_email(state, live.email if live else None)

    def _backup_unknown_live(self, live: TokenBundle | None) -> None:
        if not live or not live.raw:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            self.keyring.write_vault_raw(f"_live-{stamp}", live.raw)
        except KeyringError:
            pass

    def switch(
        self,
        name: str,
        *,
        stop: bool = False,
        launch: bool | None = None,
        validate: bool = True,
        force: bool = False,
    ) -> SwitchResult:
        name = safe_name(name, "profil adı")
        with KEYRING_LOCK, keyring_file_lock(blocking=True), self._lock:
            state = self.state()
            meta = state.profiles.get(name)
            if not meta:
                raise AccountError(f"'{name}' profili bulunamadı.")
            target_raw = self.keyring.read_vault_raw(name)
            if not target_raw:
                raise AccountError(
                    f"'{name}' için kayıtlı token bulunamadı. Antigravity'de bu hesaba giriş yapıp yeniden yakalayın."
                )
            try:
                decode_token(target_raw)
            except TokenFormatError as exc:
                raise AccountError(f"'{name}' token'ı okunamadı: {exc}") from exc

            was_running = self.app.running()
            stopped = False
            if was_running:
                if not stop:
                    raise AppRunningError(
                        "Antigravity açık. Geçiş için önce kapatın veya 'kapat ve geç' seçeneğini kullanın."
                    )
                result = self.app.stop(timeout=12.0, force=force)
                stopped = True
                if result.remaining:
                    raise AppRunningError(
                        f"Antigravity tamamen kapanmadı (PID: {', '.join(map(str, result.remaining))}). "
                        "Uygulamayı elle kapatıp tekrar deneyin."
                    )

            before = None
            try:
                before = self.keyring.read_live()
            except (KeyringError, TokenFormatError):
                before = None
            if before and before.email:
                match = self._find_live_profile(state, before)
                if match:
                    try:
                        self.keyring.write_vault_raw(match.name, before.raw)
                    except KeyringError:
                        pass
                else:
                    self._backup_unknown_live(before)

            launched = False
            warning: str | None = None
            app_storage_before = None
            try:
                app_storage_path = getattr(self.app, "APP_STORAGE", antigravity.APP_STORAGE)
                if app_storage_path.is_file():
                    app_storage_before = app_storage_path.read_bytes()
            except OSError:
                app_storage_before = None
            try:
                self.keyring.write_live_raw(target_raw)
                try:
                    self.app.update_last_login(meta.email)
                except Exception:
                    warning = "app_storage.json güncellenemedi; Antigravity yine de keyring'deki hesabı kullanır."

                if validate and self.validator is not None:
                    try:
                        result = self.validator(name)
                    except Exception as exc:
                        raise AccountError(f"Hesap doğrulanamadı: {exc}") from exc
                    if not getattr(result, "ok", False):
                        err = getattr(result, "error", None) or "bilinmeyen hata"
                        raise AccountError(f"Hesap doğrulanamadı: {err}")
                    email = getattr(result, "email", None)
                    if email and email.strip().lower() != meta.email.strip().lower():
                        raise AccountError(
                            f"Keyring'deki hesap beklenenden farklı: {email} != {meta.email}."
                        )
                    try:
                        live_after = self.keyring.read_live()
                        if live_after:
                            self.keyring.write_vault_raw(name, live_after.raw)
                    except (KeyringError, TokenFormatError):
                        pass

                state = self.state()
                state.active = name
                if name in state.profiles:
                    state.profiles[name].last_used = now_iso()
                    state.profiles[name].needs_login = False
                self.save(state)

                if launch is None:
                    launch = state.settings.launch_after_switch
                if launch:
                    try:
                        self.app.start()
                        launched = True
                    except Exception as exc:
                        warning = f"Geçiş yapıldı ancak Antigravity başlatılamadı: {exc}"
                return SwitchResult(
                    profile=name,
                    email=meta.email,
                    stopped=stopped,
                    launched=launched,
                    warning=warning,
                )
            except Exception:
                # Canlı keyring'i mümkünse eski haline getir.
                try:
                    if before:
                        self.keyring.write_live_raw(before.raw)
                    else:
                        self.keyring.clear("gemini", "antigravity")
                except KeyringError:
                    pass
                if app_storage_before is not None:
                    try:
                        app_storage_path.write_bytes(app_storage_before)
                    except OSError:
                        pass
                raise

    # ---------- metadata ----------

    def rename(self, old: str, new: str) -> storage.ProfileMeta:
        old = safe_name(old, "eski profil adı")
        new = safe_name(new, "yeni profil adı")
        if old == new:
            raise AccountError("Eski ve yeni ad aynı.")
        with KEYRING_LOCK, keyring_file_lock(blocking=True), self._lock:
            state = self.state()
            meta = state.profiles.get(old)
            if not meta:
                raise AccountError(f"'{old}' profili bulunamadı.")
            if new in state.profiles:
                raise AccountConflict(f"'{new}' zaten var.")
            raw = self.keyring.read_vault_raw(old)
            if not raw:
                raise AccountError(f"'{old}' token'ı kasada bulunamadı.")
            self.keyring.write_vault_raw(new, raw)
            self.keyring.delete_vault_raw(old)
            if meta.label == old:
                meta.label = new
            meta.name = new
            state.profiles.pop(old)
            state.profiles[new] = meta
            if state.active == old:
                state.active = new
            if state.primary == old:
                state.primary = new
            self.save(state)
            storage.remove_profile_dir(old)
            self._write_marker(meta)
            return meta

    def delete(self, name: str) -> None:
        name = safe_name(name, "profil adı")
        with KEYRING_LOCK, keyring_file_lock(blocking=True), self._lock:
            state = self.state()
            meta = state.profiles.get(name)
            if not meta:
                raise AccountError(f"'{name}' profili bulunamadı.")
            if meta.primary:
                raise AccountError(
                    f"'{name}' ana hesap; silinemez. Önce başka bir hesabı ana hesap yapın."
                )
            if state.active == name:
                raise AccountError(
                    f"'{name}' şu an aktif. Silmeden önce başka bir hesaba geçin."
                )
            self.keyring.delete_vault_raw(name)
            state.profiles.pop(name, None)
            if state.primary == name:
                state.primary = None
            self.save(state)
            storage.remove_profile_dir(name)
            storage.clear_quota_cache(name)

    def set_primary(self, name: str) -> storage.ProfileMeta:
        name = safe_name(name, "profil adı")
        with self._lock:
            state = self.state()
            meta = state.profiles.get(name)
            if not meta:
                raise AccountError(f"'{name}' profili bulunamadı.")
            state.primary = name
            for p in state.profiles.values():
                p.primary = p.name == name
            self.save(state)
            self._write_marker(meta)
            return meta

    def update_quota_meta(self, name: str, quota) -> None:
        try:
            with self._lock:
                state = self.state()
                meta = state.profiles.get(name)
                if not meta:
                    return
                changed = False
                if getattr(quota, "account_name", None) and quota.account_name != meta.account_name:
                    meta.account_name = quota.account_name
                    changed = True
                if getattr(quota, "plan_name", None) and quota.plan_name != meta.plan_name:
                    meta.plan_name = quota.plan_name
                    changed = True
                needs = bool(getattr(quota, "needs_login", False))
                if needs != meta.needs_login:
                    meta.needs_login = needs
                    changed = True
                if changed:
                    self.save(state)
        except OSError:
            pass

    def mark_needs_login(self, name: str, needs: bool = True) -> None:
        with self._lock:
            state = self.state()
            meta = state.profiles.get(name)
            if meta and meta.needs_login != needs:
                meta.needs_login = needs
                self.save(state)
