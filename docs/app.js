'use strict';
const $ = id => document.getElementById(id);
const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const DEFAULT_BASE = window.PRENATAL_CONFIG?.apiBase || '';
let base = DEFAULT_BASE;
try { base = localStorage.getItem('prenatal-api') || base; } catch {}
const selection = new CaseSelection(), chosen = selection.items, labIds = new Set(), requests = new Set();
let access = '', result = null, revision = 0, searchVersion = 0, extractVersion = 0, busy = false;
let searchTerms = [], extraction = null, connectionVersion = 0, timer;
// authMode from /api/status: 'accounts' (doctor login), 'token' (shared access code) or 'none'.
// user: {username, name, role ('admin' | 'doctor' | 'lab'), must_change}; lab accounts do not use text extraction.
let authMode = '', user = null, modelState = '', modelPoll, lastWarmup = 0, lastModel = null;
// B26: the public page keeps the API server from config.js; only a page served from localhost (or Shift+click on
// the server name, for testing) may change it, and a different saved server shows a permanent red warning.
let serverUnlocked = false;
const canChangeServer = () => serverUnlocked || FrontLogic.serverLockState(location.hostname, base, DEFAULT_BASE).canChange;
function showServerLock() {
  const s = FrontLogic.serverLockState(location.hostname, base, DEFAULT_BASE);
  $('server-warning').hidden = !s.warn; $('server-warning-host').textContent = s.host || '(chưa đặt)';
  $('login-server').classList.toggle('locked', !canChangeServer());
  $('login-server').title = canChangeServer() ? 'Đổi máy chủ' : '';
}
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
function localStatus(id, message = '') { $(id).textContent = message; }
// A 401 from these means a wrong password or code, not an ended session.
const CREDENTIAL_PATHS = new Set(['/api/login', '/api/login/totp', '/api/totp/enable', '/api/totp/disable', '/api/change_password']);
const apiError = (message, status = 0, data = {}) => Object.assign(Error(message), {status, data});
// Polling and keep-alive checks; they do not count as the user being at the computer.
const BACKGROUND_PATHS = new Set(['/api/status', '/api/session']);
async function api(path, payload, timeout = 120000, controller = new AbortController()) {
  if (!base && location.hostname.endsWith('github.io')) throw Error('Mở Kết nối để nhập địa chỉ máy chủ Ubuntu.');
  requests.add(controller);
  if (!BACKGROUND_PATHS.has(path)) markActivity();  // a request the user started counts as activity
  const timeoutId = setTimeout(() => controller.abort('timeout'), timeout);
  try {
    const response = await fetch(base + path, {
      method: payload ? 'POST' : 'GET',
      headers: {...(payload ? {'Content-Type':'application/json'} : {}), ...(access ? {Authorization:'Bearer ' + access} : {})},
      ...(payload ? {body:JSON.stringify(payload)} : {}), signal:controller.signal, cache:'no-store'
    });
    let data = null;
    try { data = await response.json(); } catch {}
    // Too many requests: say how long to wait and keep the session (the server sends Retry-After).
    if (response.status === 429) throw apiError(FrontLogic.tooFastMessage(response.headers.get('Retry-After'), data || {}, {preferServer:path.startsWith('/api/login')}), 429, data || {});
    if (!data || typeof data !== 'object') throw apiError('Máy chủ trả dữ liệu không hợp lệ. Kiểm tra lại kết nối.', response.status);
    if (!response.ok) {
      if (response.status === 401 && authMode === 'accounts' && access && !CREDENTIAL_PATHS.has(path)) sessionEnded();
      if (response.status === 403 && data.must_change && user) { user.must_change = true; openAccount(); }
      // Admin without 2FA: only /api/session, /api/totp/* and logout work until it is turned on.
      if (response.status === 403 && data.totp_required && user) { sessionInfo = {...(sessionInfo || {}), totp_required:true}; openAccount(); }
      showModel(data);
      throw apiError(data.error || 'Không xử lý được yêu cầu. Vui lòng thử lại.', response.status, data);
    }
    return data;
  } catch (e) {
    if (controller.signal.aborted) throw Error(controller.signal.reason === 'timeout' ? 'Máy chủ phản hồi chậm. Danh sách HPO vẫn được giữ; bạn có thể thử lại.' : 'Yêu cầu đã hủy.');
    if (e instanceof TypeError) throw Error('Chưa kết nối được máy chủ. Kiểm tra Ubuntu và mã truy cập; danh sách HPO vẫn được giữ.');
    throw e;
  } finally { clearTimeout(timeoutId); requests.delete(controller); }
}
// Files from the API (admin exports, bug-report screenshots): same bearer header and error handling as api(), but the
// body comes back as a Blob. The page has no cookies, so a plain <a href> or <img src> could not authenticate.
async function apiBlob(path, timeout = 60000) {
  if (!base && location.hostname.endsWith('github.io')) throw Error('Mở Kết nối để nhập địa chỉ máy chủ Ubuntu.');
  const controller = new AbortController(); requests.add(controller); markActivity();
  const timeoutId = setTimeout(() => controller.abort('timeout'), timeout);
  try {
    const response = await fetch(base + path, {headers:access ? {Authorization:'Bearer ' + access} : {}, signal:controller.signal, cache:'no-store'});
    if (!response.ok) {
      let data = null;
      try { data = await response.json(); } catch {}
      if (response.status === 429) throw apiError(FrontLogic.tooFastMessage(response.headers.get('Retry-After'), data || {}), 429, data || {});
      if (response.status === 401 && authMode === 'accounts' && access) sessionEnded();
      throw apiError((data && data.error) || 'Không xử lý được yêu cầu. Vui lòng thử lại.', response.status, data || {});
    }
    return await response.blob();
  } catch (e) {
    if (controller.signal.aborted) throw Error(controller.signal.reason === 'timeout' ? 'Máy chủ phản hồi chậm. Bạn có thể thử lại.' : 'Yêu cầu đã hủy.');
    if (e instanceof TypeError) throw Error('Chưa kết nối được máy chủ. Kiểm tra Ubuntu và mã truy cập; danh sách HPO vẫn được giữ.');
    throw e;
  } finally { clearTimeout(timeoutId); requests.delete(controller); }
}
window.apiBlob = apiBlob;
window.apiDownload = async function (path, filename) {
  const blob = await apiBlob(path);
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = String(filename || 'tai-xuong').replace(/[\\/:*?"<>|]+/g, '_'); a.rel = 'noopener';
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
};
const MODEL_TEXT = {stopped:'Model: nghỉ · tự bật khi dùng', paused:'Model: tạm dừng · GPU bận việc khác', loading:'Model: đang khởi động…', ready:'Model: sẵn sàng', failed:'Model: chưa chạy được'};
const MODEL_SHORT = {stopped:'GPU nghỉ', paused:'GPU bận', loading:'Đang bật…', ready:'Sẵn sàng', failed:'Lỗi model'};
function showModel(data) {
  if (!data || !data.model_state) return;
  modelState = data.model_state; lastModel = data;
  const el = $('model-state');
  el.hidden = data.model_configured === false || user?.role === 'lab';
  el.textContent = MODEL_TEXT[modelState] || ''; el.dataset.short = MODEL_SHORT[modelState] || '';
  el.className = 'model-state ' + modelState;
  el.title = modelState === 'failed' ? data.model_error || '' : modelState === 'stopped' ? 'Model trích văn bản rời GPU sau ' + (data.gpu_idle_minutes || 60) + ' phút không dùng và tự khởi động lại khi bác sĩ đăng nhập hoặc mở Nhập đoạn văn.' : '';
  clearTimeout(modelPoll);
  if (modelState === 'loading') modelPoll = setTimeout(pollModel, 3000);
}
// Model state, 2FA state and the idle limit come from /api/session after sign-in; /api/status only says online.
let sessionInfo = null;
function applySession(data) {
  if (!data || typeof data !== 'object') return;
  sessionInfo = data;
  if (user && data.user && data.user.username === user.username) user = {...user, ...data.user};
  showModel(data);
}
async function refreshSession() {
  try { const data = await api('/api/session', null, 15000); applySession(data); return data; } catch { return null; }
}
async function pollModel() { await refreshSession(); }
// Text extraction is about to be used: ask the server to load Model 1 on the GPU in the background.
async function warmup() {
  if (authMode !== 'none' && !access || user?.role === 'lab' || user?.must_change) return;
  if (['ready','loading','paused'].includes(modelState) || Date.now() - lastWarmup < 30000) return;
  lastWarmup = Date.now();
  try { showModel(await api('/api/warmup', {}, 20000)); } catch {}
}
const ROLE_NAMES = {admin:'Quản trị', doctor:'Bác sĩ', lab:'Phòng Lab'};
function showUser() {
  // One account button for everything: password, admin page (admins), server, logout.
  $('account-open').textContent = user ? '👤 ' + user.name : ''; $('account-open').hidden = !user;
  $('account-open').title = user ? 'Tài khoản · ' + (ROLE_NAMES[user.role] || '') : '';
  $('account-admin').hidden = user?.role !== 'admin';
  // With accounts the sign-in page logs people in; this button only sets the server for the shared access code.
  $('settings-open').hidden = !!user || authMode === 'accounts';
  // Phòng Lab: HPO search, matching and the lab sheet; no text extraction on the GPU.
  const lab = user?.role === 'lab';
  $('tab-text').hidden = lab;
  if (lab && !$('paragraph').hidden) activateTab('search', false);
  showModel(lastModel);
}
// The account dialog blocks the workspace (no Close, Esc ignored) while a temporary password must be replaced,
// then, for an admin without 2FA, until two-step verification is on.
const totpForced = () => !user?.must_change && !!sessionInfo?.totp_required;
const accountLocked = () => !!user?.must_change || totpForced() || !!window.twoFactorBlocking?.();
function openAccount() {
  const forced = !!user?.must_change, totp = totpForced();
  $('account-title').textContent = forced ? 'Đặt mật khẩu mới' : totp ? 'Bật xác thực 2 lớp' : 'Tài khoản';
  $('account-who').textContent = user ? user.name + ' · ' + user.username + ' · ' + (ROLE_NAMES[user.role] || '') : '';
  $('account-forced').hidden = !forced; $('totp-forced').hidden = !totp;
  $('password-form').hidden = totp; $('totp-section').hidden = forced || !user;
  $('account-admin').hidden = forced || totp || user?.role !== 'admin'; $('account-server').hidden = forced || totp || !canChangeServer();
  $('account-error').hidden = true; $('account-ok').hidden = true;
  window.renderTwoFactor?.();
  $('account-close').hidden = accountLocked();
  if (!$('account').open) $('account').showModal();
  (totp ? $('totp-body').querySelector('button, input') : $('old-password'))?.focus();
}
// Server address (and the shared access code when the server has no accounts); signing in is on the sign-in page.
function openSettings(message = '') {
  const accounts = authMode === 'accounts';
  $('api-url').value = base; $('api-url').readOnly = !canChangeServer(); $('token-fields').hidden = accounts;
  $('settings-title').textContent = 'Kết nối máy chủ';
  $('connect').textContent = 'Kết nối';
  $('settings-hint').textContent = accounts
    ? 'Phiên đăng nhập chỉ giữ trong tab này; tải lại trang cần đăng nhập lại. Model trích văn bản khởi động khi bác sĩ đăng nhập và tự rời GPU khi không dùng.'
    : 'Mã truy cập chỉ giữ trong bộ nhớ của tab này. Địa chỉ máy chủ có thể được lưu, không lưu nội dung ca.';
  $('settings-error').textContent = message; $('settings-error').hidden = !message;
  if (!$('settings').open) $('settings').showModal();
}
// With accounts, the page shows only the sign-in card until someone is logged in.
// state: 'checking' | 'login' | 'totp' (second step) | 'offline' | 'locked', or null to show the workspace.
function showGate(state, message = '') {
  $('login-screen').hidden = !state; document.querySelector('main').hidden = !!state;
  document.querySelector('.top-actions').hidden = !!state;
  if (!state) return;
  $('login-inputs').hidden = state !== 'login'; $('login-totp').hidden = $('login-back').hidden = state !== 'totp';
  $('login-submit').hidden = !['login', 'totp'].includes(state);
  $('login-submit').textContent = state === 'totp' ? 'Xác nhận' : 'Đăng nhập';
  $('login-title').textContent = state === 'totp' ? 'Xác thực 2 lớp' : 'Đăng nhập';
  $('login-retry').hidden = !['offline', 'locked'].includes(state);
  $('login-sub').textContent = state === 'checking' ? 'Đang kết nối máy chủ…'
    : state === 'offline' ? 'Chưa kết nối được máy chủ. Kiểm tra mạng rồi bấm Thử kết nối lại.'
    : state === 'locked' ? 'Hệ thống tạm khóa: máy chủ không đọc được danh sách tài khoản. Liên hệ quản trị viên rồi bấm Thử kết nối lại.'
    : state === 'totp' ? (pendingLogin?.user?.name ? pendingLogin.user.name + ': m' : 'M') + 'ở ứng dụng xác thực trên điện thoại và nhập mã 6 số của Fe2Gen.'
    : 'Dùng tài khoản do quản trị viên cấp.';
  $('login-error').textContent = message; $('login-error').hidden = !message;
  let host = location.host;
  try { if (base) host = new URL(base).host; } catch {}
  $('login-server-name').textContent = host; showServerLock();
  if (state === 'login') ($('login-username').value ? $('login-password') : $('login-username')).focus();
  if (state === 'totp') ($('login-backup-label').hidden ? $('login-code') : $('login-backup')).focus();
}
function sessionEnded() {
  access = ''; user = null; sessionInfo = null; pendingLogin = null; window.resetTwoFactor?.(); window.resetCorrection?.(); window.resetBugReport?.(); window.resetDiseaseFeedback?.(); showUser();
  for (const d of ['account', 'admin', 'settings', 'lab', 'correction', 'bug-report']) if ($(d).open) $(d).close();
  showGate('login', 'Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại; danh sách HPO của ca vẫn được giữ.');
}
function updateActions() {
  $('reviewed').disabled = busy || !!selection.conflicts || !chosen.size;
  $('match').disabled = busy || !!selection.conflicts || !chosen.size || !$('reviewed').checked;
  $('extract').disabled = busy;
  $('extract').textContent = busy === 'extract' ? 'Đang tách dấu hiệu…' : 'Tách dấu hiệu từ văn bản';
  $('match').textContent = busy === 'match' ? 'Đang đối chiếu…' : 'Đối chiếu bệnh →';
  $('export').disabled = !result || !labIds.size || !!selection.conflicts || !!busy;
  $('conflict-summary').hidden = !selection.conflicts;
  $('conflict-summary').textContent = selection.conflicts ? selection.conflicts + ' HPO có trạng thái khác nhau giữa các nguồn. Chọn trạng thái cuối cùng trước khi đối chiếu.' : '';
}
function invalidate() {
  revision++; result = null; labIds.clear(); $('reviewed').checked = false; $('pattern').hidden = true;
  window.resetDiseaseFeedback?.();  // the flagged diseases and the draft belong to the result just dropped
  $('results-title').textContent = 'Kết quả bệnh tiềm năng';
  $('inheritance-filter').innerHTML = '<option value="">Tất cả kiểu di truyền</option>';
  $('results').innerHTML = '<div class="empty"><div class="empty-symbol">↻</div><h3>Sẵn sàng cho lần đối chiếu tiếp theo</h3><p>Duyệt danh sách HPO hiện tại để xem kết quả phù hợp với ca này.</p></div>';
  if ($('lab').open) $('lab').close();
  updateActions();
}
function add(term, state = 'CÓ', source = {kind:'manual'}) {
  const outcome = selection.add(term, state, source);
  if (outcome !== 'duplicate') invalidate();
  notice(outcome === 'conflict'
    ? 'HPO ' + term.id + ' có trạng thái mâu thuẫn. Trạng thái cũ được giữ cho đến khi bác sĩ quyết định.'
    : outcome === 'duplicate' ? 'HPO ' + term.id + ' đã có; giữ nguyên trạng thái bác sĩ đã chọn.'
    : 'Đã thêm ' + (term.vi || term.en) + ' vào danh sách HPO của ca.');
  renderSelected(); renderSearch(); renderMentions();
  return outcome;
}
function renderSelected() {
  $('hpo-count').textContent = chosen.size;
  $('selected').innerHTML = chosen.size ? '' : '<div class="selection-empty">Chưa có dấu hiệu nào được chọn. Tìm HPO hoặc tách đoạn văn ở hàng phía trên.</div>';
  for (const h of chosen.values()) {
    const div = document.createElement('div'); div.className = 'chosen' + (h.pending.length ? ' has-conflict' : '');
    const sourceNames = [...new Set(h.sources.map(s => s.kind === 'text' ? 'Đoạn văn' : s.kind === 'feature' ? 'Khảo sát bổ sung' : 'Tìm HPO'))];
    const stale = selection.stale(h.id, $('clinical-text').value);
    div.innerHTML = '<div class="chosen-head"><strong>' + escape(h.vi || h.en) + '</strong><small>' + escape(h.en) + ' · ' + escape(h.id) + '</small></div>' +
      '<div class="source-label">' + sourceNames.map(s => '<span>' + escape(s) + '</span>').join('') + (stale ? '<small class="stale-source">Đoạn văn đã thay đổi · cần duyệt lại</small>' : '') + '</div>' +
      '<label class="status-control"><span>Trạng thái</span><select aria-label="Trạng thái ' + h.id + '">' + ['CÓ','NGHI NGỜ','KHÔNG'].map(s => '<option ' + (s === h.status ? 'selected' : '') + '>' + s + '</option>').join('') + '</select></label>' +
      '<div class="chosen-actions">' + (user?.role === 'lab' ? '' : '<button class="correct-hpo quiet" aria-label="Sửa mã ' + escape(h.id) + '">Sửa mã</button>') + '<button class="remove-hpo quiet" aria-label="Xóa ' + escape(h.id) + '">Xóa</button></div>';
    div.querySelector('.remove-hpo').onclick = () => { selection.remove(h.id); invalidate(); renderSelected(); renderSearch(); renderMentions(); };
    // "Sửa mã": this HPO is wrong for the phrase it came from (or for the case); the doctor names the right code.
    const fix = div.querySelector('.correct-hpo');
    if (fix) fix.onclick = () => {
      const src = h.sources.find(s => s.kind === 'text' && s.phrase);
      window.openCorrection?.({phrase:src ? src.phrase : (h.vi || h.en), wrong:[h.id], wrongNames:[h.vi || h.en], text:src ? src.text : '', start:src ? src.start : null, status:h.status, source:'selected'});
    };
    div.querySelector('select').onchange = e => { selection.resolve(h.id, e.target.value); invalidate(); renderSelected(); renderSearch(); renderMentions(); notice('Đã cập nhật trạng thái. Duyệt danh sách trước khi đối chiếu lại.'); };
    if (h.pending.length) {
      const conflict = document.createElement('div'); conflict.className = 'conflict';
      const states = [...new Set(h.pending.map(p => p.status))];
      conflict.innerHTML = '<strong>Cần xác nhận trạng thái</strong><p>Đang chọn: ' + escape(h.status) + '. Nguồn mới gợi ý: ' + states.map(escape).join(', ') + '.</p>';
      for (const state of [h.status, ...states.filter(s => s !== h.status)]) {
        const b = document.createElement('button'); b.className = 'quiet'; b.textContent = (state === h.status ? 'Giữ ' : 'Chọn ') + state;
        b.onclick = () => { selection.resolve(h.id, state); invalidate(); renderSelected(); renderSearch(); renderMentions(); notice('Đã xác nhận trạng thái ' + state + ' cho ' + h.id + '.'); };
        conflict.append(b);
      }
      div.append(conflict);
    }
    $('selected').append(div);
  }
  updateActions();
}
function renderSearch() {
  $('search-results').replaceChildren();
  for (const term of searchTerms) {
    const h = chosen.get(term.id), b = document.createElement('button');
    b.className = 'search-item' + (h ? ' is-selected' : '');
    b.innerHTML = '<span><strong>' + escape(term.vi || term.en) + '</strong><small>' + escape(term.en) + ' · ' + escape(term.id) + '</small></span><span class="selection-action">' + (h ? 'Đã chọn · ' + escape(h.status) : '+ Thêm HPO') + '</span>';
    b.onclick = () => add(term);
    $('search-results').append(b);
  }
}
function renderMentions() {
  $('mentions').replaceChildren();
  if (!extraction || extraction.text !== $('clinical-text').value) return;
  for (const m of extraction.mentions) {
    const box = document.createElement('div'); box.className = 'mention';
    box.innerHTML = '<div class="mention-heading"><blockquote>“' + escape(m.mention_text) + '”</blockquote><span class="subtle">Cụm nguyên văn</span></div>' +
      '<p class="hint">' + escape(m.explanation) + (m.context_review ? ' · Kiểm tra phủ định, chủ thể và tiền sử trong câu gốc.' : '') + '</p>';
    const status = document.createElement('select'); status.setAttribute('aria-label', 'Trạng thái gợi ý: ' + m.mention_text);
    status.innerHTML = (m.context_review ? '<option value="">Bác sĩ chọn trạng thái…</option>' : '') + ['CÓ','NGHI NGỜ','KHÔNG'].map(s => '<option>' + s + '</option>').join('');
    status.value = m.reviewStatus; status.onchange = () => { m.reviewStatus = status.value; renderMentions(); };
    const label = document.createElement('label'); label.className = 'mention-status'; label.textContent = 'Trạng thái trước khi thêm'; label.append(status); box.append(label);
    if (!m.candidates.length) {
      const p = document.createElement('p'); p.className = 'hint'; p.textContent = 'Chưa tìm thấy mã phù hợp. Tìm HPO bằng một cụm ngắn hơn.'; box.append(p);
    }
    for (const term of m.candidates) {
      const h = chosen.get(term.id), b = document.createElement('button'); b.className = 'search-item' + (h ? ' is-selected' : '');
      const different = h && m.reviewStatus && h.status !== m.reviewStatus;
      b.innerHTML = '<span><strong>' + escape(term.vi || term.en) + '</strong><small>' + escape(term.id) + ' · ' + escape(term.en) + '</small></span><span class="selection-action">' + (different ? 'Khác trạng thái · xem xét' : h ? 'Đã chọn · ' + escape(h.status) : '+ Thêm HPO') + '</span>';
      b.disabled = !m.reviewStatus;
      b.onclick = () => add(term, m.reviewStatus, {kind:'text', text:extraction.text, phrase:m.mention_text, start:m.span_start});
      box.append(b);
    }
    // "Báo mã sai": the suggested codes are wrong for this phrase; the doctor names the right one for the admin to approve.
    if (user?.role !== 'lab') {
      const text = extraction.text, top = m.candidates.slice(0, 3);
      const fix = document.createElement('button'); fix.type = 'button'; fix.className = 'quiet text-button correction-open'; fix.textContent = 'Báo mã sai';
      fix.onclick = () => window.openCorrection?.({phrase:m.mention_text, wrong:top.map(c => c.id), wrongNames:top.map(c => c.vi || c.en), text, start:m.span_start, status:m.reviewStatus, source:'text'});
      box.append(fix);
    }
    $('mentions').append(box);
  }
}
function activateTab(mode, focus = true) {
  $('manual').hidden = mode !== 'search'; $('paragraph').hidden = mode !== 'text';
  for (const m of ['search','text']) { $('tab-' + m).setAttribute('aria-selected', String(m === mode)); $('tab-' + m).tabIndex = m === mode ? 0 : -1; }
  if (focus) $(mode === 'search' ? 'query' : 'clinical-text').focus();
  if (mode === 'text') warmup();
}
for (const mode of ['search','text']) {
  $('tab-' + mode).onclick = () => activateTab(mode);
  $('tab-' + mode).onkeydown = e => { if (['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) {e.preventDefault(); const next = e.key === 'Home' ? 'search' : e.key === 'End' ? 'text' : mode === 'search' ? 'text' : 'search'; activateTab(next, false); $('tab-' + next).focus();} };
}
let searchController;
$('query').oninput = () => {
  clearTimeout(timer); searchController?.abort('superseded');
  const seq = ++searchVersion, q = $('query').value.trim(); searchTerms = []; renderSearch();
  localStatus('search-status', q ? 'Đang tìm thuật ngữ…' : 'Nhập tên dấu hiệu để bắt đầu tìm.');
  if (!q) return;
  timer = setTimeout(async () => {
    searchController = new AbortController();
    try {
      const terms = await api('/api/hpo_search?q=' + encodeURIComponent(q), null, 20000, searchController);
      if (seq !== searchVersion) return;
      searchTerms = terms; renderSearch(); localStatus('search-status', terms.length ? terms.length + ' gợi ý · Chọn để thêm vào danh sách chung.' : 'Không tìm thấy. Thử cụm ngắn hơn hoặc mã HPO.');
    } catch (e) { if (seq === searchVersion) localStatus('search-status', e.message); }
  },250);
};
$('clinical-text').onfocus = warmup;
$('clinical-text').oninput = () => {
  extractVersion++; extraction = null; renderMentions(); invalidate(); renderSelected();
  localStatus('extract-status', 'Đoạn văn đã thay đổi. HPO đã chọn vẫn được giữ; tách lại để cập nhật gợi ý.');
};
$('extract').onclick = async () => {
  if (busy) return;
  const text = $('clinical-text').value;
  if (!text.trim()) { localStatus('extract-status','Nhập đoạn mô tả siêu âm trước.'); return; }
  const seq = ++extractVersion; busy = 'extract'; idle.busy++; updateActions();
  localStatus('extract-status', modelState === 'ready' ? 'Model 1 đang tách dấu hiệu. Các HPO đã chọn được giữ nguyên.'
    : 'Model 1 đang khởi động trên GPU (thường dưới 1 phút) rồi tách dấu hiệu. Các HPO đã chọn được giữ nguyên.');
  try {
    const data = await api('/api/extract_hpo', {text}, 420000);  // server: up to 120 s loading + 300 s extraction
    pollModel();
    if (seq !== extractVersion) return;
    extraction = {text, model_version:typeof data.model_version === 'string' ? data.model_version : '', mentions:data.mentions.map(m => ({...m, reviewStatus:m.context_review ? '' : m.status}))};
    renderMentions();
    localStatus('extract-status', data.mentions.length ? 'Tìm thấy ' + data.mentions.length + ' cụm. Chọn HPO phù hợp bên dưới; không tự ghi đè danh sách đã duyệt.' : 'Chưa tách được dấu hiệu. Bạn vẫn có thể tìm HPO thủ công.');
  } catch (e) { if (seq === extractVersion) localStatus('extract-status', e.message); }
  finally { busy = false; idle.busy--; markActivity(); updateActions(); }
};
$('clear-hpos').onclick = () => { selection.clear(); invalidate(); renderSelected(); renderSearch(); renderMentions(); notice('Đã xóa danh sách HPO. Đoạn văn và gợi ý vẫn được giữ.'); };
$('reviewed').onchange = updateActions;
$('wes-genes').oninput = () => { if (result) invalidate(); };
$('match').onclick = async () => {
  if (busy || selection.conflicts || !$('reviewed').checked) return;
  const seq = revision;
  const snapshot = {hpos:structuredClone([...chosen.values()]), text:$('clinical-text').value, wesText:$('wes-genes').value.trim()};
  busy = 'match'; idle.busy++; updateActions(); notice('Đang đối chiếu hồ sơ bệnh hiếm…');
  try {
    const data = await api('/api/match_diseases', {hpos:snapshot.hpos.map(h => ({id:h.id,status:h.status})), genes:snapshot.wesText});
    if (seq !== revision) return;
    result = {...data, ...snapshot};
    labIds.clear();
    // Grouped results preselect the best match of each mechanism column.
    const first = data.grouped ? GROUPS.slice(0,3).map(g => data.candidates.find(c => c.group === g.id)).filter(Boolean) : data.candidates.slice(0,5);
    for (const c of first) labIds.add(c.disease_id);
    const modes = [...new Set(data.candidates.flatMap(c => c.inheritance_modes))].sort();
    $('inheritance-filter').innerHTML = '<option value="">Tất cả kiểu di truyền</option>' + modes.map(m => '<option>' + escape(m) + '</option>').join('');
    renderResults(); notice('');
  } catch (e) { if (seq === revision) {result = null;labIds.clear();window.resetDiseaseFeedback?.();notice(e.message);} }
  finally { busy = false; idle.busy--; markActivity(); updateActions(); }
};

const GROUPS=[
  {id:'NST',title:'Bất thường nhiễm sắc thể',test:'QF-PCR / Karyotype'},
  {id:'CNV',title:'Vi mất đoạn / CNV',test:'CMA (microarray)'},
  {id:'DON_GEN',title:'Bất thường đơn gen',test:'WES / Panel gen'},
  {id:'CHUA_RO',title:'Chưa rõ cơ chế di truyền',test:'Không xếp được vào 3 nhóm trên'}];
const groupTitle=id=>(GROUPS.find(g=>g.id===id)||{title:''}).title;
function pills(values,cls){return values.length?values.map(v=>'<span class="pill '+cls+'">'+escape(v)+'</span>').join(''):'<span class="hint">Chưa có thông tin</span>';}
function renderResults(){
  if(!result)return;
  try{drawResults();}finally{window.renderDiseaseFeedback?.();}
}
function drawResults(){
  const filter=$('inheritance-filter').value;
  const list=result.candidates.filter(c=>!filter||c.inheritance_modes.includes(filter));
  $('results-title').textContent=list.length+' bệnh tiềm năng';
  $('pattern').textContent=result.clinical_pattern;$('pattern').hidden=!result.clinical_pattern;
  $('results').replaceChildren();
  if(!list.length){$('results').innerHTML='<p class="no-results">Không có kết quả phù hợp bộ lọc.</p>';return;}
  if(!result.grouped){for(const c of list)$('results').append(diseaseCard(c));return;}
  if(result.wes){
    const w=result.wes, wl=w.diseases.map(id=>list.find(c=>c.disease_id===id)).filter(Boolean);
    const box=document.createElement('section');box.className='top-overall wes';
    box.innerHTML='<header class="group-head"><h3>Xếp hạng theo gene WES</h3><span>'+w.genes.length+' gene ứng viên · thứ tự theo kiểu hình của ca · cần bác sĩ và phòng xét nghiệm duyệt</span></header>'+
      (w.genes_ranked.length?'<p><b>Gene ưu tiên:</b> '+w.genes_ranked.slice(0,10).map((g,i)=>'<span class="pill '+(i<5?'purple':'gray')+'">'+(i+1)+'. '+escape(g)+'</span>').join(' ')+'</p>':'<p class="hint">Không gene nào trong danh sách có bệnh khớp kiểu hình.</p>')+
      (wl.length?'<ol>'+wl.map(c=>'<li><b>'+escape(c.disease_name)+'</b> <span class="hint">'+escape(c.disease_id)+' · gene '+c.wes_genes.map(escape).join(', ')+' · '+c.match_percentage+'% tương đồng</span></li>').join('')+'</ol>':'')+
      (w.unknown_genes.length?'<p class="hint">Chưa có liên kết bệnh trong dữ liệu: '+w.unknown_genes.slice(0,30).map(escape).join(', ')+(w.unknown_genes.length>30?'…':'')+'</p>':'');
    $('results').append(box);
  }
  const top=(result.top_overall||[]).map(id=>list.find(c=>c.disease_id===id)).filter(Boolean);
  if(top.length){
    const box=document.createElement('section');box.className='top-overall';
    box.innerHTML='<header class="group-head"><h3>'+top.length+' gợi ý hàng đầu</h3><span>Trộn cả ba nhóm, ưu tiên theo thứ tự xét nghiệm · cần bác sĩ duyệt</span></header><ol>'+
      top.map(c=>'<li><b>'+escape(c.disease_name)+'</b> <span class="pill '+({NST:'purple',CNV:'orange',DON_GEN:'green'}[c.group]||'gray')+'">'+escape(groupTitle(c.group))+'</span> <span class="hint">'+escape(c.disease_id)+' · '+c.match_percentage+'% tương đồng</span></li>').join('')+'</ol>';
    $('results').append(box);
  }
  const columns=document.createElement('div');columns.className='group-columns';
  for(const g of GROUPS){
    const items=list.filter(c=>c.group===g.id&&c.in_column!==false);
    if(g.id==='CHUA_RO'&&!items.length)continue;
    const col=document.createElement('section');col.className='group-column'+(g.id==='CHUA_RO'?' group-other':'');
    col.innerHTML='<header class="group-head"><h3>'+escape(g.title)+'</h3><span>'+escape(g.test)+'</span></header>';
    if(!items.length)col.insertAdjacentHTML('beforeend','<p class="no-results">Không có bệnh nào trong nhóm này chia sẻ HPO đã duyệt.</p>');
    for(const c of items)col.append(diseaseCard(c));
    columns.append(col);
  }
  $('results').append(columns);
}
function diseaseCard(c){
    const wrongFlag=!!window.isWrongDisease?.(c.disease_id);
    const card=document.createElement('article');card.className='disease'+(wrongFlag?' is-wrong':'');
    const match=c.disease_id.match(/^(OMIM|ORPHA|ORPHANET):(\d+)$/);
    const link=match?(match[1]==='OMIM'?'https://omim.org/entry/':'https://www.orpha.net/en/disease/detail/')+match[2]:'';
    card.innerHTML='<div class="disease-top"><div class="rank">'+c.rank+'</div><div class="disease-title"><h3>'+escape(c.disease_name)+'</h3>'+(link?'<a target="_blank" rel="noopener noreferrer" href="'+link+'">'+escape(c.disease_id)+' ↗</a>':escape(c.disease_id))+((c.equivalent_ids||[]).length?'<span class="overall">Cùng bệnh: '+c.equivalent_ids.map(escape).join(', ')+'</span>':'')+(c.overall_rank&&c.overall_rank!==c.rank?'<span class="overall">Hạng chung '+c.overall_rank+'</span>':'')+(c.mixed_mechanism?'<span class="pill amber mixed" title="Bệnh có thể do CNV hoặc do biến thể một gen">Cơ chế hỗn hợp</span>':'')+'</div><div class="score">'+c.match_percentage+'%<small>TƯƠNG ĐỒNG</small></div></div>'+
      '<div class="evidence-row"><span class="evidence-label">HPO khớp</span><div>'+pills(c.matched_phenotypes.map(p=>(p.vi||p.en)+' · '+p.id+(p.relation==='exact'?'':' (ngữ nghĩa)')),'blue')+'</div></div>'+
      '<div class="evidence-row"><span class="evidence-label">Kiểu di truyền</span><div>'+pills(c.inheritance_modes,'green')+'</div></div>'+
      '<div class="evidence-row"><span class="evidence-label">Gen liên quan</span><div>'+pills(c.causative_genes,'gray')+'</div></div>'+
      '<details class="features"><summary>Dấu hiệu chưa ghi nhận · Xem '+c.clinical_features_to_check.length+'</summary><p class="hint">Bác sĩ xem xét tính phù hợp với tuổi thai và khả năng khảo sát. Danh sách có thể gồm dấu hiệu sau sinh; chưa ghi nhận không có nghĩa là âm tính.</p><div class="feature-list"></div></details>'+
      (c.model3_rationale||c.model3_recommended_tests?'<div class="recommendation"><strong>Gợi ý hội chẩn · cần bác sĩ duyệt</strong><p>'+escape(c.model3_rationale)+'</p>'+escape(c.model3_recommended_tests)+'</div>':'')+
      '<div class="disease-foot"><label class="check lab-choice"><input type="checkbox" '+(labIds.has(c.disease_id)?'checked':'')+'> Đưa bệnh này vào phiếu Lab</label>'+
      '<button type="button" class="quiet text-button disease-wrong" data-disease-id="'+escape(c.disease_id)+'" aria-pressed="'+(wrongFlag?'true':'false')+'" title="AI đề xuất sai bệnh này: đánh dấu rồi ghi bệnh đúng ở ô Bệnh đúng của ca">'+(wrongFlag?'Đã báo sai · bỏ':'Báo sai')+'</button></div>';
    const featureList=card.querySelector('.feature-list');
    for(const t of c.clinical_features_to_check){
      const b=document.createElement('button');b.className='pill amber';b.textContent=(t.vi||t.en)+' · '+t.id+' +';
      b.title='Thêm vào ca với trạng thái Nghi ngờ để bác sĩ duyệt';
      b.onclick=()=>add(t,'NGHI NGỜ',{kind:'feature'});
      featureList.append(b);
    }
    card.querySelector('input').onchange=e=>{if(e.target.checked)labIds.add(c.disease_id);else labIds.delete(c.disease_id);updateActions();};
    // "Báo sai": Model 2 ranked this disease wrongly; the doctor names the right one in the panel under the results.
    card.querySelector('.disease-wrong').onclick=()=>window.toggleWrongDisease?.(c);
    return card;
}
$('inheritance-filter').onchange=renderResults;
$('export').onclick=()=>{
  if(!result||!labIds.size)return;
  const rows=result.candidates.filter(c=>labIds.has(c.disease_id));
  const table=(heads,body)=>'<table><thead><tr>'+heads.map(h=>'<th>'+escape(h)+'</th>').join('')+'</tr></thead><tbody>'+body.map(r=>'<tr>'+r.map(v=>'<td>'+escape(v)+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
  $('sheet').innerHTML='<p style="text-align:center">HỘI CHẨN DI TRUYỀN TIỀN SẢN</p><h1>PHIẾU GỬI LAB DI TRUYỀN</h1><p>Mã ca: '+escape($('case-id').value||'……………………')+' · Tuổi thai: '+escape($('gestation').value||'……………………')+'\nBác sĩ: '+escape($('doctor').value||'……………………')+'\nNgày lập: '+escape(new Date().toLocaleString('vi-VN'))+'</p>'+
  (result.wesText?'<h2>Gene ứng viên từ WES</h2><p>'+escape(result.wesText)+'</p>':'')+'<h2>1. Mô tả siêu âm</h2><p>'+escape(result.text||'Nhập dấu hiệu bằng tra cứu HPO.')+'</p><h2>2. Kiểu hình đã duyệt</h2>'+
  table(['Mã HPO','Tiếng Việt','Tiếng Anh','Trạng thái'],result.hpos.map(h=>[h.id,h.vi,h.en,h.status]))+
  '<h2>3. Bệnh được bác sĩ chọn để hội chẩn</h2>'+table(['Hạng','Bệnh / mã','Tương đồng','Kiểu di truyền','Gen'],rows.map(c=>[(c.group?groupTitle(c.group)+' #':'')+c.rank,c.disease_name+' · '+c.disease_id,c.match_percentage+'%',c.inheritance_modes.join(', '),c.causative_genes.join(', ')]))+
  '<p>Phần trăm là độ tương đồng kiểu hình, không phải xác suất mắc bệnh.</p><h2>4. Gợi ý xét nghiệm để bác sĩ xem xét</h2>'+
  rows.filter(c=>c.model3_recommended_tests).map(c=>'<p><strong>'+escape(c.disease_name)+'</strong>\n'+escape(c.model3_recommended_tests)+'</p>').join('')+
  '<p>Chỉ định / ghi chú bác sĩ: ........................................................................\n...........................................................................................................</p><div class="signature"><div>Bác sĩ chỉ định<br>(Ký, ghi rõ họ tên)</div><div>Phòng Lab tiếp nhận<br>(Ký, ghi rõ họ tên)</div></div>';
  $('lab').showModal();
};
$('print').onclick=()=>window.print();$('close-lab').onclick=()=>$('lab').close();
function hasCase(){return chosen.size||$('clinical-text').value||$('case-id').value||$('wes-genes').value;}
$('new-case').onclick=()=>{
  if((chosen.size||$('clinical-text').value)&&!confirm('Bắt đầu ca mới và xóa nội dung ca hiện tại trong phiên này?'))return;
  resetCase();
};
function resetCase(){
  extractVersion++;searchVersion++;connectionVersion++;clearTimeout(timer);for(const c of requests)c.abort('new-case');selection.clear();extraction=null;searchTerms=[];for(const id of ['case-id','gestation','doctor','clinical-text','query','wes-genes'])$(id).value='';
  renderMentions();renderSearch();invalidate();renderSelected();notice('');localStatus('extract-status','');localStatus('search-status','Nhập tên dấu hiệu để bắt đầu tìm.');
}
$('settings-open').onclick=()=>openSettings();
$('settings-close').onclick=()=>$('settings').close();
$('logout').onclick=async()=>{
  // A shared hospital computer: the next person must not see this case.
  if(hasCase()&&!confirm('Đăng xuất sẽ xóa nội dung ca đang mở trong tab này. Tiếp tục?'))return;
  try{await api('/api/logout',{},15000);}catch{}
  for(const d of ['account','admin','settings','lab','correction','bug-report'])if($(d).open)$(d).close();
  signedOut();showGate('login');checkConnection();
};
// After logout: forget the user, the 2FA state and the case. /api/logout sends Clear-Site-Data "cache", which
// leaves localStorage alone; still reread the saved server instead of assuming it is kept or gone.
function signedOut(){
  resetCase();access='';user=null;sessionInfo=null;pendingLogin=null;window.resetTwoFactor?.();window.resetCorrection?.();window.resetBugReport?.();window.resetDiseaseFeedback?.();showUser();
  $('idle-warning').hidden=true;
  try{const saved=localStorage.getItem('prenatal-api');if(saved)base=saved;}catch{}
}

// B12/B27: sign out by itself after idle_minutes (from /api/session) without mouse, keyboard, touch or scrolling,
// with a 60-second warning. Text extraction and matching pause the countdown while they run. A shared hospital
// computer left signed in must not show the case to the next person.
const idle={last:Date.now(),busy:0,lastPing:Date.now(),ending:false};
const idleOn=()=>authMode==='accounts'&&!!access&&!!user;
function markActivity(){idle.last=Date.now();if(!$('idle-warning').hidden)checkIdle();}
function placeIdleWarning(){
  // Modal dialogs make the rest of the page inert: show the warning inside the open one.
  const host=[...document.querySelectorAll('dialog')].reverse().find(d=>d.open&&d.matches(':modal'))||document.body;
  if($('idle-warning').parentElement!==host)host.prepend($('idle-warning'));
}
function checkIdle(){
  if(idle.ending)return;
  if(!idleOn()){$('idle-warning').hidden=true;return;}
  const now=Date.now();
  const s=FrontLogic.idleState({now,lastActivity:idle.last,idleMinutes:sessionInfo?.idle_minutes,busy:idle.busy>0});
  if(s.phase==='expired')return idleLogout();
  if(s.phase==='warn'){$('idle-seconds').textContent=s.secondsLeft;placeIdleWarning();}
  $('idle-warning').hidden=s.phase!=='warn';
  // Typing a long report sends no request: keep the server session alive while the doctor is active.
  if(FrontLogic.shouldPing({now,lastActivity:idle.last,lastPing:idle.lastPing})){idle.lastPing=now;refreshSession();}
}
async function idleLogout(){
  idle.ending=true;
  const minutes=FrontLogic.idleMinutesOf(sessionInfo?.idle_minutes);
  for(const d of ['account','admin','settings','lab','correction','bug-report'])if($(d).open)$(d).close();
  resetCase();  // clear the case first, before waiting for the network
  try{await api('/api/logout',{},15000);}catch{}
  signedOut();idle.ending=false;
  showGate('login','Đã tự đăng xuất sau '+minutes+' phút không thao tác; nội dung ca trong tab này đã được xóa.');
}
for(const type of ['pointerdown','pointermove','keydown','wheel','scroll','touchstart'])
  addEventListener(type,markActivity,{capture:true,passive:true});
$('idle-stay').onclick=()=>{markActivity();idle.lastPing=Date.now();refreshSession();};
// Back to a hidden tab: timers may have been slowed, so decide at once; then ask the server whether the session
// still exists (401 → sign-in page).
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState!=='visible'||!idleOn())return;
  checkIdle();
  if(idleOn())refreshSession();
});
setInterval(checkIdle,1000);
$('account-open').onclick=()=>openAccount();
$('account-close').onclick=()=>$('account').close();
$('account').oncancel=e=>{if(accountLocked())e.preventDefault();};  // Esc cannot skip a temporary password or required 2FA
$('account-server').onclick=()=>{$('account').close();openSettings();};
$('account-admin').onclick=()=>{$('account').close();openAdmin();};
$('password-form').onsubmit=async e=>{
  e.preventDefault();
  const show=(id,msg)=>{$(id).textContent=msg;$(id).hidden=!msg;};
  show('account-error','');show('account-ok','');
  const old_password=$('old-password').value,new_password=$('new-password').value;
  if(new_password!==$('new-password2').value)return show('account-error','Hai lần nhập mật khẩu mới không khớp.');
  if(new_password.length<10)return show('account-error','Mật khẩu mới cần ít nhất 10 ký tự.');
  $('change-password').disabled=true;
  try{
    const data=await api('/api/change_password',{old_password,new_password},30000);
    const forced=user.must_change;user=data.user;showUser();
    for(const id of ['old-password','new-password','new-password2'])$(id).value='';
    if(forced){notice('Đã đặt mật khẩu mới. Chào '+user.name+'.');await refreshSession();
      if(totpForced())openAccount();else $('account').close();}
    else show('account-ok','Đã đổi mật khẩu. Các thiết bị khác đã đăng nhập bằng tài khoản này cần đăng nhập lại.');
  }catch(err){show('account-error',err.message);}
  finally{$('change-password').disabled=false;}
};
$('connect').onclick=async e=>{
  e.preventDefault();
  try{
    // The public page ignores a typed address unless changing server is allowed (see canChangeServer).
    const value=canChangeServer()?$('api-url').value.trim().replace(/\/+$/,''):base;
    if(value){const u=new URL(value);if(u.protocol!=='https:'&&!(u.protocol==='http:'&&['localhost','127.0.0.1'].includes(u.hostname)))throw Error('Máy chủ từ xa phải dùng HTTPS.');if(u.username||u.password||u.search||u.hash)throw Error('Chỉ nhập địa chỉ gốc của máy chủ.');}
    for(const c of requests)c.abort('connection-changed');searchVersion++;extractVersion++;connectionVersion++;clearTimeout(timer);
    if(value!==base){access='';user=null;authMode='';showUser();}
    if(value!==base){base=value;try{localStorage.setItem('prenatal-api',base);}catch{}}
    showServerLock();
    if($('access-token').value)access=$('access-token').value;
    $('access-token').value='';searchTerms=[];extraction=null;renderSearch();renderMentions();
    invalidate();$('settings').close();checkConnection();
  }catch(err){$('settings-error').textContent=err.message;$('settings-error').hidden=false;}
};
async function checkConnection(){
  const seq=connectionVersion;
  try{const status=await api('/api/status',null,15000);if(seq!==connectionVersion)return;
    authMode=status.auth_mode||(status.auth_required?'token':'none');
    // 'locked': the server could not read its accounts and refuses everything until an admin fixes it.
    if(authMode==='locked'){$('connection').textContent='○ Máy chủ tạm khóa';if(!user)showGate('locked');else notice('Máy chủ tạm khóa: không đọc được danh sách tài khoản. Liên hệ quản trị viên.');return;}
    $('connection').textContent='● Máy chủ trực tuyến';$('connection').style.color='#286340';showUser();
    if(authMode==='accounts'&&!access){showGate('login');return;}
    showGate(null);
    if(authMode==='token'&&!access){notice('Máy chủ yêu cầu mã truy cập. Mở Kết nối để nhập mã.');return;}
    notice('');
    const session=await refreshSession();
    if(seq===connectionVersion&&session&&session.model_configured===false)notice('Tra cứu và đối chiếu sẵn sàng. Model trích văn bản chưa được cấu hình.');}
  catch(e){if(seq!==connectionVersion)return;
    if(e.data?.auth_mode==='locked'||e.status===503&&/tạm khóa/i.test(e.message)){$('connection').textContent='○ Máy chủ tạm khóa';if(!user)showGate('locked');else notice(e.message);return;}
    $('connection').textContent='○ Chưa kết nối Ubuntu';
    // The generic network error mentions the access code; the sign-in card says it more simply.
    if(user)notice(e.message);else showGate('offline',/Chưa kết nối được máy chủ/.test(e.message)?'':e.message);}
}
// Sign-in: password, then (accounts with 2FA) a 6-digit code or a backup code within 5 minutes.
let pendingLogin=null;  // {pending, user, at, failures}
const PENDING_MS=5*60000,CODE_TRIES=5;
const BAD_REPLY='Máy chủ trả dữ liệu không hợp lệ. Kiểm tra lại kết nối.';
async function signedIn(step,data){
  access=step.token;user=step.user;lastModel=data;pendingLogin=null;window.resetTwoFactor?.();window.resetCorrection?.();window.resetBugReport?.();window.resetDiseaseFeedback?.();
  idle.last=idle.lastPing=Date.now();
  showUser();notice('');
  await refreshSession();  // 2FA requirement and the idle limit
  showGate(null);
  if(user.must_change||totpForced())openAccount();
}
function useBackup(on){
  $('login-backup-label').hidden=!on;$('login-code-label').hidden=on;
  $('login-use-backup').textContent=on?'Dùng mã 6 số trong ứng dụng':'Không có điện thoại? Dùng mã dự phòng';
  $('login-code').value=$('login-backup').value='';
}
function backToPassword(message=''){pendingLogin=null;useBackup(false);showGate('login',message);}
async function submitCode(){
  const backup=!$('login-backup-label').hidden;
  if(Date.now()-pendingLogin.at>PENDING_MS)return backToPassword('Đã quá 5 phút từ khi nhập mật khẩu. Vui lòng đăng nhập lại.');
  const code=FrontLogic.normalizeCode(backup?$('login-backup').value:$('login-code').value,backup);
  if(!code)return showGate('totp',backup?'Nhập mã dự phòng đúng như trong danh sách đã lưu.':'Nhập đủ 6 chữ số đang hiện trong ứng dụng.');
  const attempt=pendingLogin;
  try{
    const data=await api('/api/login/totp',{pending:attempt.pending,code},30000);
    const step=FrontLogic.loginStep(data);
    if(step.kind!=='session')throw Error(BAD_REPLY);
    await signedIn(step,data);
  }catch(err){
    if(pendingLogin!==attempt)return;  // went back meanwhile
    // The server cancels the pending sign-in after 5 wrong codes or 5 minutes: start again from the password.
    if(err.status===401)attempt.failures++;
    if(attempt.failures>=CODE_TRIES||[400,410].includes(err.status)||err.data?.pending_invalid)return backToPassword(err.message+' Vui lòng đăng nhập lại từ bước mật khẩu.');
    showGate('totp',err.message);
  }finally{$('login-code').value=$('login-backup').value='';}
}
$('login-form').onsubmit=async e=>{
  e.preventDefault();
  if($('login-submit').disabled)return;
  $('login-submit').disabled=true;$('login-error').hidden=true;
  try{
    if(pendingLogin)return await submitCode();
    const username=$('login-username').value.trim(),password=$('login-password').value;
    if(!username||!password)return showGate('login','Nhập tên đăng nhập và mật khẩu.');
    try{
      const data=await api('/api/login',{username,password},30000);
      const step=FrontLogic.loginStep(data);
      if(step.kind==='totp'){pendingLogin={pending:step.pending,user:step.user,at:Date.now(),failures:0};useBackup(false);showGate('totp');return;}
      if(step.kind!=='session')throw Error(BAD_REPLY);
      await signedIn(step,data);
    }catch(err){showGate('login',err.message);}
    finally{$('login-password').value='';}
  }finally{$('login-submit').disabled=false;}
};
$('login-back').onclick=()=>backToPassword();
$('login-use-backup').onclick=()=>{useBackup($('login-backup-label').hidden);$('login-error').hidden=true;($('login-backup-label').hidden?$('login-code'):$('login-backup')).focus();};
$('login-retry').onclick=()=>{showGate('checking');checkConnection();};
$('login-server').onclick=e=>{if(e.shiftKey)serverUnlocked=true;if(canChangeServer()){showServerLock();openSettings();}};
$('server-default').onclick=()=>{try{localStorage.removeItem('prenatal-api');}catch{}location.reload();};
renderSelected();showGate('checking');checkConnection();

