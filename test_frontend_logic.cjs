// node test_frontend_logic.cjs — pure sign-in security decisions from docs/idle.js (no browser needed).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const F = require('./docs/idle.js');
const PROD = 'https://adminpc-system-product-name.taila6ff46.ts.net';

test('server lock: public page on the default server is quiet and locked', () => {
  const s = F.serverLockState('ditruyenhungvuongorg.github.io', PROD, PROD);
  assert.deepEqual([s.local, s.nonDefault, s.warn, s.canChange], [false, false, false, false]);
  assert.equal(F.serverLockState('x.github.io', PROD + '/', PROD).warn, false, 'trailing slash is the same server');
});
test('server lock: public page on another server warns and cannot change it', () => {
  const s = F.serverLockState('ditruyenhungvuongorg.github.io', 'https://evil.example', PROD);
  assert.equal(s.warn, true); assert.equal(s.canChange, false); assert.equal(s.host, 'evil.example');
  assert.equal(F.serverLockState('x.github.io', '', PROD).warn, true, 'no server is not the default server');
});
test('server lock: a page served from localhost may change server without warning', () => {
  for (const host of ['localhost', '127.0.0.1', '[::1]', 'LOCALHOST', 'app.localhost']) {
    const s = F.serverLockState(host, 'http://127.0.0.1:8000', PROD);
    assert.equal(s.canChange, true, host); assert.equal(s.warn, false, host); assert.equal(s.nonDefault, true);
  }
  assert.equal(F.serverLockState('localhost.evil.com', 'https://evil.example', PROD).warn, true);
});

test('idle: active, warning in the last minute, expired at the limit', () => {
  const t0 = 1_000_000, idleMinutes = 30, at = s => F.idleState({now: t0 + s * 1000, lastActivity: t0, idleMinutes});
  assert.equal(at(0).phase, 'active');
  assert.equal(at(30 * 60 - 61).phase, 'active');
  assert.equal(at(30 * 60 - 60).phase, 'warn'); assert.equal(at(30 * 60 - 60).secondsLeft, 60);
  assert.equal(at(30 * 60 - 1).secondsLeft, 1);
  assert.equal(at(30 * 60).phase, 'expired'); assert.equal(at(99 * 60).remaining, 0);
});
test('idle: a long request pauses the countdown; activity restarts it', () => {
  const t0 = 5_000_000;
  assert.equal(F.idleState({now: t0 + 2 * 3600e3, lastActivity: t0, idleMinutes: 60, busy: true}).phase, 'active');
  assert.equal(F.idleState({now: t0 + 61 * 60e3, lastActivity: t0 + 60 * 60e3, idleMinutes: 60}).phase, 'active');
});
test('idle: missing or invalid idle_minutes falls back to 60; tiny limits still warn', () => {
  for (const v of [undefined, null, 0, -5, 'abc']) assert.equal(F.idleMinutesOf(v), 60);
  assert.equal(F.idleMinutesOf('15'), 15);
  const s = F.idleState({now: 31e3, lastActivity: 0, idleMinutes: 1});  // warning is at most half the limit
  assert.equal(s.phase, 'warn'); assert.equal(s.secondsLeft, 29);
});
test('keep-alive ping only after activity and at most once per interval', () => {
  assert.equal(F.shouldPing({now: 10 * 60e3, lastActivity: 9 * 60e3, lastPing: 0}), true);
  assert.equal(F.shouldPing({now: 4 * 60e3, lastActivity: 3 * 60e3, lastPing: 0}), false);
  assert.equal(F.shouldPing({now: 20 * 60e3, lastActivity: 1, lastPing: 2}), false, 'no activity since last ping');
});

test('login step: session, second factor, or invalid reply', () => {
  const s = F.loginStep({token: 'abc', user: {username: 'bs.an', name: 'An', role: 'doctor'}, model_state: 'loading'});
  assert.equal(s.kind, 'session'); assert.equal(s.token, 'abc');
  const t = F.loginStep({second_factor: true, pending: 'p1', user: {username: 'admin', name: 'Quản trị'}});
  assert.equal(t.kind, 'totp'); assert.equal(t.pending, 'p1'); assert.equal(t.user.name, 'Quản trị');
  assert.equal(F.loginStep({second_factor: true, pending: 'p1', token: 'x', user: {}}).kind, 'totp', 'second factor wins');
  for (const bad of [null, {}, {token: ''}, {token: 'x'}, {second_factor: true}, {second_factor: 'yes', pending: 'p'}])
    assert.equal(F.loginStep(bad).kind, 'invalid');
});
test('codes: six digits for the app, backup codes kept as typed without spaces', () => {
  assert.equal(F.normalizeCode(' 123 456 '), '123456'); assert.equal(F.normalizeCode('123-456'), '123456');
  for (const bad of ['12345', '1234567', 'abcdef', '']) assert.equal(F.normalizeCode(bad), '');
  assert.equal(F.normalizeCode(' abcd-1234 ', true), 'abcd-1234'); assert.equal(F.normalizeCode('a b', true), '');
  assert.equal(F.normalizeCode('<script>', true), '');
});
test('secret shown in groups of four; only otpauth TOTP links are displayed', () => {
  assert.equal(F.formatSecret('jbswy3dpehpk3pxpjbswy3dpehpk3pxp'), 'JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP');
  assert.equal(F.formatSecret('ABCDE='), 'ABCD E'); assert.equal(F.formatSecret(null), '');
  assert.equal(F.isOtpauth('otpauth://totp/Fe2Gen:bs.an?secret=ABC&issuer=Fe2Gen'), true);
  for (const bad of ['javascript:alert(1)', 'otpauth://hotp/x', 'https://x', 'otpauth://totp/a b']) assert.equal(F.isOtpauth(bad), false);
});
test('rate limit: wait time from Retry-After seconds or date; sign-in keeps the server sentence', () => {
  const now = Date.parse('2026-10-26T10:00:00Z');
  assert.equal(F.retryAfterSeconds('17'), 17);
  assert.equal(F.retryAfterSeconds('Mon, 26 Oct 2026 10:00:30 GMT', now), 30);
  assert.equal(F.retryAfterSeconds(null), 0); assert.equal(F.retryAfterSeconds('soon'), 0);
  assert.equal(F.tooFastMessage('12', {error: 'x'}), 'Thao tác quá nhanh, thử lại sau 12 giây.');
  assert.equal(F.tooFastMessage(null, {retry_after: 5}), 'Thao tác quá nhanh, thử lại sau 5 giây.');
  assert.equal(F.tooFastMessage(null, {}), 'Thao tác quá nhanh, thử lại sau ít phút.');
  assert.equal(F.tooFastMessage('60', {error: 'Đăng nhập sai nhiều lần.'}, {preferServer: true}), 'Đăng nhập sai nhiều lần.');
});
