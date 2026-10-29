"""Doctor feedback on disease matching ("Bệnh đúng của ca"): when Model 2 ranks the wrong disease, the doctor
flags the wrong suggestions and records the right disease next to them. The whole case snapshot (reviewed HPO
terms with their status, WES genes, the AI's top suggestions as shown, the flagged ids, the correct disease and a
note) is kept on the server as the retraining set for Model 2. Nothing here changes the live ranking and there
is no approval flow: an admin marks items "Đã xem" and downloads the export.

JSON file WEB_DISEASE_FEEDBACK_FILE (default ~/prenatal_pubcasefinder/.deployment/disease_feedback.json, mode 600,
written atomically; re-read when it changes on disk). Shape {"items": [ITEM, ...]} where ITEM is what the API
returns: {"id", "time", "user", "name", "case_code", "hpos": [{"id","vi","en","status"}], "genes": [...],
"suggestions": [{"disease_id","disease_name","rank","overall_rank","group","match_percentage"}], "wrong_ids": [...],
"correct": {"disease_id", "disease_name"}, "note", "status": "new"|"reviewed", "reviewed_by", "reviewed_at"}.
A case snapshot is clinical content (no patient identity, but the doctor's case code and the findings): kept with
the same care as lab_cases.json, 600 and in the encrypted backup.
Caps: WEB_DISEASE_FEEDBACK_PER_USER (default 50) "new" items per user and WEB_DISEASE_FEEDBACK_MAX_MB (default 50)
for the whole file, estimated from the JSON text; 0 turns a cap off. Standard library only.
"""
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path

from corrections import HPO_ID, StoreError, now, text_field
from model1_process import write_private_json
from ratelimit import env_int

DEFAULT_FILE = Path.home() / 'prenatal_pubcasefinder/.deployment/disease_feedback.json'
LABELS = {'new': 'Chờ xem', 'reviewed': 'Đã xem'}
STATUSES = tuple(LABELS)
HPO_STATUSES = ('CÓ', 'NGHI NGỜ', 'KHÔNG')
MAX_ITEMS = 200       # the longest list one request returns; counts stay complete
MAX_HPOS = 100
MAX_GENES = 50
MAX_SUGGESTIONS = 10  # also the most wrong_ids one item may carry
MAX_RANK = 100000     # rank / overall_rank: 0..MAX_RANK integers; match_percentage: 0..100
MAX_CASE_CODE = 80    # the page's #case-id maxlength
# Caps (0 turns one off): WEB_DISEASE_FEEDBACK_PER_USER "new" items one user may have waiting for the admin, and
# WEB_DISEASE_FEEDBACK_MAX_MB the whole file, estimated from the JSON text of the items plus the new one.
DEFAULT_PER_USER = 50
DEFAULT_MAX_MB = 50
FULL = 'Kho ghi nhận bệnh đúng đã đầy; nhờ quản trị dọn bớt.'
GENE_SYMBOL = re.compile(r'^[A-Z0-9][A-Z0-9.-]{0,39}$')
DISEASE_ID = re.compile(r'^[A-Z]+:[A-Z0-9.-]{1,30}$')
NOT_IN_CATALOG = 'Mã bệnh không có trong danh mục.'
BAD_SUGGESTIONS = 'Danh sách gợi ý của AI không hợp lệ.'


def disease_id_key(text):
    """A typed disease id in catalog form ('omim:123456' -> 'OMIM:123456', 'Orphanet:558' -> 'ORPHA:558'), or ''
    when text is not shaped like an id."""
    key = ' '.join(str(text or '').split()).upper().replace(' ', '')
    if key.startswith('ORPHANET:'):
        key = 'ORPHA:' + key[len('ORPHANET:'):]
    return key if DISEASE_ID.match(key) else ''


def _hpo_items(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError('Chọn ít nhất một HPO của ca.')
    if len(raw) > MAX_HPOS:
        raise ValueError(f'Tối đa {MAX_HPOS} HPO mỗi ca.')
    hpos, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('Dấu hiệu HPO không hợp lệ.')
        hid = item.get('id')
        if not isinstance(hid, str) or not HPO_ID.match(hid.strip().upper()) or item.get('status') not in HPO_STATUSES:
            raise ValueError('Dấu hiệu HPO không hợp lệ.')
        hid = hid.strip().upper()
        if hid in seen:
            continue
        seen.add(hid)
        hpos.append({'id': hid, 'vi': text_field(item, 'vi', 200), 'en': text_field(item, 'en', 200), 'status': item['status']})
    return hpos


def _genes(raw):
    if raw in (None, ''):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_GENES:
        raise ValueError(f'Danh sách gene không hợp lệ (tối đa {MAX_GENES} gene).')
    genes = []
    for item in raw:
        symbol = str(item).strip().upper() if isinstance(item, str) else ''
        if not GENE_SYMBOL.match(symbol):
            raise ValueError(f'Ký hiệu gene không hợp lệ: {str(item)[:20]}')
        if symbol not in genes:
            genes.append(symbol)
    return genes


def _number_or_none(value, integer=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(BAD_SUGGESTIONS)
    try:
        number = float(value)  # an int beyond float range raises OverflowError
    except OverflowError:
        raise ValueError(BAD_SUGGESTIONS) from None
    if not math.isfinite(number):  # NaN / Infinity would be stored and emitted as invalid JSON
        raise ValueError(BAD_SUGGESTIONS)
    if integer:
        if not number.is_integer() or not 0 <= number <= MAX_RANK:
            raise ValueError(BAD_SUGGESTIONS)
        return int(number)
    if not 0 <= number <= 100:
        raise ValueError(BAD_SUGGESTIONS)
    return number


def _suggestions(raw):
    if raw in (None, ''):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SUGGESTIONS:
        raise ValueError(BAD_SUGGESTIONS)
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(BAD_SUGGESTIONS)
        did = item.get('disease_id')
        if not isinstance(did, str) or not 1 <= len(did.strip()) <= 40:
            raise ValueError(BAD_SUGGESTIONS)
        did = did.strip()
        if did in seen:
            continue
        seen.add(did)
        group = item.get('group')
        if group is not None and (not isinstance(group, str) or len(group) > 20):
            raise ValueError(BAD_SUGGESTIONS)
        out.append({'disease_id': did, 'disease_name': text_field(item, 'disease_name', 200),
                    'rank': _number_or_none(item.get('rank'), integer=True),
                    'overall_rank': _number_or_none(item.get('overall_rank'), integer=True),
                    'group': group, 'match_percentage': _number_or_none(item.get('match_percentage'))})
    return out


def parse_feedback(payload, resolve):
    """Validate a POST /api/disease_feedback body. resolve(disease_id) returns (catalog id, catalog name) or raises
    ValueError when the id is not in the catalog. Returns the fields of a new item."""
    if not isinstance(payload, dict):
        raise ValueError('Dữ liệu không hợp lệ.')
    hpos = _hpo_items(payload.get('hpos'))
    suggestions = _suggestions(payload.get('suggestions'))
    raw_wrong = payload.get('wrong_ids') or []
    if not isinstance(raw_wrong, list) or len(raw_wrong) > MAX_SUGGESTIONS:
        raise ValueError(f'Tối đa {MAX_SUGGESTIONS} bệnh báo sai mỗi ca.')
    shown = {s['disease_id'] for s in suggestions}
    wrong = []
    for did in raw_wrong:
        if not isinstance(did, str) or did.strip() not in shown:
            raise ValueError('Bệnh báo sai phải nằm trong danh sách AI đã gợi ý.')
        if did.strip() not in wrong:
            wrong.append(did.strip())
    correct = payload.get('correct')
    if not isinstance(correct, dict):
        raise ValueError('Ghi tên bệnh đúng.')
    name = text_field(correct, 'disease_name', 200, required=True, message='Ghi tên bệnh đúng.')
    did = correct.get('disease_id')
    if did is not None:
        if not isinstance(did, str) or not disease_id_key(did):
            raise ValueError(NOT_IN_CATALOG)
        did, name = resolve(disease_id_key(did))
        if did in wrong:
            raise ValueError('Bệnh đúng không thể là bệnh đã báo sai.')
    return {'case_code': text_field(payload, 'case_code', MAX_CASE_CODE), 'hpos': hpos, 'genes': _genes(payload.get('genes')),
            'suggestions': suggestions, 'wrong_ids': wrong, 'correct': {'disease_id': did, 'disease_name': name},
            'note': text_field(payload, 'note', 500)}


class DiseaseFeedbackStore:
    def __init__(self, path=None, per_user=None, max_mb=None):
        self.path = Path(path or os.getenv('WEB_DISEASE_FEEDBACK_FILE') or DEFAULT_FILE)
        self.lock = threading.Lock()
        self.items, self.mtime = [], None
        # "new" items one user may have waiting, and the bytes the file may hold; 0 = no cap.
        self.per_user = env_int('WEB_DISEASE_FEEDBACK_PER_USER', DEFAULT_PER_USER) if per_user is None else int(per_user)
        self.max_bytes = env_int('WEB_DISEASE_FEEDBACK_MAX_MB', DEFAULT_MAX_MB) * 1000000 if max_mb is None else int(max_mb * 1000000)

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
            raise StoreError('disease_feedback.json không đọc được.') from exc
        items = data.get('items') if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise StoreError('disease_feedback.json không đúng cấu trúc.')
        self.items, self.mtime = [i for i in items if isinstance(i, dict)], mtime

    def _save(self, items=None):
        """Write items (default the current list) and, only once the file is on disk, make them the current list:
        a failed write leaves no phantom item in memory."""
        if items is None:
            items = self.items
        write_private_json(self.path, {'items': items})
        self.items, self.mtime = items, self.path.stat().st_mtime_ns

    def _check_caps(self, user, item):
        """ValueError (shown to the doctor) when user has per_user items waiting or the file would outgrow max_bytes."""
        if self.per_user > 0:
            waiting = sum(1 for i in self.items if i.get('user') == user and i.get('status') == 'new')
            if waiting >= self.per_user:
                raise ValueError(f'Bác sĩ đã có {self.per_user} ghi nhận chưa xem; chờ quản trị xem trước khi ghi thêm.')
        if self.max_bytes > 0:
            size = len(json.dumps(self.items, ensure_ascii=False)) + len(json.dumps(item, ensure_ascii=False))
            if size > self.max_bytes:
                raise ValueError(FULL)

    def all(self):
        """Every item, newest first (copies)."""
        with self.lock:
            self._refresh()
            return [dict(i) for i in reversed(self.items)]

    # ------------------------------------------------------------------ doctors
    def add(self, user, name, fields):
        with self.lock:
            self._refresh()
            ids = {i.get('id') for i in self.items}
            new_id = secrets.token_hex(6)
            while new_id in ids:
                new_id = secrets.token_hex(6)
            item = {'id': new_id, 'time': now(), 'user': user, 'name': name or user, **fields,
                    'status': 'new', 'reviewed_by': None, 'reviewed_at': None}
            self._check_caps(user, item)
            self._save(self.items + [item])  # copy-on-write: self.items changes only after the file is written
            return dict(item)

    def own(self, user, status='all'):
        if status not in STATUSES + ('all',):
            status = 'all'
        return [i for i in self.all() if i.get('user') == user and (status == 'all' or i.get('status') == status)][:MAX_ITEMS]

    # ------------------------------------------------------------------ admin
    def listing(self, status='new', user=''):
        """(the newest MAX_ITEMS matching items, complete counts per status)."""
        items = self.all()
        counts = {s: sum(1 for i in items if i.get('status') == s) for s in STATUSES}
        if status not in STATUSES + ('all',):
            status = 'new'
        chosen = [i for i in items if (status == 'all' or i.get('status') == status) and (not user or i.get('user') == user)]
        return chosen[:MAX_ITEMS], counts

    def set_status(self, item_id, status, by):
        if status not in STATUSES:
            raise ValueError('Trạng thái không hợp lệ.')
        with self.lock:
            self._refresh()
            item = next((i for i in self.items if i.get('id') == item_id), None)
            if item is None:
                raise ValueError('Không tìm thấy ghi nhận này.')
            if status == 'reviewed':
                item.update(status='reviewed', reviewed_by=by, reviewed_at=now())
            else:
                item.update(status='new', reviewed_by=None, reviewed_at=None)
            self._save()
            return dict(item)

    def delete(self, item_id):
        with self.lock:
            self._refresh()
            item = next((i for i in self.items if i.get('id') == item_id), None)
            if item is None:
                raise ValueError('Không tìm thấy ghi nhận này.')
            self.items.remove(item)
            self._save()


def export_payload(items):
    """What /api/admin/disease_feedback/export writes: every item regardless of status, as Model 2 training cases."""
    cases = []
    for item in items:
        correct = item.get('correct') or {}
        cases.append({'id': item.get('id', ''), 'time': item.get('time', ''), 'user': item.get('user', ''),
                      'case_code': item.get('case_code', ''),
                      'hpos': [{'id': h.get('id'), 'status': h.get('status')} for h in item.get('hpos') or [] if isinstance(h, dict)],
                      'genes': list(item.get('genes') or []),
                      'correct_disease_id': correct.get('disease_id'), 'correct_disease_name': correct.get('disease_name', ''),
                      'wrong_disease_ids': list(item.get('wrong_ids') or []),
                      'ai_suggestions': [{'disease_id': s.get('disease_id'), 'rank': s.get('rank'), 'overall_rank': s.get('overall_rank'),
                                          'group': s.get('group'), 'match_percentage': s.get('match_percentage')}
                                         for s in item.get('suggestions') or [] if isinstance(s, dict)],
                      'note': item.get('note', '')})
    return {'exported': time.strftime('%Y-%m-%d %H:%M:%S'), 'count': len(cases), 'cases': cases}
