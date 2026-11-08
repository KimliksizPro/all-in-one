"""`aio` komut satırı arayüzü."""
from __future__ import annotations

import json
import threading
import time
import webbrowser

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__, antigravity, storage
from .accounts import AccountError, AppRunningError, NoLiveAccountError
from .keyring import KeyringError
from .probe import ProbeError, UserQuota
from .quota import QuotaError
from .service import Service
from .util import human_duration, human_percent, redact

console = Console()


def _svc() -> Service:
    return Service()


def _fail(exc: Exception, code: int = 1) -> None:
    console.print(f"[red]Hata:[/red] {redact(exc)}")
    raise SystemExit(code)


def _status_color(quota) -> str:
    if quota is None:
        return "dim"
    if not quota.ok:
        return "red"
    worst = quota.worst_remaining
    if worst is None:
        return "dim"
    pct = worst * 100.0
    if pct <= 0:
        return "bold red"
    if pct <= 5:
        return "red"
    if pct <= 20:
        return "yellow"
    return "green"


def _fmt_groups(quota) -> str:
    if not quota or not quota.groups:
        return "—"
    lines = []
    for g in quota.groups:
        reset = human_duration(g.reset_in_seconds) if g.reset_in_seconds is not None else "—"
        value = human_percent(g.remaining_percent) if g.remaining_percent is not None else "kota bildirilmedi"
        lines.append(f"{g.name}: {value}  (reset {reset})")
    return "\n".join(lines)


def _accounts_table_from_snapshot(snap: dict, *, show_groups: bool = False) -> Table:
    table = Table(show_lines=True, expand=True)
    table.add_column("Profil", style="bold", no_wrap=True)
    table.add_column("E-posta", overflow="fold")
    table.add_column("Plan", no_wrap=True)
    table.add_column("En düşük", justify="right", no_wrap=True)
    if show_groups:
        table.add_column("Model kotaları", overflow="fold")
    table.add_column("Durum", no_wrap=True)
    for acc in snap["accounts"]:
        qdict = acc.get("quota")
        quota_obj = UserQuota.from_dict(qdict) if qdict else None
        badges = []
        if acc["primary"]:
            badges.append("[magenta]ANA[/magenta]")
        if acc["active"]:
            badges.append("[cyan]AKTİF[/cyan]")
        if acc.get("needs_login"):
            badges.append("[red]GİRİŞ[/red]")
        if quota_obj and quota_obj.stale:
            badges.append("[dim]ÖNBELLEK[/dim]")
        worst = quota_obj.worst_remaining if quota_obj else None
        worst_pct = worst * 100.0 if worst is not None else None
        row = [
            f"{acc['label']}\n[dim]{acc['name']}[/dim] {' '.join(badges)}",
            acc["email"] or "—",
            (qdict or {}).get("plan_name") or acc.get("plan_name") or "—",
            human_percent(worst_pct),
        ]
        if show_groups:
            row.append(_fmt_groups(quota_obj))
        row.append("hazır" if qdict and qdict.get("ok") else ("hata" if qdict else "kota yok"))
        table.add_row(*row)
    return table


def _print_snapshot(snap: dict) -> None:
    app = snap["app"]
    live = snap["live"]
    pids = app.get("pids") or []
    pid_text = ", ".join(map(str, pids[:4])) + (f" +{len(pids) - 4}" if len(pids) > 4 else "")
    app_line = (
        f"[green]açık[/green] (PID {pid_text})"
        if app["running"]
        else "[dim]kapalı[/dim]"
    )
    live_line = f"[bold]{live['email']}[/bold]" if live.get("email") else "[yellow]oturum yok[/yellow]"
    live_error = f"\n[red]Keyring:[/red] {redact(live['error'])}" if live.get("error") else ""
    console.print(
        Panel.fit(
            f"[bold]Antigravity[/bold]  {app_line}   v{app['version']}\n"
            f"[bold]Canlı keyring[/bold]  {live_line}\n"
            f"[bold]Aktif profil[/bold]  {snap.get('active') or '—'}   "
            f"[bold]Ana hesap[/bold]  {snap.get('primary') or '—'}\n"
            f"[bold]Veri[/bold]  {snap['storage']['root']}{live_error}",
            title="all-in-one",
            subtitle=f"v{snap['version']}",
        )
    )
    for warning in snap.get("warnings") or []:
        console.print(f"[yellow]Uyarı:[/yellow] {warning}")
    if snap["accounts"]:
        console.print(_accounts_table_from_snapshot(snap))
    else:
        console.print("[yellow]Kayıtlı hesap yok.[/yellow] Antigravity'de giriş yapıp [bold]aio init[/bold] çalıştırın.")
    if snap["alerts"]:
        console.print()
        _print_alerts(snap["alerts"])
    if snap["refresh"].get("running"):
        r = snap["refresh"]
        console.print(f"[cyan]Kota yenileme sürüyor:[/cyan] {r['done']}/{r['total']} ({r['current'] or ''})")


def _print_alerts(alerts: list[dict]) -> None:
    table = Table(title="Kota uyarıları", show_lines=False)
    table.add_column("Seviye", no_wrap=True)
    table.add_column("Hesap", no_wrap=True)
    table.add_column("Grup")
    table.add_column("Kalan", justify="right")
    table.add_column("Sıfırlanma")
    colors = {"warn": "yellow", "critical": "red", "empty": "bold red"}
    for alert in alerts:
        reset_text = "—"
        if alert.get("reset_at"):
            from .util import parse_iso, utc_now
            reset_dt = parse_iso(alert["reset_at"])
            if reset_dt:
                reset_text = human_duration(int(max(0, (reset_dt - utc_now()).total_seconds())))
        table.add_row(
            f"[{colors.get(alert['level'], 'white')}]{alert['level_label']}[/]",
            f"{alert['label']} [dim]({alert['account']})[/dim]",
            alert["group_name"],
            alert["remaining_label"],
            reset_text,
        )
    console.print(table)


def _json_out(data) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Komutlar
# ---------------------------------------------------------------------------


@click.group(help="all-in-one: Antigravity / Gemini hesap ve kota merkezi.")
@click.version_option(version=__version__, prog_name="aio")
def main() -> None:
    storage.ensure_dirs()


@main.command("init", help="Canlı Antigravity hesabını ana hesap olarak yakala.")
def init_cmd() -> None:
    svc = _svc()
    try:
        state = svc.bootstrap()
    except (KeyringError, AccountError) as exc:
        _fail(exc)
    if not state.profiles:
        console.print("[yellow]Kayıtlı hesap yok.[/yellow] Antigravity'de giriş yapıp tekrar deneyin.")
        return
    console.print(
        f"[green]Hazır:[/green] {len(state.profiles)} hesap, aktif [bold]{state.active}[/bold], "
        f"ana hesap [bold]{state.primary}[/bold]."
    )
    console.print(f"Veri: [dim]{storage.root_dir()}[/dim]")


@main.command("status", help="Hesap, keyring ve uygulama durumunu göster.")
@click.option("--json", "as_json", is_flag=True, help="JSON çıktısı.")
def status_cmd(as_json: bool) -> None:
    svc = _svc()
    try:
        snap = svc.snapshot()
    except Exception as exc:
        _fail(exc)
    if as_json:
        _json_out(snap)
    else:
        _print_snapshot(snap)


@main.group("accounts", help="Hesap profillerini yönet.")
def accounts_group() -> None:
    pass


@accounts_group.command("list", help="Kayıtlı hesapları listele.")
@click.option("--json", "as_json", is_flag=True)
def accounts_list(as_json: bool) -> None:
    svc = _svc()
    try:
        snap = svc.snapshot()
    except Exception as exc:
        _fail(exc)
    if as_json:
        _json_out(snap["accounts"])
    else:
        _print_snapshot(snap)


def _run_login(svc: Service) -> None:
    try:
        state = svc.start_login()
    except Exception as exc:
        _fail(exc)
    if not state.get("running") and state.get("status") not in {"success", "success_same"}:
        console.print(f"[red]Giriş başlatılamadı:[/red] {state.get('error') or state.get('message')}")
        raise SystemExit(1)
    console.print("[cyan]Antigravity girişi başlatıldı.[/cyan] Tarayıcıda Google hesabını seç.")
    last = None
    while True:
        state = svc.login.state()
        status = state.get("status")
        message = state.get("message") or ""
        if status != last:
            if status in {"starting", "waiting"}:
                console.print(f"[dim]{message}[/dim]")
            elif status == "success":
                console.print(f"[green]{message}[/green]")
            elif status == "success_same":
                console.print(f"[yellow]{message}[/yellow]")
            elif status in {"error", "timeout"}:
                console.print(f"[red]{message}[/red] {state.get('error') or ''}")
            last = status
        if not state.get("running"):
            break
        time.sleep(1.0)


@accounts_group.command("login", help="Antigravity'nin kendi giriş akışıyla yeni hesap ekle.")
def accounts_login() -> None:
    _run_login(_svc())


@main.command("login", help="Antigravity arayüzüyle yeni hesap ekle (kısayol).")
def login_cmd() -> None:
    _run_login(_svc())


@accounts_group.command("capture", help="Antigravity'de açık olan hesabı profil olarak kaydet.")
@click.argument("name")
@click.option("--label", help="Görünen ad (varsayılan: Ana hesap / profil adı).")
@click.option("--overwrite", is_flag=True, help="Aynı adlı profilin üzerine yaz.")
@click.option("--primary", "make_primary", is_flag=True, help="Bu hesabı ana hesap yap.")
def accounts_capture(name: str, label: str | None, overwrite: bool, make_primary: bool) -> None:
    svc = _svc()
    try:
        result = svc.capture(name, label=label, overwrite=overwrite, primary=make_primary)
    except (AccountError, KeyringError, NoLiveAccountError) as exc:
        _fail(exc)
    console.print(f"[green]{result.message}[/green]")
    console.print(f"Ana hesap: [bold]{svc.accounts.state().primary}[/bold]")


@accounts_group.command("add", help="capture ile aynı: açık hesabı yeni profil olarak ekle.")
@click.argument("name")
@click.option("--label")
@click.option("--overwrite", is_flag=True)
@click.option("--primary", "make_primary", is_flag=True)
def accounts_add(name: str, label: str | None, overwrite: bool, make_primary: bool) -> None:
    accounts_capture.callback(name, label, overwrite, make_primary)


def _do_switch(name: str, stop: bool, launch: bool | None, validate: bool, force: bool, restart: bool) -> None:
    svc = _svc()
    if restart:
        stop = True
        if launch is None:
            launch = True
    try:
        result = svc.switch(name, stop=stop, launch=launch, validate=validate)
    except AppRunningError as exc:
        _fail(exc)
    except (AccountError, QuotaError, KeyringError, ProbeError) as exc:
        _fail(exc)
    console.print(f"[green]Geçiş yapıldı:[/green] {result.profile} → {result.email}")
    if result.warning:
        console.print(f"[yellow]Uyarı:[/yellow] {result.warning}")
    if result.launched:
        console.print("[dim]Antigravity başlatıldı.[/dim]")


@accounts_group.command("use", help="Bir hesaba geç.")
@click.argument("name")
@click.option("--stop", is_flag=True, help="Gerekiyorsa Antigravity'yi kapat.")
@click.option("--launch/--no-launch", default=None, help="Geçişten sonra Antigravity'yi aç/kapat.")
@click.option("--restart", is_flag=True, help="Kapat, geç ve yeniden aç.")
@click.option("--no-validate", is_flag=True, help="Geçiş sonrası kota doğrulamasını atla.")
@click.option("--force", is_flag=True, help="Kapanmayan süreçleri zorla sonlandır.")
def accounts_use(name: str, stop: bool, launch: bool | None, restart: bool, no_validate: bool, force: bool) -> None:
    _do_switch(name, stop, launch, not no_validate, force, restart)


@main.command("use", help="Kısayol: bir hesaba geç.")
@click.argument("name")
@click.option("--stop", is_flag=True)
@click.option("--launch/--no-launch", default=None)
@click.option("--restart", is_flag=True)
@click.option("--no-validate", is_flag=True)
@click.option("--force", is_flag=True)
def use_cmd(name: str, stop: bool, launch: bool | None, restart: bool, no_validate: bool, force: bool) -> None:
    _do_switch(name, stop, launch, not no_validate, force, restart)


@accounts_group.command("rename", help="Profili yeniden adlandır.")
@click.argument("old")
@click.argument("new")
def accounts_rename(old: str, new: str) -> None:
    svc = _svc()
    try:
        meta = svc.rename(old, new)
    except (AccountError, KeyringError) as exc:
        _fail(exc)
    console.print(f"[green]Yeniden adlandırıldı:[/green] {old} → {meta.name}")


@accounts_group.command("delete", help="Profili sil.")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Onay sorma.")
def accounts_delete(name: str, yes: bool) -> None:
    if not yes:
        click.confirm(f"'{name}' profili ve keyring kaydı silinsin mi?", abort=True)
    svc = _svc()
    try:
        svc.delete(name)
    except (AccountError, KeyringError) as exc:
        _fail(exc)
    console.print(f"[green]Silindi:[/green] {name}")


@accounts_group.command("primary", help="Bir hesabı ana hesap yap.")
@click.argument("name")
def accounts_primary(name: str) -> None:
    svc = _svc()
    try:
        meta = svc.set_primary(name)
    except (AccountError, KeyringError) as exc:
        _fail(exc)
    console.print(f"[green]Ana hesap:[/green] {meta.name} ({meta.email})")


@main.command("quota", help="Kota durumunu göster / yenile.")
@click.argument("name", required=False)
@click.option("--all", "all_accounts", is_flag=True, help="Tüm hesapları yenile.")
@click.option("--refresh", is_flag=True, help="Kotayı canlı oku.")
@click.option("--json", "as_json", is_flag=True)
def quota_cmd(name: str | None, all_accounts: bool, refresh: bool, as_json: bool) -> None:
    svc = _svc()
    try:
        svc.bootstrap()
        if refresh:
            if all_accounts or not name:
                svc.quota.refresh_all(force=True)
            else:
                svc.refresh_one(name, force=True)
        snap = svc.snapshot()
    except QuotaError as exc:
        _fail(exc)
    except (KeyringError, ProbeError, AccountError) as exc:
        _fail(exc)
    if as_json:
        _json_out(snap["accounts"])
        return
    if not snap["accounts"]:
        console.print("[yellow]Kayıtlı hesap yok.[/yellow]")
        return
    table = _accounts_table_from_snapshot(snap, show_groups=True)
    console.print(table)
    for warning in snap.get("warnings") or []:
        console.print(f"[yellow]Uyarı:[/yellow] {warning}")
    if snap["alerts"]:
        _print_alerts(snap["alerts"])


@main.command("ui", help="Yerel web arayüzünü başlat.")
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--no-browser", is_flag=True, help="Tarayıcıyı otomatik açma.")
def ui_cmd(port: int, no_browser: bool) -> None:
    try:
        import uvicorn
    except ImportError:
        _fail(RuntimeError("uvicorn kurulu değil. `pip install -e .` çalıştırın."))
    from .server import app

    url = f"http://127.0.0.1:{port}"
    console.print(Panel.fit(
        f"[bold]all-in-one[/bold] web arayüzü\n{url}\n\n"
        "[dim]Sadece 127.0.0.1 dinlenir. Durdurmak için Ctrl+C.[/dim]",
        title="aio ui",
    ))
    if not no_browser:
        def _open() -> None:
            time.sleep(0.8)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=_open, daemon=True).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass


@main.command("watch", help="Kotayı periyodik oku ve eşik uyarılarını bildir.")
@click.option("--interval", type=int, default=None, help="Saniye (varsayılan: ayarlardaki poll_seconds).")
@click.option("--all", "all_accounts", is_flag=True, help="Tüm hesapları yenile (Antigravity kapalı olmalı).")
@click.option("--once", is_flag=True, help="Bir kez çalış ve çık.")
@click.option("--no-notify", is_flag=True, help="Masaüstü bildirimi gönderme.")
def watch_cmd(interval: int | None, all_accounts: bool, once: bool, no_notify: bool) -> None:
    svc = _svc()
    try:
        svc.bootstrap()
    except (KeyringError, AccountError) as exc:
        _fail(exc)
    settings = svc.accounts.state().settings
    delay = interval if interval is not None else settings.poll_seconds
    old_callback = svc.quota.on_result
    svc.quota.on_result = None
    try:
        while True:
            try:
                if all_accounts:
                    svc.quota.refresh_all(force=True)
                else:
                    svc.refresh_active(force=True)
                current, sent = svc.evaluate_alerts(notify=not no_notify)
                if sent:
                    for alert in sent:
                        console.print(f"[yellow]Bildirim:[/yellow] {alert.message}")
                elif current:
                    _print_alerts([a.to_dict() for a in current])
                else:
                    console.print(f"[dim]{time.strftime('%H:%M:%S')} kota normal.[/dim]")
            except QuotaError as exc:
                console.print(f"[yellow]Uyarı:[/yellow] {redact(exc)}")
            except (KeyringError, ProbeError, AccountError) as exc:
                console.print(f"[red]Hata:[/red] {redact(exc)}")
            if once:
                break
            time.sleep(max(30, int(delay)))
    except KeyboardInterrupt:
        console.print("\n[dim]Durduruldu.[/dim]")
    finally:
        svc.quota.on_result = old_callback


@main.group("app", help="Antigravity uygulamasını yönet.")
def app_group() -> None:
    pass


@app_group.command("status")
def app_status() -> None:
    info = antigravity.app_info()
    console.print(
        Panel.fit(
            f"Kurulu: {'evet' if info.installed else 'hayır'}\n"
            f"Çalışıyor: {'evet' if info.running else 'hayır'}\n"
            f"Sürüm: {info.version}\n"
            f"Binary: {info.binary or '—'}\n"
            f"LS: {info.ls_binary or '—'}\n"
            f"PID: {', '.join(map(str, info.pids[:6])) + (f' +{len(info.pids) - 6}' if len(info.pids) > 6 else '') or '—'}",
            title="Antigravity",
        )
    )


@app_group.command("start")
def app_start() -> None:
    try:
        pid = antigravity.start()
    except antigravity.AppError as exc:
        _fail(exc)
    console.print(f"[green]Başlatıldı:[/green] PID {pid}")


@app_group.command("stop")
@click.option("--force", is_flag=True, help="Kapanmayan süreçleri SIGKILL ile sonlandır.")
def app_stop(force: bool) -> None:
    result = antigravity.stop(force=force)
    if result.remaining:
        console.print(f"[red]Kapanmadı:[/red] PID {', '.join(map(str, result.remaining))}")
        raise SystemExit(1)
    console.print(f"[green]Durduruldu.[/green] PID: {', '.join(map(str, result.stopped)) or '—'}")


@main.command("doctor", help="Kurulum, keyring ve kota bağlantısını kontrol et.")
@click.option("--json", "as_json", is_flag=True)
@click.option("--probe", is_flag=True, help="Canlı kota RPC'sini de dene.")
def doctor_cmd(as_json: bool, probe: bool) -> None:
    svc = _svc()
    try:
        report = svc.doctor(probe=probe)
    except Exception as exc:
        _fail(exc)
    if as_json:
        _json_out(report)
        return
    table = Table(title="aio doctor", show_lines=False)
    table.add_column("Kontrol", style="bold", no_wrap=True)
    table.add_column("Durum", no_wrap=True)
    table.add_column("Detay", overflow="fold")
    table.add_column("Öneri", overflow="fold")
    for check in report["checks"]:
        table.add_row(
            check["name"],
            "[green]OK[/green]" if check["ok"] else "[red]HATA[/red]",
            check["detail"],
            check.get("hint") or "",
        )
    console.print(table)
    if report.get("quota"):
        q = report["quota"]
        console.print(
            f"[bold]Kota:[/bold] {q.get('email')} · {human_percent(q['worst_remaining'] * 100 if q.get('worst_remaining') is not None else None)} · "
            f"{len(q.get('groups') or [])} model"
        )


if __name__ == "__main__":
    main()
