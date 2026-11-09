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

  // ---------- ikonlar (Lucide, ISC lisansı; CSP nedeniyle satır içi) ----------

  const ICONS = {
    layers: '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    settings: '<path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
    plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
    search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    monitor: '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/>',
    more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    moreV: '<circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    swap: '<path d="m16 3 4 4-4 4"/><path d="M20 7H4"/><path d="m8 21-4-4 4-4"/><path d="M4 17h16"/>',
    power: '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
    play: '<polygon points="6 3 20 12 6 21 6 3"/>',
    star: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
    pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    login: '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="m10 17 5-5-5-5"/><path d="M15 12H3"/>',
    external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    userPlus: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6"/><path d="M22 11h-6"/>',
    gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    sort: '<path d="m3 16 4 4 4-4"/><path d="M7 20V4"/><path d="M11 4h10"/><path d="M11 8h7"/><path d="M11 12h4"/>',
  };

  function icon(name, cls = '') {
    return `<svg class="icon${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICONS[name] || ''}</svg>`;
  }

  function hydrateIcons(root = document) {
    $$('i[data-icon]', root).forEach((el) => { el.outerHTML = icon(el.dataset.icon); });
  }

  function initials(account) {
    const source = String(account?.label || account?.name || account?.email || '?').trim();
    const parts = source.split(/[\s._@-]+/).filter(Boolean);
    const letters = parts.length > 1 ? parts[0][0] + parts[1][0] : source.slice(0, 2);
    return letters.toLocaleUpperCase('tr-TR');
  }

  function hueFor(value) {
    let hash = 0;
    for (const ch of String(value || '')) hash = (hash * 31 + ch.codePointAt(0)) % 360;
    return hash;
  }

  function avatar(account, size = '') {
    return `<span class="avatar${size ? ' ' + size : ''}" style="--h:${hueFor(account?.email || account?.name)}" aria-hidden="true">${esc(initials(account))}</span>`;
  }

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
    el.innerHTML = `${icon(type === 'error' ? 'alert' : type === 'success' ? 'check' : 'info')}<span></span>`;
    el.lastElementChild.textContent = message;
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
    updateCountdowns();
    tickMeta();
    syncBusy();
    const footer = $('#footer-version');
    if (footer) footer.textContent = d.version ? `Sunucu v${d.version}` : 'Sunucu sürümü bilinmiyor';
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
    let mark = '<span class="spinner"></span>';
    let title = 'Giriş bekleniyor';
    let text = li.message || '';
    let sub = '';
    let action = '';
    if (li.running) {
      title = li.status === 'starting' ? 'Antigravity hazırlanıyor' : 'Tarayıcıda giriş bekleniyor';
      sub = li.before_email ? `Mevcut canlı hesap: ${li.before_email}` : 'Henüz canlı hesap yok.';
      if (li.auth_url) {
        action = `<a class="btn btn-small btn-ghost" href="${esc(li.auth_url)}" target="_blank" rel="noreferrer">${icon('external')}<span>Giriş sayfasını aç</span></a>`;
      }
    } else if (li.status === 'success' || li.status === 'success_same') {
      cls += ' success';
      mark = icon('check');
      title = li.status === 'success' ? 'Hesap eklendi' : 'Aynı hesap seçildi';
      sub = li.captured_email ? `Kayıtlı profil: ${li.captured_email}` : '';
      action = '<button class="btn btn-small" data-action="login-reset">Tamam</button>';
    } else {
      cls += ' error';
      mark = icon('alert');
      title = 'Giriş tamamlanamadı';
      sub = li.error ? li.error : '';
      action = '<button class="btn btn-small btn-ghost" data-action="login-reset">Kapat</button>';
    }
    el.className = cls;
    setHtml(el, `
      <div class="login-icon" aria-hidden="true">${mark}</div>
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

  function renderSummary(d) {
    const accounts = d.accounts || [];
    const counts = d.counts || {};
    $('#stat-accounts').textContent = fmtNum(counts.accounts ?? accounts.length);
    const primary = accounts.find((a) => a.primary);
    $('#stat-primary').textContent = primary ? `Ana hesap: ${primary.label}` : 'Ana hesap yok';


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
    el.innerHTML = items.map((item) => `<div class="warning-box">${icon('info')}<span>${esc(item)}</span></div>`).join('');
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
      btn.lastElementChild.textContent = 'Yenileniyor…';
      btn.classList.add('is-spinning');
    } else {
      progress.classList.add('hidden');
      progress.removeAttribute('aria-valuenow');
      btn.disabled = state.busy;
      btn.lastElementChild.textContent = 'Yenile';
      btn.classList.remove('is-spinning');
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
    const shown = alerts.slice(0, 6);
    const items = shown.map((a) => {
      const level = a.level === 'empty' ? 'Doldu' : a.level === 'critical' ? 'Kritik' : 'Azalıyor';
      const reset = a.reset_at ? `<span class="alert-reset">${icon('clock')}<span data-reset="${esc(a.reset_at)}">${esc(fmtCountdown(a.reset_at))}</span></span>` : '';
      return `<li class="lvl-${esc(a.level)}"><span class="alert-level">${esc(level)}</span><span class="alert-what"><b>${esc(a.label)}</b><span>${esc(a.group_name)}</span></span><span class="alert-val">${esc(a.remaining_label)}</span>${reset}</li>`;
    }).join('');
    const extra = alerts.length > shown.length ? `<li class="alert-more">+ ${alerts.length - shown.length} uyarı daha</li>` : '';
    banner.innerHTML = `
      <div class="alert-head">
        <span class="alert-icon" aria-hidden="true">${icon('alert')}</span>
        <span class="alert-title">${critical ? 'Kota kritik seviyede' : 'Kota azalıyor'}</span>
        <span class="alert-count">${alerts.length} uyarı</span>
        <button class="icon-btn alert-dismiss" data-action="dismiss-alerts" title="Gizle" aria-label="Uyarıları gizle">${icon('x')}</button>
      </div>
      <ul class="alert-list">${items}${extra}</ul>`;
  }

  function renderLive(d) {
    const panel = $('#live-panel');
    const live = d.live || {};
    const app = d.app || {};
    const running = Boolean(app.running);
    const appState = running ? `Antigravity açık${app.version ? ' · v' + app.version : ''}` : 'Antigravity kapalı';
    const appButton = running
      ? `<button class="btn btn-text" data-action="app-stop">${icon('power')}<span>Antigravity'yi kapat</span></button>`
      : `<button class="btn btn-tonal" data-action="app-start">${icon('play')}<span>Antigravity'yi aç</span></button>`;
    const refreshBtn = `<button class="btn btn-tonal" data-action="refresh-active">${icon('refresh')}<span>Aktif kotayı yenile</span></button>`;
    const appLine = `
      <div class="app-state${running ? ' open' : ''}"><span class="dot"></span>${esc(appState)}
        <details class="app-details" data-key="app-details"><summary title="Uygulama yolları">${icon('info')}<span class="sr-only">Uygulama yolları</span></summary><div class="app-meta">${esc(app.binary || 'Antigravity binary bulunamadı')}${app.user_data_dir ? `<br>${esc(app.user_data_dir)}` : ''}</div></details>
      </div>`;

    if (!live.available) {
      panel.className = 'hero';
      setHtml(panel, `
        <span class="avatar xl avatar-empty" aria-hidden="true">${icon('login')}</span>
        <h2 class="hero-title">Canlı oturum yok</h2>
        <p class="hero-sub">Antigravity'de bir Google hesabıyla giriş yapın; bu alan canlı oturumu gösterir.</p>
        ${live.error ? `<div class="error-box">${esc(live.error)}</div>` : ''}
        ${appLine}
        <div class="btn-row">${refreshBtn}${appButton}</div>`);
      return;
    }

    const known = live.known_profile;
    const knownAccount = known ? accountFor(known) : null;
    const auth = live.auth || {};
    const authIneligible = Boolean(auth.ineligible);
    const authEligible = auth.eligible === true && auth.has_valid_auth === true && !authIneligible;
    const warnings = [];
    if (!known) warnings.push('Bu canlı hesap henüz kayıtlı değil. Aşağıdan yakalayın.');
    if (known && !live.matches_active) warnings.push('Canlı hesap, aktif profille eşleşmiyor; sayfa yenilenince senkronize edilir.');
    if (live.expired) warnings.push('Access token süresi dolmuş; Antigravity açıldığında otomatik yenilenir.');
    if (authIneligible) warnings.push((auth.message || 'Antigravity bu Google hesabını uygun bulmuyor.').replace(/\s+/g, ' '));
    panel.className = `hero${warnings.length ? ' is-warning' : ''}`;

    const chips = [];
    chips.push(`<span class="chip ${known ? 'good' : 'warn'}">${known ? icon('check') : icon('alert')}${known ? `Profil: ${esc(knownAccount?.label || known)}` : 'Kayıtlı değil'}</span>`);
    chips.push(`<span class="chip ${authIneligible ? 'bad' : authEligible ? 'good' : ''}">${icon('shield')}${authIneligible ? 'Uygun değil' : authEligible ? 'Doğrulandı' : 'Uygunluk bilinmiyor'}</span>`);
    if (live.expiry) chips.push(`<span class="chip ${live.expired ? 'bad' : ''}">${icon('clock')}Token ${esc(fmtDateTime(live.expiry))}</span>`);
    if (live.auth_method) chips.push(`<span class="chip">${esc(live.auth_method)}</span>`);

    const warningHtml = warnings.map((w) => `<div class="warning-box">${icon('alert')}<span>${esc(w)}</span></div>`).join('');
    let authActions = '';
    if (authIneligible) {
      if (safeVerificationUrl(auth.verification_url)) {
        authActions += `<a data-verification-link class="btn btn-text" href="${esc(auth.verification_url)}" target="_blank" rel="noreferrer">${icon('external')}<span>Doğrulamayı aç</span></a>`;
      }
      if (d.primary && d.active !== d.primary) {
        authActions += `<button class="btn btn-tonal" data-action="switch-primary">${icon('swap')}<span>Ana hesaba geç</span></button>`;
      }
    }
    const captureBtn = !known
      ? `<button class="btn btn-filled" data-action="capture-current">${icon('download')}<span>Canlı hesabı yakala</span></button>`
      : `<button class="btn btn-text" data-action="capture-current">${icon('download')}<span>Token'ı güncelle</span></button>`;
    const name = live.account_name || knownAccount?.label || live.email || 'Bilinmeyen hesap';

    setHtml(panel, `
      <div class="hero-avatar">
        ${avatar({ email: live.email, label: live.account_name || knownAccount?.label || live.email }, 'xl')}
        <span class="live-dot${running ? ' on' : ''}" title="${running ? 'Antigravity açık' : 'Antigravity kapalı'}"></span>
      </div>
      <h2 class="hero-title">${esc(name)}</h2>
      <p class="hero-sub" title="${esc(live.email || '')}">${esc(live.email || 'E-posta okunamadı')}</p>
      <div class="chips">${chips.join('')}</div>
      ${warningHtml ? `<div class="hero-warnings">${warningHtml}</div>` : ''}
      ${appLine}
      <div class="btn-row">${captureBtn}${refreshBtn}${appButton}${authActions}</div>`);
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

  const FAMILIES = [['gemini', 'Gemini'], ['claude', 'Claude'], ['gpt', 'GPT'], ['argon', 'Argon'], ['other', 'Diğer']];

  function familyOf(model) {
    return FAMILIES.some(([key]) => key === model.family) ? model.family : 'other';
  }

  function meter(pct, label) {
    if (pct === null) return '<div class="bar bar-unknown"><i></i></div>';
    return `<div class="bar" role="img" aria-label="${esc(label)}"><i class="${fillClass(pct)}" style="width:${Math.max(pct > 0 ? 2 : 0, Math.min(100, pct))}%"></i></div>`;
  }

  function ring(pct, label) {
    const value = pct === null ? 0 : Math.max(0, Math.min(100, pct));
    return `<div class="ring ${toneClass(pct)}" role="img" aria-label="${esc(label)}">
      <svg viewBox="0 0 44 44" aria-hidden="true"><circle class="ring-track" cx="22" cy="22" r="19" pathLength="100"/>${pct === null ? '' : `<circle class="ring-fill" cx="22" cy="22" r="19" pathLength="100" stroke-dasharray="${value} 100"/>`}</svg>
      <span class="ring-val">${pct === null ? '—' : esc(fmtPct(pct))}</span>
    </div>`;
  }

  function accountCard(a) {
    const q = a.quota;
    const groups = q?.groups || [];
    const knownModels = groups.filter((model) => modelPct(model) !== null);
    const worstModel = knownModels.reduce((worst, model) => !worst || model.remaining < worst.remaining ? model : worst, null);
    const pct = modelPct(worstModel);
    const auth = liveAuthFor(a);
    const needsLogin = Boolean(a.needs_login || q?.needs_login);
    const eligibility = auth?.ineligible === true ? 'Uygun değil' : auth?.eligible === true && auth?.has_valid_auth === true ? 'Doğrulandı' : 'Bilinmiyor';
    const tokenStatus = needsLogin ? 'Giriş gerekli' : auth?.has_valid_auth === true ? 'Geçerli (canlı)' : 'Doğrulanmadı';
    const badges = [];
    if (a.active) badges.push(`<span class="badge badge-active">${icon('check')}Aktif</span>`);
    if (a.primary) badges.push(`<span class="badge badge-primary">${icon('star')}Ana</span>`);
    const plan = q?.plan_name || a.plan_name;
    if (plan) badges.push(`<span class="badge">${esc(plan)}</span>`);
    if (auth?.ineligible) badges.push('<span class="badge badge-bad">Uygun değil</span>');
    if (needsLogin) badges.push(`<span class="badge badge-bad">${icon('alert')}Giriş gerekli</span>`);
    if (q && q.ok === false) badges.push('<span class="badge badge-bad">Hata</span>');
    if (q?.stale || (q && a.quota_stale)) badges.push('<span class="badge badge-muted" title="Kota önbellekten gösteriliyor">Önbellek</span>');

    let summary;
    if (!q) {
      summary = `${ring(null, 'Kota okunmadı')}<div class="q-side"><span class="q-label">Kota okunmadı</span><span class="q-meta">“Kotayı oku” ile güncelleyin</span></div>`;
    } else if (pct === null) {
      summary = `${ring(null, 'Kota bildirilmedi')}<div class="q-side"><span class="q-label">Kota bildirilmedi</span><span class="q-meta">${knownModels.length}/${q.raw_model_count ?? groups.length} model biliniyor</span></div>`;
    } else {
      const resetIso = worstModel.reset_at;
      summary = `${ring(pct, `${worstModel.name} kalan kota: ${fmtPct(pct)}`)}
        <div class="q-side">
          <span class="q-label">En düşük kota</span>
          <span class="q-model" title="${esc(worstModel.name)}">${esc(worstModel.name)}</span>
          <span class="q-meta">${resetIso ? `${icon('clock')}<span data-reset="${esc(resetIso)}" title="${esc(fmtDateTime(resetIso))}">${esc(fmtCountdown(resetIso))}</span> sonra yenilenir` : 'Yenilenme bildirilmedi'}</span>
        </div>`;
    }

    const familyRows = FAMILIES.map(([family, label]) => {
      const models = groups.filter((model) => familyOf(model) === family);
      if (!models.length) return '';
      const known = models.map(modelPct).filter((v) => v !== null);
      const min = known.length ? Math.min(...known) : null;
      return `<div class="fam" data-key="fam:${family}" title="${esc(label)}: ${models.length} model (en düşük)"><span class="fam-name">${esc(label)}</span>${meter(min, `${label}: ${fmtPct(min)}`)}<span class="fam-val ${toneClass(min)}">${min === null ? '—' : esc(fmtPct(min))}</span></div>`;
    }).join('');

    let detail = '';
    if (q?.error) detail += `<div class="error-box">${esc(q.error)}</div>`;
    if (groups.length) {
      detail += FAMILIES.map(([family, label]) => {
        const models = groups.filter((model) => familyOf(model) === family);
        if (!models.length) return '';
        return `<div class="model-family" data-key="family:${family}"><div class="family-head">${esc(label)}</div>${models.map((model) => {
          const remainingPct = modelPct(model);
          const modelId = model.model_id || '';
          return `
            <div class="model" data-key="${esc(model.id || modelId || model.name)}">
              <div class="model-top"><span class="model-name" title="${esc(modelId ? `Model ID: ${modelId}` : model.name)}">${esc(model.name)}</span><span class="model-val ${toneClass(remainingPct)}"${remainingPct === null ? '' : ` title="Kalan oran: ${esc(model.remaining)}"`}>${remainingPct === null ? '—' : esc(fmtPct(remainingPct))}</span></div>
              ${meter(remainingPct, `${model.name}: ${fmtPct(remainingPct)}`)}
              <div class="model-meta">${model.reset_at ? `<time datetime="${esc(model.reset_at)}" title="${esc(model.reset_at)}">${esc(fmtDateTime(model.reset_at))}</time> · <span data-reset="${esc(model.reset_at)}">${esc(fmtCountdown(model.reset_at))}</span>` : 'Yenilenme bildirilmedi'}</div>
            </div>`;
        }).join('')}</div>`;
      }).join('');
    } else if (q?.ok) {
      detail += '<div class="quota-empty">Model kotası bulunamadı.</div>';
    }
    const facts = [`<span>Token<b>${esc(tokenStatus)}</b></span>`, `<span>Antigravity<b>${esc(eligibility)}</b></span>`];
    if (q?.credits_prompt != null) facts.push(`<span title="Kalan / aylık">Prompt kredisi<b>${fmtNum(q.credits_prompt)}${q.monthly_prompt ? ' / ' + fmtNum(q.monthly_prompt) : ''}</b></span>`);
    if (q?.credits_flow != null) facts.push(`<span title="Kalan / aylık">Flow kredisi<b>${fmtNum(q.credits_flow)}${q.monthly_flow ? ' / ' + fmtNum(q.monthly_flow) : ''}</b></span>`);
    detail += `<div class="facts">${facts.join('')}</div>`;

    const disabledDelete = a.primary || a.active;
    const deleteTitle = a.primary ? 'Ana hesap silinemez' : a.active ? 'Aktif hesap silinemez; önce başka hesaba geçin' : 'Profili sil';
    const last = q?.fetched_at ? `Güncellendi: ${relTime(q.fetched_at)}` : (a.last_used ? `Son kullanım: ${relTime(a.last_used)}` : 'Hiç okunmadı');

    return `
      <article data-key="${esc(a.name)}" class="account${a.active ? ' is-active' : ''}${a.primary ? ' is-primary' : ''}">
        <header class="acct-head" data-key="acct-head">
          ${avatar(a)}
          <div class="acct-names">
            <h3 title="${esc(a.label)}">${esc(a.label)}</h3>
            <div class="acct-email" title="${esc(a.email || '')}">${esc(a.email || '—')}</div>
          </div>
          <details class="menu" data-key="menu">
            <summary class="icon-btn" title="Diğer işlemler" aria-label="Diğer işlemler">${icon('moreV')}</summary>
            <div class="menu-pop" role="menu">
              <button role="menuitem" data-action="refresh-one" data-name="${esc(a.name)}">${icon('refresh')}Kotayı oku</button>
              ${a.primary ? '' : `<button role="menuitem" data-action="set-primary" data-name="${esc(a.name)}">${icon('star')}Ana yap</button>`}
              <button role="menuitem" data-action="rename" data-name="${esc(a.name)}">${icon('pencil')}Yeniden adlandır</button>
              <hr>
              <button role="menuitem" class="danger" data-action="delete" data-name="${esc(a.name)}" ${disabledDelete ? 'disabled' : ''} title="${esc(deleteTitle)}">${icon('trash')}Sil</button>
            </div>
          </details>
        </header>
        <div class="badges" data-key="badges">${badges.join('')}</div>
        <div class="acct-summary" data-key="acct-summary">${summary}</div>
        ${familyRows ? `<div class="fam-list" data-key="fam-list">${familyRows}</div>` : ''}
        <footer class="acct-foot" data-key="acct-foot">
          ${a.active
            ? `<span class="btn btn-tonal is-current" aria-disabled="true">${icon('check')}<span>Kullanımda</span></span>`
            : `<button class="btn btn-filled" data-action="switch" data-name="${esc(a.name)}">${icon('swap')}<span>Bu hesaba geç</span></button>`}
          <button class="btn btn-text" data-action="refresh-one" data-name="${esc(a.name)}" data-key="refresh-foot">${icon('refresh')}<span>Kotayı oku</span></button>
        </footer>
        <details class="acct-details" data-key="acct-details">
          <summary><span>${esc(last)}</span><span class="acct-toggle">${groups.length ? `${groups.length} model` : 'Ayrıntılar'}${icon('chevron')}</span></summary>
          <div class="acct-detail-body">${detail}</div>
        </details>
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
              <button class="icon-btn modal-close" aria-label="Kapat">${icon('x')}</button>
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
        const focusTarget = $('input', modal) || $('.modal-actions .btn-primary', modal) || $('.modal-actions button', modal);
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

  // Açık ⋯ menülerini dışarı tıklayınca veya bir seçimden sonra kapat.
  document.addEventListener('click', (event) => {
    const inside = event.target.closest('details.menu');
    const picked = event.target.closest('.menu-pop [data-action]');
    $$('details.menu[open]').forEach((menu) => {
      if (menu !== inside || picked) menu.open = false;
    });
  }, true);

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
  $('#account-sort').addEventListener('change', (event) => {
    state.sort = event.target.value;
    if (state.data) renderAccounts(state.data);
  });
  $('#account-filter').addEventListener('click', (event) => {
    const button = event.target.closest('[data-filter]');
    if (!button) return;
    state.filter = button.dataset.filter;
    $$('[data-filter]', event.currentTarget).forEach((el) => el.setAttribute('aria-checked', String(el === button)));
    if (state.data) renderAccounts(state.data);
  });
  const colorPreference = window.matchMedia('(prefers-color-scheme: light)');
  const THEMES = { auto: ['monitor', 'Tema: sistem'], light: ['sun', 'Tema: açık'], dark: ['moon', 'Tema: koyu'] };
  function currentTheme() {
    const choice = localStorage.getItem('aio.theme') || 'auto';
    return Object.hasOwn(THEMES, choice) ? choice : 'auto';
  }
  function applyTheme() {
    const theme = currentTheme();
    document.documentElement.dataset.theme = theme === 'auto' ? colorPreference.matches ? 'light' : 'dark' : theme;
    const button = $('#theme-btn');
    const [name, label] = THEMES[theme];
    button.innerHTML = icon(name);
    button.title = label;
    button.setAttribute('aria-label', label);
  }
  $('#theme-btn').addEventListener('click', () => {
    const order = ['auto', 'light', 'dark'];
    const next = order[(order.indexOf(currentTheme()) + 1) % order.length];
    localStorage.setItem('aio.theme', next);
    applyTheme();
    toast(THEMES[next][1], 'info', 1600);
  });
  colorPreference.addEventListener('change', applyTheme);
  hydrateIcons();
  applyTheme();
  $('#search').addEventListener('input', (event) => {
    state.search = event.target.value;
    if (state.data) renderAccounts(state.data);
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
