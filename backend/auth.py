"""Doctor accounts and login sessions.

Accounts live in a JSON file outside the repository (WEB_USERS_FILE, default
~/prenatal_pubcasefinder/.deployment/users.json), managed with manage_users.py; passwords are stored as
scrypt hashes. Sessions are kept in memory only: a restart of the service asks everyone to log in again.
The service knows from the sessions who is working, and Model 1 is loaded only for them.
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
MIN_PASSWORD = 8
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
MAX_FAILURES, LOCK_SECONDS = 5, 300


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


class UserStore:
    """users.json: {"users": {username: {"name", "password", "disabled", "created"}}}; reread when it changes."""

    def __init__(self, path=None):
        self.path = Path(path or os.getenv('WEB_USERS_FILE') or DEFAULT_USERS_FILE)
        self.lock = threading.Lock()
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
            return dict(self.users)

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
            self.mtime = None


class Auth:
    """Accounts when users.json lists at least one user; else the shared WEB_ACCESS_TOKEN; else open (local use)."""

    def __init__(self, store=None, token=None, session_hours=12, idle_hours=4):
        self.store = store or UserStore()
        self.token = os.getenv('WEB_ACCESS_TOKEN', '') if token is None else token
        self.session_seconds, self.idle_seconds = session_hours * 3600, idle_hours * 3600
        self.lock = threading.Lock()
        self.sessions = {}   # sha256(token) -> {"user", "name", "created", "seen"}
        self.failures = {}   # username -> (count, locked_until)
        self.dummy = hash_password('timing-equaliser')

    @property
    def mode(self):
        return 'accounts' if self.store.all() else 'token' if self.token else 'none'

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def login(self, username, password):
        """Returns (session_token, user_info); raises PermissionError with a message for the doctor."""
        username = str(username or '').strip().lower()
        password = str(password or '')
        now = time.time()
        with self.lock:
            count, until = self.failures.get(username, (0, 0))
        if until > now:
            raise PermissionError(f'Đăng nhập sai nhiều lần. Thử lại sau {int(until - now) // 60 + 1} phút.')
        user = self.store.get(username) if USERNAME.match(username) else None
        ok = check_password(password, user['password'] if user else self.dummy) and user is not None
        with self.lock:
            if not ok:
                count += 1
                self.failures[username] = (0, now + LOCK_SECONDS) if count >= MAX_FAILURES else (count, 0)
                raise PermissionError('Tên đăng nhập hoặc mật khẩu không đúng.')
            self.failures.pop(username, None)
            token = secrets.token_urlsafe(32)
            self.sessions[self._key(token)] = {'user': username, 'name': user.get('name') or username,
                                               'created': now, 'seen': now}
        return token, {'username': username, 'name': user.get('name') or username}

    def session(self, token):
        """The session for a bearer token, refreshed; None when unknown, expired or the account was disabled."""
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
        if not self.store.get(s['user']):
            self.logout(token)
            return None
        s['seen'] = now
        return s

    def logout(self, token):
        with self.lock:
            self.sessions.pop(self._key(token or ''), None)

    def active(self, within_seconds):
        """Number of sessions with activity in the last within_seconds."""
        now = time.time()
        with self.lock:
            return sum(1 for s in self.sessions.values() if now - s['seen'] <= within_seconds)

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
