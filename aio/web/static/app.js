'use strict';

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const numFmt = new Intl.NumberFormat('tr-TR');
  const pctFmt = new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 4, useGrouping: false });
  const precisePctFmt = new Intl.NumberFormat('tr-TR', { maximumSignificantDigits: 17, useGrouping: false });
  const STORAGE_BROWSER_NOTIFY = 'aio.browserNotify';
  const STORAGE_NOTIFIED = 'aio.notifiedSig';

  const state = {
    data: null,
    loading: false,
    busy: false,
    search: '',
    pollTimer: null,
    stateRequest: null,
    failures: 0,
    reloadPending: false,
    filter: 'all',
    sort: 'default',
    selectedAccount: null,
    manualDirty: new Set(),
    sections: new Map(),
    cards: new Map(),
    dismissedSig: sessionStorage.getItem('aio.dismissedAlerts') || '',
    browserNotify: localStorage.getItem(STORAGE_BROWSER_NOTIFY) === '1',
    lastRefreshError: '',
    lastLoginStatus: 'idle',
  };

  // ---------- yardımcılar ----------

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function fmtPct(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
    const n = Number(value);
    const rounded = Number(n.toFixed(4));
    const formatter = (n !== 0 && rounded === 0) || (n !== 100 && rounded === 100) ? precisePctFmt : pctFmt;
    return formatter.format(n) + '%';
  }

  function fmtNum(value) {
    if (value === null || value === undefined) return '—';
    return numFmt.format(Number(value));
  }

  function toneFor(pct) {
    if (pct === null || pct === undefined) return 'unknown';
    const s = state.data?.settings || {};
    const warn = Number(s.warn_percent ?? 20);
    const critical = Number(s.critical_percent ?? 5);
    if (pct <= 0) return 'empty';
    if (pct <= critical) return 'critical';
    if (pct <= warn) return 'warn';
    return 'good';
  }

  function toneClass(pct) { return `tone-${toneFor(pct)}`; }

  function fillClass(pct) {
    const t = toneFor(pct);
    return `fill-${t === 'unknown' ? 'unknown' : t}`;
  }

  function fmtCountdown(iso) {
    if (!iso) return '—';
    const target = new Date(iso).getTime();
    if (!Number.isFinite(target)) return '—';
    let diff = Math.floor((target - Date.now()) / 1000);
    if (diff <= 0) return 'sıfırlanıyor…';
    if (diff < 60) return '<1 dk';
    const minutes = Math.floor(diff / 60);
    if (minutes < 60) return `${minutes} dk`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} sa ${minutes % 60} dk`;
    const days = Math.floor(hours / 24);
    return `${days} gün ${hours % 24} sa`;
  }

  function fmtDateTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return '—';
    return d.toLocaleString('tr-TR', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    });
  }

  function relTime(iso) {
    if (!iso) return '—';
    const target = new Date(iso).getTime();
    if (!Number.isFinite(target)) return '—';
    const diff = Math.floor((Date.now() - target) / 1000);
    if (diff < 45) return 'az önce';
    if (diff < 3600) return `${Math.floor(diff / 60)} dk önce`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} sa önce`;
    return `${Math.floor(diff / 86400)} gün önce`;
  }

  function worstPct(account) {
    const q = account?.quota;
    if (!q || q.worst_remaining === null || q.worst_remaining === undefined) return null;
    const pct = Number(q.worst_remaining) * 100;
    return Number.isFinite(pct) ? pct : null;
  }

  function modelPct(model) {
    if (model?.remaining === null || model?.remaining === undefined) return null;
    const pct = Number(model.remaining) * 100;
    return Number.isFinite(pct) ? pct : null;
  }

  function newestFetch(data) {
    let newest = null;
    for (const account of data.accounts || []) {
      const value = account.quota?.fetched_at;
      if (!value) continue;
      if (!newest || new Date(value) > new Date(newest)) newest = value;
    }
    return newest;
  }

  function alertSignature(alerts) {
    return (alerts || []).map((a) => `${a.account}|${a.group_id}|${a.level}|${a.reset_at || ''}`).join(';');
  }

  function labelFor(name) {
    const account = (state.data?.accounts || []).find((a) => a.name === name);
    return account?.label || name;
  }

  function accountFor(name) {
    return (state.data?.accounts || []).find((a) => a.name === name);
  }

  async function api(path, opts = {}) {
    const { timeout = opts.method && opts.method !== 'GET' ? 120000 : 15000, ...options } = opts;
    const controller = new AbortController();
    options.signal = controller.signal;
    options.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    const timer = window.setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, options);
      let body = {};
      try { body = await response.json(); } catch (err) {
        if (controller.signal.aborted) throw err;
      }
      if (!response.ok) {
        const error = new Error(body.detail || `HTTP ${response.status}`);
        error.status = response.status;
        error.payload = body;
        throw error;
      }
      return body;
    } catch (err) {
      if (controller.signal.aborted) {
        throw new Error(options.method && options.method !== 'GET'
          ? 'İstek zaman aşımına uğradı. İşlem sunucuda sürebilir; yeniden denemeden önce durumu kontrol edin.'
          : 'Sunucu yanıt vermedi; bağlantı yeniden denenecek.');
      }
      throw err;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function toast(message, type = 'info', timeout = 4200) {
    const root = $('#toast-root');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    root.appendChild(el);
    window.setTimeout(() => {
      el.style.opacity = '0';
      el.style.transform = 'translateX(10px)';
      window.setTimeout(() => el.remove(), 180);
    }, timeout);
  }

  function setConn(ok) {
    const el = $('#conn');
    el.className = `conn ${ok ? 'on' : 'off'}`;
    const label = ok ? 'Canlı' : 'Bağlantı yok';
    if ($('span', el).textContent !== label) $('span', el).textContent = label;
    if (ok) el.removeAttribute('title');
  }

  function setBusy(value) {
    state.busy = value;
    syncBusy();
  }

  function syncBusy() {
    const login = Boolean(state.data?.login?.running);
    const refreshing = Boolean(state.data?.refresh?.running);
    const locked = state.busy || login || refreshing;
    for (const id of ['accounts', 'live-panel', 'add-panel']) {
      const element = $(`#${id}`);
      element.inert = locked;
      element.classList.toggle('is-busy', locked);
    }
    for (const id of ['add-btn', 'settings-btn']) $(`#${id}`).disabled = locked;
    $('#account-select').disabled = locked || !(state.data?.accounts || []).length;
    $('#switch-selected').disabled = locked || !$('#account-select').value || $('#account-select').value === state.data?.active;
    $('#capture-btn').disabled = locked || !state.data?.live?.available;
    $('#refresh-btn').disabled = locked;
    $('#login-btn').disabled = locked;
    const status = $('#operation-status');
    status.classList.toggle('hidden', !locked);
    status.textContent = state.busy ? 'İşlem sürüyor… Lütfen tamamlanmasını bekleyin.'
      : login ? (state.data.login.message || 'Google hesabıyla giriş bekleniyor…')
        : refreshing ? `Kotalar yenileniyor: ${state.data.refresh.done || 0} / ${state.data.refresh.total || 0}` : '';
    $('#accounts').setAttribute('aria-busy', String(locked));
  }

  // Tek zamanlayıcı: istek bittikten sonra planlanır; gizli sekmede durur.
  async function load(quiet = false) {
    if (document.hidden) return;
    if (state.loading) {
      state.reloadPending = true;
      return state.stateRequest;
    }
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.loading = true;
    state.stateRequest = (async () => {
      try {
        const data = await api('/api/state');
        if (state.busy) return;
        state.data = data;
        state.failures = 0;
        setConn(true);
        render();
      } catch (err) {
        state.failures += 1;
        setConn(false);
        $('#conn').title = err.message;
        if (!quiet) toast(err.message || 'Sunucuya bağlanılamadı.', 'error');
      } finally {
        state.loading = false;
        state.stateRequest = null;
        const pending = state.reloadPending;
        state.reloadPending = false;
        schedulePoll(pending ? 0 : undefined);
      }
    })();
    return state.stateRequest;
  }

  function schedulePoll(delay) {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (document.hidden || state.loading) return;
    const active = state.busy || state.data?.refresh?.running || state.data?.login?.running;
    const next = delay ?? (state.failures ? Math.min(60000, 5000 * 2 ** Math.min(state.failures - 1, 4)) : active ? 2200 : 15000);
    state.pollTimer = window.setTimeout(() => load(true), next);
  }

  // ---------- render ----------

  function stateSignature(data) {
    return JSON.stringify(data, (key, value) => ['generated_at', 'keyring_busy', 'pids', 'cached', 'verification_url'].includes(key) ? undefined : value);
  }

  function renderSection(key, data, callback) {
    const signature = stateSignature(data);
    if (state.sections.get(key) === signature) return;
    state.sections.set(key, signature);
    callback();
  }

  // Reconcile existing nodes instead of replacing focused controls or open details.
  function patchNode(current, next) {
    if (current.nodeType !== next.nodeType || current.nodeName !== next.nodeName) {
      current.replaceWith(next);
      return next;
    }
    if (current.nodeType === Node.TEXT_NODE) {
      if (current.textContent !== next.textContent) current.textContent = next.textContent;
      return current;
    }
    if (current.nodeType !== Node.ELEMENT_NODE) return current;
    for (const attr of Array.from(current.attributes)) {
      if (current.tagName === 'DETAILS' && attr.name === 'open') continue;
      if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
    }
    for (const attr of Array.from(next.attributes)) {
      if (current.tagName === 'DETAILS' && attr.name === 'open') continue;
      if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
    }
    const key = (node) => node.nodeType === Node.ELEMENT_NODE
      ? node.id || node.dataset.key || (node.dataset.action ? `${node.dataset.action}:${node.dataset.name || ''}` : '') : '';
    let cursor = current.firstChild;
    for (const child of Array.from(next.childNodes)) {
      const childKey = key(child);
      const match = childKey ? Array.from(current.childNodes).find((node) => key(node) === childKey) : cursor && !key(cursor) ? cursor : null;
      if (!match) {
        current.insertBefore(child, cursor);
        continue;
      }
      if (match !== cursor) current.insertBefore(match, cursor);
      cursor = patchNode(match, child).nextSibling;
    }
    while (cursor) {
      const nextSibling = cursor.nextSibling;
      cursor.remove();
      cursor = nextSibling;
    }
    return current;
  }

  function setHtml(element, html) {
    const focused = element.contains(document.activeElement) ? document.activeElement : null;
    const template = document.createElement('template');
    template.innerHTML = html;
    const next = element.cloneNode(false);
    next.append(template.content);
    patchNode(element, next);
    if (focused?.isConnected && document.activeElement !== focused && !focused.closest('[inert]')) focused.focus({ preventScroll: true });
  }

  function render() {
    if (!state.data) return;
    const d = state.data;
    // Rotating verification tokens update the link, not the surrounding panel.
    const verify = $('[data-verification-link]');
    if (verify && safeVerificationUrl(d.live?.auth?.verification_url)) verify.href = d.live.auth.verification_url;
    renderSummary(d);
    renderProgress(d);
    renderSection('warnings', [d.warnings, d.refresh?.errors], () => renderWarnings(d));
    renderSection('login', d.login, () => renderLogin(d));
    renderSection('alerts', d.alerts, () => renderAlerts(d));
    renderSection('live', [d.live, Boolean(safeVerificationUrl(d.live?.auth?.verification_url)), d.app, d.primary, d.active, d.accounts?.map((a) => [a.name, a.label])], () => renderLive(d));
    renderAccounts(d);
    renderAdd(d);
    renderSection('switcher', [d.accounts?.map((a) => [a.name, a.label, a.email, a.primary, a.active]), d.active], () => renderSwitcher(d));
    updateCountdowns();
    tickMeta();
    syncBusy();
    const footer = $('#footer-version');
    if (footer) footer.textContent = `v${d.version || ''}`;
    $('#server-version').textContent = `Sunucu: ${d.version ? 'v' + d.version : 'sürüm bilinmiyor'}`;
    if (d.keyring_busy) {
      $('#conn').title = 'Secret Service meşgul; canlı oturum son bilinen gözlemden gösteriliyor.';
    }
    document.title = d.alerts?.length ? `(${d.alerts.length}) all-in-one · Antigravity` : 'all-in-one · Antigravity';
    maybeBrowserNotify(d);
  }

  function safeVerificationUrl(value) {
    if (typeof value !== 'string') return false;
    try { return new URL(value).protocol === 'https:'; } catch { return false; }
  }

  function liveAuthFor(account) {
    const live = state.data?.live;
    if (!live?.available || !account.email || !live.email || account.email.toLowerCase() !== live.email.toLowerCase()) return null;
    return live.auth || null;
  }

  function renderLogin(d) {
    const el = $('#login-banner');
    if (!el) return;
    const li = d.login || {};
    if (!li.status || li.status === 'idle') {
      el.classList.add('hidden');
      el.innerHTML = '';
      state.lastLoginStatus = 'idle';
      return;
    }
    el.classList.remove('hidden');
    let cls = 'login-banner';
    let icon = '<span class="spinner"></span>';
    let title = 'Giriş bekleniyor';
    let text = li.message || '';
    let sub = '';
    let action = '';
    if (li.running) {
      title = li.status === 'starting' ? 'Antigravity hazırlanıyor' : 'Tarayıcıda giriş bekleniyor';
      sub = li.before_email ? `Mevcut canlı hesap: ${li.before_email}` : 'Henüz canlı hesap yok.';
      if (li.auth_url) {
        action = `<a class="btn btn-small btn-ghost" href="${esc(li.auth_url)}" target="_blank" rel="noreferrer">Giriş sayfasını aç</a>`;
      }
    } else if (li.status === 'success' || li.status === 'success_same') {
      cls += ' success';
      icon = '✓';
      title = li.status === 'success' ? 'Hesap eklendi' : 'Aynı hesap seçildi';
      sub = li.captured_email ? `Kayıtlı profil: ${li.captured_email}` : '';
      action = '<button class="btn btn-small" data-action="login-reset">Tamam</button>';
    } else {
      cls += ' error';
      icon = '!';
      title = 'Giriş tamamlanamadı';
      sub = li.error ? li.error : '';
      action = '<button class="btn btn-small btn-ghost" data-action="login-reset">Kapat</button>';
    }
    el.className = cls;
    setHtml(el, `
      <div class="login-icon" aria-hidden="true">${icon}</div>
      <div class="login-main">
        <div class="login-title">${esc(title)}</div>
        <div class="login-text">${esc(text)}</div>
        ${sub ? `<div class="login-sub">${esc(sub)}</div>` : ''}
      </div>
      ${action}`);
    if (li.status !== state.lastLoginStatus) {
      if (li.status === 'success') {
        toast(li.message || 'Hesap eklendi.', 'success', 7000);
        if (li.captured_profile && !state.data?.app?.running) {
          window.setTimeout(() => refreshOne(li.captured_profile), 700);
        }
      } else if (li.status === 'success_same') toast(li.message || 'Aynı hesap seçildi.', 'info', 8000);
      else if (li.status === 'error' || li.status === 'timeout') toast(`${li.message} ${li.error || ''}`.trim(), 'error', 8000);
      state.lastLoginStatus = li.status;
    }
  }

  function renderSwitcher(d) {
    const select = $('#account-select');
    const button = $('#switch-selected');
    if (!select || !button) return;
    const accounts = d.accounts || [];
    const active = d.active;
    const selected = state.selectedAccount ?? (select.value || active);
    setHtml(select, accounts.map((a) => {
      const marks = [];
      if (a.primary) marks.push('ANA');
      if (a.active) marks.push('AKTİF');
      const suffix = marks.length ? ` [${marks.join(' · ')}]` : '';
      return `<option data-key="${esc(a.name)}" value="${esc(a.name)}">${esc(a.label || a.name)} — ${esc(a.email || a.name)}${esc(suffix)}</option>`;
    }).join(''));
    select.value = accounts.some((a) => a.name === selected) ? selected : active || accounts[0]?.name || '';
    state.selectedAccount = select.value;
    select.disabled = accounts.length === 0 || Boolean(d.login?.running);
    button.disabled = select.disabled || !select.value || select.value === active;
    button.textContent = select.value === active ? 'Aktif' : 'Geç';
  }

  function renderSummary(d) {
    const accounts = d.accounts || [];
    const live = d.live || {};
    const counts = d.counts || {};
    $('#stat-accounts').textContent = fmtNum(counts.accounts ?? accounts.length);
    const primary = accounts.find((a) => a.primary);
    $('#stat-primary').textContent = primary ? `Ana: ${primary.label}` : 'Ana hesap yok';

    $('#stat-live').textContent = live.available ? (live.email || 'Bilinmiyor') : 'Oturum yok';
    $('#stat-live-sub').textContent = d.app?.running
      ? `Antigravity açık${d.app.version ? ' · v' + d.app.version : ''}`
      : 'Antigravity kapalı';

    let lowest = null;
    for (const account of accounts) {
      const pct = worstPct(account);
      if (pct === null) continue;
      if (!lowest || pct < lowest.pct) lowest = { pct, account };
    }
    if (lowest) {
      $('#stat-low').textContent = fmtPct(lowest.pct);
      $('#stat-low').className = toneClass(lowest.pct);
      $('#stat-low-sub').textContent = lowest.account.label;
    } else {
      $('#stat-low').textContent = '—';
      $('#stat-low').className = '';
      $('#stat-low-sub').textContent = 'Kota okunmadı';
    }

    $('#stat-alerts').textContent = fmtNum(counts.alerts || 0);
    $('#stat-alerts').className = (counts.alerts || 0) > 0 ? 'tone-warn' : '';
    const newest = newestFetch(d);
    $('#stat-refresh').textContent = d.refresh?.running
      ? `Yenileniyor ${d.refresh.done}/${d.refresh.total}`
      : newest ? `Son güncelleme ${relTime(newest)}` : 'Henüz kota yok';
  }

  function renderWarnings(d) {
    const el = $('#warnings');
    if (!el) return;
    const items = [];
    for (const warning of d.warnings || []) items.push(warning);
    for (const error of d.refresh?.errors || []) items.push(`Yenileme: ${error}`);
    if (!items.length) {
      el.classList.add('hidden');
      el.innerHTML = '';
      return;
    }
    el.classList.remove('hidden');
    el.innerHTML = items.map((item) => `<div class="warning-box">${esc(item)}</div>`).join('');
  }

  function renderProgress(d) {
    const progress = $('#refresh-progress');
    const btn = $('#refresh-btn');
    const running = Boolean(d.refresh?.running);
    if (running) {
      const total = Math.max(1, Number(d.refresh.total || 1));
      const pct = Math.max(0, Math.min(100, (Number(d.refresh.done || 0) / total) * 100));
      progress.classList.remove('hidden');
      progress.firstElementChild.style.width = `${Math.max(2, pct)}%`;
      progress.setAttribute('aria-valuenow', String(Math.round(pct)));
      progress.setAttribute('aria-valuetext', `${d.refresh.done || 0} / ${d.refresh.total || 0} hesap`);
      btn.disabled = true;
      btn.textContent = 'Yenileniyor…';
    } else {
      progress.classList.add('hidden');
      progress.removeAttribute('aria-valuenow');
      btn.disabled = state.busy;
      btn.textContent = 'Yenile';
      if (d.refresh?.error && d.refresh.error !== state.lastRefreshError) {
        state.lastRefreshError = d.refresh.error;
        toast(d.refresh.error, 'error');
      }
      if (!d.refresh?.error) state.lastRefreshError = '';
    }
  }

  function renderAlerts(d) {
    const banner = $('#alert-banner');
    const alerts = d.alerts || [];
    if (!alerts.length) {
      state.dismissedSig = '';
      sessionStorage.removeItem('aio.dismissedAlerts');
      banner.classList.add('hidden');
      banner.innerHTML = '';
      return;
    }
    const sig = alertSignature(alerts);
    if (state.dismissedSig === sig) {
      banner.classList.add('hidden');
      banner.innerHTML = '';
      return;
    }
    const critical = alerts.some((a) => a.level === 'critical' || a.level === 'empty');
    banner.className = `alert-banner${critical ? ' critical' : ''}`;
    const shown = alerts.slice(0, 8);
    const items = shown.map((a) => {
      const level = a.level === 'empty' ? 'DOLDU' : a.level === 'critical' ? 'KRİTİK' : 'AZALIYOR';
      const reset = a.reset_at ? ` · ${fmtCountdown(a.reset_at)}` : '';
      return `<li><b>${esc(level)}</b><span><b>${esc(a.label)}</b> · ${esc(a.group_name)} · ${esc(a.remaining_label)}${esc(reset)}</span></li>`;
    }).join('');
    const extra = alerts.length > shown.length ? `<li><span>+ ${alerts.length - shown.length} uyarı daha</span></li>` : '';
    banner.innerHTML = `
      <div class="alert-icon" aria-hidden="true">${critical ? '!' : '△'}</div>
      <div class="alert-body">
        <div class="alert-title">${critical ? 'Kota kritik seviyede' : 'Kota azalıyor'}</div>
        <ul class="alert-list">${items}${extra}</ul>
      </div>
      <button class="alert-dismiss" data-action="dismiss-alerts" title="Gizle">×</button>`;
  }

  function renderLive(d) {
    const panel = $('#live-panel');
    const live = d.live || {};
    const app = d.app || {};
    const running = Boolean(app.running);
    const appState = running ? 'Açık' : 'Kapalı';
    const appClass = running ? 'open' : '';
    const appButtons = running
      ? `<button class="btn btn-small btn-ghost" data-action="app-stop">Antigravity'yi kapat</button>`
      : `<button class="btn btn-small" data-action="app-start">Antigravity'yi aç</button>`;
    const refreshBtn = `<button class="btn btn-small btn-ghost" data-action="refresh-active">Aktif kotayı yenile</button>`;

    if (!live.available) {
      panel.classList.remove('is-warning');
      setHtml(panel, `
        <div class="live-main">
          <div class="eyebrow">Canlı Antigravity oturumu</div>
          <h2 class="live-title">Oturum yok</h2>
          <p class="live-sub">Antigravity'de bir Google hesabıyla giriş yapın; bu panel son bilinen canlı oturumu gösterir.</p>
          ${live.error ? `<div class="error-box" style="margin-top:12px">${esc(live.error)}</div>` : ''}
        </div>
        <div class="live-side">
          <div class="app-state ${appClass}"><span class="dot"></span> Antigravity: ${appState}</div>
          <div class="app-meta">${esc(app.binary || 'Antigravity binary bulunamadı')}</div>
          <div class="btn-row">${appButtons}${refreshBtn}</div>
        </div>`);
      return;
    }

    const known = live.known_profile;
    const knownAccount = known ? accountFor(known) : null;
    const auth = live.auth || {};
    const authIneligible = Boolean(auth.ineligible);
    const authEligible = auth.eligible === true && auth.has_valid_auth === true && !authIneligible;
    const warnings = [];
    if (!known) warnings.push('Bu canlı hesap henüz kayıtlı değil. Aşağıdaki formdan yakalayın.');
    if (known && !live.matches_active) warnings.push('Canlı hesap, aktif profille eşleşmiyor; sayfa yenilenince senkronize edilir.');
    if (live.expired) warnings.push('Access token süresi dolmuş; Antigravity açıldığında otomatik yenilenir.');
    if (authIneligible) warnings.push((auth.message || 'Antigravity bu Google hesabını uygun bulmuyor.').replace(/\s+/g, ' '));
    panel.className = `live-panel card${warnings.length ? ' is-warning' : ''}`;

    const chips = [];
    chips.push(`<span class="chip ${known ? 'good' : 'warn'}"><b>${known ? 'Kayıtlı' : 'Yeni'}</b> ${esc(known || '—')}</span>`);
    if (knownAccount) chips.push(`<span class="chip"><b>${esc(knownAccount.label)}</b></span>`);
    if (live.auth_method) chips.push(`<span class="chip">Auth <b>${esc(live.auth_method)}</b></span>`);
    if (live.expiry) chips.push(`<span class="chip ${live.expired ? 'bad' : ''}">Token <b>${esc(fmtDateTime(live.expiry))}</b></span>`);
    chips.push(`<span class="chip ${running ? 'good' : ''}">Antigravity <b>${esc(appState)}</b></span>`);
    chips.push(`<span class="chip ${authIneligible ? 'bad' : authEligible ? 'good' : ''}">Antigravity uygunluğu <b>${authIneligible ? 'Uygun değil' : authEligible ? 'Doğrulandı' : 'Bilinmiyor'}</b></span>`);

    const warningHtml = warnings.map((w) => `<div class="warning-box">${esc(w)}</div>`).join('');
    let authActions = '';
    if (authIneligible) {
      if (safeVerificationUrl(auth.verification_url)) {
        authActions += `<a data-verification-link class="btn btn-small btn-ghost" href="${esc(auth.verification_url)}" target="_blank" rel="noreferrer">Doğrulamayı aç</a>`;
      }
      if (d.primary && d.active !== d.primary) {
        authActions += `<button class="btn btn-small" data-action="switch-primary">Ana hesaba geç</button>`;
      }
    }
    const captureBtn = !known
      ? `<button class="btn btn-small btn-primary" data-action="capture-current">Canlı hesabı yakala</button>`
      : `<button class="btn btn-small btn-ghost" data-action="capture-current">Token'ı güncelle</button>`;

    setHtml(panel, `
      <div class="live-main">
        <div class="eyebrow">Canlı Antigravity oturumu</div>
        <h2 class="live-title">${esc(live.email || 'E-posta okunamadı')}</h2>
        <p class="live-sub">${esc(live.account_name || '')}</p>
        <div class="chips">${chips.join('')}</div>
        ${warningHtml ? `<div style="display:grid;gap:8px;margin-top:12px">${warningHtml}</div>` : ''}
      </div>
      <div class="live-side">
        <div class="app-state ${appClass}"><span class="dot"></span> Antigravity: ${appState}</div>
        <details class="app-details" data-key="app-details"><summary>Uygulama yolları</summary><div class="app-meta">${esc(app.binary || '')}<br>${esc(app.user_data_dir || '')}</div></details>
        <div class="btn-row">${appButtons}${refreshBtn}${captureBtn}${authActions}</div>
      </div>`);
  }

  function renderAccounts(d) {
    const grid = $('#accounts');
    const focused = grid.contains(document.activeElement) ? document.activeElement : null;
    const all = d.accounts || [];
    const query = state.search.trim().toLocaleLowerCase('tr-TR');
    const accounts = all.filter((a) => {
      if (query && !`${a.label} ${a.name} ${a.email} ${a.account_name || ''}`.toLocaleLowerCase('tr-TR').includes(query)) return false;
      const pct = worstPct(a);
      if (state.filter === 'attention') return a.needs_login || a.quota?.needs_login || a.quota?.ok === false || a.quota_stale || a.quota?.stale || (pct !== null && toneFor(pct) !== 'good') || liveAuthFor(a)?.ineligible;
      if (state.filter === 'available') return pct !== null && pct > 0 && a.quota?.ok !== false && !a.needs_login && !a.quota?.needs_login;
      if (state.filter === 'unknown') return pct === null;
      if (state.filter === 'active') return a.active;
      return true;
    });
    accounts.sort((a, b) => {
      if (state.sort === 'name') return String(a.label || a.name).localeCompare(String(b.label || b.name), 'tr');
      if (state.sort.startsWith('quota-')) {
        const ap = worstPct(a), bp = worstPct(b);
        if (ap === null || bp === null) return ap === bp ? 0 : ap === null ? 1 : -1;
        return state.sort === 'quota-desc' ? bp - ap : ap - bp;
      }
      return Number(Boolean(b.active)) - Number(Boolean(a.active)) || Number(Boolean(b.primary)) - Number(Boolean(a.primary));
    });
    $('#accounts-sub').textContent = all.length ? `${all.length} hesap · ${all.filter((a) => a.primary).length} ana · ${all.filter((a) => a.active).length} aktif` : 'Henüz hesap yok';
    $('#account-results').textContent = `${accounts.length} / ${all.length} hesap`;
    $('#empty').classList.toggle('hidden', all.length > 0);
    $('#no-results').classList.toggle('hidden', !all.length || accounts.length > 0);
    const names = new Set(all.map((a) => a.name));
    const visible = new Set(accounts.map((a) => a.name));
    for (const [name, card] of state.cards) {
      if (!names.has(name)) { card.element.remove(); state.cards.delete(name); }
      else card.element.hidden = !visible.has(name);
    }
    let cursor = grid.firstElementChild;
    for (const account of accounts) {
      const signature = stateSignature([account, liveAuthFor(account), d.settings?.warn_percent, d.settings?.critical_percent]);
      let card = state.cards.get(account.name);
      if (!card || card.signature !== signature) {
        const template = document.createElement('template');
        template.innerHTML = accountCard(account).trim();
        const next = template.content.firstElementChild;
        if (card) patchNode(card.element, next);
        else { card = { element: next }; state.cards.set(account.name, card); }
        card.signature = signature;
      }
      card.element.hidden = false;
      if (card.element !== cursor) grid.insertBefore(card.element, cursor);
      cursor = card.element.nextElementSibling;
    }
    if (focused?.isConnected && !focused.closest('[hidden], [inert]') && document.activeElement !== focused) focused.focus({ preventScroll: true });
    syncBusy();
  }

  function accountCard(a) {
    const q = a.quota;
    const groups = q?.groups || [];
    const knownModels = groups.filter((model) => modelPct(model) !== null);
    const worstModel = knownModels.reduce((worst, model) => !worst || model.remaining < worst.remaining ? model : worst, null);
    const pct = modelPct(worstModel);
    const auth = liveAuthFor(a);
    const eligibility = auth?.ineligible === true ? 'Uygun değil' : auth?.eligible === true && auth?.has_valid_auth === true ? 'Doğrulandı' : 'Bilinmiyor';
    const tokenStatus = a.needs_login || q?.needs_login ? 'Giriş gerekli' : auth?.has_valid_auth === true ? 'Geçerli (canlı)' : 'Doğrulanmadı';
    const badges = [];
    if (a.primary) badges.push('<span class="badge badge-primary">Ana</span>');
    if (a.active) badges.push('<span class="badge badge-active">Aktif</span>');
    if (auth?.ineligible) badges.push('<span class="badge badge-login">Antigravity uygun değil</span>');
    const plan = q?.plan_name || a.plan_name;
    if (plan) badges.push(`<span class="badge badge-plan">${esc(plan)}</span>`);
    if (a.needs_login || q?.needs_login) badges.push('<span class="badge badge-login">Giriş gerekli</span>');
    if (q?.stale || a.quota_stale) badges.push('<span class="badge badge-stale">Önbellek</span>');
    if (q && q.ok === false) badges.push('<span class="badge badge-login">Hata</span>');
    if (knownModels.some((model) => model.remaining === 0)) badges.push('<span class="badge badge-exhausted">Doldu</span>');

    let quotaHtml = '';
    if (q) {
      const totalModels = q.raw_model_count ?? groups.length;
      const resetIso = worstModel?.reset_at || null;
      const coverage = `${knownModels.length}/${totalModels} modelin kotası biliniyor`;
      quotaHtml += `
        <div class="quota-hero${pct === null ? ' quota-hero-unknown' : ''}" data-key="quota-hero">
          <div class="quota-value ${toneClass(pct)}">${pct === null ? 'Kota bildirilmedi' : fmtPct(pct)}</div>
          <div class="quota-caption">${worstModel ? `<span>En düşük model kotası</span><b>${esc(worstModel.name)}</b>` : ''}<span class="quota-coverage">${esc(coverage)}</span>${resetIso ? `<span class="reset">Yenilenme: <span data-reset="${esc(resetIso)}" title="${esc(fmtDateTime(resetIso))}">${esc(fmtCountdown(resetIso))}</span></span>` : ''}</div>
        </div>`;
      if (pct !== null) quotaHtml += `<div class="bar" data-key="quota-bar" role="img" aria-label="${esc(worstModel.name)} kalan kota: ${esc(fmtPct(pct))}"><i class="${fillClass(pct)}" style="width:${Math.max(0, Math.min(100, pct))}%"></i></div>`;
      if (q.error) quotaHtml += `<div class="error-box quota-error">${esc(q.error)}</div>`;
      if (groups.length) {
        const families = [['argon', 'Argon'], ['gemini', 'Gemini'], ['claude', 'Claude'], ['gpt', 'GPT'], ['other', 'Diğer']];
        quotaHtml += `<details class="quota-details" data-key="quota-details" open><summary>Model kotaları <span>${groups.length} model</span></summary><div class="model-families">${families.map(([family, label]) => {
          const models = groups.filter((model) => (families.some(([key]) => key === model.family) ? model.family : 'other') === family);
          if (!models.length) return '';
          return `<details class="model-family" data-key="family:${family}" open><summary>${label} <span>${models.length} model</span></summary><div class="groups">${models.map((model) => {
            const remainingPct = modelPct(model);
            const modelId = model.model_id || '';
            return `
              <div class="group" data-key="${esc(model.id || modelId || model.name)}">
                <div class="group-top"><span class="group-name" title="${esc(modelId ? `Model ID: ${modelId}` : model.name)}">${esc(model.name)}${modelId ? `<span class="sr-only"> · Model ID: ${esc(modelId)}</span>` : ''}</span><span class="group-value ${toneClass(remainingPct)}"${remainingPct === null ? '' : ` title="Kalan oran: ${esc(model.remaining)}"`}>${remainingPct === null ? 'Kota bildirilmedi' : fmtPct(remainingPct)}</span></div>
                <div class="group-meta">${model.reset_at ? `<span>Yenilenme: <time datetime="${esc(model.reset_at)}" title="${esc(model.reset_at)}">${esc(fmtDateTime(model.reset_at))}</time> · <span data-reset="${esc(model.reset_at)}">${esc(fmtCountdown(model.reset_at))}</span></span>` : '<span>Yenilenme zamanı bildirilmedi</span>'}</div>
              </div>`;
          }).join('')}</div></details>`;
        }).join('')}</div></details>`;
      } else if (q.ok) {
        quotaHtml += `<div class="quota-empty"><span>Model kotası bulunamadı.</span></div>`;
      }
      if (q.credits_prompt != null || q.credits_flow != null) {
        const prompt = q.credits_prompt != null ? `${fmtNum(q.credits_prompt)}${q.monthly_prompt ? ' / ' + fmtNum(q.monthly_prompt) : ''}` : '—';
        const flow = q.credits_flow != null ? `${fmtNum(q.credits_flow)}${q.monthly_flow ? ' / ' + fmtNum(q.monthly_flow) : ''}` : '—';
        quotaHtml += `<div class="credits"><span class="credit" title="Kalan / aylık">Prompt <b>${prompt}</b></span><span class="credit" title="Kalan / aylık">Flow <b>${flow}</b></span></div>`;
      }
    } else {
      quotaHtml = `<div class="quota-empty"><span>Kota henüz okunmadı. “Kotayı oku” ile güncelleyin.</span></div>`;
    }

    const disabledSwitch = a.active;
    const disabledDelete = a.primary || a.active;
    const deleteTitle = a.primary ? 'Ana hesap silinemez' : a.active ? 'Aktif hesap silinemez; önce başka hesaba geçin' : 'Profili sil';
    const last = q?.fetched_at ? `son: ${relTime(q.fetched_at)}` : (a.last_used ? `son kullanım: ${relTime(a.last_used)}` : '');

    return `
      <article data-key="${esc(a.name)}" class="account-card${a.active ? ' is-active' : ''}${a.primary ? ' is-primary' : ''}">
        <div class="account-head" data-key="account-head">
          <div class="account-title">
            <h3>${esc(a.label)}</h3>
            ${a.account_name ? `<div class="login-name">${esc(a.account_name)}</div>` : ''}
            <div class="account-email">${esc(a.email || '—')}</div>
          </div>
          <div class="badges">${badges.join('')}</div>
        </div>
        <div class="account-health" data-key="account-health"><span>Token: <b>${esc(tokenStatus)}</b></span><span>Antigravity: <b>${esc(eligibility)}</b></span></div>
        ${quotaHtml}
        <div class="account-foot" data-key="account-foot">
          <div class="account-actions">
            <button class="btn btn-small ${a.active ? '' : 'btn-primary'}" data-action="switch" data-name="${esc(a.name)}" ${disabledSwitch ? 'disabled' : ''}>${a.active ? 'Aktif' : 'Geç'}</button>
            <button class="btn btn-small btn-ghost" data-action="refresh-one" data-name="${esc(a.name)}">Kotayı oku</button>
          </div>
          <details class="secondary-actions" data-key="secondary-actions"><summary>Diğer işlemler</summary><div class="account-actions">
            ${a.primary ? '' : `<button class="btn btn-small btn-ghost" data-action="set-primary" data-name="${esc(a.name)}">Ana yap</button>`}
            <button class="btn btn-small btn-ghost" data-action="rename" data-name="${esc(a.name)}">Yeniden adlandır</button>
            <button class="btn btn-small btn-danger" data-action="delete" data-name="${esc(a.name)}" ${disabledDelete ? 'disabled' : ''} title="${esc(deleteTitle)}">Sil</button>
          </div></details>
          <span class="account-note">${esc(last)}</span>
        </div>
      </article>`;
  }

  function renderAdd(d) {
    const live = d.live || {};
    const hint = $('#add-hint');
    const nameInput = $('#capture-name');
    const labelInput = $('#capture-label');
    const submit = $('#capture-btn');
    const known = live.known_profile ? accountFor(live.known_profile) : null;
    const loginBusy = Boolean(d.login?.running);
    const loginBtn = $('#login-btn');
    if (loginBtn) loginBtn.disabled = state.busy || loginBusy;
    if (!live.available) {
      hint.innerHTML = 'Antigravity\'de oturum yok. Önce uygulamayı açıp giriş yapın.';
      submit.disabled = true;
      return;
    }
    submit.disabled = state.busy || loginBusy;
    if (known) {
      hint.innerHTML = `Canlı hesap <b>${esc(live.email)}</b> zaten <b>${esc(known.label)}</b> olarak kayıtlı. “Yakala” token'ı tazeler.`;
      submit.textContent = 'Bu hesabı güncelle';
      if (!state.manualDirty.has('capture-name') && document.activeElement !== nameInput) nameInput.value = known.name;
      if (!state.manualDirty.has('capture-label') && document.activeElement !== labelInput) labelInput.value = known.label;
    } else {
      hint.innerHTML = `Canlı hesap <b>${esc(live.email || 'bilinmiyor')}</b> henüz kayıtlı değil. Bir profil adı verip yakalayın.`;
      submit.textContent = 'Yakala';
      if (!state.manualDirty.has('capture-name') && document.activeElement !== nameInput) nameInput.value = suggestName(live.email);
      if (!state.manualDirty.has('capture-label') && document.activeElement !== labelInput) labelInput.value = '';
    }
  }

  function suggestName(email) {
    const base = String(email || '').split('@')[0].toLowerCase()
      .replace(/[^a-z0-9._-]/g, '-')
      .replace(/-+/g, '-')
      .replace(/^[-.]+|[-.]+$/g, '')
      .slice(0, 40);
    if (base) return base;
    return `hesap${((state.data?.accounts || []).length + 1)}`;
  }

  function updateCountdowns() {
    $$('[data-reset]').forEach((el) => {
      const value = el.getAttribute('data-reset');
      if (!value) return;
      el.textContent = fmtCountdown(value);
    });
  }

  function tickMeta() {
    if (!state.data || state.data.refresh?.running) return;
    const el = $('#stat-refresh');
    if (!el) return;
    const newest = newestFetch(state.data);
    el.textContent = newest ? `Son güncelleme ${relTime(newest)}` : 'Henüz kota yok';
  }

  // ---------- tarayıcı bildirimi ----------

  function maybeBrowserNotify(d) {
    if (!state.browserNotify || !('Notification' in window) || Notification.permission !== 'granted') return;
    const critical = (d.alerts || []).filter((a) => a.level === 'critical' || a.level === 'empty');
    if (!critical.length) {
      localStorage.removeItem(STORAGE_NOTIFIED);
      return;
    }
    const sig = critical.map((a) => `${a.account}|${a.group_id}|${a.reset_at || ''}`).join(';');
    if (!sig || localStorage.getItem(STORAGE_NOTIFIED) === sig) return;
    localStorage.setItem(STORAGE_NOTIFIED, sig);
    const first = critical[0];
    try {
      const notification = new Notification(`Antigravity: ${first.label}`, {
        body: critical.map((a) => `${a.group_name}: ${a.remaining_label}`).join('\n'),
        tag: 'aio-quota',
        renotify: true,
      });
      notification.onclick = () => { window.focus(); notification.close(); };
    } catch (err) {
      // Tarayıcı bildirimi desteklenmiyorsa sessizce geç.
    }
  }

  // ---------- eylemler ----------

  function openModal({ title, subtitle = '', body = '', actions = [], wide = false }) {
    return new Promise((resolve) => {
      const root = $('#modal-root');
      if (root.childElementCount) { resolve(null); return; }
      const previousFocus = document.activeElement;
      const actionHtml = actions.map((action, index) => `
        <button class="btn ${action.primary ? 'btn-primary' : action.danger ? 'btn-danger' : 'btn-ghost'}" data-modal-action="${index}">${esc(action.label)}</button>
      `).join('');
      root.innerHTML = `
        <div class="modal-backdrop">
          <div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1">
            <div class="modal-head">
              <div><h2 id="modal-title">${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ''}</div>
              <button class="modal-close" aria-label="Kapat">×</button>
            </div>
            <div class="modal-body">${body}</div>
            <div class="modal-actions">${actionHtml}</div>
          </div>
        </div>`;
      document.body.classList.add('modal-open');
      $('.shell').inert = true;
      const backdrop = $('.modal-backdrop', root);
      const modal = $('.modal', root);
      const close = (value) => {
        document.body.classList.remove('modal-open');
        $('.shell').inert = false;
        root.innerHTML = '';
        document.removeEventListener('keydown', onKey);
        if (previousFocus?.isConnected && !previousFocus.closest('[inert]')) previousFocus.focus();
        resolve(value);
      };
      const onKey = (event) => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(null); return; }
        if (event.key !== 'Tab') return;
        const targets = $$('button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]', modal);
        const first = targets[0], last = targets[targets.length - 1];
        if (!first) { event.preventDefault(); modal.focus(); return; }
        if (event.shiftKey && (document.activeElement === first || document.activeElement === modal)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      };
      document.addEventListener('keydown', onKey);
      backdrop.addEventListener('click', (event) => {
        if (event.target === backdrop) close(null);
      });
      $('.modal-close', root).addEventListener('click', () => close(null));
      $$('[data-modal-action]', root).forEach((button) => {
        button.addEventListener('click', () => {
          const action = actions[Number(button.dataset.modalAction)];
          let value;
          try {
            value = typeof action.onClick === 'function' ? action.onClick(modal) : action.value;
          } catch (err) {
            toast(err.message || 'İşlem tamamlanamadı.', 'error');
            return;
          }
          if (value === undefined) return;
          close(value);
        });
      });
      window.setTimeout(() => {
        if (!modal.isConnected) return;
        const focusTarget = $('input, button.btn-primary, button', modal);
        (focusTarget || modal).focus();
      }, 30);
    });
  }

  async function confirmAction({ title, subtitle, body, actionLabel = 'Devam', danger = false }) {
    return openModal({
      title,
      subtitle,
      body: `<p>${body}</p>`,
      actions: [
        { label: 'Vazgeç', value: false },
        { label: actionLabel, value: true, primary: !danger, danger },
      ],
    });
  }

  async function refreshAll() {
    if (state.data?.login?.running) {
      toast('Antigravity hesap girişi sürüyor; bitmesini bekleyin.', 'info');
      return;
    }
    if (state.busy || state.data?.refresh?.running) {
      toast('Kota yenileme zaten sürüyor.', 'info');
      return;
    }
    const d = state.data;
    const running = Boolean(d?.app?.running);
    const many = (d?.accounts || []).length > 1;
    let stop = false;
    if (running && many) {
      const ok = await confirmAction({
        title: 'Tüm kotaları yenile',
        subtitle: 'Antigravity açık',
        body: 'Tüm hesapların kotasını okumak için Antigravity kapatılmalı. Uygulama kapatılıp keyring sırayla taranacak. Devam edilsin mi?',
        actionLabel: 'Kapat ve yenile',
      });
      if (!ok) return;
      stop = true;
    }
    setBusy(true);
    try {
      if (stop) {
        await api('/api/app/stop', { method: 'POST', body: JSON.stringify({ force: false }) });
      }
      const result = await api('/api/quota/refresh', {
        method: 'POST',
        body: JSON.stringify({ all: true, force: true }),
      });
      if (result.state) {
        state.data = result.state;
        render();
      }
      schedulePoll(2200);
      toast('Kota taraması başladı. Antigravity kapalı kalacak; bitince açabilirsiniz.', 'info', 6500);
    } catch (err) {
      if (err.payload?.needs_stop) {
        toast('Antigravity kapatılamadı veya hâlâ açık. Önce kapatıp tekrar deneyin.', 'error');
      } else {
        toast(err.message, 'error');
      }
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function refreshOne(name) {
    if (state.data?.login?.running) {
      toast('Antigravity hesap girişi sürüyor; bitmesini bekleyin.', 'info');
      return;
    }
    if (state.busy || state.data?.refresh?.running) return;
    setBusy(true);
    try {
      const result = await api('/api/quota/refresh', {
        method: 'POST',
        body: JSON.stringify({ name, force: true }),
      });
      if (result.state) {
        state.data = result.state;
        render();
      }
      toast(`${labelFor(name)} kotası güncellendi.`, 'success');
    } catch (err) {
      if (err.payload?.needs_stop) {
        const ok = await confirmAction({
          title: 'Antigravity kapatılsın mı?',
          body: 'Bu hesabın kotasını okumak için Antigravity kapatılmalı. Kapatıp devam edelim mi?',
          actionLabel: 'Kapat ve oku',
        });
        if (ok) {
          try {
            await api('/api/app/stop', { method: 'POST', body: JSON.stringify({ force: false }) });
            const result = await api('/api/quota/refresh', { method: 'POST', body: JSON.stringify({ name, force: true }) });
            if (result.state) { state.data = result.state; render(); }
            toast(`${labelFor(name)} kotası güncellendi.`, 'success');
            return;
          } catch (stopErr) {
            toast(stopErr.message, 'error');
          }
        }
      } else {
        toast(err.message, 'error');
      }
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function switchAccount(name) {
    if (state.data?.login?.running) {
      toast('Antigravity hesap girişi sürüyor; bitmesini bekleyin.', 'info');
      return;
    }
    if (state.busy) return;
    const account = accountFor(name);
    if (!account) return;
    const running = Boolean(state.data?.app?.running);
    const launch = Boolean(state.data?.settings?.launch_after_switch);
    const ok = await confirmAction({
      title: `${account.label} hesabına geç`,
      subtitle: account.email,
      body: running
        ? `Antigravity kapatılacak, keyring bu hesaba yazılacak${launch ? ' ve uygulama yeniden açılacak' : ''}. Skills, MCP ve settings dosyaları değişmez.`
        : `Canlı keyring bu hesaba yazılacak${launch ? ' ve Antigravity açılacak' : ''}. Skills, MCP ve settings dosyaları değişmez.`,
      actionLabel: running ? 'Kapat ve geç' : 'Geç',
    });
    if (!ok) return;
    setBusy(true);
    try {
      const result = await api(`/api/accounts/${encodeURIComponent(name)}/switch`, {
        method: 'POST',
        body: JSON.stringify({ stop: running, launch, verify: true }),
      });
      if (result.state) {
        state.data = result.state;
        render();
      }
      toast(result.warning || `Geçildi: ${result.email}`, result.warning ? 'info' : 'success', 6000);
    } catch (err) {
      toast(err.message, 'error', 7000);
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function captureCurrent(event) {
    if (event) event.preventDefault();
    if (state.data?.login?.running) {
      toast('Antigravity hesap girişi sürüyor; bitmesini bekleyin.', 'info');
      return;
    }
    if (state.busy) return;
    const live = state.data?.live || {};
    if (!live.available) {
      toast('Antigravity\'de aktif oturum yok.', 'error');
      return;
    }
    const name = $('#capture-name').value.trim();
    const label = $('#capture-label').value.trim();
    if (!name) {
      toast('Profil adı gerekli.', 'error');
      $('#capture-name').focus();
      return;
    }
    const known = live.known_profile;
    const overwrite = Boolean(known && known === name);
    setBusy(true);
    try {
      const result = await api('/api/accounts/capture', {
        method: 'POST',
        body: JSON.stringify({ name, label: label || null, overwrite, primary: false }),
      });
      if (result.state) {
        state.data = result.state;
        render();
      }
      toast(result.message || 'Hesap kaydedildi.', 'success');
    } catch (err) {
      if (err.payload?.existing) {
        toast(`Bu hesap zaten “${err.payload.existing}” olarak kayıtlı.`, 'error', 6500);
      } else {
        toast(err.message, 'error');
      }
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function renameAccount(name) {
    const account = accountFor(name);
    if (!account) return;
    const value = await openModal({
      title: 'Profili yeniden adlandır',
      subtitle: account.email,
      body: `<label>Yeni ad<input id="rename-input" type="text" maxlength="64" value="${esc(account.name)}" autocomplete="off"></label>`,
      actions: [
        { label: 'Vazgeç', value: null },
        {
          label: 'Kaydet',
          primary: true,
          onClick: (modal) => {
            const next = $('#rename-input', modal).value.trim();
            if (!next) {
              toast('Yeni ad boş olamaz.', 'error');
              return undefined;
            }
            return next;
          },
        },
      ],
    });
    if (!value || value === name) return;
    setBusy(true);
    try {
      const result = await api(`/api/accounts/${encodeURIComponent(name)}/rename`, {
        method: 'POST',
        body: JSON.stringify({ new_name: value }),
      });
      if (result.state) state.data = result.state;
      toast(`Yeniden adlandırıldı: ${name} → ${value}`, 'success');
      render();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function deleteAccount(name) {
    const account = accountFor(name);
    if (!account) return;
    if (account.primary || account.active) {
      toast(account.primary ? 'Ana hesap silinemez.' : 'Aktif hesap silinemez; önce başka hesaba geçin.', 'error');
      return;
    }
    const ok = await confirmAction({
      title: 'Profili sil',
      subtitle: account.email,
      body: `“${account.label}” profili ve keyring kaydı silinecek. Bu işlem geri alınamaz.`,
      actionLabel: 'Sil',
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      const result = await api(`/api/accounts/${encodeURIComponent(name)}/delete`, {
        method: 'POST',
        body: JSON.stringify({}),
      });
      if (result.state) state.data = result.state;
      toast('Profil silindi.', 'success');
      render();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function setPrimary(name) {
    const account = accountFor(name);
    if (!account) return;
    const ok = await confirmAction({
      title: 'Ana hesabı değiştir',
      subtitle: account.email,
      body: `“${account.label}” ana hesap olacak. Ana hesap korumalıdır ve yanlışlıkla silinemez.`,
      actionLabel: 'Ana yap',
    });
    if (!ok) return;
    setBusy(true);
    try {
      const result = await api(`/api/accounts/${encodeURIComponent(name)}/primary`, {
        method: 'POST',
        body: JSON.stringify({}),
      });
      if (result.state) state.data = result.state;
      toast(`Ana hesap: ${account.label}`, 'success');
      render();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function appAction(action) {
    if (state.data?.login?.running && action === 'app-stop') {
      toast('Giriş sürerken Antigravity kapatılamaz.', 'info');
      return;
    }
    if (state.busy) return;
    if (action === 'app-stop') {
      const ok = await confirmAction({
        title: 'Antigravity kapatılsın mı?',
        body: 'Açık Antigravity oturumu kapatılacak. Devam edilsin mi?',
        actionLabel: 'Kapat',
        danger: true,
      });
      if (!ok) return;
    }
    setBusy(true);
    try {
      const result = await api(`/api/${action === 'app-stop' ? 'app/stop' : 'app/start'}`, {
        method: 'POST',
        body: JSON.stringify(action === 'app-stop' ? { force: false } : {}),
      });
      if (result.state) state.data = result.state;
      toast(action === 'app-stop' ? 'Antigravity kapatıldı.' : 'Antigravity başlatıldı.', 'success');
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function startLogin() {
    if (state.busy) return;
    if (state.data?.login?.running) {
      toast('Antigravity girişi zaten sürüyor.', 'info');
      return;
    }
    setBusy(true);
    try {
      const result = await api('/api/accounts/login/start', { method: 'POST', body: '{}' });
      if (result.state) state.data = result.state;
      render();
      schedulePoll(2200);
      toast('Giriş başlatıldı. Açılan tarayıcıda Google hesabını seç.', 'info', 7000);
    } catch (err) {
      toast(err.message, 'error', 7000);
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function resetLogin() {
    if (state.busy) return;
    setBusy(true);
    try {
      const result = await api('/api/accounts/login/reset', { method: 'POST', body: '{}' });
      if (result.state) state.data = result.state;
      render();
      toast('Giriş durumu temizlendi.', 'info');
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
      load(true);
    }
  }

  async function openAccountAdd() {
    const choice = await openModal({
      title: 'Yeni hesap ekle',
      subtitle: 'Antigravity üzerinden giriş',
      body: `
        <p>Antigravity'nin kendi Google giriş akışı açılacak. Tarayıcıda eklemek istediğin hesabı seç; giriş tamamlanınca token otomatik kaydedilir.</p>
        <div class="modal-note">Mevcut hesabından çıkış yapılmaz. Ana hesabın kasada korunur. Varsayılan hesap otomatik seçilirse tarayıcıda <b>“Başka hesap kullan”</b> seçeneğini kullan.</div>`,
      actions: [
        { label: 'Vazgeç', value: null },
        { label: 'Mevcut canlıyı yakala', value: 'manual' },
        { label: 'Antigravity ile giriş yap', value: 'login', primary: true },
      ],
    });
    if (choice === 'login') return startLogin();
    if (choice === 'manual') {
      $('#manual-capture').open = true;
      $('#add-panel').scrollIntoView({ behavior: 'smooth', block: 'center' });
      window.setTimeout(() => $('#capture-name').focus(), 260);
    }
  }

  async function openSettings() {
    const s = state.data?.settings || {};
    const permission = ('Notification' in window) ? Notification.permission : 'unsupported';
    const value = await openModal({
      title: 'Ayarlar',
      subtitle: 'Kota eşikleri ve yenileme davranışı',
      wide: true,
      body: `
        <div class="modal-grid">
          <label>Uyarı eşiği (%)
            <input id="set-warn" type="number" min="1" max="95" step="1" value="${esc(s.warn_percent ?? 20)}">
          </label>
          <label>Kritik eşik (%)
            <input id="set-critical" type="number" min="0" max="90" step="1" value="${esc(s.critical_percent ?? 5)}">
          </label>
          <label>Yenileme aralığı (sn)
            <input id="set-poll" type="number" min="30" max="86400" step="30" value="${esc(s.poll_seconds ?? 300)}">
          </label>
          <label>Tümünü yenileme aralığı (sn)
            <input id="set-all-poll" type="number" min="60" max="86400" step="60" value="${esc(s.all_poll_seconds ?? 900)}">
          </label>
        </div>
        <label class="check"><input id="set-notify" type="checkbox" ${s.desktop_notifications ? 'checked' : ''}> Masaüstü bildirimi gönder (notify-send)</label>
        <label class="check"><input id="set-launch" type="checkbox" ${s.launch_after_switch ? 'checked' : ''}> Geçişten sonra Antigravity'yi aç</label>
        <label class="check"><input id="set-auto-all" type="checkbox" ${s.auto_refresh_all ? 'checked' : ''}> Arka planda tüm hesapları da yenile (Antigravity kapalıyken)</label>
        <div class="modal-note">
          Tarayıcı bildirimi: <b>${esc(permission)}</b>.
          <button type="button" class="btn btn-small btn-ghost" id="notify-permission">${permission === 'granted' ? 'Test bildirimi' : 'İzin ver'}</button>
        </div>
        <div class="modal-note">Skills, MCP ve settings dosyaları hiçbir zaman değiştirilmez. Token'lar yalnızca Secret Service'te tutulur.</div>`,
      actions: [
        { label: 'Vazgeç', value: null },
        {
          label: 'Kaydet',
          primary: true,
          onClick: (modal) => {
            const warn = Number($('#set-warn', modal).value);
            const critical = Number($('#set-critical', modal).value);
            const poll = Number($('#set-poll', modal).value);
            const allPoll = Number($('#set-all-poll', modal).value);
            if (!Number.isFinite(warn) || warn < 1 || warn > 95) {
              toast('Uyarı eşiği 1–95 arasında olmalı.', 'error');
              return undefined;
            }
            if (!Number.isFinite(critical) || critical < 0 || critical > warn) {
              toast('Kritik eşik 0 ile uyarı eşiği arasında olmalı.', 'error');
              return undefined;
            }
            if (!Number.isFinite(poll) || poll < 30 || poll > 86400) {
              toast('Yenileme aralığı 30–86400 saniye arasında olmalı.', 'error');
              return undefined;
            }
            if (!Number.isFinite(allPoll) || allPoll < 60 || allPoll > 86400) {
              toast('Toplu yenileme aralığı 60–86400 saniye arasında olmalı.', 'error');
              return undefined;
            }
            return {
              warn_percent: warn,
              critical_percent: critical,
              poll_seconds: Math.round(poll),
              all_poll_seconds: Math.round(allPoll),
              desktop_notifications: $('#set-notify', modal).checked,
              launch_after_switch: $('#set-launch', modal).checked,
              auto_refresh_all: $('#set-auto-all', modal).checked,
            };
          },
        },
      ],
    });
    if (value) {
      setBusy(true);
      try {
        const result = await api('/api/settings', {
          method: 'PUT',
          body: JSON.stringify(value),
        });
        if (result.state) state.data = result.state;
        toast('Ayarlar kaydedildi.', 'success');
        render();
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        setBusy(false);
        load(true);
      }
    }
  }

  // Modal içindeki izin düğmesi: modal kapanmadan tıklanabilsin diye
  // delegasyonla yakalanır.
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('#notify-permission');
    if (!button) return;
    event.preventDefault();
    if (!('Notification' in window)) {
      toast('Bu tarayıcı bildirimleri desteklemiyor.', 'error');
      return;
    }
    if (Notification.permission === 'granted') {
      state.browserNotify = true;
      localStorage.setItem(STORAGE_BROWSER_NOTIFY, '1');
      try {
        new Notification('all-in-one', { body: 'Tarayıcı bildirimleri çalışıyor.' });
      } catch (err) { /* yok say */ }
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission === 'granted') {
      state.browserNotify = true;
      localStorage.setItem(STORAGE_BROWSER_NOTIFY, '1');
      toast('Tarayıcı bildirimleri açıldı.', 'success');
      button.textContent = 'Test bildirimi';
    } else {
      toast('Bildirim izni verilmedi.', 'info');
    }
  });

  // ---------- olaylar ----------

  document.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-action]');
    if (!target) return;
    const action = target.dataset.action;
    const name = target.dataset.name;
    if (action === 'dismiss-alerts') {
      const sig = alertSignature(state.data?.alerts || []);
      state.dismissedSig = sig;
      sessionStorage.setItem('aio.dismissedAlerts', sig);
      renderAlerts(state.data);
      return;
    }
    if (state.busy || state.data?.login?.running || state.data?.refresh?.running || target.disabled) return;
    if (action === 'switch') { event.preventDefault(); await switchAccount(name); return; }
    if (action === 'refresh-one') { event.preventDefault(); await refreshOne(name); return; }
    if (action === 'refresh-active') {
      event.preventDefault();
      const active = state.data?.active;
      if (active) await refreshOne(active);
      else await refreshAll();
      return;
    }
    if (action === 'set-primary') { event.preventDefault(); await setPrimary(name); return; }
    if (action === 'rename') { event.preventDefault(); await renameAccount(name); return; }
    if (action === 'delete') { event.preventDefault(); await deleteAccount(name); return; }
    if (action === 'capture-current') {
      event.preventDefault();
      $('#manual-capture').open = true;
      $('#add-panel').scrollIntoView({ behavior: 'smooth', block: 'center' });
      window.setTimeout(() => $('#capture-name').focus(), 260);
      return;
    }
    if (action === 'login-reset') {
      event.preventDefault();
      await resetLogin();
      return;
    }
    if (action === 'switch-primary') {
      event.preventDefault();
      const primary = state.data?.primary;
      if (primary) await switchAccount(primary);
      return;
    }
    if (action === 'app-start' || action === 'app-stop') {
      event.preventDefault();
      await appAction(action);
    }
  });

  $('#refresh-btn').addEventListener('click', refreshAll);
  $('#settings-btn').addEventListener('click', openSettings);
  $('#add-btn').addEventListener('click', openAccountAdd);
  $('#login-btn').addEventListener('click', startLogin);
  $('#capture-form').addEventListener('submit', captureCurrent);
  for (const id of ['capture-name', 'capture-label']) {
    $(`#${id}`).addEventListener('input', () => state.manualDirty.add(id));
  }
  for (const [id, key] of [['account-filter', 'filter'], ['account-sort', 'sort']]) {
    $(`#${id}`).addEventListener('change', (event) => {
      state[key] = event.target.value;
      if (state.data) renderAccounts(state.data);
    });
  }
  const colorPreference = window.matchMedia('(prefers-color-scheme: light)');
  function applyTheme() {
    const choice = localStorage.getItem('aio.theme') || 'auto';
    const theme = ['auto', 'light', 'dark'].includes(choice) ? choice : 'auto';
    document.documentElement.dataset.theme = theme === 'auto' ? colorPreference.matches ? 'light' : 'dark' : theme;
    $('#theme-select').value = theme;
  }
  $('#theme-select').addEventListener('change', (event) => {
    localStorage.setItem('aio.theme', event.target.value);
    applyTheme();
  });
  colorPreference.addEventListener('change', applyTheme);
  applyTheme();
  $('#search').addEventListener('input', (event) => {
    state.search = event.target.value;
    if (state.data) renderAccounts(state.data);
  });
  $('#account-select').addEventListener('change', () => {
    const active = state.data?.active;
    const select = $('#account-select');
    state.selectedAccount = select.value;
    const button = $('#switch-selected');
    button.textContent = select.value === active ? 'Aktif' : 'Geç';
    syncBusy();
  });
  $('#switch-selected').addEventListener('click', () => {
    const name = $('#account-select').value;
    if (name) switchAccount(name);
  });

  document.addEventListener('keydown', (event) => {
    if ($('#modal-root').childElementCount || event.ctrlKey || event.altKey || event.metaKey) return;
    const tag = (event.target?.tagName || '').toLowerCase();
    const typing = ['input', 'textarea', 'select', 'button', 'summary', 'a'].includes(tag) || event.target?.isContentEditable;
    if (event.key === 'Escape' && state.search) {
      state.search = '';
      $('#search').value = '';
      if (state.data) renderAccounts(state.data);
      return;
    }
    if (typing) return;
    if (event.key === 'r' || event.key === 'R') { event.preventDefault(); refreshAll(); }
    if (event.key === 'n' || event.key === 'N') {
      event.preventDefault();
      $('#manual-capture').open = true;
      $('#add-panel').scrollIntoView({ behavior: 'smooth', block: 'center' });
      window.setTimeout(() => $('#capture-name').focus(), 200);
    }
    if (event.key === '/') {
      event.preventDefault();
      $('#search').focus();
    }
  });

  document.addEventListener('visibilitychange', () => {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    if (!document.hidden) load(true);
  });

  load(false);
})();
