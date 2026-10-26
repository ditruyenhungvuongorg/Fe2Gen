"""Cases a doctor sends to the genetics lab: the only case content this server keeps.

Decisions of 26/10/2026 (user): a case holds the lab sheet (case code, gestation, doctor, reviewed HPO with status,
diseases chosen for the lab, WES genes) plus the ultrasound description; it stays until someone deletes it; lab
accounts and admins see every case, a doctor sees the cases they sent; lab and admin set a status and a note.
Cases belong to the hospital, so deleting the sending doctor's account keeps them.

Stored in WEB_LAB_CASES_FILE (default ~/prenatal_pubcasefinder/.deployment/lab_cases.json, mode 600), rewritten
atomically. A file that cannot be read raises instead of being treated as empty, so cases are never silently lost.
"""
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

DEFAULT_FILE = Path.home() / 'prenatal_pubcasefinder/.deployment/lab_cases.json'
STATUSES = {'new': 'Mới gửi', 'received': 'Lab đã nhận', 'testing': 'Đang xét nghiệm', 'result': 'Có kết quả'}
HPO_STATUSES = ('CÓ', 'NGHI NGỜ', 'KHÔNG')
READ_ALL = ('lab', 'admin')
MAX_CASES = 2000
CASE_ID = re.compile(r'^[0-9a-f]{12}$')


def _text(value, limit):
    return str(value if value is not None else '').strip()[:limit]


def _strings(values, limit, size):
    return [_text(v, size) for v in values[:limit]] if isinstance(values, list) else []


class LabCases:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('WEB_LAB_CASES_FILE') or DEFAULT_FILE)
        self.lock = threading.Lock()

    def _load(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding='utf-8'))['cases']

    def _save(self, cases):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'cases': cases}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    @staticmethod
    def _clean(payload):
        hpos = payload.get('hpos')
        if not isinstance(hpos, list) or not 1 <= len(hpos) <= 100:
            raise ValueError('Ca gửi Lab cần từ 1 đến 100 HPO đã duyệt.')
        clean_hpos = []
        for h in hpos:
            if not isinstance(h, dict) or not re.fullmatch(r'HP:\d{7}', str(h.get('id', ''))) or h.get('status') not in HPO_STATUSES:
                raise ValueError('HPO hoặc trạng thái không hợp lệ.')
            clean_hpos.append({'id': h['id'], 'vi': _text(h.get('vi'), 200), 'en': _text(h.get('en'), 200), 'status': h['status']})
        diseases = payload.get('diseases') or []
        if not isinstance(diseases, list) or len(diseases) > 50:
            raise ValueError('Chọn tối đa 50 bệnh để gửi Lab.')
        clean_diseases = []
        for d in diseases:
            if not isinstance(d, dict) or not _text(d.get('disease_id'), 40):
                raise ValueError('Bệnh không hợp lệ.')
            rank, pct = d.get('rank'), d.get('match_percentage')
            clean_diseases.append({
                'disease_id': _text(d['disease_id'], 40), 'disease_name': _text(d.get('disease_name'), 300),
                'group': _text(d.get('group'), 20), 'rank': rank if isinstance(rank, int) else None,
                'match_percentage': float(pct) if isinstance(pct, (int, float)) else None,
                'inheritance_modes': _strings(d.get('inheritance_modes'), 10, 120),
                'causative_genes': _strings(d.get('causative_genes'), 60, 30),
                'recommended_tests': _text(d.get('recommended_tests'), 1000)})
        return {'case_id': _text(payload.get('case_id'), 80), 'gestation': _text(payload.get('gestation'), 40),
                'doctor': _text(payload.get('doctor'), 100), 'text': _text(payload.get('text'), 6000),
                'wes_genes': _text(payload.get('wes_genes'), 20000), 'hpos': clean_hpos, 'diseases': clean_diseases}

    def create(self, sender, sender_name, payload):
        now = time.strftime('%Y-%m-%d %H:%M')
        case = {'id': secrets.token_hex(6), 'created': now, 'sender': sender, 'sender_name': _text(sender_name, 80),
                **self._clean(payload), 'status': 'new', 'note': '', 'updated': now, 'updated_by': sender}
        with self.lock:
            cases = self._load()
            if len(cases) >= MAX_CASES:
                raise ValueError(f'Đã có {len(cases)} ca gửi Lab; xoá các ca đã xong trước khi gửi thêm.')
            cases.insert(0, case)
            self._save(cases)
        return case

    def visible(self, username, role):
        """Lab and admin: every case; anyone else: the cases they sent. Newest first."""
        with self.lock:
            cases = self._load()
        return cases if role in READ_ALL else [c for c in cases if c['sender'] == username]

    def _change(self, case_id, apply):
        if not CASE_ID.match(case_id or ''):
            raise ValueError('Mã ca gửi Lab không hợp lệ.')
        with self.lock:
            cases = self._load()
            index = next((i for i, c in enumerate(cases) if c['id'] == case_id), None)
            if index is None:
                raise ValueError('Không còn ca này (có thể đã bị xoá).')
            result = apply(cases, index)
            self._save(cases)
        return result

    def update(self, case_id, username, role, status=None, note=None):
        """Lab and admin set the status and the note shown to the sending doctor."""
        if role not in READ_ALL:
            raise PermissionError('Chỉ Phòng Lab hoặc quản trị cập nhật ca gửi Lab.')
        if status is not None and status not in STATUSES:
            raise ValueError('Trạng thái không hợp lệ.')

        def apply(cases, i):
            c = cases[i]
            if status is not None:
                c['status'] = status
            if note is not None:
                c['note'] = _text(note, 2000)
            c['updated'], c['updated_by'] = time.strftime('%Y-%m-%d %H:%M'), username
            return c
        return self._change(case_id, apply)

    def delete(self, case_id, username, role):
        """Lab and admin delete any case; the sending doctor may withdraw their own."""
        def apply(cases, i):
            if role not in READ_ALL and cases[i]['sender'] != username:
                raise PermissionError('Bạn chỉ xoá được ca do chính mình gửi.')
            return cases.pop(i)
        return self._change(case_id, apply)
