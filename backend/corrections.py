"""Doctor corrections ("Góp ý sửa mã"): a phrase the model mapped wrongly and the HPO code the doctor says is
right. An admin approves or rejects each one; approved items take effect at once in extraction and HPO search
(WebSystem.apply_corrections) and are exported for the next bundle build (tools/export_corrections.py).

JSON file WEB_CORRECTIONS_FILE (default ~/prenatal_pubcasefinder/.deployment/corrections.json, mode 600, written
atomically; re-read when it changes on disk). Shape {"items": [ITEM, ...]} where ITEM is what the API returns:
{"id", "time", "user", "name", "phrase", "wrong": [{"id","vi","en"}], "right": {"id","vi","en"}, "context",
"note", "source", "status": "pending"|"approved"|"rejected", "decided_by", "decided_at", "decision_note"};
a decided item also carries "decided_ts" (time.time() float, the order of approvals within one minute).
Phrases are clinical wording (no patient identity), kept with the same care as lab_cases.json: 600 and in the
encrypted backup. Standard library only.
"""
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from pathlib import Path

from model1_process import write_private_json

DEFAULT_FILE = Path.home() / 'prenatal_pubcasefinder/.deployment/corrections.json'
LABELS = {'pending': 'Chờ duyệt', 'approved': 'Đã duyệt', 'rejected': 'Đã từ chối'}
STATUSES = tuple(LABELS)
SOURCES = ('text', 'search', 'selected')
HPO_ID = re.compile(r'^HP:\d{7}$')
MAX_WRONG = 5
MAX_ITEMS = 200  # the longest list one request returns


class StoreError(OSError):
    """A store file that cannot be parsed or has the wrong shape: an operator problem (answered 500), never a
    422 with the parser's message as if the doctor's input were wrong."""


def phrase_key(text):
    """Same rule as web_service.phrase_key: case and spacing ignored, diacritics kept."""
    return ' '.join(unicodedata.normalize('NFC', str(text)).casefold().split())


def query_text(text):
    """A memory row's query_text for build_bundle.py: NFC, lowercased, whitespace collapsed."""
    return ' '.join(unicodedata.normalize('NFC', str(text)).lower().split())


def now():
    return time.strftime('%Y-%m-%d %H:%M')


def text_field(payload, key, limit, required=False, message=''):
    value = payload.get(key)
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ValueError(message or 'Dữ liệu không hợp lệ.')
    value = value.strip()
    if required and not value:
        raise ValueError(message)
    if len(value) > limit:
        raise ValueError(f'Trường "{key}" dài quá {limit:,} ký tự.'.replace(',', '.'))
    return value


def parse_submission(payload, resolve):
    """Validate a POST /api/corrections body. resolve(hpo_id) returns {"id","vi","en"} for a valid code (the
    primary id when an alias was given) or raises ValueError. Returns the fields of a new item."""
    if not isinstance(payload, dict):
        raise ValueError('Dữ liệu không hợp lệ.')
    phrase = text_field(payload, 'phrase', 200, required=True, message='Nhập cụm từ cần sửa.')
    right_id = payload.get('right_id')
    if not isinstance(right_id, str) or not HPO_ID.match(right_id.strip().upper()):
        raise ValueError('Chọn mã HPO đúng (dạng HP:0000000).')
    right = resolve(right_id.strip().upper())
    wrong_ids = payload.get('wrong_ids') or []
    if not isinstance(wrong_ids, list) or len(wrong_ids) > MAX_WRONG:
        raise ValueError(f'Tối đa {MAX_WRONG} mã sai mỗi góp ý.')
    wrong = []
    for hid in wrong_ids:
        if not isinstance(hid, str) or not HPO_ID.match(hid.strip().upper()):
            raise ValueError('Mã HPO sai có định dạng không hợp lệ.')
        term = resolve(hid.strip().upper())
        if term['id'] != right['id'] and term['id'] not in {w['id'] for w in wrong}:
            wrong.append(term)
    source = payload.get('source') or 'text'
    if source not in SOURCES:
        raise ValueError('Nguồn góp ý không hợp lệ.')
    return {'phrase': phrase, 'right': right, 'wrong': wrong,
            'context': text_field(payload, 'context', 400), 'note': text_field(payload, 'note', 500), 'source': source}


class CorrectionStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('WEB_CORRECTIONS_FILE') or DEFAULT_FILE)
        self.lock = threading.Lock()
        self.items, self.mtime = [], None

    # ------------------------------------------------------------------ file
    def _refresh(self):
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self.items, self.mtime = [], None
            return
        if mtime == self.mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except ValueError as exc:  # json.JSONDecodeError, UnicodeDecodeError
            raise StoreError('corrections.json không đọc được.') from exc
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise StoreError('corrections.json không đúng cấu trúc.')
        self.items, self.mtime = [i for i in items if isinstance(i, dict)], mtime

    def _save(self, items=None):
        """Write items (default the current list) and, only once the file is on disk, make them the current list:
        a failed write leaves no phantom item in memory."""
        if items is None:
            items = self.items
        write_private_json(self.path, {'items': items})
        self.items, self.mtime = items, self.path.stat().st_mtime_ns

    def all(self):
        """Every item, newest first (copies)."""
        with self.lock:
            self._refresh()
            return [dict(i) for i in reversed(self.items)]

    # ------------------------------------------------------------------ doctors
    def add(self, user, name, fields):
        """Store a new pending item; a pending one by the same user with the same phrase and right code is
        returned instead. Returns (item, duplicate)."""
        key = phrase_key(fields['phrase'])
        with self.lock:
            self._refresh()
            for item in reversed(self.items):
                if item.get('user') == user and item.get('status') == 'pending' and phrase_key(item.get('phrase', '')) == key \
                        and (item.get('right') or {}).get('id') == fields['right']['id']:
                    return dict(item), True
            ids = {i.get('id') for i in self.items}
            new_id = secrets.token_hex(6)
            while new_id in ids:
                new_id = secrets.token_hex(6)
            item = {'id': new_id, 'time': now(), 'user': user, 'name': name or user, **fields,
                    'status': 'pending', 'decided_by': None, 'decided_at': None, 'decision_note': ''}
            self._save(self.items + [item])  # copy-on-write: self.items changes only after the file is written
            return dict(item), False

    def own(self, user, status='all'):
        if status not in STATUSES + ('all',):
            status = 'all'
        return [i for i in self.all() if i.get('user') == user and (status == 'all' or i.get('status') == status)][:MAX_ITEMS]

    # ------------------------------------------------------------------ admin
    def listing(self, status='pending', user=''):
        items = self.all()
        counts = {s: sum(1 for i in items if i.get('status') == s) for s in STATUSES}
        if status not in STATUSES + ('all',):
            status = 'pending'
        chosen = [i for i in items if (status == 'all' or i.get('status') == status) and (not user or i.get('user') == user)]
        return chosen[:MAX_ITEMS], counts

    def decide(self, item_id, decision, note, by):
        if decision not in ('approved', 'rejected'):
            raise ValueError('Quyết định không hợp lệ.')
        if not isinstance(note, str) or len(note) > 500:
            raise ValueError('Ghi chú dài quá 500 ký tự.')
        with self.lock:
            self._refresh()
            item = next((i for i in self.items if i.get('id') == item_id), None)
            if item is None:
                raise ValueError('Không tìm thấy góp ý này.')
            item.update(status=decision, decided_by=by, decided_at=now(), decided_ts=time.time(), decision_note=note.strip())
            self._save()
            return dict(item)

    def approved_items(self):
        """Approved items, the most recently decided first (the order apply_corrections keeps per phrase).
        decided_at has minute resolution, so decided_ts breaks ties; items from before it sort as 0."""
        items = [i for i in self.all() if i.get('status') == 'approved' and (i.get('right') or {}).get('id')]
        def ts(item):  # a hand-edited, non-numeric decided_ts sorts as 0 instead of breaking the list
            try:
                return float(item.get('decided_ts') or 0)
            except (TypeError, ValueError):
                return 0.0
        return sorted(items, key=lambda i: (ts(i), i.get('time') or ''), reverse=True)


def export_payload(items):
    """What /api/admin/corrections/export and tools/export_corrections.py write for approved items."""
    rows, memory, phrases, seen_memory, seen_phrase = [], [], [], set(), set()
    for item in items:
        right = item.get('right') or {}
        rows.append({'mention_text': item.get('phrase', ''), 'hpo_id': right.get('id', ''),
                     'wrong_hpo_ids': [w.get('id') for w in item.get('wrong') or [] if w.get('id')],
                     'context': item.get('context', ''), 'note': item.get('note', ''), 'user': item.get('user', ''),
                     'time': item.get('time', ''), 'approved_by': item.get('decided_by'), 'approved_at': item.get('decided_at')})
        text = query_text(item.get('phrase', ''))
        if text and (text, right.get('id')) not in seen_memory:
            seen_memory.add((text, right.get('id')))
            memory.append({'query_text': text, 'hpo_id': right.get('id', '')})
        key = (phrase_key(item.get('phrase', '')), right.get('id'))
        if key[0] and key not in seen_phrase:
            seen_phrase.add(key)
            phrases.append({'id': right.get('id', ''), 'vi': ' '.join(item.get('phrase', '').split()), 'en': right.get('en', '')})
    return {'exported': time.strftime('%Y-%m-%d %H:%M:%S'), 'count': len(rows), 'items': rows,
            'memory_rows': memory, 'doctor_phrases': phrases}
