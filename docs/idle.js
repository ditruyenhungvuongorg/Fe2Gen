// Pure front-end decisions (no DOM, no network) for sign-in security: session idle timeout, which API server the
// page may use, the two sign-in steps and rate-limit messages. app.js wires them to the page; test_frontend_logic.cjs
// tests them under Node.
(function (root) {
  'use strict';
  const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);
  const trimBase = value => String(value || '').trim().replace(/\/+$/, '');
  const hostOf = value => { try { return new URL(value).host; } catch { return String(value || ''); } };

  // B26: on the public page the API server is the one in config.js. Changing it is for development (page served
  // from localhost); a different saved server on the public page is shown as a permanent red warning, because
  // someone at a shared computer could point the page at a fake server and collect passwords.
  function serverLockState(hostname, base, defaultBase) {
    const host = String(hostname || '').toLowerCase();
    const local = LOCAL_HOSTS.has(host) || host.endsWith('.localhost');
    const nonDefault = trimBase(base) !== trimBase(defaultBase);
    return {local, nonDefault, warn: !local && nonDefault, canChange: local, host: hostOf(trimBase(base))};
  }

  // B12/B27: the page signs out by itself after idleMinutes without mouse, keyboard or scrolling. A long request
  // (text extraction, matching) counts as activity while it runs. The warning shows warnSeconds before the end.
  const DEFAULT_IDLE_MINUTES = 60;
  function idleMinutesOf(value) {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : DEFAULT_IDLE_MINUTES;
  }
  function idleState({now, lastActivity, idleMinutes, warnSeconds = 60, busy = false}) {
    const idleMs = idleMinutesOf(idleMinutes) * 60000;
    const warnMs = Math.min(warnSeconds * 1000, idleMs / 2);
    const remaining = busy ? idleMs : Math.max(0, lastActivity + idleMs - now);
    const phase = remaining <= 0 ? 'expired' : remaining <= warnMs ? 'warn' : 'active';
    return {phase, remaining, secondsLeft: Math.ceil(remaining / 1000)};
  }
  // Keep the server session alive while the doctor works without sending requests (typing a long report):
  // at most one ping per interval, and only if there was activity since the last one.
  function shouldPing({now, lastActivity, lastPing, intervalMs = 5 * 60000}) {
    return lastActivity > lastPing && now - lastPing >= intervalMs;
  }

  // A case left behind by an ended session (expired, forced logout, server restart) is kept only for the same
  // account signing in again; any other account, or an unknown owner, starts from an empty case.
  function keepsCase(owner, next) {
    return !!(owner && next && owner.username && owner.username === next.username && owner.role === next.role);
  }

  // B5: /api/login answers with a session, or asks for the second factor when the account has 2FA on.
  function loginStep(data) {
    if (data && data.second_factor === true && typeof data.pending === 'string' && data.pending)
      return {kind: 'totp', pending: data.pending, user: data.user && typeof data.user === 'object' ? data.user : {}};
    if (data && typeof data.token === 'string' && data.token && data.user && typeof data.user === 'object')
      return {kind: 'session', token: data.token, user: data.user};
    return {kind: 'invalid'};
  }
  // A 6-digit app code (spaces allowed while typing) or a one-time backup code, sent as shown on the backup list
  // (spaces dropped, dashes kept; the server normalises case and dashes).
  function normalizeCode(value, backup = false) {
    if (backup) {
      const code = String(value || '').replace(/\s+/g, '');
      return /^[A-Za-z0-9-]{4,40}$/.test(code) ? code : '';
    }
    const code = String(value || '').replace(/[\s-]+/g, '');
    return /^\d{6}$/.test(code) ? code : '';
  }
  // Base32 secret in groups of four for typing into Google Authenticator / Aegis.
  function formatSecret(secret) {
    return (String(secret || '').replace(/[\s=]+/g, '').toUpperCase().match(/.{1,4}/g) || []).join(' ');
  }
  const isOtpauth = uri => /^otpauth:\/\/totp\/[^\s]+$/.test(String(uri || ''));

  // 429 from any endpoint: say how long to wait. Retry-After is seconds or an HTTP date. Sign-in lockouts keep
  // the server's own sentence (it names the minutes).
  function retryAfterSeconds(header, now = Date.now()) {
    if (header == null || header === '') return 0;
    const text = String(header).trim();
    if (/^\d+$/.test(text)) return Number(text);
    const at = Date.parse(text);
    return Number.isFinite(at) ? Math.max(0, Math.ceil((at - now) / 1000)) : 0;
  }
  function tooFastMessage(header, data = {}, {now = Date.now(), preferServer = false} = {}) {
    if (preferServer && data && data.error) return data.error;
    const seconds = retryAfterSeconds(header, now) || Number(data && data.retry_after) || 0;
    return seconds > 0 ? 'Thao tác quá nhanh, thử lại sau ' + Math.ceil(seconds) + ' giây.' : 'Thao tác quá nhanh, thử lại sau ít phút.';
  }

  const api = {serverLockState, idleState, idleMinutesOf, shouldPing, keepsCase, loginStep, normalizeCode, formatSecret, isOtpauth,
               retryAfterSeconds, tooFastMessage, DEFAULT_IDLE_MINUTES};
  root.FrontLogic = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
