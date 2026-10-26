"""Accounts, roles and login sessions.

Accounts live in a JSON file outside the repository (WEB_USERS_FILE, default
~/prenatal_pubcasefinder/.deployment/users.json), passwords as scrypt hashes. The first admin is created on the
server with manage_users.py; admins manage the other accounts on the web. Roles:
  admin  - everything, plus the admin page
  doctor - HPO search, text extraction with Model 1 (GPU), disease matching, lab sheet
  lab    - HPO search, disease matching, lab sheet; no text extraction, so a lab login never loads the GPU
Sessions are kept in memory only: a restart of the service asks everyone to log in again.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

import totp

DEFAULT_USERS_FILE = Path.home() / 'prenatal_pubcasefinder/.deployment/users.json'
USERNAME = re.compile(r'^[a-z0-9._-]{3,32}$')
ROLES = {'admin': 'Quản trị', 'doctor': 'Bác sĩ', 'lab': 'Phòng Lab'}
GPU_ROLES = ('admin', 'doctor')  # roles that use text extraction (Model 1 on the GPU); a doctor's login loads it,
                                 # an admin's only when they open text extraction
MIN_PASSWORD = 10
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
# A username is locked for 1, 2, 4 ... up to WEB_LOCK_MAX_SECONDS (default 5 minutes) from its 5th wrong password
# in a row; more than 30 wrong passwords in a minute over all usernames (password spraying) pause every login for
# that minute. The lock is kept short because anyone can trigger it for a known username (serve_web also limits
# login attempts per connection source and per username); an admin can lift it on the admin page.
def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


def _env_float(name, default):
    try:
        value = float(os.getenv(name, default))
        return value if value > 0 else float(default)
    except ValueError:
        return float(default)


LOCK_AFTER, LOCK_BASE, LOCK_MAX = 5, 60, _env_int('WEB_LOCK_MAX_SECONDS', 300)
GLOBAL_FAILURES, GLOBAL_WINDOW = 30, 60
COMMON_PASSWORDS = {
    '1234567890', '0123456789', '12345678910', '1234512345', '1111111111', '0000000000', '1122334455', '9876543210',
    'qwertyuiop', 'password12', 'password123', 'password1234', 'passw0rd123', 'iloveyou123', 'abcd123456',
    'abc1234567', 'qwerty1234', 'qwerty123456', 'asdfghjkl1', 'admin12345', 'administrator', 'admin123456',
    'matkhau123', 'matkhau1234', 'matkhaumoi', 'matkhaucuatoi', 'anhyeuem123', 'emyeuanh123', 'benhvien123',
    'bacsi12345', 'fe2gen1234', 'fe2gen2026', 'hungvuong123', 'ditruyen123', 'letmein123', 'welcome123',
    'changeme123', 'trustno1234', 'football123', 'princess123', 'sunshine123', 'zaq12wsxcde', '1q2w3e4r5t',
}
TEMP_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'  # no 0/o, 1/l/i


class LoginRefused(PermissionError):
    def __init__(self, message, event='login_fail'):
        super().__init__(message)
        self.event = event


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return 'scrypt${n}${r}${p}$'.format(**SCRYPT) + base64.b64encode(salt).decode() + '$' + base64.b64encode(digest).decode()


def check_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split('$')
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def password_problem(password, username=''):
    """Why a new password is refused, or None."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f'Mật khẩu cần ít nhất {MIN_PASSWORD} ký tự.'
    if len(password) > 200:
        return 'Mật khẩu quá dài.'
    low = password.lower()
    if low in COMMON_PASSWORDS or len(set(low)) < 4:
        return 'Mật khẩu quá dễ đoán. Dùng một câu ngắn hoặc kết hợp chữ, số khó đoán.'
    if username and username.lower() in low:
        return 'Mật khẩu không được chứa tên đăng nhập.'
    return None


def temporary_password():
    """Readable one-time password, e.g. "k7mq-x2rf-9tdh" (about 74 bits)."""
    raw = ''.join(secrets.choice(TEMP_ALPHABET) for _ in range(15))
    return '-'.join(raw[i:i + 5] for i in range(0, 15, 5))


def has_totp(user):
    return bool(isinstance(user.get('totp'), dict) and user['totp'].get('enabled') and user['totp'].get('secret'))


def public_user(username, user):
    """What may leave the server about an account: never the password hash or the TOTP secret."""
    return {'username': username, 'name': user.get('name') or username, 'role': user.get('role', 'doctor'),
            'disabled': bool(user.get('disabled')), 'must_change': bool(user.get('must_change')),
            'created': user.get('created', ''), 'last_login': user.get('last_login', ''),
            'totp_enabled': has_totp(user), 'totp': has_totp(user), 'temp_expired': temp_expired(user)}


def temp_expired(user, now=None):
    """A temporary password past its 'temp_until' (accounts made before this field never expire)."""
    until = user.get('temp_until') if user.get('must_change') else None
    return bool(until) and (time.time() if now is None else now) > until


class StoreError(ValueError):
    """users.json cannot be read or written safely; the message is shown to the user."""


def env_flag(name, default):
    return os.getenv(name, default).strip().lower() not in ('0', 'false', 'no', 'off', '')


class UserStore:
    """users.json: {"users": {username: {"name", "role", "password", "disabled", "must_change", "created",
    "last_login"}}}; reread when it changes on disk (manage_users.py).

    problem is None while the file reads well, else 'missing', 'unreadable' or 'corrupt'. A missing file means no
    accounts. An unreadable or corrupt file keeps the last good copy in memory (if any) so logged-in doctors keep
    working, and writes are refused so the broken file is not overwritten blindly. on_problem(kind) is called
    once per new problem (serve_web writes it to the audit log)."""

    def __init__(self, path=None, on_problem=None):
        self.path = Path(path or os.getenv('WEB_USERS_FILE') or DEFAULT_USERS_FILE)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.users, self.mtime = {}, None
        self.problem, self.problem_key = None, None
        self.on_problem = on_problem

    def _report(self, kind, key, detail=''):
        self.problem = kind
        if key != self.problem_key:
            self.problem_key = key
            print(f'[auth] users.json {kind}: {self.path} {detail}'.rstrip(), file=sys.stderr, flush=True)
            if self.on_problem:
                try:
                    self.on_problem(kind)
                except Exception:  # reporting must never break a request
                    pass

    def _refresh(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self.users, self.mtime = {}, None
            self._report('missing', 'missing')
            return
        except OSError as exc:
            self._report('unreadable', ('unreadable', type(exc).__name__), type(exc).__name__)
            return
        if mtime == self.mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            users = data.get('users') if isinstance(data, dict) else None
            if not isinstance(users, dict) or not all(isinstance(u, dict) for u in users.values()):
                raise ValueError('no "users" object')
        except OSError as exc:
            self._report('unreadable', ('unreadable', mtime), type(exc).__name__)
            return
        except ValueError as exc:  # JSON syntax (JSONDecodeError, UnicodeDecodeError) or shape
            self._report('corrupt', ('corrupt', mtime), type(exc).__name__)
            return
        self.users, self.mtime = users, mtime
        self.problem, self.problem_key = None, None

    def all(self):
        with self.lock:
            self._refresh()
            return {k: dict(v) for k, v in self.users.items()}

    def get(self, username):
        user = self.all().get(username)
        return None if not user or user.get('disabled') else user

    def save(self, users):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'users': users}, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
        with self.lock:
            self.users, self.mtime = {k: dict(v) for k, v in users.items()}, None
            self.problem, self.problem_key = None, None

    def modify(self, change):
        """Read the current file, apply change(users) and save; returns what change returns."""
        with self.write_lock:
            with self.lock:
                self.mtime = None
            users = self.all()
            if self.problem in ('unreadable', 'corrupt'):
                raise StoreError('Không đọc được danh sách tài khoản; chưa lưu thay đổi. Liên hệ quản trị viên.')
            result = change(users)
            self.save(users)
            return result


LOCKED_MESSAGE = 'Hệ thống tạm khóa: không đọc được danh sách tài khoản. Liên hệ quản trị viên.'


class Auth:
    """Accounts when users.json lists at least one user.

    With require_accounts (WEB_REQUIRE_ACCOUNTS, default on) a missing, unreadable, corrupt or empty users.json
    gives mode 'locked': the service refuses every API request instead of opening up. Only with
    WEB_REQUIRE_ACCOUNTS=0 (local use) does it fall back to the shared WEB_ACCESS_TOKEN, then to no login."""

    PENDING_SECONDS, TOTP_TRIES = 300, 5  # second step of a login: 5 minutes, 5 codes

    def __init__(self, store=None, token=None, session_hours=None, idle_hours=None, require_accounts=None, admin_2fa=None,
                 failures_file=None):
        self.store = store or UserStore()
        self.token = os.getenv('WEB_ACCESS_TOKEN', '') if token is None else token
        self.require_accounts = (env_flag('WEB_REQUIRE_ACCOUNTS', '1') if require_accounts is None
                                 else bool(require_accounts))
        # WEB_ADMIN_2FA_REQUIRED (default on): an admin session without a verified second factor can only set one up.
        self.admin_2fa = env_flag('WEB_ADMIN_2FA_REQUIRED', '1') if admin_2fa is None else bool(admin_2fa)
        self.pending = {}    # sha256(pending token) -> {"user", "created", "tries"}: password right, code not yet
        # WEB_SESSION_HOURS (12) from login; WEB_IDLE_MINUTES (60) without a request; the page logs out by itself
        # at the same idle limit. WEB_TEMP_PASSWORD_HOURS (72): a one-time password not used by then stops working.
        session_hours = _env_float('WEB_SESSION_HOURS', 12) if session_hours is None else session_hours
        idle_hours = _env_float('WEB_IDLE_MINUTES', 60) / 60 if idle_hours is None else idle_hours
        self.session_seconds, self.idle_seconds = session_hours * 3600, idle_hours * 3600
        self.temp_seconds = _env_float('WEB_TEMP_PASSWORD_HOURS', 72) * 3600
        self.lock = threading.Lock()
        self.sessions = {}   # sha256(token) -> {"user", "name", "role", "must_change", "created", "seen"}
        self.failures = {}   # username -> (count, locked_until, last_failure); kept in failures_file across restarts
        self.failures_file = Path(failures_file or os.getenv('WEB_LOGIN_FAILURES_FILE')
                                  or self.store.path.with_name('login_failures.json'))
        self._load_failures()
        self.recent = []     # times of wrong passwords, all usernames
        self.dummy = hash_password('timing-equaliser')

    @property
    def mode(self):
        if self.store.all():
            return 'accounts'
        if self.require_accounts:
            return 'locked'
        return 'token' if self.token else 'none'

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    # ------------------------------------------------------------------ login
    MAX_FAILURE_KEYS = 2000

    def _load_failures(self):
        try:
            data = json.loads(self.failures_file.read_text(encoding='utf-8'))
            now = time.time()
            self.failures = {str(k)[:64]: (int(v[0]), float(v[1]), float(v[2])) for k, v in data.items()
                             if isinstance(v, list) and len(v) == 3 and now - float(v[2]) <= 3600}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            print('[auth] login_failures.json not read:', type(exc).__name__, file=sys.stderr, flush=True)

    def _save_failures(self):
        """Write the lock counters (mode 600); call with self.lock held. Entries older than an hour are dropped and
        at most MAX_FAILURE_KEYS kept, so names sprayed by an attacker cannot grow the file without bound."""
        now = time.time()
        live = sorted(((k, v) for k, v in self.failures.items() if now - v[2] <= 3600), key=lambda kv: -kv[1][2])
        self.failures = dict(live[:self.MAX_FAILURE_KEYS])
        try:
            self.failures_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.failures_file.with_suffix('.tmp')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({k: list(v) for k, v in self.failures.items()}, f)
            os.replace(tmp, self.failures_file)
        except OSError as exc:  # the lock still works in memory
            print('[auth] login_failures.json not written:', type(exc).__name__, file=sys.stderr, flush=True)

    def _forget(self, username):
        """Clear username's wrong-password count; call with self.lock held."""
        if self.failures.pop(username, None) is not None:
            self._save_failures()

    def _failed(self, username, now):
        """Count a wrong password or code toward username's lock; call with self.lock held."""
        count, _, last = self.failures.get(username, (0, 0, 0))
        count = 0 if now - last > 3600 else count
        count += 1
        self.recent.append(now)
        lock = min(LOCK_MAX, LOCK_BASE * 2 ** (count - LOCK_AFTER)) if count >= LOCK_AFTER else 0
        self.failures[username] = (count, now + lock, now)
        self._save_failures()

    def _check_lock(self, username, now):
        with self.lock:
            self.recent = [t for t in self.recent if now - t < GLOBAL_WINDOW]
            if len(self.recent) >= GLOBAL_FAILURES:
                raise LoginRefused('Hệ thống tạm ngừng nhận đăng nhập 1 phút vì có nhiều lần nhập sai.', 'login_paused')
            _, until, _ = self.failures.get(username, (0, 0, 0))
        if until > now:
            raise LoginRefused(f'Đăng nhập sai nhiều lần. Thử lại sau {int(until - now) // 60 + 1} phút.', 'login_locked')

    def login(self, username, password):
        """Returns (session_token, user_info); raises LoginRefused with a message for the user.
        When the account has 2FA, returns (None, {"second_factor": True, "pending": <token for login_totp>})."""
        username = str(username or '').strip().lower()[:64]
        password = str(password or '')[:200]
        now = time.time()
        self._check_lock(username, now)
        user = self.store.get(username) if USERNAME.match(username) else None
        ok = check_password(password, user['password'] if user else self.dummy) and user is not None
        if not ok:
            with self.lock:
                self._failed(username, now)
            raise LoginRefused('Tên đăng nhập hoặc mật khẩu không đúng.')
        if temp_expired(user, now):
            raise LoginRefused('Mật khẩu tạm đã hết hạn; nhờ quản trị viên đặt lại.', 'temp_password_expired')
        if has_totp(user):  # the wrong-password count is cleared only once the code is right too
            pending = secrets.token_urlsafe(32)
            with self.lock:
                self.pending = {k: v for k, v in self.pending.items() if now - v['created'] <= self.PENDING_SECONDS}
                self.pending[self._key(pending)] = {'user': username, 'created': now, 'tries': 0}
            return None, {'second_factor': True, 'pending': pending,
                          'user': {'username': username, 'name': user.get('name') or username}}
        with self.lock:
            self._forget(username)
        return self._open_session(username, user, now, mfa=False)

    def login_totp(self, pending, given):
        """Second step: a 6-digit code (or a backup code) for a pending login. Returns (token, info, used_backup)."""
        now, key = time.time(), self._key(str(pending or ''))
        with self.lock:
            p = self.pending.get(key)
            if p and now - p['created'] > self.PENDING_SECONDS:
                del self.pending[key]
                p = None
        if not p:
            raise LoginRefused('Bước xác thực đã hết hạn. Vui lòng đăng nhập lại.', 'totp_expired')
        username = p['user']
        self._check_lock(username, now)
        user = self.store.get(username)
        if not user or not has_totp(user):
            with self.lock:
                self.pending.pop(key, None)
            raise LoginRefused('Bước xác thực đã hết hạn. Vui lòng đăng nhập lại.', 'totp_expired')
        cfg, given = user['totp'], str(given or '').strip()[:20]
        step = totp.verify(cfg['secret'], given, t=now, after=cfg.get('last'))
        backup = None
        if step is None and len(given.replace('-', '')) == 8:
            digest = totp.hash_backup(given)
            backup = next((h for h in cfg.get('backup', []) if hmac.compare_digest(h, digest)), None)
        if step is None and backup is None:
            with self.lock:
                self._failed(username, now)
                p['tries'] += 1
                gone = p['tries'] >= self.TOTP_TRIES
                if gone:
                    self.pending.pop(key, None)
            raise LoginRefused('Mã xác thực không đúng.' + (' Vui lòng đăng nhập lại.' if gone else ''), 'totp_fail')

        def spend(users):
            t = users.get(username, {}).get('totp') or {}
            if backup is not None:
                t['backup'] = [h for h in t.get('backup', []) if h != backup]
            else:
                t['last'] = step  # the same code cannot log in twice
        with self.lock:
            if self.pending.pop(key, None) is None:  # two tabs racing with one pending login
                raise LoginRefused('Bước xác thực đã hết hạn. Vui lòng đăng nhập lại.', 'totp_expired')
            self._forget(username)
        self.store.modify(spend)
        token, info = self._open_session(username, user, now, mfa=True)
        return token, info, backup is not None

    def _open_session(self, username, user, now, mfa):
        info = public_user(username, user)
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[self._key(token)] = {'user': username, 'name': info['name'], 'role': info['role'],
                                               'must_change': info['must_change'], 'created': now, 'seen': now,
                                               'mfa': mfa, 'totp': info['totp']}

        def stamp(users):
            if username in users:
                users[username]['last_login'] = time.strftime('%Y-%m-%d %H:%M')
        try:
            self.store.modify(stamp)
        except (StoreError, OSError):  # a read-only or broken file must not block a valid login
            pass
        return token, {k: info[k] for k in ('username', 'name', 'role', 'must_change')}

    def needs_2fa(self, s):
        """An admin session without a verified second factor (WEB_ADMIN_2FA_REQUIRED)."""
        return bool(s and self.admin_2fa and s['role'] == 'admin' and not s.get('mfa'))

    def describe(self, s):
        """The session as the page sees it. totp_required: an admin who must turn on 2FA before anything else;
        idle_minutes: this session's idle limit (the page logs out by itself after it). 'totp', 'needs_2fa' and
        'session_idle_minutes' are older names of the same fields."""
        required, idle = self.needs_2fa(s), round(self.idle_seconds / 60)
        return {'username': s['user'], 'name': s['name'], 'role': s['role'], 'must_change': s['must_change'],
                'totp_enabled': bool(s.get('totp')), 'totp_required': required, 'idle_minutes': idle,
                'totp': bool(s.get('totp')), 'needs_2fa': required, 'session_idle_minutes': idle}

    def session_fields(self, s):
        """Top-level fields of /api/session and the login answer (also without a personal session)."""
        return {'totp_enabled': bool(s and s.get('totp')), 'totp_required': self.needs_2fa(s),
                'idle_minutes': round(self.idle_seconds / 60)}

    # ------------------------------------------------------------------ second factor (TOTP)
    def totp_setup(self, token):
        """A new secret for the logged-in user, kept as totp.pending_secret in users.json (not yet in force) until
        a code from the app confirms it."""
        s = self.session(token)
        if not s:
            raise PermissionError('Phiên đăng nhập đã hết hạn.')
        secret = totp.generate_secret()

        def apply(users):
            if has_totp(users[s['user']]):
                raise ValueError('Xác thực 2 lớp đã bật. Đổi điện thoại: tắt rồi bật lại, hoặc nhờ quản trị viên đặt lại 2FA.')
            users[s['user']]['totp'] = {'pending_secret': secret}
        self.store.modify(apply)
        return secret, totp.uri(s['user'], secret)

    def totp_enable(self, token, given):
        """Confirm the pending secret with a code; returns the 8 backup codes (shown once)."""
        s = self.session(token)
        if not s:
            raise PermissionError('Phiên đăng nhập đã hết hạn.')
        stored = self.store.get(s['user']) or {}
        secret = (stored.get('totp') or {}).get('pending_secret') if not has_totp(stored) else None
        if not secret:
            raise ValueError('Bấm "Bật xác thực 2 lớp" để tạo khóa mới trước.')
        step = totp.verify(secret, given)
        if step is None:
            raise ValueError('Mã chưa đúng. Kiểm tra giờ trên điện thoại và nhập mã đang hiện.')
        codes = totp.backup_codes()

        def apply(users):
            if (users[s['user']].get('totp') or {}).get('pending_secret') != secret:
                raise ValueError('Khóa 2 lớp vừa đổi ở nơi khác; bấm "Bật xác thực 2 lớp" lại.')
            users[s['user']]['totp'] = {'secret': secret, 'enabled': True, 'last': step,
                                        'backup': [totp.hash_backup(c) for c in codes],
                                        'since': time.strftime('%Y-%m-%d %H:%M')}
        self.store.modify(apply)
        s.update(mfa=True, totp=True)
        self.end_sessions(s['user'], keep=token)  # other devices log in again, now with a code
        return codes

    def totp_disable(self, token, password, given):
        """The user turns their own 2FA off with their password and a current code (or a backup code).
        Refused for admins while WEB_ADMIN_2FA_REQUIRED is on."""
        s = self.session(token)
        if not s:
            raise PermissionError('Phiên đăng nhập đã hết hạn.')
        user = self.store.get(s['user']) or {}
        if not has_totp(user):
            raise ValueError('Xác thực 2 lớp chưa bật.')
        if self.admin_2fa and s['role'] == 'admin':
            raise ValueError('Tài khoản quản trị phải giữ xác thực 2 lớp. Đổi điện thoại: nhờ quản trị viên khác đặt lại.')
        if not check_password(str(password or '')[:200], user['password']):
            raise ValueError('Mật khẩu hiện tại không đúng.')
        cfg, given = user['totp'], str(given or '').strip()[:20]
        if totp.verify(cfg['secret'], given, after=cfg.get('last')) is None and not any(
                hmac.compare_digest(h, totp.hash_backup(given)) for h in cfg.get('backup', [])):
            raise ValueError('Mã xác thực không đúng.')

        def apply(users):
            users[s['user']].pop('totp', None)
        self.store.modify(apply)
        s['totp'] = False

    def locked_until(self, username):
        """When username's lock after wrong passwords ends (epoch seconds), or 0."""
        with self.lock:
            _, until, _ = self.failures.get(username, (0, 0, 0))
        return until if until > time.time() else 0

    def totp_reset(self, actor, username):
        """Admin: remove username's second factor (lost phone); the user is logged out everywhere."""
        if username == actor:
            raise ValueError('Không tự đặt lại 2FA của chính mình; nhờ quản trị viên khác hoặc dùng manage_users.py.')

        def apply(users):
            if username not in users:
                raise ValueError('Không có tài khoản ' + str(username))
            if not users[username].pop('totp', None):
                raise ValueError(f'{username} chưa bật xác thực 2 lớp.')
        self.store.modify(apply)
        self.end_sessions(username)

    def session(self, token):
        """The session for a bearer token, refreshed from users.json (role and name changes apply at once);
        None when unknown, expired or the account was disabled or removed."""
        if not token:
            return None
        now, key = time.time(), self._key(token)
        with self.lock:
            s = self.sessions.get(key)
            if not s:
                return None
            if now - s['created'] > self.session_seconds or now - s['seen'] > self.idle_seconds:
                del self.sessions[key]
                return None
        user = self.store.get(s['user'])
        if not user:
            self.logout(token)
            return None
        s.update(name=user.get('name') or s['user'], role=user.get('role', 'doctor'),
                 must_change=bool(user.get('must_change')), totp=has_totp(user), seen=now)
        return s

    def locked_seconds(self, username):
        """Seconds left on username's lock after wrong passwords (0 when not locked)."""
        with self.lock:
            _, until, _ = self.failures.get(username, (0, 0, 0))
        return max(0, int(until - time.time()))

    def unlock(self, username):
        """Forget username's wrong passwords (admin page); True when it was locked."""
        was = self.locked_seconds(username) > 0
        with self.lock:
            self._forget(username)
        return was

    def logout(self, token):
        with self.lock:
            self.sessions.pop(self._key(token or ''), None)

    def end_sessions(self, username, keep=None):
        """Log out every session of username (except the token keep); returns how many."""
        skip = self._key(keep) if keep else None
        with self.lock:
            gone = [k for k, s in self.sessions.items() if s['user'] == username and k != skip]
            for k in gone:
                del self.sessions[k]
        return len(gone)

    def active(self, within_seconds, roles=None):
        """Number of sessions with activity in the last within_seconds (optionally only these roles)."""
        now = time.time()
        with self.lock:
            return sum(1 for s in self.sessions.values()
                       if now - s['seen'] <= within_seconds and (roles is None or s['role'] in roles))

    def list_sessions(self):
        now = time.time()
        with self.lock:
            live = [s for s in self.sessions.values()
                    if now - s['created'] <= self.session_seconds and now - s['seen'] <= self.idle_seconds]
        return sorted(({'username': s['user'], 'name': s['name'], 'role': s['role'],
                        'since': time.strftime('%Y-%m-%d %H:%M', time.localtime(s['created'])),
                        'idle_minutes': int((now - s['seen']) // 60)} for s in live), key=lambda s: s['idle_minutes'])

    def check(self, header):
        """Authorization header -> (allowed, session or None)."""
        mode = self.mode
        if mode == 'locked':
            return False, None
        if mode == 'none':
            return True, None
        token = header[7:] if header.startswith('Bearer ') else ''
        if mode == 'token':
            return hmac.compare_digest(token.encode(), self.token.encode()), None
        s = self.session(token)
        return s is not None, s

    # ------------------------------------------------------------------ own password
    def change_password(self, token, old, new):
        s = self.session(token)
        if not s:
            raise PermissionError('Phiên đăng nhập đã hết hạn.')
        user = self.store.get(s['user'])
        if not check_password(str(old or ''), user['password']):
            raise ValueError('Mật khẩu hiện tại không đúng.')
        if old == new:
            raise ValueError('Mật khẩu mới cần khác mật khẩu hiện tại.')
        problem = password_problem(new, s['user'])
        if problem:
            raise ValueError(problem)
        digest = hash_password(new)

        def apply(users):
            users[s['user']].update(password=digest, must_change=False)
            users[s['user']].pop('temp_until', None)
        self.store.modify(apply)
        s['must_change'] = False
        self.end_sessions(s['user'], keep=token)  # other devices log in again with the new password

    # ------------------------------------------------------------------ admin
    def create_user(self, username, name, role):
        """New account with a one-time password the user must change; returns that password."""
        username = str(username or '').strip().lower()
        name = str(name or '').strip()[:80] or username
        if not USERNAME.match(username):
            raise ValueError('Tên đăng nhập gồm 3–32 ký tự a-z, 0-9, dấu chấm, gạch ngang hoặc gạch dưới.')
        if role not in ROLES:
            raise ValueError('Vai trò không hợp lệ.')
        temp = temporary_password()

        def add(users):
            if username in users:
                raise ValueError('Tên đăng nhập đã có.')
            users[username] = {'name': name, 'role': role, 'password': hash_password(temp), 'disabled': False,
                               'temp_until': int(time.time() + self.temp_seconds),
                               'must_change': True, 'created': time.strftime('%Y-%m-%d %H:%M')}
        self.store.modify(add)
        return temp

    def update_user(self, actor, username, name=None, role=None, disabled=None):
        """Change name, role or locked state; returns the changed fields. The last active admin stays."""
        if role is not None and role not in ROLES:
            raise ValueError('Vai trò không hợp lệ.')
        changes = {}

        def apply(users):
            user = users.get(username)
            if not user:
                raise ValueError('Không có tài khoản ' + str(username))
            if name is not None and str(name).strip() and str(name).strip()[:80] != user.get('name'):
                changes['name'] = str(name).strip()[:80]
            if role is not None and role != user.get('role', 'doctor'):
                changes['role'] = role
            if disabled is not None and bool(disabled) != bool(user.get('disabled')):
                if disabled and username == actor:
                    raise ValueError('Không thể tự khóa tài khoản đang dùng.')
                changes['disabled'] = bool(disabled)
            user.update(changes)
            self._keep_an_admin(users)
        self.store.modify(apply)
        if changes.get('disabled'):
            self.end_sessions(username)
        return changes

    def reset_password(self, username):
        temp = temporary_password()

        def apply(users):
            if username not in users:
                raise ValueError('Không có tài khoản ' + str(username))
            users[username].update(password=hash_password(temp), must_change=True,
                                   temp_until=int(time.time() + self.temp_seconds))
        self.store.modify(apply)
        self.end_sessions(username)
        with self.lock:
            self._forget(username)  # a reset also lifts a lock
        return temp

    def delete_user(self, actor, username):
        if username == actor:
            raise ValueError('Không thể tự xóa tài khoản đang dùng.')

        def apply(users):
            if username not in users:
                raise ValueError('Không có tài khoản ' + str(username))
            del users[username]
            self._keep_an_admin(users)
        self.store.modify(apply)
        self.end_sessions(username)

    @staticmethod
    def _keep_an_admin(users):
        if not any(u.get('role') == 'admin' and not u.get('disabled') for u in users.values()):
            raise ValueError('Cần giữ ít nhất một tài khoản quản trị đang hoạt động.')
