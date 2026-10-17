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
import threading
import time
from pathlib import Path

DEFAULT_USERS_FILE = Path.home() / 'prenatal_pubcasefinder/.deployment/users.json'
USERNAME = re.compile(r'^[a-z0-9._-]{3,32}$')
ROLES = {'admin': 'Quản trị', 'doctor': 'Bác sĩ', 'lab': 'Phòng Lab'}
GPU_ROLES = ('admin', 'doctor')  # roles that use text extraction (Model 1 on the GPU); a doctor's login loads it,
                                 # an admin's only when they open text extraction
MIN_PASSWORD = 10
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
# A username is locked for 1, 2, 4 ... up to 15 minutes from its 5th wrong password in a row; more than 30 wrong
# passwords in a minute over all usernames (password spraying) pause every login for that minute.
LOCK_AFTER, LOCK_BASE, LOCK_MAX = 5, 60, 900
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


def public_user(username, user):
    return {'username': username, 'name': user.get('name') or username, 'role': user.get('role', 'doctor'),
            'disabled': bool(user.get('disabled')), 'must_change': bool(user.get('must_change')),
            'created': user.get('created', ''), 'last_login': user.get('last_login', '')}


class UserStore:
    """users.json: {"users": {username: {"name", "role", "password", "disabled", "must_change", "created",
    "last_login"}}}; reread when it changes on disk (manage_users.py)."""

    def __init__(self, path=None):
        self.path = Path(path or os.getenv('WEB_USERS_FILE') or DEFAULT_USERS_FILE)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.users, self.mtime = {}, None

    def _refresh(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            self.users, self.mtime = {}, None
            return
        if mtime != self.mtime:
            self.users = json.loads(self.path.read_text(encoding='utf-8')).get('users', {})
            self.mtime = mtime

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

    def modify(self, change):
        """Read the current file, apply change(users) and save; returns what change returns."""
        with self.write_lock:
            with self.lock:
                self.mtime = None
            users = self.all()
            result = change(users)
            self.save(users)
            return result


class Auth:
    """Accounts when users.json lists at least one user; else the shared WEB_ACCESS_TOKEN; else open (local use)."""

    def __init__(self, store=None, token=None, session_hours=12, idle_hours=4):
        self.store = store or UserStore()
        self.token = os.getenv('WEB_ACCESS_TOKEN', '') if token is None else token
        self.session_seconds, self.idle_seconds = session_hours * 3600, idle_hours * 3600
        self.lock = threading.Lock()
        self.sessions = {}   # sha256(token) -> {"user", "name", "role", "must_change", "created", "seen"}
        self.failures = {}   # username -> (count, locked_until, last_failure)
        self.recent = []     # times of wrong passwords, all usernames
        self.dummy = hash_password('timing-equaliser')

    @property
    def mode(self):
        return 'accounts' if self.store.all() else 'token' if self.token else 'none'

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    # ------------------------------------------------------------------ login
    def login(self, username, password):
        """Returns (session_token, user_info); raises LoginRefused with a message for the user."""
        username = str(username or '').strip().lower()[:64]
        password = str(password or '')[:200]
        now = time.time()
        with self.lock:
            self.recent = [t for t in self.recent if now - t < GLOBAL_WINDOW]
            if len(self.recent) >= GLOBAL_FAILURES:
                raise LoginRefused('Hệ thống tạm ngừng nhận đăng nhập 1 phút vì có nhiều lần nhập sai.', 'login_paused')
            count, until, last = self.failures.get(username, (0, 0, 0))
            if now - last > 3600:
                count = 0
        if until > now:
            raise LoginRefused(f'Đăng nhập sai nhiều lần. Thử lại sau {int(until - now) // 60 + 1} phút.', 'login_locked')
        user = self.store.get(username) if USERNAME.match(username) else None
        ok = check_password(password, user['password'] if user else self.dummy) and user is not None
        with self.lock:
            if not ok:
                count += 1
                self.recent.append(now)
                lock = min(LOCK_MAX, LOCK_BASE * 2 ** (count - LOCK_AFTER)) if count >= LOCK_AFTER else 0
                self.failures[username] = (count, now + lock, now)
                raise LoginRefused('Tên đăng nhập hoặc mật khẩu không đúng.')
            self.failures.pop(username, None)
        info = public_user(username, user)
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[self._key(token)] = {'user': username, 'name': info['name'], 'role': info['role'],
                                               'must_change': info['must_change'], 'created': now, 'seen': now}

        def stamp(users):
            if username in users:
                users[username]['last_login'] = time.strftime('%Y-%m-%d %H:%M')
        self.store.modify(stamp)
        return token, {k: info[k] for k in ('username', 'name', 'role', 'must_change')}

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
                 must_change=bool(user.get('must_change')), seen=now)
        return s

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
            users[username].update(password=hash_password(temp), must_change=True)
        self.store.modify(apply)
        self.end_sessions(username)
        with self.lock:
            self.failures.pop(username, None)  # a reset also lifts a lock
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
