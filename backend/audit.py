"""Audit log: who did what and when, never case content (no report text, HPO or genes).

JSON lines in WEB_AUDIT_FILE (default ~/prenatal_pubcasefinder/.deployment/audit.jsonl, mode 600). Above 5 MB the
file moves to audit.jsonl.1 (one older file kept), so the log stays readable from the admin page.

Tamper evidence: every line carries "prev", the SHA-256 of the previous line as written (the first line ever:
64 zeros; the chain continues across rotation). Editing, inserting or deleting a line breaks the chain at that
point; `python audit.py verify [audit.jsonl.1 audit.jsonl]` reports the first broken line. Someone who can rewrite
the whole file can also rebuild the chain, so each line is also printed to stderr with the prefix [audit] and
kept by journald as a second copy (WEB_AUDIT_STDERR=0 turns that off).
"""
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

DEFAULT_AUDIT_FILE = Path.home() / 'prenatal_pubcasefinder/.deployment/audit.jsonl'
MAX_BYTES = 5 * 1024 * 1024
GENESIS = '0' * 64

EVENTS = {
    'login_ok': 'Đăng nhập', 'login_fail': 'Đăng nhập sai', 'login_locked': 'Đăng nhập khi đang bị khóa',
    'login_paused': 'Đăng nhập khi hệ thống tạm ngừng', 'logout': 'Đăng xuất', 'password_changed': 'Đổi mật khẩu',
    'user_created': 'Tạo tài khoản', 'user_updated': 'Sửa tài khoản', 'password_reset': 'Đặt lại mật khẩu',
    'user_deleted': 'Xóa tài khoản', 'sessions_ended': 'Buộc đăng xuất', 'gpu': 'Điều khiển GPU',
    'extract': 'Tách đoạn văn', 'match': 'Đối chiếu bệnh', 'auth_store_error': 'Lỗi đọc danh sách tài khoản',
    'rate_limited': 'Vượt giới hạn tốc độ', 'login_unlocked': 'Gỡ khóa đăng nhập',
    'totp_enabled': 'Bật xác thực 2 lớp', 'totp_fail': 'Nhập sai mã 2 lớp', 'totp_expired': 'Hết hạn bước mã 2 lớp',
    'totp_backup_used': 'Dùng mã dự phòng 2 lớp', 'totp_reset': 'Đặt lại xác thực 2 lớp',
    'totp_disabled': 'Tắt xác thực 2 lớp',
    'temp_password_expired': 'Mật khẩu tạm đã hết hạn',
    # Góp ý sửa mã / báo lỗi: only ids and counts, never the phrase, HPO codes, notes or report text.
    'correction_submitted': 'Gửi góp ý sửa mã', 'correction_approved': 'Duyệt góp ý sửa mã',
    'correction_rejected': 'Từ chối góp ý sửa mã', 'corrections_exported': 'Tải góp ý đã duyệt',
    'bug_report_created': 'Gửi báo lỗi', 'bug_report_updated': 'Cập nhật báo lỗi', 'bug_report_deleted': 'Xóa báo lỗi',
}


def line_hash(line):
    return hashlib.sha256(line.rstrip('\r\n').encode('utf-8')).hexdigest()


def last_line(path):
    """The last non-empty line of path, or None (reads at most the last 64 KB)."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode('utf-8', 'replace')
    except OSError:
        return None
    lines = [x for x in tail.splitlines() if x.strip()]
    return lines[-1] if lines else None


class AuditLog:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('WEB_AUDIT_FILE') or DEFAULT_AUDIT_FILE)
        self.lock = threading.Lock()
        self.echo = os.getenv('WEB_AUDIT_STDERR', '1') != '0'

    def write(self, event, user=None, **detail):
        entry = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'event': event, 'user': user or ''}
        entry.update({k: v for k, v in detail.items() if v not in (None, '') and k != 'prev'})
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                previous = last_line(self.path)
                if previous is None:  # new file: continue from the rotated one if it is there
                    previous = last_line(self.path.with_name(self.path.name + '.1'))
                entry['prev'] = line_hash(previous) if previous is not None else GENESIS
                line = json.dumps(entry, ensure_ascii=False) + '\n'
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + '.1'))
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'a', encoding='utf-8') as f:
                    f.write(line)
            if self.echo:
                print('[audit]', line.rstrip('\n'), file=sys.stderr, flush=True)
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
            entry.pop('prev', None)
            if (not user or entry.get('user') == user) and (not event or entry.get('event') == event):
                out.append(entry)
                if len(out) >= limit:
                    break
        return out


def verify(paths):
    """Check the hash chain over paths in order (oldest first). Returns (ok, report lines)."""
    report, previous, chained, legacy, checked = [], None, False, 0, 0
    for path in paths:
        try:
            lines = Path(path).read_text(encoding='utf-8').splitlines()
        except OSError as exc:
            return False, [f'{path}: không đọc được ({type(exc).__name__})']
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                prev = json.loads(line).get('prev')
            except ValueError:
                return False, report + [f'{path}:{number}: không phải JSON']
            if prev is None:
                if chained:
                    return False, report + [f'{path}:{number}: thiếu trường prev giữa chuỗi (dòng chèn thêm?)']
                legacy += 1
            else:
                expected = line_hash(previous) if previous is not None else GENESIS
                if prev != expected:
                    if previous is None:
                        report.append(f'{path}:{number}: nối tiếp một file cũ hơn không có ở đây')
                    else:
                        return False, report + [f'{path}:{number}: chuỗi băm bị đứt (dòng này hoặc dòng trước đã bị '
                                                f'sửa, xóa hoặc chèn)']
                chained = True
                checked += 1
            previous = line
    if legacy:
        report.append(f'{legacy} dòng cũ (trước khi có chuỗi băm) không kiểm được')
    report.append(f'OK: {checked} dòng khớp chuỗi băm')
    return True, report


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != 'verify':
        print('Dùng: python audit.py verify [audit.jsonl.1 audit.jsonl]  (cũ trước, mới sau)')
        return 2
    paths = argv[1:]
    if not paths:
        current = Path(os.getenv('WEB_AUDIT_FILE') or DEFAULT_AUDIT_FILE)
        older = current.with_name(current.name + '.1')
        paths = [str(p) for p in (older, current) if p.exists()]
    ok, report = verify(paths)
    print('\n'.join(report))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
