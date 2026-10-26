"""Cases sent to the lab: validation, who sees and changes what, and the HTTP routes (26/10/2026)."""
import json
import os
import tempfile
import threading
import types
import unittest
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from audit import AuditLog
from auth import Auth, UserStore, hash_password
from lab_cases import LabCases

CASE = {'case_id': 'TS-001', 'gestation': '22 tuần', 'doctor': 'BS. An', 'text': 'Thai 22 tuần, đầu nhỏ, đa ối.',
        'wes_genes': 'FGFR3, TSC2',
        'hpos': [{'id': 'HP:0000252', 'vi': 'Tật đầu nhỏ', 'en': 'Microcephaly', 'status': 'CÓ'},
                 {'id': 'HP:0001561', 'vi': 'Đa ối', 'en': 'Polyhydramnios', 'status': 'NGHI NGỜ'}],
        'diseases': [{'disease_id': 'ORPHA:3380', 'disease_name': 'Trisomy 18', 'group': 'NST', 'rank': 1,
                      'match_percentage': 41.5, 'inheritance_modes': [], 'causative_genes': []}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = LabCases(Path(self.dir.name, 'lab_cases.json'))

    def tearDown(self):
        self.dir.cleanup()

    def test_doctor_sends_lab_reads_and_answers(self):
        case = self.store.create('bs.an', 'BS. An', CASE)
        self.assertEqual((case['status'], case['text'], len(case['hpos'])), ('new', CASE['text'], 2))
        self.assertEqual([c['id'] for c in self.store.visible('lab.hv', 'lab')], [case['id']])
        self.assertEqual(self.store.visible('bs.binh', 'doctor'), [])  # another doctor's case stays hidden
        self.store.update(case['id'], 'lab.hv', 'lab', status='testing', note='Đã lấy mẫu ối.')
        mine = self.store.visible('bs.an', 'doctor')[0]
        self.assertEqual((mine['status'], mine['note'], mine['updated_by']), ('testing', 'Đã lấy mẫu ối.', 'lab.hv'))
        if os.name == 'posix':
            self.assertEqual(Path(self.dir.name, 'lab_cases.json').stat().st_mode & 0o777, 0o600)

    def test_permissions(self):
        case = self.store.create('bs.an', 'BS. An', CASE)
        with self.assertRaises(PermissionError):
            self.store.update(case['id'], 'bs.an', 'doctor', status='result')  # only lab or admin answer
        with self.assertRaises(PermissionError):
            self.store.delete(case['id'], 'bs.binh', 'doctor')  # not the sender
        self.store.delete(case['id'], 'bs.an', 'doctor')  # the sender may withdraw it
        self.assertEqual(self.store.visible('quan.tri', 'admin'), [])

    def test_validation(self):
        for bad in ({**CASE, 'hpos': []}, {**CASE, 'hpos': [{'id': 'HP:1', 'status': 'CÓ'}]},
                    {**CASE, 'hpos': [{'id': 'HP:0000252', 'status': 'MAYBE'}]}, {**CASE, 'diseases': [{}]}):
            with self.assertRaises(ValueError):
                self.store.create('bs.an', 'BS. An', bad)
        with self.assertRaises(ValueError):
            self.store.update('../etc', 'lab.hv', 'lab', status='new')
        case = self.store.create('bs.an', 'BS. An', {**CASE, 'text': 'x' * 9000})
        self.assertEqual(len(case['text']), 6000)

    def test_unreadable_file_is_not_treated_as_empty(self):
        Path(self.dir.name, 'lab_cases.json').write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):  # json error: the cases are never silently replaced
            self.store.create('bs.an', 'BS. An', CASE)


class HttpTests(unittest.TestCase):
    """The /api/lab routes through serve_web with real accounts, a fake model and temporary files."""

    @classmethod
    def setUpClass(cls):
        import serve_web
        cls.dir = tempfile.TemporaryDirectory()
        d = Path(cls.dir.name)
        store = UserStore(d / 'users.json')
        store.save({name: {'name': name, 'role': role, 'password': hash_password('mat-khau-' + name)}
                    for name, role in (('bs.an', 'doctor'), ('bs.binh', 'doctor'), ('lab.hv', 'lab'), ('quan.tri', 'admin'))})
        cls.audit_path = d / 'audit.jsonl'
        runner = types.SimpleNamespace(adapter_path=None, start=lambda: 'stopped', touch=lambda: None, stop=lambda *a: None,
                                       status=lambda: {'model_state': 'stopped', 'model_ready': False, 'model_error': '',
                                                       'model_load_seconds': None, 'idle_minutes': 60})
        system = types.SimpleNamespace(matcher=types.SimpleNamespace(profiles=[1]), canonical={}, runner=runner)
        cls.quiet = redirect_stderr(StringIO())
        cls.quiet.__enter__()
        cls.patches = [patch.object(serve_web, 'SYSTEM', system), patch.object(serve_web, 'AUTH', Auth(store)),
                       patch.object(serve_web, 'AUDIT', AuditLog(cls.audit_path)),
                       patch.object(serve_web, 'LAB', LabCases(d / 'lab_cases.json'))]
        for p in cls.patches:
            p.start()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), serve_web.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for p in cls.patches:
            p.stop()
        cls.quiet.__exit__(None, None, None)
        cls.dir.cleanup()

    def call(self, path, payload=None, token=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={'Content-Type': 'application/json',
                                              **({'Authorization': 'Bearer ' + token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def login(self, name):
        code, data = self.call('/api/login', {'username': name, 'password': 'mat-khau-' + name})
        self.assertEqual(code, 200)
        return data['token']

    def test_flow(self):
        an, binh, lab, admin = (self.login(n) for n in ('bs.an', 'bs.binh', 'lab.hv', 'quan.tri'))
        self.assertEqual(self.call('/api/lab/cases')[0], 401)  # nothing without a session
        code, sent = self.call('/api/lab/cases/create', CASE, an)
        self.assertEqual(code, 200)
        cid = sent['case']['id']
        self.assertEqual(self.call('/api/lab/cases/create', CASE, lab)[0], 403)  # the lab receives, it does not send
        self.assertEqual([c['id'] for c in self.call('/api/lab/cases', token=lab)[1]['cases']], [cid])
        self.assertEqual(self.call('/api/lab/cases', token=binh)[1]['cases'], [])
        self.assertEqual(self.call('/api/lab/cases/update', {'id': cid, 'status': 'result'}, an)[0], 403)
        code, out = self.call('/api/lab/cases/update', {'id': cid, 'status': 'result', 'note': 'Trisomy 18 (QF-PCR).'}, lab)
        self.assertEqual((code, out['case']['status']), (200, 'result'))
        mine = self.call('/api/lab/cases', token=an)[1]['cases'][0]
        self.assertEqual((mine['status'], mine['note']), ('result', 'Trisomy 18 (QF-PCR).'))
        self.assertEqual(self.call('/api/lab/cases/update', {'id': cid, 'status': 'done?'}, lab)[0], 422)
        self.assertEqual(self.call('/api/lab/cases/delete', {'id': cid}, binh)[0], 403)
        self.assertEqual(self.call('/api/lab/cases/delete', {'id': cid}, admin)[0], 200)
        self.assertEqual(self.call('/api/lab/cases', token=admin)[1]['cases'], [])
        log = self.audit_path.read_text(encoding='utf-8')
        events = [json.loads(line)['event'] for line in log.splitlines()]
        for e in ('lab_case_sent', 'lab_case_updated', 'lab_case_deleted'):
            self.assertIn(e, events)
        for content in ('đầu nhỏ', 'TS-001', 'HP:0000252', 'Trisomy'):  # the audit log holds no case content
            self.assertNotIn(content, log)


if __name__ == '__main__':
    unittest.main(verbosity=2)
