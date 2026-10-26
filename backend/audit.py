"""Audit log: who did what and when, never case content (no report text, HPO or genes).

JSON lines in WEB_AUDIT_FILE (default ~/prenatal_pubcasefinder/.deployment/audit.jsonl, mode 600). Above 5 MB the
file moves to audit.jsonl.1 (one older file kept), so the log stays readable from the admin page.
"""
import json
import os
import threading
import time
from pathlib import Path

DEFAULT_AUDIT_FILE = Path.home() / 'prenatal_pubcasefinder/.deployment/audit.jsonl'
MAX_BYTES = 5 * 1024 * 1024

EVENTS = {
    'login_ok': 'Đăng nhập', 'login_fail': 'Đăng nhập sai', 'login_locked': 'Đăng nhập khi đang bị khóa',
    'login_paused': 'Đăng nhập khi hệ thống tạm ngừng', 'logout': 'Đăng xuất', 'password_changed': 'Đổi mật khẩu',
    'user_created': 'Tạo tài khoản', 'user_updated': 'Sửa tài khoản', 'password_reset': 'Đặt lại mật khẩu',
    'user_deleted': 'Xóa tài khoản', 'sessions_ended': 'Buộc đăng xuất', 'gpu': 'Điều khiển GPU',
    'extract': 'Tách đoạn văn', 'match': 'Đối chiếu bệnh', 'auth_store_error': 'Lỗi đọc danh sách tài khoản',
    'rate_limited': 'Vượt giới hạn tốc độ', 'login_unlocked': 'Gỡ khóa đăng nhập',
}


class AuditLog:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('WEB_AUDIT_FILE') or DEFAULT_AUDIT_FILE)
        self.lock = threading.Lock()

    def write(self, event, user=None, **detail):
        entry = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'event': event, 'user': user or ''}
        entry.update({k: v for k, v in detail.items() if v not in (None, '')})
        line = json.dumps(entry, ensure_ascii=False) + '\n'
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + '.1'))
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'a', encoding='utf-8') as f:
                    f.write(line)
        except OSError as exc:  # the log must never break a request
            print('Audit log not written:', exc, flush=True)

    def tail(self, limit=200, user='', event=''):
        """Newest entries first, optionally only one user or one event."""
        try:
            with self.lock:
                lines = self.path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (not user or entry.get('user') == user) and (not event or entry.get('event') == event):
                out.append(entry)
                if len(out) >= limit:
                    break
        return out
