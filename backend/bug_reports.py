"""Bug reports ("Báo lỗi") with screenshots, kept on the server for the admin page; no e-mail leaves the host.

Directory WEB_BUG_REPORTS_DIR (default ~/prenatal_pubcasefinder/.deployment/bug_reports, mode 700):
index.json (600, written atomically) holds the metadata, {"items": [REPORT, ...]} with
REPORT = {"id", "time", "user", "name", "role", "text", "page", "client_info", "status": "new"|"closed",
"closed_by", "closed_at", "images": [{"n", "name", "type", "bytes"}]}; each image is the file <id>_<n>.png|.jpg|.webp
(600) in the same directory. Ids are 12 hex characters; client file names are metadata only, never paths.
A screenshot can show a case, so the folder is treated like lab_cases.json: 600/700 and in the encrypted backup.
Caps: WEB_BUG_REPORTS_PER_USER (default 20) open ("new") reports per user and WEB_BUG_REPORTS_MAX_MB (default 500)
of images in the folder, counted from index.json; 0 turns a cap off. Standard library only.
"""
import base64
import json
import os
import secrets
import threading
import time
from pathlib import Path

from corrections import StoreError
from model1_process import write_private_json

DEFAULT_DIR = Path.home() / 'prenatal_pubcasefinder/.deployment/bug_reports'
MAX_IMAGES = 3
MAX_ITEMS = 200  # the longest list one request returns; counts stay complete
DEFAULT_PER_USER = 20
DEFAULT_MAX_MB = 500
FULL = 'Kho báo lỗi đã đầy; nhờ quản trị dọn bớt báo lỗi cũ.'
MAX_IMAGE_BYTES = 4000000
IMAGE_TYPES = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}
STATUSES = ('new', 'closed')
BAD_IMAGE = 'Ảnh không hợp lệ hoặc không phải PNG/JPEG/WEBP.'
INFO_LIMITS = {'model_version': 60, 'user_agent': 300, 'viewport': 40, 'app_version': 40}


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


def magic_matches(kind, data):
    if kind == 'image/png':
        return data.startswith(b'\x89PNG\r\n\x1a\n')
    if kind == 'image/jpeg':
        return data.startswith(b'\xff\xd8\xff')
    if kind == 'image/webp':
        return data[:4] == b'RIFF' and data[8:12] == b'WEBP'
    return False


def parse_report(payload):
    """Validate a POST /api/bug_reports body; returns (text, page, client_info, images) where images are
    (name, type, bytes). Raises ValueError with the message the page shows."""
    if not isinstance(payload, dict):
        raise ValueError('Dữ liệu không hợp lệ.')
    text = payload.get('text')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Mô tả lỗi không được để trống.')
    text = text.strip()
    if len(text) > 4000:
        raise ValueError('Mô tả lỗi dài quá 4.000 ký tự.')
    page = payload.get('page') or ''
    if not isinstance(page, str) or len(page) > 200:
        raise ValueError('Trường "page" không hợp lệ.')
    raw_info = payload.get('client_info') or {}
    if not isinstance(raw_info, dict):
        raise ValueError('Trường "client_info" không hợp lệ.')
    info = {}
    for key, limit in INFO_LIMITS.items():
        value = raw_info.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f'Trường "{key}" không hợp lệ.')
        info[key] = value
    raw_images = payload.get('images') or []
    if not isinstance(raw_images, list):
        raise ValueError(BAD_IMAGE)
    if len(raw_images) > MAX_IMAGES:
        raise ValueError(f'Tối đa {MAX_IMAGES} ảnh mỗi báo lỗi.')
    images = []
    for image in raw_images:
        if not isinstance(image, dict):
            raise ValueError(BAD_IMAGE)
        name, kind, data = image.get('name'), image.get('type'), image.get('data')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or kind not in IMAGE_TYPES or not isinstance(data, str):
            raise ValueError(BAD_IMAGE)
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            raise ValueError(BAD_IMAGE) from None
        if not 1 <= len(raw) <= MAX_IMAGE_BYTES or not magic_matches(kind, raw):
            raise ValueError(BAD_IMAGE)
        images.append((name.strip(), kind, raw))
    return text, page, info, images


class BugReportStore:
    def __init__(self, directory=None, per_user=None, max_mb=None):
        self.dir = Path(directory or os.getenv('WEB_BUG_REPORTS_DIR') or DEFAULT_DIR)
        self.index = self.dir / 'index.json'
        self.lock = threading.Lock()
        self.items, self.mtime = [], None
        # Open reports one user may have, and the image bytes the folder may hold; 0 = no cap.
        self.per_user = env_int('WEB_BUG_REPORTS_PER_USER', DEFAULT_PER_USER) if per_user is None else int(per_user)
        self.max_bytes = env_int('WEB_BUG_REPORTS_MAX_MB', DEFAULT_MAX_MB) * 1000000 if max_mb is None else int(max_mb * 1000000)

    # ------------------------------------------------------------------ file
    def _refresh(self):
        try:
            mtime = self.index.stat().st_mtime_ns
        except FileNotFoundError:
            self.items, self.mtime = [], None
            return
        if mtime == self.mtime:
            return
        try:
            data = json.loads(self.index.read_text(encoding='utf-8'))
        except ValueError as exc:  # json.JSONDecodeError, UnicodeDecodeError
            raise StoreError('index.json không đọc được.') from exc
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise StoreError('index.json không đúng cấu trúc.')
        self.items, self.mtime = [i for i in items if isinstance(i, dict)], mtime

    def _save(self):
        write_private_json(self.index, {'items': self.items})
        self.mtime = self.index.stat().st_mtime_ns

    def _ensure_dir(self):
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def all(self):
        with self.lock:
            self._refresh()
            return [dict(i) for i in reversed(self.items)]

    def _file(self, report_id, n, kind):
        return self.dir / f'{report_id}_{n}{IMAGE_TYPES[kind]}'

    def _image_bytes(self):
        """Image bytes the folder holds according to index.json (no directory scan)."""
        total = 0
        for item in self.items:
            for image in item.get('images') or []:
                if isinstance(image, dict) and isinstance(image.get('bytes'), int):
                    total += image['bytes']
        return total

    def _check_caps(self, user, images):
        """ValueError (shown to the doctor) when user has per_user open reports or the images would overfill the folder."""
        if self.per_user > 0:
            open_reports = sum(1 for i in self.items if i.get('user') == user and i.get('status') == 'new')
            if open_reports >= self.per_user:
                raise ValueError(f'Bác sĩ đã có {self.per_user} báo lỗi chưa xử lý; chờ quản trị xử lý trước khi gửi thêm.')
        if self.max_bytes > 0 and self._image_bytes() + sum(len(raw) for _, _, raw in images) > self.max_bytes:
            raise ValueError(FULL)

    # ------------------------------------------------------------------ write
    def add(self, user, name, role, text, page, client_info, images):
        """images: [(client name, content type, bytes)]. Returns the new id. ValueError when a cap is reached;
        on any failure no file of this report stays behind."""
        with self.lock:
            self._refresh()
            self._check_caps(user, images)
            self._ensure_dir()
            ids = {i.get('id') for i in self.items}
            report_id = secrets.token_hex(6)
            while report_id in ids:
                report_id = secrets.token_hex(6)
            meta, written = [], []
            item = {'id': report_id, 'time': time.strftime('%Y-%m-%d %H:%M'), 'user': user, 'name': name or user,
                    'role': role, 'text': text, 'page': page, 'client_info': client_info, 'status': 'new',
                    'closed_by': None, 'closed_at': None, 'images': meta}
            try:
                for n, (client_name, kind, raw) in enumerate(images):
                    path = self._file(report_id, n, kind)
                    written.append(path)
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, 'wb') as f:
                        f.write(raw)
                    meta.append({'n': n, 'name': client_name, 'type': kind, 'bytes': len(raw)})
                self.items.append(item)
                self._save()
            except BaseException:  # index.json not written: no orphan screenshots, no item the file does not hold
                if item in self.items:
                    self.items.remove(item)
                for path in written:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            return report_id

    def set_status(self, report_id, status, by):
        if status not in STATUSES:
            raise ValueError('Trạng thái không hợp lệ.')
        with self.lock:
            self._refresh()
            item = next((i for i in self.items if i.get('id') == report_id), None)
            if item is None:
                raise ValueError('Không tìm thấy báo lỗi này.')
            if status == 'closed':
                item.update(status='closed', closed_by=by, closed_at=time.strftime('%Y-%m-%d %H:%M'))
            else:
                item.update(status='new', closed_by=None, closed_at=None)
            self._save()
            return dict(item)

    def delete(self, report_id):
        with self.lock:
            self._refresh()
            item = next((i for i in self.items if i.get('id') == report_id), None)
            if item is None:
                raise ValueError('Không tìm thấy báo lỗi này.')
            for image in item.get('images') or []:
                n = image.get('n') if isinstance(image, dict) else None
                if isinstance(n, int) and not isinstance(n, bool) and image.get('type') in IMAGE_TYPES:
                    self._file(report_id, n, image['type']).unlink(missing_ok=True)
            self.items.remove(item)
            self._save()

    # ------------------------------------------------------------------ read
    def listing(self, status='new'):
        """(the newest MAX_ITEMS matching reports, complete counts per status)."""
        items = self.all()
        counts = {s: sum(1 for i in items if i.get('status') == s) for s in STATUSES}
        if status not in STATUSES + ('all',):
            status = 'new'
        return [i for i in items if status == 'all' or i.get('status') == status][:MAX_ITEMS], counts

    def image(self, report_id, n):
        """(bytes, content type, download file name) or None."""
        item = next((i for i in self.all() if i.get('id') == report_id), None)
        if item is None:
            return None
        meta = next((m for m in item.get('images') or [] if m.get('n') == n), None)
        if meta is None or meta.get('type') not in IMAGE_TYPES:
            return None
        path = self._file(report_id, n, meta['type'])
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return data, meta['type'], path.name
