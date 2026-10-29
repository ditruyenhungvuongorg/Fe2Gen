"""Model 1 on demand (child process, idle release, pause file, free-VRAM check) and doctor accounts.
Runs without GPU or HPO data: a fake runner stands in for Model 1."""
import json
import re
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

# Tests also run on the production host (deployment step 3): keep serve_web's module-level stores away from the
# real users.json, audit log, access log and model state file (the lockout file sits next to users.json).
# Child processes inherit these variables.
if 'FE2GEN_TEST_DIR' not in os.environ:
    os.environ['FE2GEN_TEST_DIR'] = tempfile.mkdtemp(prefix='fe2gen-test-')
    for _name, _file in (('WEB_USERS_FILE', 'users.json'), ('WEB_AUDIT_FILE', 'audit.jsonl'),
                         ('WEB_ACCESS_LOG', 'access.log'), ('MODEL1_STATE_FILE', 'model1_state.json'),
                         ('WEB_CORRECTIONS_FILE', 'corrections.json'), ('WEB_BUG_REPORTS_DIR', 'bug_reports'),
                         ('WEB_DISEASE_FEEDBACK_FILE', 'disease_feedback.json')):
        os.environ[_name] = str(Path(os.environ['FE2GEN_TEST_DIR'], _file))

from accesslog import AccessLog
from audit import AuditLog
from auth import Auth, UserStore, check_password, hash_password, password_problem, public_user, temporary_password
from bug_reports import BugReportStore
from corrections import CorrectionStore
from disease_feedback import DiseaseFeedbackStore
from model1_process import Model1Process, ModelProcessError, ModelUnavailable

FAKE = [sys.executable, '-c', 'import sys, model1_process, test_access; '
        'sys.exit(model1_process.worker_main(test_access.FakeRunner))']


class FakeRunner:
    def __init__(self):
        self.manifest = {'model_version': 'vtest', 'base_model_dir': '/private'}

    def load_model(self):
        if os.getenv('FAKE_MODEL1') == 'fail':
            raise RuntimeError('Model 1 v6 cần GPU CUDA.')
        print('Unsloth banner printed on stdout')  # must not break the protocol
        time.sleep(float(os.getenv('FAKE_LOAD_SECONDS', '0')))

    def extract(self, text):
        if text == 'crash':
            os._exit(3)
        if text == 'long':
            raise ValueError('Đoạn quá dài cho một lần trích.')
        if text == 'boom':
            raise KeyError('x')
        return {'findings': [], 'errors': [], 'seconds': 0.0, 'echo': text}


def model(**kw):
    kw.setdefault('pause_file', None)
    kw.setdefault('free_mib', lambda: None)
    return Model1Process('v6', Path('/bundle'), command=FAKE, **kw)


class Model1ProcessTests(unittest.TestCase):
    def setUp(self):
        self.log = StringIO()
        self.quiet = redirect_stderr(self.log)
        self.quiet.__enter__()
        self.models = []

    def tearDown(self):
        for m in self.models:
            m.stop()
        self.quiet.__exit__(None, None, None)

    def make(self, **kw):
        m = model(**kw)
        self.models.append(m)
        return m

    def test_nothing_runs_until_needed_then_serves(self):
        m = self.make()
        self.assertEqual(m.status()['model_state'], 'stopped')
        self.assertIsNone(m.proc)
        self.assertEqual(m.extract('Thai đầu nhỏ.')['echo'], 'Thai đầu nhỏ.')
        self.assertTrue(m.is_loaded)
        self.assertEqual(m.manifest, {'model_version': 'vtest'})  # only the version leaves the child
        self.assertEqual(m.extract_spans('lần hai')['echo'], 'lần hai')

    def test_idle_release_stops_the_process_and_next_use_restarts(self):
        m = self.make(idle_seconds=0)
        m.extract('a')
        proc = m.proc
        m.last_used -= 1  # Windows' monotonic clock ticks every ~16 ms
        m.check_idle()
        self.assertIsNotNone(proc.poll())  # the process exited: the driver frees all of its VRAM
        self.assertEqual(m.status()['model_state'], 'stopped')
        self.assertEqual(m.extract('b')['echo'], 'b')
        self.assertIsNot(m.proc, proc)

    def test_activity_postpones_release(self):
        m = self.make(idle_seconds=3600)
        m.extract('a')
        m.check_idle()
        self.assertTrue(m.is_loaded)

    def test_errors_keep_their_meaning(self):
        m = self.make()
        with self.assertRaisesRegex(ValueError, 'quá dài'):
            m.extract('long')
        with self.assertRaises(ModelProcessError):
            m.extract('boom')
        self.assertEqual(m.extract('ok')['echo'], 'ok')  # the process survives request errors

    def test_crash_reports_unavailable_and_recovers(self):
        m = self.make()
        m.extract('a')
        with self.assertRaises(ModelUnavailable):
            m.extract('crash')
        m.wait_ready(5)
        self.assertEqual(m.status()['model_state'], 'failed')
        self.assertEqual(m.extract('again')['echo'], 'again')

    def test_load_failure_is_reported_not_raised_at_start(self):
        with patch.dict(os.environ, {'FAKE_MODEL1': 'fail'}):
            m = self.make()
            self.assertEqual(m.start(), 'loading')
            self.assertFalse(m.wait_ready(30))
        status = m.status()
        self.assertEqual(status['model_state'], 'failed')
        self.assertIn('CUDA', status['model_error'])
        with self.assertRaisesRegex(RuntimeError, 'CUDA'):  # serve_web.preload_model1 logs it and keeps serving
            with patch.dict(os.environ, {'FAKE_MODEL1': 'fail'}):
                m.load_model()

    def test_low_free_vram_does_not_start(self):
        m = self.make(min_free_mib=13000, free_mib=lambda: 4000)
        self.assertEqual(m.start(), 'failed')
        self.assertIsNone(m.proc)
        with self.assertRaisesRegex(ModelUnavailable, '4,000 MiB'):
            m.ensure_ready()

    def test_pause_file_blocks_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d, 'gpu_pause')
            m = self.make(pause_file=flag)
            m.extract('a')
            flag.touch()
            proc = m.proc
            m.check_idle()
            self.assertIsNotNone(proc.poll())
            self.assertEqual(m.status()['model_state'], 'paused')
            with self.assertRaisesRegex(ModelUnavailable, 'train'):
                m.extract('b')
            flag.unlink()
            self.assertEqual(m.extract('c')['echo'], 'c')

    def test_not_configured(self):
        m = Model1Process('v38', None, pause_file=None, command=FAKE, free_mib=lambda: None)
        with self.assertRaisesRegex(ModelUnavailable, 'chưa được cấu hình'):
            m.ensure_ready()

    def test_concurrent_requests_share_one_process(self):
        m = self.make()
        out = []
        threads = [threading.Thread(target=lambda i=i: out.append(m.extract(str(i))['echo'])) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(out), ['0', '1', '2', '3'])


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = UserStore(Path(self.dir.name, 'users.json'))

    def tearDown(self):
        self.dir.cleanup()

    def add(self, name='bs.an', password='mat-khau-1', disabled=False):
        users = self.store.all()
        users[name] = {'name': 'BS. An', 'password': hash_password(password), 'disabled': disabled}
        self.store.save(users)

    def test_password_hash(self):
        stored = hash_password('mật khẩu dài')
        self.assertTrue(stored.startswith('scrypt$'))
        self.assertTrue(check_password('mật khẩu dài', stored))
        self.assertFalse(check_password('sai', stored))
        self.assertFalse(check_password('x', 'hỏng'))

    def test_mode_follows_users_file(self):  # WEB_REQUIRE_ACCOUNTS=0: local use only
        self.assertEqual(Auth(self.store, token='', require_accounts=False).mode, 'none')
        self.assertEqual(Auth(self.store, token='abc', require_accounts=False).mode, 'token')
        self.add()
        self.assertEqual(Auth(self.store, token='abc').mode, 'accounts')
        if os.name == 'posix':
            self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)

    def test_shared_token_mode(self):
        a = Auth(self.store, token='abc', require_accounts=False)
        self.assertEqual(a.check('Bearer abc'), (True, None))
        self.assertFalse(a.check('Bearer abd')[0])
        self.assertFalse(a.check('Bearer ábc')[0])  # non-ASCII header: refused, no TypeError

    def test_login_session_logout(self):
        self.add()
        a = Auth(self.store, token='abc')
        self.assertFalse(a.check('Bearer abc')[0])  # the shared code stops working once accounts exist
        token, user = a.login(' BS.AN ', 'mat-khau-1')
        self.assertEqual(user, {'username': 'bs.an', 'name': 'BS. An', 'role': 'doctor', 'must_change': False})
        ok, session = a.check('Bearer ' + token)
        self.assertTrue(ok)
        self.assertEqual(session['user'], 'bs.an')
        self.assertEqual(a.active(60), 1)
        a.logout(token)
        self.assertFalse(a.check('Bearer ' + token)[0])
        self.assertEqual(a.active(60), 0)

    def test_wrong_password_and_lockout(self):
        self.add()
        a = Auth(self.store)
        for _ in range(5):
            with self.assertRaisesRegex(PermissionError, 'không đúng'):
                a.login('bs.an', 'sai-mat-khau')
        with self.assertRaisesRegex(PermissionError, 'nhiều lần'):
            a.login('bs.an', 'mat-khau-1')
        with self.assertRaises(PermissionError):
            a.login('khong.co', 'mat-khau-1')

    def test_disabled_account_is_logged_out(self):
        self.add()
        a = Auth(self.store)
        token, _ = a.login('bs.an', 'mat-khau-1')
        time.sleep(0.01)  # a new mtime for the rewritten file
        self.add(disabled=True)
        self.assertFalse(a.check('Bearer ' + token)[0])
        with self.assertRaises(PermissionError):
            a.login('bs.an', 'mat-khau-1')

    def test_session_expires_when_idle(self):
        self.add()
        a = Auth(self.store, idle_hours=1)
        token, _ = a.login('bs.an', 'mat-khau-1')
        a.sessions[a._key(token)]['seen'] -= 3601
        self.assertFalse(a.check('Bearer ' + token)[0])

    def test_password_policy_and_temporary_password(self):
        self.assertIn('10', password_problem('ngan-qua'))
        self.assertIn('dễ đoán', password_problem('1234567890'))
        self.assertIn('dễ đoán', password_problem('aaaaaaaaaaaa'))
        self.assertIn('tên đăng nhập', password_problem('bs.an-2026-xyz', 'bs.an'))
        self.assertIsNone(password_problem('mây trắng bay qua đồi'))
        temp = temporary_password()
        self.assertRegex(temp, r'^[a-z2-9]{5}-[a-z2-9]{5}-[a-z2-9]{5}$')
        self.assertIsNone(password_problem(temp))

    def test_global_limit_stops_password_spraying(self):
        self.add()
        a = Auth(self.store)
        for i in range(30):
            with self.assertRaises(PermissionError):
                a.login(f'user{i:02d}', 'mat-khau-1')
        with self.assertRaisesRegex(PermissionError, 'tạm ngừng'):
            a.login('bs.an', 'mat-khau-1')

    def test_admin_actions_and_last_admin(self):
        self.add('quan.tri')
        self.store.modify(lambda u: u['quan.tri'].update(role='admin'))
        a = Auth(self.store)
        temp = a.create_user('Lab.Mot', 'Phòng Lab 1', 'lab')
        token, user = a.login('lab.mot', temp)
        self.assertEqual((user['role'], user['must_change']), ('lab', True))
        with self.assertRaisesRegex(ValueError, 'hiện tại'):
            a.change_password(token, 'sai', 'mây trắng bay qua đồi')
        a.change_password(token, temp, 'mây trắng bay qua đồi')
        self.assertFalse(a.check('Bearer ' + token)[1]['must_change'])
        self.assertEqual(a.update_user('quan.tri', 'lab.mot', role='doctor'), {'role': 'doctor'})
        self.assertEqual(a.check('Bearer ' + token)[1]['role'], 'doctor')  # applies to the open session
        new_temp = a.reset_password('lab.mot')
        self.assertFalse(a.check('Bearer ' + token)[0])  # reset logs the user out
        self.assertTrue(a.login('lab.mot', new_temp)[1]['must_change'])
        with self.assertRaisesRegex(ValueError, 'quản trị'):
            a.update_user('lab.mot', 'quan.tri', role='doctor')
        with self.assertRaisesRegex(ValueError, 'tự khóa'):
            a.update_user('quan.tri', 'quan.tri', disabled=True)
        with self.assertRaisesRegex(ValueError, 'tự xóa'):
            a.delete_user('quan.tri', 'quan.tri')
        with self.assertRaisesRegex(ValueError, 'đã có'):
            a.create_user('lab.mot', '', 'lab')
        a.delete_user('quan.tri', 'lab.mot')
        self.assertNotIn('lab.mot', self.store.all())

    def test_audit_log(self):
        log = AuditLog(Path(self.dir.name, 'audit.jsonl'))
        log.write('login_ok', 'bs.an', role='doctor')
        log.write('extract', 'bs.an', chars=20, findings=2)
        log.write('login_fail', 'khach')
        self.assertEqual([e['event'] for e in log.tail()], ['login_fail', 'extract', 'login_ok'])
        self.assertEqual([e['event'] for e in log.tail(user='bs.an', event='extract')], ['extract'])
        if os.name == 'posix':
            self.assertEqual(os.stat(log.path).st_mode & 0o777, 0o600)

    def test_manage_users_cli(self):
        import manage_users
        with patch('getpass.getpass', return_value='mat-khau-2'), redirect_stderr(StringIO()), \
             patch('sys.stdout', StringIO()):
            manage_users.main(['add', 'bs.binh', '--name', 'BS. Bình', '--role', 'admin', '--file', str(self.store.path)])
            self.assertEqual(Auth(self.store).login('bs.binh', 'mat-khau-2')[1]['role'], 'admin')
            manage_users.main(['role', 'bs.binh', '--role', 'lab', '--file', str(self.store.path)])
            self.assertEqual(self.store.get('bs.binh')['role'], 'lab')
            manage_users.main(['disable', 'bs.binh', '--file', str(self.store.path)])
            self.assertIsNone(self.store.get('bs.binh'))
            with self.assertRaises(SystemExit):
                manage_users.main(['add', 'X', '--file', str(self.store.path)])


class HttpTests(unittest.TestCase):
    """serve_web endpoints with a fake WebSystem: login starts Model 1, the last logout releases it."""

    @classmethod
    def setUpClass(cls):
        import serve_web
        cls.serve_web = serve_web
        cls.dir = tempfile.TemporaryDirectory()
        cls.store = UserStore(Path(cls.dir.name, 'users.json'))
        cls.store.save({'bs.an': {'name': 'BS. An', 'password': hash_password('mat-khau-1')}})
        cls.log = StringIO()
        cls.quiet = redirect_stderr(cls.log)
        cls.quiet.__enter__()
        cls.runner = model()
        system = types.SimpleNamespace(matcher=types.SimpleNamespace(profiles=[1, 2]), canonical={'HP:1': {}},
                                       runner=cls.runner, search_hpo=lambda q: [{'id': 'HP:1', 'q': q}],
                                       extract=lambda text: cls.runner.extract(text), match=lambda p: {'ok': True})
        cls.patches = [patch.object(serve_web, 'SYSTEM', system), patch.object(serve_web, 'AUTH', Auth(cls.store))]
        for p in cls.patches:
            p.start()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), serve_web.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.runner.stop()
        for p in cls.patches:
            p.stop()
        cls.quiet.__exit__(None, None, None)
        cls.dir.cleanup()

    def call(self, path, payload=None, token=None, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={'Content-Type': 'application/json', **(headers or {}),
                                              **({'Authorization': 'Bearer ' + token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        code, status = self.call('/api/status')
        # B10: without a login only online + sign-in mode; model state, errors and counts moved to /api/session
        self.assertEqual((code, status), (200, {'status': 'online', 'auth_mode': 'accounts'}))
        self.assertEqual(self.runner.status()['model_state'], 'stopped')
        self.assertIsNone(self.runner.proc)  # no VRAM before anyone logs in
        self.assertEqual(self.call('/api/hpo_search?q=a')[0], 401)
        self.assertEqual(self.call('/api/login', {'username': 'bs.an', 'password': 'sai'})[0], 401)
        code, login = self.call('/api/login', {'username': 'bs.an', 'password': 'mat-khau-1'})
        self.assertEqual(code, 200)
        self.assertIn(login['model_state'], ('loading', 'ready'))  # login starts loading Model 1
        token = login['token']
        session = self.call('/api/session', token=token)[1]
        self.assertEqual(session['user']['name'], 'BS. An')
        self.assertIn(session['model_state'], ('loading', 'ready'))
        self.assertEqual(session['profiles_count'], 2)
        self.assertEqual(self.call('/api/hpo_search?q=a', token=token)[1][0]['q'], 'a')
        code, out = self.call('/api/extract_hpo', {'text': 'Thai đầu nhỏ.'}, token)
        self.assertEqual((code, out['echo']), (200, 'Thai đầu nhỏ.'))
        self.assertEqual(self.call('/api/extract_hpo', {'text': 'long'}, token)[0], 422)
        self.assertEqual(self.call('/api/warmup', {}, token)[1]['model_state'], 'ready')
        proc = self.runner.proc
        self.assertEqual(self.call('/api/logout', {}, token)[0], 200)
        proc.wait(timeout=30)  # the last doctor logged out: the model process exits
        self.assertEqual(self.call('/api/session', token=token)[0], 401)



class AdminHttpTests(unittest.TestCase):
    """Roles on the HTTP API: admin page, lab without GPU, first login with a temporary password, audit log."""

    @classmethod
    def setUpClass(cls):
        import serve_web
        cls.dir = tempfile.TemporaryDirectory()
        cls.store = UserStore(Path(cls.dir.name, 'users.json'))
        cls.store.save({'quan.tri': {'name': 'Quản trị', 'role': 'admin', 'password': hash_password('mat-khau-quan-tri')},
                        'bs.an': {'name': 'BS. An', 'role': 'doctor', 'password': hash_password('mat-khau-1')}})
        cls.audit = AuditLog(Path(cls.dir.name, 'audit.jsonl'))
        cls.quiet = redirect_stderr(StringIO())
        cls.quiet.__enter__()
        cls.runner = model(pause_file=Path(cls.dir.name, 'gpu_pause'))
        system = types.SimpleNamespace(matcher=types.SimpleNamespace(profiles=[1]), canonical={'HP:1': {}}, runner=cls.runner,
                                       search_hpo=lambda q: [], extract=lambda text: {'mentions': [1, 2], 'seconds': 0.1},
                                       match=lambda p: {'ok': True})
        cls.patches = [patch.object(serve_web, 'SYSTEM', system), patch.object(serve_web, 'AUTH', Auth(cls.store, admin_2fa=False)),
                       patch.object(serve_web, 'AUDIT', cls.audit), patch.object(serve_web, 'free_vram_mib', lambda: 20000)]
        for p in cls.patches:
            p.start()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), serve_web.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.runner.stop()
        for p in cls.patches:
            p.stop()
        cls.quiet.__exit__(None, None, None)
        cls.dir.cleanup()

    call = HttpTests.call

    def login(self, username, password):
        code, data = self.call('/api/login', {'username': username, 'password': password})
        self.assertEqual(code, 200, data)
        return data['token'], data

    def test_roles_and_admin_page(self):
        doctor, _ = self.login('bs.an', 'mat-khau-1')
        self.assertEqual(self.call('/api/admin/users', token=doctor)[0], 403)
        admin, _ = self.login('quan.tri', 'mat-khau-quan-tri')
        code, users = self.call('/api/admin/users', token=admin)
        self.assertEqual((code, sorted(u['username'] for u in users['users'])), (200, ['bs.an', 'quan.tri']))
        self.assertNotIn('password', json.dumps(users))  # hashes never leave the server
        code, made = self.call('/api/admin/users/create', {'username': 'lab.hv', 'name': 'Lab HV', 'role': 'lab'}, admin)
        self.assertEqual(code, 200)
        self.runner.stop()
        lab, data = self.login('lab.hv', made['temp_password'])
        self.assertTrue(data['user']['must_change'])
        self.assertIsNone(self.runner.proc)  # a lab login never loads the GPU
        self.assertEqual(self.call('/api/hpo_search?q=a', token=lab)[0], 403)  # temporary password first
        self.assertEqual(self.call('/api/change_password', {'old_password': made['temp_password'],
                                                            'new_password': '123'}, lab)[0], 422)
        self.assertEqual(self.call('/api/change_password', {'old_password': made['temp_password'],
                                                            'new_password': 'mây trắng bay qua đồi'}, lab)[0], 200)
        self.assertEqual(self.call('/api/hpo_search?q=a', token=lab)[0], 200)
        self.assertEqual(self.call('/api/match_diseases', {'hpos': []}, lab)[0], 200)
        self.assertEqual(self.call('/api/extract_hpo', {'text': 'x'}, lab)[0], 403)
        self.assertEqual(self.call('/api/warmup', {}, lab)[0], 403)
        self.assertIsNone(self.runner.proc)
        code, sessions = self.call('/api/admin/sessions', token=admin)
        self.assertIn('lab.hv', [s['username'] for s in sessions['sessions']])
        self.assertEqual(self.call('/api/admin/sessions/end', {'username': 'lab.hv'}, admin)[1]['ended'], 1)
        self.assertEqual(self.call('/api/hpo_search?q=a', token=lab)[0], 401)
        self.assertEqual(self.call('/api/admin/users/update', {'username': 'quan.tri', 'role': 'doctor'}, admin)[0], 422)
        self.assertEqual(self.call('/api/admin/users/delete', {'username': 'lab.hv'}, admin)[0], 200)

    def test_gpu_controls_and_audit(self):
        admin, _ = self.login('quan.tri', 'mat-khau-quan-tri')
        doctor, _ = self.login('bs.an', 'mat-khau-1')
        self.assertEqual(self.call('/api/extract_hpo', {'text': 'Thai đầu nhỏ, bí mật.'}, doctor)[0], 200)
        code, gpu = self.call('/api/admin/gpu', {'action': 'pause'}, admin)
        self.assertEqual((code, gpu['paused'], gpu['free_mib']), (200, True, 20000))
        self.assertEqual(self.call('/api/extract_hpo', {'text': 'x'}, doctor)[0], 503)
        self.assertFalse(self.call('/api/admin/gpu', {'action': 'resume'}, admin)[1]['paused'])
        self.assertEqual(self.call('/api/login', {'username': 'bs.an', 'password': 'sai'})[0], 401)
        code, log = self.call('/api/admin/audit?limit=50', token=admin)
        events = [e['event'] for e in log['entries']]
        for event in ('login_ok', 'extract', 'gpu', 'login_fail'):
            self.assertIn(event, events)
        self.assertNotIn('bí mật', json.dumps(log, ensure_ascii=False))  # no case content in the log
        extract = next(e for e in log['entries'] if e['event'] == 'extract')
        self.assertEqual((extract['user'], extract['findings']), ('bs.an', 2))

    def test_security_headers(self):
        with urllib.request.urlopen(self.base + '/api/status', timeout=30) as r:
            self.assertIn("script-src 'self'", r.headers['Content-Security-Policy'])
            self.assertEqual(r.headers['X-Frame-Options'], 'DENY')


def fresh_rates(per_minute=1000, **override):
    from ratelimit import RateLimiter
    groups = ('hpo_search', 'match_diseases', 'extract_hpo', 'login', 'login_user', 'admin', 'corrections', 'bug_report',
              'disease_search', 'disease_feedback')
    return {g: RateLimiter(override.get(g, per_minute)) for g in groups}


def accounts(d, **users):
    """A users.json in d with these username=role accounts; each password is 'mat-khau-' + username."""
    store = UserStore(Path(d, 'users.json'))
    store.save({name: {'name': name, 'role': role, 'password': hash_password('mat-khau-' + name)}
                for name, role in users.items()})
    return store


class LiveServer:
    """serve_web.Handler on a free port with patched module globals; use as a context manager."""

    def __init__(self, **globals_):
        import serve_web
        self.serve_web = serve_web
        globals_.setdefault('SYSTEM', types.SimpleNamespace(
            matcher=types.SimpleNamespace(profiles=[1]), canonical={'HP:1': {}}, runner=model(),
            search_hpo=lambda q: [], extract=lambda text: {'mentions': [], 'seconds': 0.0}, match=lambda p: {'ok': True}))
        self.system = globals_['SYSTEM']
        globals_.setdefault('RATES', fresh_rates())  # each test starts with full budgets
        self.patches = [patch.object(serve_web, k, v) for k, v in globals_.items()]

    def __enter__(self):
        self.quiet = redirect_stderr(StringIO())
        self.stderr = self.quiet.__enter__()
        for p in self.patches:
            p.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.serve_web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.system.runner.stop()
        for p in self.patches:
            p.stop()
        self.quiet.__exit__(None, None, None)

    def call(self, path, payload=None, token=None, headers=None):
        return HttpTests.call(self, path, payload, token, headers)


def raw_get(base, path, token):
    """Send one GET on a raw socket and read until the server closes: every byte it wrote."""
    import socket
    host, port = base.rsplit('/', 1)[1].split(':')
    with socket.create_connection((host, int(port)), timeout=30) as s:
        s.sendall(f'GET {path} HTTP/1.0\r\nHost: {host}\r\nAuthorization: Bearer {token}\r\n\r\n'.encode())
        out = b''
        while chunk := s.recv(65536):
            out += chunk
    return out


class SingleResponseTests(unittest.TestCase):
    """B11: a doctor on /api/admin/* gets exactly one 403, not a 403 followed by a 404 on the same connection."""

    def test_admin_get_denied_once(self):
        with tempfile.TemporaryDirectory() as d:
            store = UserStore(Path(d, 'users.json'))
            store.save({'bs.an': {'name': 'BS. An', 'role': 'doctor', 'password': hash_password('mat-khau-1')}})
            auth = Auth(store)
            with LiveServer(AUTH=auth, AUDIT=AuditLog(Path(d, 'audit.jsonl'))) as srv:
                token, _ = auth.login('bs.an', 'mat-khau-1')
                for path in ('/api/admin/users', '/api/admin/audit?limit=5', '/api/admin/nothing'):
                    raw = raw_get(srv.base, path, token)
                    # a second reply follows the first body directly, so count status lines anywhere
                    self.assertEqual(len(re.findall(rb'HTTP/1\.[01] \d{3} ', raw)), 1, raw[:600])
                    self.assertTrue(raw.startswith(b'HTTP/1.0 403'), raw[:80])


class HeaderTests(unittest.TestCase):
    """B9: hardening headers on API and static responses; CORS from the GitHub Pages origin still works."""

    ORIGIN = 'https://ditruyenhungvuongorg.github.io'

    def test_headers_and_cors(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'WEB_ALLOWED_ORIGINS': self.ORIGIN}):
            store = UserStore(Path(d, 'users.json'))
            store.save({'bs.an': {'name': 'BS. An', 'password': hash_password('mat-khau-1')}})
            with LiveServer(AUTH=Auth(store), AUDIT=AuditLog(Path(d, 'audit.jsonl'))) as srv:
                for path in ('/api/status', '/', '/app.js'):
                    req = urllib.request.Request(srv.base + path, headers={'Origin': self.ORIGIN})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        h = r.headers
                    self.assertEqual(h['Strict-Transport-Security'], 'max-age=31536000; includeSubDomains')
                    self.assertIn('camera=()', h['Permissions-Policy'])
                    self.assertIn('microphone=()', h['Permissions-Policy'])
                    self.assertEqual(h['Cross-Origin-Opener-Policy'], 'same-origin')
                    self.assertEqual(h['Cross-Origin-Resource-Policy'], 'same-site')
                    self.assertEqual(h['Access-Control-Allow-Origin'], self.ORIGIN)
                    self.assertEqual(h['X-Content-Type-Options'], 'nosniff')
                    self.assertEqual(h['Referrer-Policy'], 'no-referrer')
                    self.assertIn("frame-ancestors 'none'", h['Content-Security-Policy'])
                req = urllib.request.Request(srv.base + '/api/login', method='OPTIONS', headers={
                    'Origin': self.ORIGIN, 'Access-Control-Request-Method': 'POST',
                    'Access-Control-Request-Headers': 'content-type,authorization'})
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.assertEqual((r.status, r.headers['Access-Control-Allow-Origin']), (204, self.ORIGIN))
                evil = {'Origin': 'https://evil.example'}
                with urllib.request.urlopen(urllib.request.Request(srv.base + '/api/status', headers=evil), timeout=30) as r:
                    self.assertIsNone(r.headers['Access-Control-Allow-Origin'])  # a foreign page cannot read it
                code, _ = srv.call('/api/login', {'username': 'bs.an', 'password': 'mat-khau-1'}, headers=evil)
                self.assertEqual(code, 403)

    def test_hsts_can_be_turned_off_for_plain_http(self):
        import serve_web
        with patch.object(serve_web, 'HSTS', False), tempfile.TemporaryDirectory() as d:
            store = UserStore(Path(d, 'users.json'))
            store.save({'bs.an': {'name': 'BS. An', 'password': hash_password('mat-khau-1')}})
            with LiveServer(AUTH=Auth(store), AUDIT=AuditLog(Path(d, 'audit.jsonl'))) as srv:
                with urllib.request.urlopen(srv.base + '/api/status', timeout=30) as r:
                    self.assertIsNone(r.headers['Strict-Transport-Security'])

    def test_page_csp_allows_only_the_configured_api(self):
        docs = Path(__file__).resolve().parent.parent / 'docs'
        html = (docs / 'index.html').read_text(encoding='utf-8')
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
        connect = next(d.split()[1:] for d in csp.split(';') if d.strip().startswith('connect-src'))
        api = re.search(r"apiBase:\s*'([^']*)'", (docs / 'config.js').read_text(encoding='utf-8')).group(1)
        self.assertEqual(sorted(connect), sorted(["'self'", api.rstrip('/')]))  # no https:, no localhost
        self.assertIn("script-src 'self';", csp)


class AccessLogTests(unittest.TestCase):
    """B8: one line per request with method, path without query, status, user, ms, bytes; nothing else."""

    def test_lines_without_query_body_or_token(self):
        with tempfile.TemporaryDirectory() as d:
            store = UserStore(Path(d, 'users.json'))
            store.save({'bs.an': {'name': 'BS. An', 'password': hash_password('mat-khau-1')}})
            log = AccessLog(Path(d, 'access.log'))
            with LiveServer(AUTH=Auth(store), AUDIT=AuditLog(Path(d, 'audit.jsonl')), ACCESS=log) as srv:
                code, login = srv.call('/api/login', {'username': 'bs.an', 'password': 'mat-khau-1'})
                token = login['token']
                srv.call('/api/hpo_search?q=' + urllib.parse.quote('não úng thủy BÍ-MẬT'), token=token)
                srv.call('/api/extract_hpo', {'text': 'Thai đầu nhỏ BÍ-MẬT-456.'}, token=token)
                srv.call('/api/nope%01', token=token)
                urllib.request.urlopen(srv.base + '/app.js', timeout=30).read()
            text = log.path.read_text(encoding='utf-8')
            lines = [json.loads(x) for x in text.splitlines()]
            # a line is written after its response, so the client may already be on the next request: no order
            self.assertEqual(sorted((e['method'], e['path'], e['status'], e['user']) for e in lines), sorted([
                ('POST', '/api/login', 200, '-'), ('GET', '/api/hpo_search', 200, 'bs.an'),
                ('POST', '/api/extract_hpo', 200, 'bs.an'), ('GET', '/api/nope%01', 404, 'bs.an'), ('GET', '/app.js', 200, '-')]))
            self.assertEqual(set(lines[0]), {'time', 'method', 'path', 'status', 'user', 'ms', 'bytes'})
            self.assertGreater(next(e for e in lines if e['path'] == '/app.js')['bytes'], 1000)
            for secret in (token, 'mat-khau-1', 'BÍ-MẬT', 'B\\u00cd', 'não', 'Thai'):
                self.assertNotIn(secret, text)
            if os.name == 'posix':
                self.assertEqual(os.stat(log.path).st_mode & 0o777, 0o600)

    def test_rotation_and_failures_do_not_break_requests(self):
        with tempfile.TemporaryDirectory() as d:
            log = AccessLog(Path(d, 'access.log'), max_bytes=200, keep=3)
            for i in range(40):
                log.write('GET', '/api/status', 200)
            names = sorted(p.name for p in Path(d).iterdir())
            self.assertEqual(names, ['access.log', 'access.log.1', 'access.log.2', 'access.log.3'])
            Path(d, 'blocker').write_text('x')
            broken = AccessLog(Path(d, 'blocker', 'access.log'))  # parent is a file: every write fails
            with redirect_stderr(StringIO()):
                broken.write('GET', '/', 200)
            self.assertIsNone(AccessLog('off').path)
            self.assertEqual(AccessLog.clean_path('/ab cạ?q=x'), '/a_b_c_')


class LimitTests(unittest.TestCase):
    """B6/B7: per-key request budgets (429 + Retry-After), bounded queue, connection cap, header deadline, unlock."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = accounts(self.dir.name, **{'bs.an': 'doctor', 'quan.tri': 'admin'})
        self.audit = AuditLog(Path(self.dir.name, 'audit.jsonl'))

    def tearDown(self):
        self.dir.cleanup()

    def login(self, srv, name):
        code, data = srv.call('/api/login', {'username': name, 'password': 'mat-khau-' + name})
        self.assertEqual(code, 200, data)
        return data['token']

    def test_search_budget_per_session(self):
        with LiveServer(AUTH=Auth(self.store, admin_2fa=False), AUDIT=self.audit, RATES=fresh_rates(hpo_search=3)) as srv:
            token = self.login(srv, 'bs.an')
            other = self.login(srv, 'quan.tri')
            self.assertEqual([srv.call('/api/hpo_search?q=a', token=token)[0] for _ in range(3)], [200] * 3)
            req = urllib.request.Request(srv.base + '/api/hpo_search?q=a', headers={'Authorization': 'Bearer ' + token})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=30)
            self.assertEqual(caught.exception.code, 429)
            self.assertGreaterEqual(int(caught.exception.headers['Retry-After']), 1)
            self.assertEqual(srv.call('/api/hpo_search?q=a', token=token)[0], 429)
            self.assertEqual(srv.call('/api/hpo_search?q=a', token=other)[0], 200)  # another session, own budget
        limited = [e for e in self.audit.tail() if e['event'] == 'rate_limited']
        self.assertEqual([(e['user'], e['group']) for e in limited], [('bs.an', 'hpo_search')])  # one line, not two

    def test_login_budget_per_source_counts_failures_only(self):
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit, RATES=fresh_rates(login=3)) as srv:
            for _ in range(5):
                self.login(srv, 'bs.an')  # successful logins are refunded
            for name in ('khach1', 'khach2', 'khach3'):
                self.assertEqual(srv.call('/api/login', {'username': name, 'password': 'sai-mat-khau'})[0], 401)
            code, data = srv.call('/api/login', {'username': 'bs.an', 'password': 'mat-khau-bs.an'})
            self.assertEqual(code, 429)
            self.assertIn('thử lại sau', data['error'])

    def test_login_budget_per_username(self):
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit, RATES=fresh_rates(login_user=2)) as srv:
            for _ in range(2):
                self.assertEqual(srv.call('/api/login', {'username': 'bs.an', 'password': 'sai-mat-khau'})[0], 401)
            self.assertEqual(srv.call('/api/login', {'username': 'bs.an', 'password': 'sai-mat-khau'})[0], 429)
            self.assertEqual(srv.call('/api/login', {'username': 'quan.tri', 'password': 'sai-mat-khau'})[0], 401)

    def test_queue_of_six_refuses_one(self):
        import serve_web
        from ratelimit import WorkQueue
        gate = threading.Event()
        system = types.SimpleNamespace(matcher=types.SimpleNamespace(profiles=[1]), canonical={}, runner=model(),
                                       search_hpo=lambda q: [], extract=lambda t: {},
                                       match=lambda p: (gate.wait(30), {'ok': True})[1])
        work = WorkQueue(5)
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit, SYSTEM=system, WORK=work) as srv:
            token = self.login(srv, 'bs.an')
            results = []
            hpos = {'hpos': [{'id': 'HP:0000252', 'status': 'CÓ'}]}
            send = lambda: results.append(srv.call('/api/match_diseases', hpos, token)[0])
            first = threading.Thread(target=send)
            first.start()
            deadline = time.monotonic() + 10
            while work.slot._value and time.monotonic() < deadline:  # the first request holds the slot
                time.sleep(0.01)
            waiters = [threading.Thread(target=send) for _ in range(6)]
            for t in waiters:
                t.start()
            while (work.waiting < 5 or not results) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(results, [503])  # the sixth waiter is refused at once; five wait
            gate.set()
            for t in [first] + waiters:
                t.join(30)
            self.assertEqual(sorted(results), [200] * 6 + [503])
        self.assertGreaterEqual(serve_web.QUEUE_SECONDS, 30)

    def test_connection_cap_and_header_deadline(self):
        import socket
        import serve_web
        system = types.SimpleNamespace(matcher=types.SimpleNamespace(profiles=[1]), canonical={}, runner=model())
        with patch.object(serve_web, 'HEADER_SECONDS', 1), patch.object(serve_web, 'AUTH', Auth(self.store)), \
             patch.object(serve_web, 'SYSTEM', system), patch.object(serve_web, 'AUDIT', self.audit), \
             patch.object(serve_web, 'ACCESS', AccessLog('off')), redirect_stderr(StringIO()):
            server = serve_web.Server(('127.0.0.1', 0), serve_web.Handler, max_connections=2)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            port = server.server_address[1]
            try:
                idle = [socket.create_connection(('127.0.0.1', port), timeout=10) for _ in range(2)]
                time.sleep(0.2)
                with socket.create_connection(('127.0.0.1', port), timeout=10) as third:
                    self.assertTrue(third.recv(4096).startswith(b'HTTP/1.0 503'))  # over the cap: short 503
                # slowloris: the request line arrives, then one header byte every 0.3 s; dropped after 1 s
                started = time.monotonic()
                idle[0].sendall(b'GET / HTTP/1.0\r\n')
                closed = False
                while time.monotonic() - started < 5:
                    try:
                        idle[0].sendall(b'X')
                        idle[0].settimeout(0.3)
                        if idle[0].recv(1) == b'':
                            closed = True
                            break
                    except socket.timeout:
                        continue
                    except OSError:
                        closed = True
                        break
                self.assertTrue(closed)
                self.assertLess(time.monotonic() - started, 4)
                for s in idle:
                    s.close()
                time.sleep(1.5)  # the other idle connection times out as well and frees its slot
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=10) as r:
                    self.assertEqual(r.status, 200)
            finally:
                server.shutdown()
                server.server_close()

    def test_admin_unlocks_a_locked_login(self):
        with LiveServer(AUTH=Auth(self.store, admin_2fa=False), AUDIT=self.audit) as srv:
            admin = self.login(srv, 'quan.tri')
            for _ in range(5):
                srv.call('/api/login', {'username': 'bs.an', 'password': 'sai-mat-khau'})
            self.assertEqual(srv.call('/api/login', {'username': 'bs.an', 'password': 'mat-khau-bs.an'})[0], 429)
            users = {u['username']: u for u in srv.call('/api/admin/users', token=admin)[1]['users']}
            self.assertTrue(users['bs.an']['login_locked'])
            self.assertFalse(users['quan.tri']['login_locked'])
            code, out = srv.call('/api/admin/users/unlock', {'username': 'bs.an'}, admin)
            self.assertEqual((code, out['was_locked']), (200, True))
            self.login(srv, 'bs.an')
            self.assertEqual(srv.call('/api/admin/users/unlock', {'username': 'khong.co'}, admin)[0], 422)
        events = [(e['event'], e.get('target')) for e in self.audit.tail()]
        self.assertIn(('login_unlocked', 'bs.an'), events)

    def test_limiter_refills(self):
        from ratelimit import RateLimiter
        r = RateLimiter(60, burst=2)
        self.assertEqual([r.take('k', now=0), r.take('k', now=0)], [0, 0])
        self.assertEqual(r.take('k', now=0), 1)
        self.assertEqual(r.take('k', now=1.01), 0)  # one per second comes back
        self.assertEqual(RateLimiter(0).take('k'), 0)  # 0 = off


class TotpTests(unittest.TestCase):
    """B5: TOTP (RFC 6238) for admins (required) and doctors (optional); backup codes; reset."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = accounts(self.dir.name, **{'bs.an': 'doctor', 'quan.tri': 'admin', 'quan.tri2': 'admin'})
        self.audit = AuditLog(Path(self.dir.name, 'audit.jsonl'))

    def tearDown(self):
        self.dir.cleanup()

    def test_rfc6238_vectors_and_window(self):
        import base64
        import totp
        secret = base64.b32encode(b'12345678901234567890').decode()  # RFC 6238 appendix B, SHA-1
        for t, expected in ((59, '94287082'), (1111111109, '07081804'), (1234567890, '89005924')):
            self.assertEqual(totp.code(secret, t, digits=8), expected)
        self.assertEqual(totp.code(secret, 59), '287082')
        now = 1_700_000_000
        self.assertIsNotNone(totp.verify(secret, totp.code(secret, now), t=now))
        self.assertIsNotNone(totp.verify(secret, totp.code(secret, now - 30), t=now))   # one step late: accepted
        self.assertIsNotNone(totp.verify(secret, totp.code(secret, now + 30), t=now))   # one step early: accepted
        self.assertIsNone(totp.verify(secret, totp.code(secret, now - 60), t=now))      # two steps: refused
        self.assertIsNone(totp.verify(secret, totp.code(secret, now + 60), t=now))
        step = totp.verify(secret, totp.code(secret, now), t=now)
        self.assertIsNone(totp.verify(secret, totp.code(secret, now), t=now, after=step))  # no replay
        self.assertIsNone(totp.verify(secret, 'abcdef', t=now))
        self.assertEqual(len(totp.generate_secret()), 32)
        self.assertTrue(totp.uri('bs.an', 'ABC').startswith('otpauth://totp/Fe2Gen:bs.an?secret=ABC&issuer=Fe2Gen'))
        codes = totp.backup_codes()
        self.assertEqual((len(codes), len(set(codes))), (8, 8))
        self.assertRegex(codes[0], r'^[a-z2-9]{4}-[a-z2-9]{4}$')

    def login(self, srv, name):
        code, data = srv.call('/api/login', {'username': name, 'password': 'mat-khau-' + name})
        self.assertEqual(code, 200, data)
        return data

    def enable(self, srv, token):
        import totp
        code, setup = srv.call('/api/totp/setup', {}, token)
        self.assertEqual(code, 200, setup)
        self.assertTrue(setup['uri'].startswith('otpauth://totp/'))
        self.assertEqual(srv.call('/api/totp/enable', {'code': '000000'}, token)[0], 422)
        code, done = srv.call('/api/totp/enable', {'code': totp.code(setup['secret'])}, token)
        self.assertEqual(code, 200, done)
        self.assertEqual(len(done['backup_codes']), 8)
        return setup['secret'], done['backup_codes']

    def test_admin_must_enable_before_admin_pages(self):
        import totp
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit) as srv:
            admin = self.login(srv, 'quan.tri')
            self.assertEqual((admin['totp_required'], admin['totp_enabled']), (True, False))
            token = admin['token']
            code, data = srv.call('/api/admin/users', token=token)
            self.assertEqual((code, data.get('totp_required')), (403, True))
            # changing the password is allowed before 2FA is on (here refused only because the old one is wrong)
            self.assertEqual(srv.call('/api/change_password', {'old_password': 'x', 'new_password': 'y'}, token)[0], 422)
            session = srv.call('/api/session', token=token)[1]
            self.assertEqual((session['totp_required'], session['idle_minutes']), (True, 60))
            self.assertEqual(srv.call('/api/admin/users/create', {'username': 'x.y', 'role': 'lab'}, token)[0], 403)
            self.assertEqual(srv.call('/api/hpo_search?q=a', token=token)[0], 403)
            self.assertEqual(srv.call('/api/session', token=token)[0], 200)
            secret, backup = self.enable(srv, token)
            self.assertFalse(srv.call('/api/session', token=token)[1]['totp_required'])
            code, users = srv.call('/api/admin/users', token=token)  # the same session is now verified
            self.assertEqual(code, 200)
            self.assertNotIn(secret, json.dumps(users))                   # the secret never leaves again
            self.assertNotIn('backup', json.dumps(users))
            self.assertTrue(next(u for u in users['users'] if u['username'] == 'quan.tri')['totp_enabled'])
            # next login: password, then code
            code, first = srv.call('/api/login', {'username': 'quan.tri', 'password': 'mat-khau-quan.tri'})
            self.assertEqual((code, first['second_factor'], first['user']), (200, True, {'username': 'quan.tri', 'name': 'quan.tri'}))
            self.assertNotIn('token', first)
            self.assertEqual(srv.call('/api/admin/users', token=first['pending'])[0], 401)  # pending is no session
            code, bad = srv.call('/api/login/totp', {'pending': first['pending'], 'code': '000000'})
            self.assertEqual(code, 401)
            # the code used to enable cannot be replayed; wait for the next step is too slow, use a backup code
            code, ok = srv.call('/api/login/totp', {'pending': first['pending'], 'code': backup[0]})
            self.assertEqual(code, 200, ok)
            self.assertEqual((ok['totp_required'], ok['totp_enabled']), (False, True))
            self.assertEqual(srv.call('/api/admin/users', token=ok['token'])[0], 200)
            # a backup code works once
            first = srv.call('/api/login', {'username': 'quan.tri', 'password': 'mat-khau-quan.tri'})[1]
            self.assertEqual(srv.call('/api/login/totp', {'pending': first['pending'], 'code': backup[0]})[0], 401)
            self.assertEqual(srv.call('/api/login/totp', {'pending': first['pending'], 'code': backup[1]})[0], 200)
        stored = self.store.all()['quan.tri']['totp']
        self.assertEqual(len(stored['backup']), 6)
        self.assertNotIn(backup[2], json.dumps(stored))  # only hashes are kept
        events = [e['event'] for e in self.audit.tail()]
        for event in ('totp_enabled', 'totp_fail', 'totp_backup_used', 'login_ok'):
            self.assertIn(event, events)

    def test_five_wrong_codes_end_the_pending_login(self):
        import totp
        auth = Auth(self.store)
        users = self.store.all()
        users['bs.an']['totp'] = {'secret': totp.generate_secret(), 'enabled': True, 'backup': []}
        self.store.save(users)
        with LiveServer(AUTH=auth, AUDIT=self.audit) as srv:
            first = self.login(srv, 'bs.an')
            for _ in range(4):
                self.assertEqual(srv.call('/api/login/totp', {'pending': first['pending'], 'code': '000000'})[0], 401)
            code, data = srv.call('/api/login/totp', {'pending': first['pending'], 'code': '000000'})
            self.assertIn('đăng nhập lại', data['error'])
            secret = self.store.all()['bs.an']['totp']['secret']
            code, data = srv.call('/api/login/totp', {'pending': first['pending'], 'code': totp.code(secret)})
            self.assertEqual(code, 401)  # the pending login is gone, even with the right code
            # wrong codes count toward the username lock like wrong passwords
            self.assertEqual(srv.call('/api/login', {'username': 'bs.an', 'password': 'mat-khau-bs.an'})[0], 429)
        self.assertEqual(sum(e['event'] == 'totp_fail' for e in self.audit.tail()), 5)

    def test_pending_login_expires(self):
        import totp
        users = self.store.all()
        users['bs.an']['totp'] = {'secret': totp.generate_secret(), 'enabled': True, 'backup': []}
        self.store.save(users)
        auth = Auth(self.store)
        _, first = auth.login('bs.an', 'mat-khau-bs.an')
        auth.pending[auth._key(first['pending'])]['created'] -= 301
        with self.assertRaisesRegex(PermissionError, 'hết hạn'):
            auth.login_totp(first['pending'], totp.code(users['bs.an']['totp']['secret']))

    def test_doctor_without_2fa_logs_in_as_before_and_may_enable(self):
        import totp
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit) as srv:
            doctor = self.login(srv, 'bs.an')
            self.assertIn('token', doctor)
            self.assertFalse(doctor['totp_required'])
            self.assertEqual(srv.call('/api/hpo_search?q=a', token=doctor['token'])[0], 200)
            secret, _ = self.enable(srv, doctor['token'])
            self.assertEqual(srv.call('/api/totp/setup', {}, doctor['token'])[0], 422)  # already on
            first = self.login(srv, 'bs.an')
            self.assertTrue(first['second_factor'])
            later = time.time() + 30  # the next step: the code used to enable it was spent
            code, ok = srv.call('/api/login/totp', {'pending': first['pending'], 'code': totp.code(secret, later)})
            self.assertEqual(code, 200, ok)

    def test_admin_resets_another_users_2fa(self):
        import totp
        users = self.store.all()
        for name in ('quan.tri', 'bs.an'):
            users[name]['totp'] = {'secret': totp.generate_secret(), 'enabled': True, 'backup': []}
        self.store.save(users)
        auth = Auth(self.store)
        with LiveServer(AUTH=auth, AUDIT=self.audit) as srv:
            first = self.login(srv, 'quan.tri')
            admin = srv.call('/api/login/totp', {'pending': first['pending'],
                                                 'code': totp.code(users['quan.tri']['totp']['secret'])})[1]['token']
            self.assertEqual(srv.call('/api/admin/users/totp_reset', {'username': 'quan.tri'}, admin)[0], 422)  # not self
            self.assertEqual(srv.call('/api/admin/users/totp_reset', {'username': 'bs.an'}, admin)[0], 200)
            self.assertNotIn('totp', self.store.all()['bs.an'])
            self.assertIn('token', self.login(srv, 'bs.an'))  # back to password only
            self.assertEqual(srv.call('/api/admin/users/totp_reset', {'username': 'bs.an'}, admin)[0], 422)
        self.assertIn(('totp_reset', 'bs.an'), [(e['event'], e.get('target')) for e in self.audit.tail()])

    def test_policy_off_for_local_use(self):
        with LiveServer(AUTH=Auth(self.store, admin_2fa=False), AUDIT=self.audit) as srv:
            admin = self.login(srv, 'quan.tri')
            self.assertEqual(srv.call('/api/admin/users', token=admin['token'])[0], 200)
        with patch.dict(os.environ, {'WEB_ADMIN_2FA_REQUIRED': '0'}):
            self.assertFalse(Auth(self.store).admin_2fa)
        with patch.dict(os.environ, {}):
            os.environ.pop('WEB_ADMIN_2FA_REQUIRED', None)
            self.assertTrue(Auth(self.store).admin_2fa)

    def test_manage_users_totp_reset(self):
        import manage_users
        import totp
        users = self.store.all()
        users['quan.tri']['totp'] = {'secret': totp.generate_secret(), 'enabled': True, 'backup': []}
        self.store.save(users)
        with patch('sys.stdout', StringIO()):
            manage_users.main(['totp-reset', 'quan.tri', '--file', str(self.store.path)])
        self.assertNotIn('totp', self.store.all()['quan.tri'])


class SessionPolicyTests(unittest.TestCase):
    """B12/B13/B16: configurable idle and session limits, temporary passwords expire, logout clears the cache."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = accounts(self.dir.name, **{'bs.an': 'doctor', 'quan.tri': 'admin'})

    def tearDown(self):
        self.dir.cleanup()

    def test_idle_and_session_limits_from_environment(self):
        with patch.dict(os.environ, {'WEB_IDLE_MINUTES': '30', 'WEB_SESSION_HOURS': '8'}):
            a = Auth(self.store)
        self.assertEqual((a.idle_seconds, a.session_seconds), (1800, 8 * 3600))
        with patch.dict(os.environ, {}):
            for k in ('WEB_IDLE_MINUTES', 'WEB_SESSION_HOURS'):
                os.environ.pop(k, None)
            a = Auth(self.store)
        self.assertEqual((a.idle_seconds, a.session_seconds), (3600, 12 * 3600))  # defaults: 60 min, 12 h
        token, _ = a.login('bs.an', 'mat-khau-bs.an')
        self.assertEqual(a.describe(a.session(token))['session_idle_minutes'], 60)
        a.sessions[a._key(token)]['seen'] -= 3601
        self.assertFalse(a.check('Bearer ' + token)[0])

    def test_temporary_password_expires(self):
        with patch.dict(os.environ, {'WEB_TEMP_PASSWORD_HOURS': '72'}):
            a = Auth(self.store)
        temp = a.create_user('lab.mot', 'Lab 1', 'lab')
        until = self.store.all()['lab.mot']['temp_until']
        self.assertAlmostEqual(until, time.time() + 72 * 3600, delta=60)
        self.assertTrue(a.login('lab.mot', temp)[1]['must_change'])          # still valid
        self.store.modify(lambda u: u['lab.mot'].update(temp_until=int(time.time()) - 1))
        with self.assertRaisesRegex(PermissionError, 'hết hạn') as caught:
            a.login('lab.mot', temp)
        self.assertEqual(caught.exception.event, 'temp_password_expired')
        with self.assertRaisesRegex(PermissionError, 'không đúng'):           # a wrong password says nothing more
            a.login('lab.mot', 'sai-mat-khau-tam')
        new_temp = a.reset_password('lab.mot')                               # the admin issues a new one
        token, _ = a.login('lab.mot', new_temp)
        a.change_password(token, new_temp, 'mây trắng bay qua đồi')
        self.assertNotIn('temp_until', self.store.all()['lab.mot'])
        self.assertFalse(public_user('lab.mot', self.store.all()['lab.mot'])['temp_expired'])
        legacy = {'name': 'x', 'password': hash_password('mat-khau-legacy'), 'must_change': True}  # made before the field
        self.store.modify(lambda u: u.update({'cu.ma': legacy}))
        self.assertTrue(a.login('cu.ma', 'mat-khau-legacy')[1]['must_change'])

    def test_expired_temporary_password_over_http(self):
        audit = AuditLog(Path(self.dir.name, 'audit.jsonl'))
        auth = Auth(self.store, admin_2fa=False)
        temp = auth.create_user('bs.binh', 'BS. Bình', 'doctor')
        self.store.modify(lambda u: u['bs.binh'].update(temp_until=1))
        with LiveServer(AUTH=auth, AUDIT=audit) as srv:
            code, data = srv.call('/api/login', {'username': 'bs.binh', 'password': temp})
            self.assertEqual(code, 403)
            self.assertIn('nhờ quản trị viên đặt lại', data['error'])
            admin = srv.call('/api/login', {'username': 'quan.tri', 'password': 'mat-khau-quan.tri'})[1]['token']
            users = {u['username']: u for u in srv.call('/api/admin/users', token=admin)[1]['users']}
            self.assertTrue(users['bs.binh']['temp_expired'])
            sessions = srv.call('/api/admin/sessions', token=admin)[1]
            self.assertEqual((sessions['idle_minutes'], sessions['session_hours']), (60, 12))
        self.assertIn('temp_password_expired', [e['event'] for e in audit.tail()])

    def test_logout_clears_site_cache(self):
        with LiveServer(AUTH=Auth(self.store), AUDIT=AuditLog(Path(self.dir.name, 'audit.jsonl'))) as srv:
            token = srv.call('/api/login', {'username': 'bs.an', 'password': 'mat-khau-bs.an'})[1]['token']
            req = urllib.request.Request(srv.base + '/api/logout', data=b'{}', method='POST',
                                         headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                self.assertEqual(r.headers['Clear-Site-Data'], '"cache"')


class ModelStateFileTests(unittest.TestCase):
    """B10: gpu.sh reads the model state from a local file, not from the public /api/status."""

    def test_state_file_follows_the_process(self):
        with tempfile.TemporaryDirectory() as d, redirect_stderr(StringIO()):
            state = Path(d, 'model1_state.json')
            m = model(state_file=state)
            try:
                m.extract('a')
                self.assertEqual(json.loads(state.read_text(encoding='utf-8'))['model_state'], 'ready')
                m.stop('test')
                data = json.loads(state.read_text(encoding='utf-8'))
                self.assertEqual((data['model_state'], data['paused']), ('stopped', False))
                if os.name == 'posix':
                    self.assertEqual(os.stat(state).st_mode & 0o777, 0o600)
            finally:
                m.stop()


class DurableLockAndAuditChainTests(unittest.TestCase):
    """B14: the login lock survives a restart; the audit log is hash-chained, verifiable and copied to stderr."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = accounts(self.dir.name, **{'bs.an': 'doctor'})

    def tearDown(self):
        self.dir.cleanup()

    def test_lock_survives_restart(self):
        a = Auth(self.store)
        for _ in range(5):
            with self.assertRaises(PermissionError):
                a.login('bs.an', 'sai-mat-khau')
        failures = Path(self.dir.name, 'login_failures.json')
        self.assertTrue(failures.exists())                      # next to users.json
        if os.name == 'posix':
            self.assertEqual(os.stat(failures).st_mode & 0o777, 0o600)
        restarted = Auth(self.store)                            # the service restarted
        self.assertGreater(restarted.locked_seconds('bs.an'), 0)
        with self.assertRaisesRegex(PermissionError, 'nhiều lần'):
            restarted.login('bs.an', 'mat-khau-bs.an')
        restarted.unlock('bs.an')
        self.assertEqual(Auth(self.store).locked_seconds('bs.an'), 0)  # the unlock is saved too
        Auth(self.store).login('bs.an', 'mat-khau-bs.an')

    def test_sprayed_names_do_not_grow_the_file_without_bound(self):
        a = Auth(self.store)
        a.MAX_FAILURE_KEYS = 50
        for i in range(80):
            with self.assertRaises(PermissionError):
                a.login(f'khach{i:03d}', 'sai')
            a.recent.clear()  # keep the global spraying pause out of the way
        self.assertLessEqual(len(json.loads(Path(self.dir.name, 'login_failures.json').read_text())), 50)

    def test_broken_failures_file_is_ignored(self):
        Path(self.dir.name, 'login_failures.json').write_text('{oops', encoding='utf-8')
        with redirect_stderr(StringIO()) as err:
            a = Auth(self.store)
        self.assertEqual(a.failures, {})
        self.assertIn('login_failures.json', err.getvalue())

    def test_audit_chain_detects_edits_and_deletions(self):
        import audit
        log = AuditLog(Path(self.dir.name, 'audit.jsonl'))
        with redirect_stderr(StringIO()) as err:
            for i in range(5):
                log.write('match', 'bs.an', hpo_count=i)
        self.assertEqual(err.getvalue().count('[audit] '), 5)  # second copy for journald
        lines = log.path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(json.loads(lines[0])['prev'], '0' * 64)
        self.assertNotIn('prev', log.tail()[0])                 # the admin page does not show the hashes
        ok, report = audit.verify([log.path])
        self.assertTrue(ok, report)
        self.assertIn('OK: 5', report[-1])
        edited = lines[:2] + [lines[2].replace('"hpo_count": 2', '"hpo_count": 9')] + lines[3:]
        log.path.write_text('\n'.join(edited) + '\n', encoding='utf-8')
        ok, report = audit.verify([log.path])
        self.assertFalse(ok)
        self.assertIn(':4:', report[-1])                        # the line after the edited one no longer matches
        log.path.write_text('\n'.join(lines[:1] + lines[2:]) + '\n', encoding='utf-8')  # a line deleted
        self.assertFalse(audit.verify([log.path])[0])
        log.path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        with patch('sys.stdout', StringIO()) as out:
            self.assertEqual(audit.main(['verify', str(log.path)]), 0)
        self.assertIn('OK', out.getvalue())

    def test_chain_continues_across_rotation_and_after_legacy_lines(self):
        import audit
        path = Path(self.dir.name, 'audit.jsonl')
        path.write_text('{"time": "2026-10-01 08:00:00", "event": "login_ok", "user": "cu"}\n', encoding='utf-8')
        log = AuditLog(path)
        with redirect_stderr(StringIO()), patch.object(audit, 'MAX_BYTES', 300):
            for i in range(8):
                log.write('match', 'bs.an', hpo_count=i)
        older = path.with_name('audit.jsonl.1')
        self.assertTrue(older.exists())
        ok, report = audit.verify([older, path])
        self.assertTrue(ok, report)
        ok, report = audit.verify([path])                       # the newest file alone continues an older one
        self.assertTrue(ok, report)
        self.assertTrue(any('nối tiếp' in r for r in report))


class ApiContractTests(unittest.TestCase):
    """Field names the page relies on: setup kept in users.json, disable, locked_until, gpu_idle_minutes."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = accounts(self.dir.name, **{'bs.an': 'doctor', 'quan.tri': 'admin'})
        self.audit = AuditLog(Path(self.dir.name, 'audit.jsonl'))

    def tearDown(self):
        self.dir.cleanup()

    def login(self, srv, name):
        code, data = srv.call('/api/login', {'username': name, 'password': 'mat-khau-' + name})
        self.assertEqual(code, 200, data)
        return data

    def test_setup_is_pending_in_users_file_until_enabled(self):
        import totp
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit) as srv:
            token = self.login(srv, 'bs.an')['token']
            setup = srv.call('/api/totp/setup', {}, token)[1]
            self.assertEqual(len(setup['secret']), 32)
            self.assertTrue(setup['uri'].startswith('otpauth://totp/Fe2Gen:bs.an?secret=' + setup['secret'] + '&issuer=Fe2Gen'))
            stored = self.store.all()['bs.an']['totp']
            self.assertEqual(stored, {'pending_secret': setup['secret']})
            self.assertIn('token', self.login(srv, 'bs.an'))         # not in force yet
            again = srv.call('/api/totp/setup', {}, token)[1]          # a new setup replaces the pending secret
            self.assertEqual(srv.call('/api/totp/enable', {'code': totp.code(setup['secret'])}, token)[0], 422)
            done = srv.call('/api/totp/enable', {'code': totp.code(again['secret'])}, token)[1]
            self.assertEqual(len(done['backup_codes']), 8)
            stored = self.store.all()['bs.an']['totp']
            self.assertNotIn('pending_secret', stored)
            self.assertEqual(len(stored['backup']), 8)
            self.assertTrue(all(len(h) == 64 for h in stored['backup']))

    def test_user_turns_own_2fa_off_but_an_admin_cannot(self):
        import totp
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit) as srv:
            token = self.login(srv, 'bs.an')['token']
            secret = srv.call('/api/totp/setup', {}, token)[1]['secret']
            backup = srv.call('/api/totp/enable', {'code': totp.code(secret)}, token)[1]['backup_codes']
            self.assertEqual(srv.call('/api/totp/disable', {'password': 'sai-mat-khau', 'code': backup[0]}, token)[0], 422)
            self.assertEqual(srv.call('/api/totp/disable', {'password': 'mat-khau-bs.an', 'code': '000000'}, token)[0], 422)
            code, out = srv.call('/api/totp/disable', {'password': 'mat-khau-bs.an', 'code': backup[0]}, token)
            self.assertEqual((code, out['ok'], out['totp_enabled']), (200, True, False))
            self.assertNotIn('totp', self.store.all()['bs.an'])
            self.assertIn('token', self.login(srv, 'bs.an'))
            admin_auth = Auth(self.store)
            users = self.store.all()
            users['quan.tri']['totp'] = {'secret': totp.generate_secret(), 'enabled': True, 'backup': []}
            self.store.save(users)
            first = self.login(srv, 'quan.tri')
            admin = srv.call('/api/login/totp', {'pending': first['pending'],
                                                 'code': totp.code(users['quan.tri']['totp']['secret'])})[1]['token']
            code, out = srv.call('/api/totp/disable', {'password': 'mat-khau-quan.tri',
                                                       'code': totp.code(users['quan.tri']['totp']['secret'], time.time() + 30)}, admin)
            self.assertEqual(code, 422)
            self.assertIn('quản trị', out['error'])
        self.assertIn('totp_disabled', [e['event'] for e in self.audit.tail()])

    def test_admin_with_temporary_password_changes_it_first_then_sets_up_2fa(self):
        auth = Auth(self.store)
        temp = auth.reset_password('quan.tri')
        with LiveServer(AUTH=auth, AUDIT=self.audit) as srv:
            data = srv.call('/api/login', {'username': 'quan.tri', 'password': temp})[1]
            token = data['token']
            self.assertTrue(data['user']['must_change'])
            self.assertEqual(srv.call('/api/totp/setup', {}, token)[0], 403)  # the password comes first
            code, out = srv.call('/api/change_password', {'old_password': temp, 'new_password': 'mây trắng bay qua đồi'}, token)
            self.assertEqual(code, 200)
            self.assertTrue(out['user']['totp_required'])
            self.assertEqual(srv.call('/api/totp/setup', {}, token)[0], 200)
            self.assertEqual(srv.call('/api/hpo_search?q=a', token=token)[0], 403)

    def test_locked_until_and_gpu_idle_minutes(self):
        with LiveServer(AUTH=Auth(self.store, admin_2fa=False), AUDIT=self.audit) as srv:
            admin = self.login(srv, 'quan.tri')
            self.assertEqual((admin['idle_minutes'], admin['gpu_idle_minutes']), (60, 60))
            for _ in range(5):
                srv.call('/api/login', {'username': 'bs.an', 'password': 'sai-mat-khau'})
            users = {u['username']: u for u in srv.call('/api/admin/users', token=admin['token'])[1]['users']}
            self.assertRegex(users['bs.an']['locked_until'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$')
            self.assertEqual(users['quan.tri']['locked_until'], '')
            self.assertEqual(srv.call('/api/admin/users/unlock', {'username': 'bs.an'}, admin['token'])[1]['ok'], True)
            session = srv.call('/api/session', token=admin['token'])[1]
            self.assertEqual((session['idle_minutes'], session['gpu_idle_minutes'], session['totp_enabled']), (60, 60, False))
            for key in ('model_state', 'model_error', 'model_load_seconds', 'model_configured'):
                self.assertIn(key, session)
            gpu = srv.call('/api/admin/gpu', token=admin['token'])[1]
            self.assertEqual(gpu['idle_minutes'], 60)  # admin.js reads the GPU idle time under this name


class ContractAlignmentTests(unittest.TestCase):
    """What docs/app.js, twofactor.js and admin.js read from the API after merging the front-end branch."""

    ORIGIN = 'https://ditruyenhungvuongorg.github.io'

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = accounts(self.dir.name, **{'bs.an': 'doctor', 'quan.tri': 'admin'})
        self.audit = AuditLog(Path(self.dir.name, 'audit.jsonl'))

    def tearDown(self):
        self.dir.cleanup()

    def login(self, srv, name, password=None):
        code, data = srv.call('/api/login', {'username': name, 'password': password or 'mat-khau-' + name})
        self.assertEqual(code, 200, data)
        return data

    def enable(self, srv, token):
        import totp
        secret = srv.call('/api/totp/setup', {}, token)[1]['secret']
        return secret, srv.call('/api/totp/enable', {'code': totp.code(secret)}, token)[1]['backup_codes']

    def test_cross_origin_page_can_read_retry_after(self):
        with patch.dict(os.environ, {'WEB_ALLOWED_ORIGINS': self.ORIGIN}), \
                LiveServer(AUTH=Auth(self.store), AUDIT=self.audit, RATES=fresh_rates(hpo_search=1)) as srv:
            token = self.login(srv, 'bs.an')['token']
            headers = {'Origin': self.ORIGIN, 'Authorization': 'Bearer ' + token}
            urllib.request.urlopen(urllib.request.Request(srv.base + '/api/hpo_search?q=a', headers=headers), timeout=30).close()
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(srv.base + '/api/hpo_search?q=a', headers=headers), timeout=30)
            e = caught.exception
            self.assertEqual(e.code, 429)
            self.assertIn('Retry-After', [h.strip() for h in e.headers['Access-Control-Expose-Headers'].split(',')])
            self.assertEqual(int(e.headers['Retry-After']), json.loads(e.read())['retry_after'])

    def test_admin_without_2fa_changes_password_then_sets_up_2fa(self):
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit) as srv:
            token = self.login(srv, 'quan.tri')['token']
            code, out = srv.call('/api/change_password', {'old_password': 'mat-khau-quan.tri',
                                                          'new_password': 'gió nam thổi qua sông'}, token)
            self.assertEqual(code, 200, out)
            self.assertTrue(out['user']['totp_required'])                     # still asked to turn 2FA on
            self.assertEqual(srv.call('/api/admin/users', token=token)[0], 403)
            self.assertEqual(srv.call('/api/totp/setup', {}, token)[0], 200)

    def test_admin_users_and_session_fields(self):
        with LiveServer(AUTH=Auth(self.store, admin_2fa=False), AUDIT=self.audit) as srv:
            admin = self.login(srv, 'quan.tri')
            self.assertEqual((admin['session_hours'], admin['idle_minutes']), (12, 60))
            session = srv.call('/api/session', token=admin['token'])[1]
            self.assertEqual((session['session_hours'], session['idle_minutes']), (12, 60))
            self.enable(srv, self.login(srv, 'bs.an')['token'])
            users = {u['username']: u for u in srv.call('/api/admin/users', token=admin['token'])[1]['users']}
            self.assertIs(users['bs.an']['totp_enabled'], True)
            self.assertIs(users['quan.tri']['totp_enabled'], False)
            self.assertTrue(all('locked_until' in u for u in users.values()))

    def test_cancelled_or_unknown_pending_says_so(self):
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit) as srv:
            self.enable(srv, self.login(srv, 'bs.an')['token'])
            pending = self.login(srv, 'bs.an')['pending']
            answers = [srv.call('/api/login/totp', {'pending': pending, 'code': '000000'}) for _ in range(5)]
            self.assertEqual([c for c, _ in answers], [401] * 5)
            self.assertEqual([bool(d.get('pending_invalid')) for _, d in answers], [False] * 4 + [True])
            code, again = srv.call('/api/login/totp', {'pending': pending, 'code': '000000'})
            self.assertEqual((code, again.get('pending_invalid')), (401, True))
            code, unknown = srv.call('/api/login/totp', {'pending': 'khong-co', 'code': '123456'})
            self.assertEqual((code, unknown.get('pending_invalid')), (401, True))

    def test_backup_code_ignores_case_spaces_and_dashes(self):
        import totp
        with LiveServer(AUTH=Auth(self.store), AUDIT=self.audit) as srv:
            secret, backup = self.enable(srv, self.login(srv, 'bs.an')['token'])
            for typed in (' ' + backup[0].upper().replace('-', ' ') + ' ', backup[1].replace('-', ''), backup[2]):
                pending = self.login(srv, 'bs.an')['pending']
                code, out = srv.call('/api/login/totp', {'pending': pending, 'code': typed})
                self.assertEqual(code, 200, (typed, out))
            pending = self.login(srv, 'bs.an')['pending']
            self.assertEqual(srv.call('/api/login/totp', {'pending': pending, 'code': backup[0]})[0], 401)  # used once
            later = totp.code(secret, time.time() + 30)  # the next step: the enabling code's step is already used
            code, out = srv.call('/api/login/totp', {'pending': pending, 'code': later[:3] + ' ' + later[3:]})
            self.assertEqual(code, 200, out)                                   # "123 456" from the app also works


class FailClosedTests(unittest.TestCase):
    """B3/B4: a missing, corrupt or empty users.json never opens the API (WEB_REQUIRE_ACCOUNTS=1)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name, 'users.json')

    def tearDown(self):
        self.dir.cleanup()

    def cases(self):
        yield 'missing', lambda: None
        yield 'corrupt', lambda: self.path.write_text('{"users": {"bs.an": ', encoding='utf-8')
        yield 'empty', lambda: self.path.write_text('{"users": {}}', encoding='utf-8')

    def test_locked_mode_refuses_every_api_call(self):
        for kind, make in self.cases():
            with self.subTest(kind):
                self.path.unlink(missing_ok=True)
                make()
                with redirect_stderr(StringIO()):
                    auth = Auth(UserStore(self.path), token='shared-code', require_accounts=True)
                    self.assertEqual(auth.mode, 'locked')
                    self.assertEqual(auth.check('Bearer shared-code'), (False, None))  # never falls back to the code
                with LiveServer(AUTH=auth, AUDIT=AuditLog(Path(self.dir.name, 'audit.jsonl'))) as srv:
                    for path, payload in (('/api/status', None), ('/api/hpo_search?q=a', None),
                                          ('/api/login', {'username': 'bs.an', 'password': 'mat-khau-1'}),
                                          ('/api/match_diseases', {'hpos': []})):
                        code, data = srv.call(path, payload, token='shared-code')
                        self.assertEqual(code, 503, (kind, path))
                        self.assertIn('Hệ thống tạm khóa', data['error'])
                    with urllib.request.urlopen(srv.base + '/', timeout=30) as r:
                        self.assertEqual(r.status, 200)  # the page still loads and shows the message

    def test_require_off_keeps_the_old_fallback(self):
        for kind, make in self.cases():
            with self.subTest(kind), redirect_stderr(StringIO()):
                self.path.unlink(missing_ok=True)
                make()
                self.assertEqual(Auth(UserStore(self.path), token='abc', require_accounts=False).mode, 'token')
                self.assertEqual(Auth(UserStore(self.path), token='', require_accounts=False).mode, 'none')

    def test_default_is_fail_closed(self):
        with patch.dict(os.environ, {}), redirect_stderr(StringIO()):
            os.environ.pop('WEB_REQUIRE_ACCOUNTS', None)
            self.assertEqual(Auth(UserStore(self.path), token='abc').mode, 'locked')
            os.environ['WEB_REQUIRE_ACCOUNTS'] = '0'
            self.assertEqual(Auth(UserStore(self.path), token='abc').mode, 'token')

    def test_corrupt_file_keeps_last_good_copy_and_reports_once(self):
        problems = []
        store = UserStore(self.path, on_problem=problems.append)
        store.save({'bs.an': {'name': 'BS. An', 'password': hash_password('mat-khau-1')}})
        auth = Auth(store, require_accounts=True)
        token, _ = auth.login('bs.an', 'mat-khau-1')
        with redirect_stderr(StringIO()) as err:
            self.path.write_text('{"users": ', encoding='utf-8')  # half-written by hand
            os.utime(self.path, ns=(time.time_ns(), time.time_ns() + 10 ** 9))
            self.assertEqual(auth.mode, 'accounts')               # no 500 and no opening up
            self.assertTrue(auth.check('Bearer ' + token)[0])     # doctors keep working
            self.assertEqual(auth.mode, 'accounts')
            with self.assertRaisesRegex(ValueError, 'Không đọc được'):  # the broken file is not overwritten blindly
                auth.create_user('bs.binh', 'BS. Bình', 'doctor')
        self.assertEqual(problems, ['corrupt'])                   # reported once per broken version of the file
        self.assertIn('corrupt', err.getvalue())
        store.save({'bs.an': {'name': 'BS. An', 'password': hash_password('mat-khau-1')}})
        self.assertIsNone(store.problem)

    def test_store_problem_is_audited(self):
        import serve_web
        log = AuditLog(Path(self.dir.name, 'audit.jsonl'))
        with patch.object(serve_web, 'AUDIT', log), redirect_stderr(StringIO()):
            store = UserStore(self.path, on_problem=serve_web.AUTH.store.on_problem)
            Auth(store, require_accounts=True).mode
            Auth(store, require_accounts=True).mode
        self.assertEqual([(e['event'], e['kind']) for e in log.tail()], [('auth_store_error', 'missing')])



def raw_fetch(base, path, token, payload=None):
    """(status, headers, body bytes) of one request; for downloads and images, which HttpTests.call cannot read."""
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def fake_hpo_system(runner):
    """A WebSystem stand-in with a small catalog, an alias and the hooks serve_web consults for corrections."""
    names = {'HP:0001520': ('Thai to so với tuổi thai', 'Large for gestational age'),
             'HP:0001518': ('Thai nhỏ so với tuổi thai', 'Small for gestational age'),
             'HP:0001640': ('Tim to', 'Cardiomegaly')}
    return types.SimpleNamespace(
        matcher=types.SimpleNamespace(profiles=[1]), canonical={h: {'id': h, 'vi': v, 'en': e} for h, (v, e) in names.items()},
        aliases={'HP:0009999': 'HP:0001520'}, term=lambda h: {'id': h, 'vi': names[h][0], 'en': names[h][1]},
        apply_corrections=Mock(return_value=1), runner=runner, search_hpo=lambda q: [],
        extract=lambda text: {'mentions': [], 'seconds': 0.0}, match=lambda p: {'ok': True})


class CorrectionsTests(unittest.TestCase):
    """Góp ý sửa mã: doctors submit, admins decide, approved items reach SYSTEM.apply_corrections and the export."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.store = accounts(d, **{'bs.an': 'doctor', 'bs.binh': 'doctor', 'lab.hv': 'lab', 'quan.tri': 'admin'})
        self.audit = AuditLog(Path(d, 'audit.jsonl'))
        self.corrections = CorrectionStore(Path(d, 'corrections.json'))
        self.system = fake_hpo_system(model())

    def tearDown(self):
        self.dir.cleanup()

    def server(self, **extra):
        return LiveServer(AUTH=Auth(self.store, admin_2fa=False), AUDIT=self.audit, CORRECTIONS=self.corrections,
                          SYSTEM=self.system, **extra)

    def login(self, srv, name):
        code, data = srv.call('/api/login', {'username': name, 'password': 'mat-khau-' + name})
        self.assertEqual(code, 200, data)
        return data['token']

    BODY = {'phrase': 'Thai to', 'right_id': 'HP:0001520', 'wrong_ids': ['HP:0001518'], 'context': 'Thai 36 tuần, thai to.',
            'note': 'Model chọn thai nhỏ', 'source': 'text'}

    def test_doctor_submits_and_reads_own_items(self):
        with self.server() as srv:
            an, binh, lab = (self.login(srv, n) for n in ('bs.an', 'bs.binh', 'lab.hv'))
            code, out = srv.call('/api/corrections', self.BODY, an)
            self.assertEqual(code, 200, out)
            item = out['item']
            self.assertEqual((out['ok'], item['status'], item['user'], item['name']), (True, 'pending', 'bs.an', 'bs.an'))
            self.assertEqual((item['right']['id'], item['right']['en']), ('HP:0001520', 'Large for gestational age'))
            self.assertEqual([w['id'] for w in item['wrong']], ['HP:0001518'])
            self.assertRegex(item['id'], r'^[0-9a-f]{12}$')
            self.assertRegex(item['time'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')
            self.assertIsNone(item['decided_by'])
            # the same pending phrase + code again: no second item
            code, again = srv.call('/api/corrections', {**self.BODY, 'phrase': ' thai TO '}, an)
            self.assertEqual((code, again['duplicate'], again['item']['id']), (200, True, item['id']))
            # an alias is stored under its primary id
            code, alias = srv.call('/api/corrections', {'phrase': 'thai lớn', 'right_id': 'HP:0009999'}, an)
            self.assertEqual((code, alias['item']['right']['id'], alias['item']['wrong'], alias['item']['source']),
                             (200, 'HP:0001520', [], 'text'))
            for body, message in (({'phrase': '', 'right_id': 'HP:0001520'}, 'Nhập cụm từ cần sửa.'),
                                  ({'phrase': 'x', 'right_id': 'HP:0000001'}, 'Mã HPO không có trong danh mục.'),
                                  ({'phrase': 'x', 'right_id': 'HP:1'}, 'HP:0000000'),
                                  ({'phrase': 'x' * 201, 'right_id': 'HP:0001520'}, '200'),
                                  ({'phrase': 'x', 'right_id': 'HP:0001520', 'wrong_ids': ['HP:0001518'] * 6}, 'Tối đa 5'),
                                  ({'phrase': 'x', 'right_id': 'HP:0001520', 'source': 'mail'}, 'Nguồn')):
                code, err = srv.call('/api/corrections', body, an)
                self.assertEqual(code, 422, (body, err))
                self.assertIn(message, err['error'])
            self.assertEqual(srv.call('/api/corrections', self.BODY, lab)[0], 403)
            code, mine = srv.call('/api/corrections', token=an)
            self.assertEqual((code, [i['phrase'] for i in mine['items']]), (200, ['thai lớn', 'Thai to']))  # newest first
            self.assertEqual(mine['labels']['pending'], 'Chờ duyệt')
            self.assertEqual(srv.call('/api/corrections?status=approved', token=an)[1]['items'], [])
            self.assertEqual(srv.call('/api/corrections', token=binh)[1]['items'], [])  # own items only
            for path in ('/api/admin/corrections', '/api/admin/corrections/export'):
                raw = raw_get(srv.base, path, an)
                self.assertEqual(len(re.findall(rb'HTTP/1\.[01] \d{3} ', raw)), 1, raw[:300])
                self.assertTrue(raw.startswith(b'HTTP/1.0 403'))
            self.assertEqual(srv.call('/api/admin/corrections/decide', {'id': item['id'], 'decision': 'approved'}, an)[0], 403)
            if os.name == 'posix':
                self.assertEqual(os.stat(self.corrections.path).st_mode & 0o777, 0o600)
        log = json.dumps(self.audit.tail(), ensure_ascii=False)
        self.assertIn('correction_submitted', log)
        self.assertIn(item['id'], log)
        for secret in ('Thai to', 'thai lớn', 'HP:0001520', 'HP:0001518', 'Model chọn'):
            self.assertNotIn(secret, log)  # clinical wording never reaches the audit log
        self.assertEqual(sum(1 for e in self.audit.tail() if e['event'] == 'correction_submitted'), 2)

    def test_admin_decides_applies_and_exports(self):
        with self.server() as srv:
            an, admin = self.login(srv, 'bs.an'), self.login(srv, 'quan.tri')
            item = srv.call('/api/corrections', self.BODY, an)[1]['item']
            other = srv.call('/api/corrections', {'phrase': 'tim to', 'right_id': 'HP:0001640'}, an)[1]['item']
            code, listing = srv.call('/api/admin/corrections', token=admin)
            self.assertEqual((code, listing['counts']), (200, {'pending': 2, 'approved': 0, 'rejected': 0}))
            self.assertEqual([i['id'] for i in listing['items']], [other['id'], item['id']])
            self.assertEqual(srv.call('/api/admin/corrections?status=approved', token=admin)[1]['items'], [])
            self.assertEqual(len(srv.call('/api/admin/corrections?user=bs.binh&status=all', token=admin)[1]['items']), 0)
            self.system.apply_corrections.reset_mock()
            code, decided = srv.call('/api/admin/corrections/decide', {'id': item['id'], 'decision': 'approved', 'note': 'Đúng'}, admin)
            self.assertEqual(code, 200, decided)
            self.assertEqual((decided['item']['status'], decided['item']['decided_by'], decided['item']['decision_note']),
                             ('approved', 'quan.tri', 'Đúng'))
            self.assertIsNotNone(decided['item']['decided_at'])
            self.system.apply_corrections.assert_called_once()
            applied = self.system.apply_corrections.call_args[0][0]
            self.assertEqual([(i['id'], i['status'], i['right']['id']) for i in applied], [(item['id'], 'approved', 'HP:0001520')])
            counts = srv.call('/api/admin/corrections', token=admin)[1]['counts']
            self.assertEqual(counts, {'pending': 1, 'approved': 1, 'rejected': 0})
            self.assertEqual(srv.call('/api/corrections?status=approved', token=an)[1]['items'][0]['id'], item['id'])
            code, err = srv.call('/api/admin/corrections/decide', {'id': 'khongco00000', 'decision': 'approved'}, admin)
            self.assertEqual((code, err['error']), (422, 'Không tìm thấy góp ý này.'))
            self.assertEqual(srv.call('/api/admin/corrections/decide', {'id': item['id'], 'decision': 'maybe'}, admin)[0], 422)
            # export: approved only, as a download
            status, headers, body = raw_fetch(srv.base, '/api/admin/corrections/export', admin)
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
            self.assertRegex(headers['Content-Disposition'], r'^attachment; filename="fe2gen_gop_y_\d{8}\.json"$')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            export = json.loads(body)
            self.assertEqual(export['count'], 1)
            row = export['items'][0]
            self.assertEqual((row['mention_text'], row['hpo_id'], row['wrong_hpo_ids'], row['user'], row['approved_by']),
                             ('Thai to', 'HP:0001520', ['HP:0001518'], 'bs.an', 'quan.tri'))
            self.assertEqual(export['memory_rows'], [{'query_text': 'thai to', 'hpo_id': 'HP:0001520'}])
            self.assertEqual(export['doctor_phrases'], [{'id': 'HP:0001520', 'vi': 'Thai to', 'en': 'Large for gestational age'}])
            # the admin may change their mind: the live map is rebuilt without the item
            self.system.apply_corrections.reset_mock()
            code, flipped = srv.call('/api/admin/corrections/decide', {'id': item['id'], 'decision': 'rejected'}, admin)
            self.assertEqual((code, flipped['item']['status']), (200, 'rejected'))
            self.assertEqual(self.system.apply_corrections.call_args[0][0], [])
            self.assertEqual(json.loads(raw_fetch(srv.base, '/api/admin/corrections/export', admin)[2])['count'], 0)
        events = [(e['event'], e.get('target'), e.get('count')) for e in self.audit.tail()]
        self.assertIn(('correction_approved', 'bs.an', None), events)
        self.assertIn(('correction_rejected', 'bs.an', None), events)
        self.assertIn(('corrections_exported', None, 1), events)
        self.assertNotIn('Thai to', json.dumps(self.audit.tail(), ensure_ascii=False))

    def test_store_rereads_file_and_orders_approved_by_decision(self):
        a = CorrectionStore(Path(self.dir.name, 'c.json'))
        fields = lambda phrase, hid: {'phrase': phrase, 'right': {'id': hid, 'vi': '', 'en': ''}, 'wrong': [], 'context': '',
                                      'note': '', 'source': 'text'}
        one, _ = a.add('bs.an', 'BS. An', fields('thai to', 'HP:0001520'))
        two, _ = a.add('bs.an', 'BS. An', fields('thai to', 'HP:0001640'))
        three, _ = a.add('bs.an', 'BS. An', fields('tim to', 'HP:0001640'))
        a.decide(three['id'], 'approved', '', 'quan.tri')
        del a.items[-1]['decided_ts']  # an item decided before decided_ts existed sorts last
        a._save()
        time.sleep(0.05)
        a.decide(two['id'], 'approved', '', 'quan.tri')
        time.sleep(0.05)
        a.decide(one['id'], 'approved', '', 'quan.tri')
        b = CorrectionStore(a.path)  # another reader (the CLI) sees the same file
        approved = b.approved_items()
        # all three decided in the same minute (decided_at ties): the most recent decision comes first
        self.assertEqual([i['id'] for i in approved], [one['id'], two['id'], three['id']])
        self.assertGreater(approved[0]['decided_ts'], approved[1]['decided_ts'])
        self.assertNotIn('decided_ts', approved[2])
        self.assertRegex(approved[0]['decided_at'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')  # kept for display
        self.assertEqual({i['status'] for i in b.listing('all')[0]}, {'approved'})
        with self.assertRaisesRegex(ValueError, 'Không tìm thấy'):
            b.decide('x', 'rejected', '', 'quan.tri')

    def test_corrupt_store_answers_500_not_422(self):
        # A hand-edited or truncated corrections.json is an operator problem: never the parser's message as a 422.
        self.corrections.path.write_text('{"items": [', encoding='utf-8')
        with self.server() as srv:
            an, admin = self.login(srv, 'bs.an'), self.login(srv, 'quan.tri')
            code, err = srv.call('/api/corrections', self.BODY, an)
            self.assertEqual((code, err['error']), (500, 'Chưa lưu được góp ý. Liên hệ quản trị viên.'))
            code, err = srv.call('/api/corrections', token=an)
            self.assertEqual((code, err['error']), (500, 'Không đọc được danh sách góp ý. Liên hệ quản trị viên.'))
            code, err = srv.call('/api/admin/corrections', token=admin)
            self.assertEqual((code, err['error']), (500, 'Không đọc được dữ liệu đã lưu. Liên hệ quản trị viên.'))
            code, err = srv.call('/api/admin/corrections/decide', {'id': 'a' * 12, 'decision': 'approved'}, admin)
            self.assertEqual((code, err['error']), (500, 'Chưa lưu được thay đổi. Liên hệ quản trị viên.'))
            # the doctor's own mistakes still answer 422 (validation runs before the store is touched)
            code, err = srv.call('/api/corrections', {**self.BODY, 'phrase': ''}, an)
            self.assertEqual((code, err['error']), (422, 'Nhập cụm từ cần sửa.'))
            time.sleep(0.02)
            self.corrections.path.write_text('{"items": "x"}', encoding='utf-8')  # parses, wrong shape
            self.assertEqual(srv.call('/api/corrections', self.BODY, an)[0], 500)
            self.corrections.path.unlink()
            self.assertEqual(srv.call('/api/corrections', self.BODY, an)[0], 200)  # a missing file is an empty store
        self.corrections.path.write_text('nope', encoding='utf-8')
        with self.assertRaises(OSError):  # StoreError is an OSError, so every existing 500 branch catches it
            CorrectionStore(self.corrections.path).all()


PNG_1X1 = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
           b'\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


class BugReportTests(unittest.TestCase):
    """Báo lỗi: every role can send text + up to 3 screenshots; they stay on disk (600) for the admin page."""

    def setUp(self):
        import base64
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.store = accounts(d, **{'bs.an': 'doctor', 'lab.hv': 'lab', 'quan.tri': 'admin'})
        self.audit = AuditLog(Path(d, 'audit.jsonl'))
        self.reports = BugReportStore(Path(d, 'bug_reports'))
        self.png = base64.b64encode(PNG_1X1).decode()

    def tearDown(self):
        self.dir.cleanup()

    def server(self, **extra):
        return LiveServer(AUTH=Auth(self.store, admin_2fa=False), AUDIT=self.audit, BUG_REPORTS=self.reports, **extra)

    def login(self, srv, name):
        code, data = srv.call('/api/login', {'username': name, 'password': 'mat-khau-' + name})
        self.assertEqual(code, 200, data)
        return data['token']

    def report(self, text='Nút Tách dấu hiệu không bấm được. BÍ-MẬT-789', images=None, **extra):
        return {'text': text, 'page': '/#tach', 'client_info': {'model_version': 'v7.1', 'viewport': '1280x720'},
                'images': [{'name': 'man-hinh.png', 'type': 'image/png', 'data': self.png}] if images is None else images, **extra}

    def test_report_stored_listed_served_closed_and_deleted(self):
        with self.server() as srv:
            lab, admin = self.login(srv, 'lab.hv'), self.login(srv, 'quan.tri')
            code, out = srv.call('/api/bug_reports', self.report(), lab)
            self.assertEqual(code, 200, out)
            rid = out['id']
            self.assertRegex(rid, r'^[0-9a-f]{12}$')
            image_file = Path(self.dir.name, 'bug_reports', rid + '_0.png')
            self.assertEqual(image_file.read_bytes(), PNG_1X1)
            index = Path(self.dir.name, 'bug_reports', 'index.json')
            self.assertNotIn(self.png[:40], index.read_text(encoding='utf-8'))  # metadata only, never the image data
            if os.name == 'posix':
                self.assertEqual(os.stat(image_file).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(index).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(index.parent).st_mode & 0o777, 0o700)
            self.assertEqual(srv.call('/api/admin/bug_reports', token=lab)[0], 403)
            code, listing = srv.call('/api/admin/bug_reports', token=admin)
            self.assertEqual((code, listing['counts']), (200, {'new': 1, 'closed': 0}))
            item = listing['items'][0]
            self.assertEqual((item['id'], item['user'], item['role'], item['status'], item['page']), (rid, 'lab.hv', 'lab', 'new', '/#tach'))
            self.assertIn('BÍ-MẬT-789', item['text'])
            self.assertEqual(item['client_info']['model_version'], 'v7.1')
            self.assertEqual(item['images'], [{'n': 0, 'name': 'man-hinh.png', 'type': 'image/png', 'bytes': len(PNG_1X1)}])
            self.assertNotIn('data', json.dumps(item['images']))
            status, headers, body = raw_fetch(srv.base, f'/api/admin/bug_reports/image?id={rid}&n=0', admin)
            self.assertEqual((status, body), (200, PNG_1X1))
            self.assertEqual(headers['Content-Type'], 'image/png')
            self.assertEqual(headers['Content-Length'], str(len(PNG_1X1)))
            self.assertEqual(headers['Content-Disposition'], f'attachment; filename="{rid}_0.png"')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            for query in (f'id={rid}&n=1', 'id=000000000000&n=0', f'id={rid}&n=x', f'id=..%2F{rid}&n=0'):
                status, _, body = raw_fetch(srv.base, '/api/admin/bug_reports/image?' + query, admin)
                self.assertEqual((status, json.loads(body)['error']), (404, 'Không tìm thấy ảnh này.'), query)
            self.assertEqual(srv.call('/api/admin/bug_reports/status', {'id': rid, 'status': 'closed'}, admin)[1], {'ok': True})
            code, closed = srv.call('/api/admin/bug_reports', token=admin)
            self.assertEqual((closed['counts'], closed['items']), ({'new': 0, 'closed': 1}, []))  # default view: new ones
            code, all_items = srv.call('/api/admin/bug_reports?status=all', token=admin)
            self.assertEqual((all_items['items'][0]['status'], all_items['items'][0]['closed_by']), ('closed', 'quan.tri'))
            self.assertEqual(srv.call('/api/admin/bug_reports/status', {'id': rid, 'status': 'new'}, admin)[0], 200)
            self.assertIsNone(srv.call('/api/admin/bug_reports?status=all', token=admin)[1]['items'][0]['closed_at'])
            code, err = srv.call('/api/admin/bug_reports/status', {'id': 'khongco00000', 'status': 'closed'}, admin)
            self.assertEqual((code, err['error']), (422, 'Không tìm thấy báo lỗi này.'))
            self.assertEqual(srv.call('/api/admin/bug_reports/status', {'id': rid, 'status': 'gone'}, admin)[0], 422)
            self.assertEqual(srv.call('/api/admin/bug_reports/delete', {'id': rid}, admin)[1], {'ok': True})
            self.assertFalse(image_file.exists())
            self.assertEqual(srv.call('/api/admin/bug_reports?status=all', token=admin)[1]['counts'], {'new': 0, 'closed': 0})
            self.assertEqual(srv.call('/api/admin/bug_reports/delete', {'id': rid}, admin)[0], 422)
            # a doctor and an admin may report too, without images
            for who in (self.login(srv, 'bs.an'), admin):
                self.assertEqual(srv.call('/api/bug_reports', self.report(images=[]), who)[0], 200)
        log = json.dumps(self.audit.tail(), ensure_ascii=False)
        self.assertNotIn('BÍ-MẬT', log)
        self.assertNotIn('Tách dấu hiệu', log)
        events = [(e['event'], e.get('report_id'), e.get('images'), e.get('status')) for e in self.audit.tail()]
        self.assertIn(('bug_report_created', rid, 1, None), events)
        self.assertIn(('bug_report_updated', rid, None, 'closed'), events)
        self.assertIn(('bug_report_deleted', rid, None, None), events)

    def test_rejects_bad_images_sizes_and_floods(self):
        import base64
        jpeg_as_png = base64.b64encode(b'\xff\xd8\xff\xe0' + b'\x00' * 20).decode()
        bad_images = ([{'name': 'a.png', 'type': 'image/png', 'data': jpeg_as_png}],   # declared PNG, JPEG bytes
                      [{'name': 'a.png', 'type': 'image/png', 'data': 'kh&ng-base64!'}],
                      [{'name': 'a.gif', 'type': 'image/gif', 'data': self.png}])
        with self.server(RATES=fresh_rates(bug_report=3)) as srv:
            doctor = self.login(srv, 'bs.an')
            for images in bad_images:
                code, err = srv.call('/api/bug_reports', self.report(images=images), doctor)
                self.assertEqual((code, err['error']), (422, 'Ảnh không hợp lệ hoặc không phải PNG/JPEG/WEBP.'), images[0]['type'])
            code, err = srv.call('/api/bug_reports', self.report(images=[]), doctor)  # the fourth this minute
            self.assertEqual(code, 429, err)
        with self.server(UPLOAD_BYTES=600) as srv:
            doctor = self.login(srv, 'bs.an')
            self.assertEqual(srv.call('/api/bug_reports', self.report(text='x', images=[]), doctor)[0], 200)
            code, err = srv.call('/api/bug_reports', self.report(text='x' * 700, images=[]), doctor)  # over 600 bytes
            self.assertEqual((code, err['error']), (413, 'Yêu cầu quá lớn hoặc rỗng.'))
            code, err = srv.call('/api/bug_reports', self.report(text='   ', images=[]), doctor)
            self.assertEqual((code, err['error']), (422, 'Mô tả lỗi không được để trống.'))
        with self.server() as srv:  # the default 8 MB limit takes 3 images; other routes keep the 100 kB cap
            doctor = self.login(srv, 'bs.an')
            big = base64.b64encode(PNG_1X1 + b'\x00' * 150000).decode()
            one = {'name': 'a.png', 'type': 'image/png', 'data': big}
            self.assertEqual(srv.call('/api/bug_reports', self.report(images=[one] * 3), doctor)[0], 200)
            code, err = srv.call('/api/bug_reports', self.report(images=[one] * 4), doctor)
            self.assertEqual((code, err['error']), (422, 'Tối đa 3 ảnh mỗi báo lỗi.'))
            code, err = srv.call('/api/corrections', {'phrase': 'x', 'right_id': 'HP:0000001', 'note': 'y' * 150000}, doctor)
            self.assertEqual(code, 413)
            self.assertEqual(len(list(Path(self.dir.name, 'bug_reports').glob('*.png'))), 3)
            self.assertEqual(len(srv.call('/api/admin/bug_reports', token=self.login(srv, 'quan.tri'))[1]['items'][0]['images']), 3)
        with self.server(RATES=fresh_rates(bug_report=1), UPLOAD_BYTES=600) as srv:  # the limit is checked before the body
            doctor = self.login(srv, 'bs.an')
            self.assertEqual(srv.call('/api/bug_reports', self.report(text='x', images=[]), doctor)[0], 200)
            code, err = srv.call('/api/bug_reports', self.report(text='x' * 700, images=[]), doctor)
            self.assertEqual((code, 'retry_after' in err), (429, True), err)

    def test_caps_per_user_and_total_size(self):
        self.reports.per_user = 1
        with self.server() as srv:
            doctor, admin = self.login(srv, 'bs.an'), self.login(srv, 'quan.tri')
            code, first = srv.call('/api/bug_reports', self.report(images=[]), doctor)
            self.assertEqual(code, 200, first)
            code, err = srv.call('/api/bug_reports', self.report(images=[]), doctor)
            self.assertEqual((code, err['error']), (422, 'Bác sĩ đã có 1 báo lỗi chưa xử lý; chờ quản trị xử lý trước khi gửi thêm.'))
            self.assertEqual(srv.call('/api/bug_reports', self.report(images=[]), self.login(srv, 'lab.hv'))[0], 200)  # per user
            self.assertEqual(srv.call('/api/admin/bug_reports/status', {'id': first['id'], 'status': 'closed'}, admin)[0], 200)
            self.assertEqual(srv.call('/api/bug_reports', self.report(images=[]), doctor)[0], 200)  # closed ones do not count
            # total size: image bytes already in index.json + the new images
            self.reports.per_user = 0
            self.reports.max_bytes = 2 * len(PNG_1X1) - 1
            code, kept = srv.call('/api/bug_reports', self.report(), doctor)
            self.assertEqual(code, 200, kept)
            code, err = srv.call('/api/bug_reports', self.report(), doctor)
            self.assertEqual((code, err['error']), (422, 'Kho báo lỗi đã đầy; nhờ quản trị dọn bớt báo lỗi cũ.'))
            self.assertEqual(len(list(Path(self.dir.name, 'bug_reports').glob('*.png'))), 1)  # nothing written for the refused one
            self.assertEqual(srv.call('/api/bug_reports', self.report(images=[]), doctor)[0], 200)  # text only still fits
            self.assertEqual(srv.call('/api/admin/bug_reports/delete', {'id': kept['id']}, admin)[0], 200)
            self.assertEqual(srv.call('/api/bug_reports', self.report(), doctor)[0], 200)  # room again after a delete
            counts = srv.call('/api/admin/bug_reports?status=all', token=admin)[1]['counts']
            self.assertEqual(counts, {'new': 4, 'closed': 1})
        with patch.dict(os.environ, {'WEB_BUG_REPORTS_PER_USER': '3', 'WEB_BUG_REPORTS_MAX_MB': '2'}):
            store = BugReportStore(Path(self.dir.name, 'x'))
        self.assertEqual((store.per_user, store.max_bytes), (3, 2000000))
        with patch.dict(os.environ, {'WEB_BUG_REPORTS_PER_USER': '', 'WEB_BUG_REPORTS_MAX_MB': 'abc'}):
            store = BugReportStore(Path(self.dir.name, 'x'))
        self.assertEqual((store.per_user, store.max_bytes), (20, 500000000))  # defaults
        import bug_reports
        for _ in range(3):
            self.reports.add('bs.an', 'BS. An', 'doctor', 'x', '/', {}, [])
        with patch.object(bug_reports, 'MAX_ITEMS', 2):
            items, counts = self.reports.listing('all')
        self.assertEqual((len(items), counts), (2, {'new': 7, 'closed': 1}))  # the newest 2, counts complete
        self.assertEqual(items[0]['user'], 'bs.an')

    def test_failed_save_leaves_no_files_and_answers_500(self):
        import bug_reports
        folder = Path(self.dir.name, 'bug_reports')
        with self.server() as srv:
            doctor, admin = self.login(srv, 'bs.an'), self.login(srv, 'quan.tri')
            with patch.object(bug_reports, 'write_private_json', side_effect=OSError('disk full')):
                code, err = srv.call('/api/bug_reports', self.report(), doctor)
            self.assertEqual((code, err['error']), (500, 'Chưa lưu được báo lỗi. Liên hệ quản trị viên.'))
            self.assertEqual([p.name for p in folder.iterdir()], [])  # no orphan screenshot, no index.json
            self.assertEqual(srv.call('/api/admin/bug_reports', token=admin)[1]['counts'], {'new': 0, 'closed': 0})
            code, out = srv.call('/api/bug_reports', self.report(), doctor)  # the store works again afterwards
            self.assertEqual(code, 200, out)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted(['index.json', out['id'] + '_0.png']))
        self.assertEqual(sum(1 for e in self.audit.tail() if e['event'] == 'bug_report_created'), 1)  # the failed one is not logged

    def test_corrupt_index_answers_500_not_422(self):
        folder = Path(self.dir.name, 'bug_reports')
        folder.mkdir()
        index = folder / 'index.json'
        index.write_text('{"items": [', encoding='utf-8')
        with self.server() as srv:
            doctor, admin = self.login(srv, 'bs.an'), self.login(srv, 'quan.tri')
            code, err = srv.call('/api/bug_reports', self.report(), doctor)
            self.assertEqual((code, err['error']), (500, 'Chưa lưu được báo lỗi. Liên hệ quản trị viên.'))
            self.assertEqual([p.name for p in folder.iterdir()], ['index.json'])  # no image written either
            code, err = srv.call('/api/admin/bug_reports', token=admin)
            self.assertEqual((code, err['error']), (500, 'Không đọc được dữ liệu đã lưu. Liên hệ quản trị viên.'))
            code, err = srv.call('/api/admin/bug_reports/status', {'id': 'a' * 12, 'status': 'closed'}, admin)
            self.assertEqual((code, err['error']), (500, 'Chưa lưu được thay đổi. Liên hệ quản trị viên.'))
            code, err = srv.call('/api/admin/bug_reports/delete', {'id': 'a' * 12}, admin)
            self.assertEqual(code, 500)
            code, err = srv.call('/api/bug_reports', self.report(text='  ', images=[]), doctor)  # validation still 422
            self.assertEqual((code, err['error']), (422, 'Mô tả lỗi không được để trống.'))
            time.sleep(0.02)
            index.write_text('{"items": {}}', encoding='utf-8')  # parses, wrong shape
            self.assertEqual(srv.call('/api/bug_reports', self.report(images=[]), doctor)[0], 500)
            index.unlink()
            self.assertEqual(srv.call('/api/bug_reports', self.report(images=[]), doctor)[0], 200)

    def test_delete_skips_image_entries_without_n(self):
        from model1_process import write_private_json
        rid = self.reports.add('bs.an', 'BS. An', 'doctor', 'x', '/', {}, [('a.png', 'image/png', PNG_1X1)])
        folder = Path(self.dir.name, 'bug_reports')
        data = json.loads((folder / 'index.json').read_text(encoding='utf-8'))
        data['items'][0]['images'] += [{'name': 'b.png', 'type': 'image/png', 'bytes': 1},   # hand-edited: no n
                                       {'n': 'x', 'type': 'image/png'}, {'n': True, 'type': 'image/png'}]
        time.sleep(0.02)
        write_private_json(folder / 'index.json', data)
        self.assertEqual(self.reports.listing('all')[0][0]['images'][1], {'name': 'b.png', 'type': 'image/png', 'bytes': 1})
        self.reports.delete(rid)  # no KeyError; the one real file goes
        self.assertEqual([p.name for p in folder.iterdir()], ['index.json'])
        self.assertEqual(self.reports.listing('all'), ([], {'new': 0, 'closed': 0}))


def fake_disease_system(runner):
    """fake_hpo_system plus the disease catalog hooks "Bệnh đúng của ca" consults: profiles_by_id (two profiles with
    .disease_name), search_diseases and categories."""
    system = fake_hpo_system(runner)
    profiles = {'OMIM:190685': types.SimpleNamespace(disease_id='OMIM:190685', disease_name='Down syndrome'),
                'ORPHA:3380': types.SimpleNamespace(disease_id='ORPHA:3380', disease_name='Trisomy 18')}
    system.profiles_by_id = profiles
    system.categories = {}
    system.search_diseases = lambda q: [{'disease_id': d, 'disease_name': p.disease_name, 'group': None}
                                        for d, p in profiles.items() if q.strip() and q.lower() in p.disease_name.lower()]
    return system


class DiseaseFeedbackTests(unittest.TestCase):
    """Bệnh đúng của ca: every role records the correct disease next to the AI's wrong suggestions; the case
    snapshot stays on the server (600) as Model 2 training data; admins review, delete and export."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.store = accounts(d, **{'bs.an': 'doctor', 'bs.binh': 'doctor', 'lab.hv': 'lab', 'quan.tri': 'admin'})
        self.audit = AuditLog(Path(d, 'audit.jsonl'))
        self.feedback = DiseaseFeedbackStore(Path(d, 'disease_feedback.json'))
        self.system = fake_disease_system(model())

    def tearDown(self):
        self.dir.cleanup()

    def server(self, **extra):
        return LiveServer(AUTH=Auth(self.store, admin_2fa=False), AUDIT=self.audit, DISEASE_FEEDBACK=self.feedback,
                          SYSTEM=self.system, **extra)

    def login(self, srv, name):
        code, data = srv.call('/api/login', {'username': name, 'password': 'mat-khau-' + name})
        self.assertEqual(code, 200, data)
        return data['token']

    SUGGESTIONS = [{'disease_id': 'ORPHA:3380', 'disease_name': 'Trisomy 18', 'rank': 1, 'overall_rank': 1, 'group': 'NST',
                    'match_percentage': 41.5},
                   {'disease_id': 'OMIM:190685', 'disease_name': 'Down syndrome', 'rank': 2, 'overall_rank': 3, 'group': 'NST',
                    'match_percentage': 30.0}]
    BODY = {'case_code': 'HV-2026-0123', 'hpos': [{'id': 'HP:0001674', 'vi': 'Kênh nhĩ thất', 'en': 'Complete AVSD', 'status': 'CÓ'},
                                                  {'id': 'HP:0001511', 'vi': 'Thai chậm tăng trưởng', 'en': 'IUGR', 'status': 'NGHI NGỜ'}],
            'genes': ['dscam', 'DSCAM'], 'suggestions': SUGGESTIONS, 'wrong_ids': ['ORPHA:3380'],
            'correct': {'disease_id': 'omim:190685', 'disease_name': 'tên bác sĩ gõ'}, 'note': 'Karyotype 47,XX,+21 BÍ-MẬT-456'}

    def test_submit_list_review_delete_and_export(self):
        with self.server() as srv:
            an, lab, admin = self.login(srv, 'bs.an'), self.login(srv, 'lab.hv'), self.login(srv, 'quan.tri')
            code, out = srv.call('/api/disease_feedback', self.BODY, an)
            self.assertEqual(code, 200, out)
            item = out['item']
            self.assertRegex(item['id'], r'^[0-9a-f]{12}$')
            self.assertEqual((item['user'], item['status'], item['reviewed_by'], item['case_code']), ('bs.an', 'new', None, 'HV-2026-0123'))
            self.assertEqual(item['correct'], {'disease_id': 'OMIM:190685', 'disease_name': 'Down syndrome'})  # catalog name wins
            self.assertEqual(item['genes'], ['DSCAM'])
            self.assertEqual(item['wrong_ids'], ['ORPHA:3380'])
            self.assertEqual([h['id'] for h in item['hpos']], ['HP:0001674', 'HP:0001511'])
            self.assertEqual(item['suggestions'][0]['match_percentage'], 41.5)
            if os.name == 'posix':
                self.assertEqual(os.stat(self.feedback.path).st_mode & 0o777, 0o600)
            # the Lab and the admin may record too; an ORPHANET: id maps to the ORPHA: catalog id
            for who in (lab, admin):
                code, more = srv.call('/api/disease_feedback', {**self.BODY, 'correct': {'disease_id': 'Orphanet:3380', 'disease_name': 'x'},
                                                                'wrong_ids': ['OMIM:190685']}, who)
                self.assertEqual(code, 200, more)
                self.assertEqual(more['item']['correct'], {'disease_id': 'ORPHA:3380', 'disease_name': 'Trisomy 18'})
            # free text: a disease outside the catalog keeps the doctor's wording with disease_id null
            code, free = srv.call('/api/disease_feedback', {**self.BODY, 'correct': {'disease_id': None, 'disease_name': ' Hội chứng lạ '}}, an)
            self.assertEqual(code, 200, free)
            self.assertEqual(free['item']['correct'], {'disease_id': None, 'disease_name': 'Hội chứng lạ'})
            # validation
            for body, message in (({**self.BODY, 'hpos': []}, 'Chọn ít nhất một HPO của ca.'),
                                  ({**self.BODY, 'correct': {'disease_id': 'OMIM:999999', 'disease_name': 'x'}}, 'Mã bệnh không có trong danh mục.'),
                                  ({**self.BODY, 'correct': {'disease_id': None, 'disease_name': ''}}, 'Ghi tên bệnh đúng.'),
                                  ({**self.BODY, 'correct': {'disease_id': None, 'disease_name': 'x'}, 'wrong_ids': ['OMIM:1']},
                                   'Bệnh báo sai phải nằm trong danh sách AI đã gợi ý.'),
                                  ({**self.BODY, 'correct': {'disease_id': 'ORPHA:3380', 'disease_name': 'x'}}, 'Bệnh đúng không thể là bệnh đã báo sai.'),
                                  ({**self.BODY, 'hpos': [{'id': 'HP:0001674', 'status': 'MAYBE'}]}, 'Dấu hiệu HPO không hợp lệ.'),
                                  ({**self.BODY, 'genes': ['bad gene!']}, 'Ký hiệu gene không hợp lệ: bad gene!'),
                                  ({**self.BODY, 'suggestions': [{'disease_id': 'ORPHA:3380', 'rank': 'first'}]}, 'Danh sách gợi ý của AI không hợp lệ.')):
                code, err = srv.call('/api/disease_feedback', body, an)
                self.assertEqual((code, err['error']), (422, message), body)
            self.assertEqual(srv.call('/api/disease_feedback', {**self.BODY, 'note': 'n' * 501}, an)[0], 422)
            # own list: newest first, own items only; status filter
            code, mine = srv.call('/api/disease_feedback', token=an)
            self.assertEqual((code, len(mine['items']), mine['labels']), (200, 2, {'new': 'Chờ xem', 'reviewed': 'Đã xem'}))
            self.assertEqual(mine['items'][0]['id'], free['item']['id'])
            self.assertEqual(srv.call('/api/disease_feedback?status=reviewed', token=an)[1]['items'], [])
            self.assertEqual(srv.call('/api/disease_feedback', token=self.login(srv, 'bs.binh'))[1]['items'], [])
            # admin: counts, filters, review, reopen, delete
            for path in ('/api/admin/disease_feedback', '/api/admin/disease_feedback/export'):
                self.assertEqual(srv.call(path, token=an)[0], 403)
            self.assertEqual(srv.call('/api/admin/disease_feedback/status', {'id': item['id'], 'status': 'reviewed'}, an)[0], 403)
            code, listing = srv.call('/api/admin/disease_feedback', token=admin)
            self.assertEqual((code, listing['counts'], len(listing['items'])), (200, {'new': 4, 'reviewed': 0}, 4))
            self.assertEqual(len(srv.call('/api/admin/disease_feedback?user=bs.an&status=all', token=admin)[1]['items']), 2)
            self.assertEqual(srv.call('/api/admin/disease_feedback/status', {'id': item['id'], 'status': 'reviewed'}, admin)[1], {'ok': True})
            code, after = srv.call('/api/admin/disease_feedback', token=admin)
            self.assertEqual((after['counts'], len(after['items'])), ({'new': 3, 'reviewed': 1}, 3))
            reviewed = srv.call('/api/admin/disease_feedback?status=reviewed', token=admin)[1]['items']
            self.assertEqual((reviewed[0]['id'], reviewed[0]['reviewed_by']), (item['id'], 'quan.tri'))
            self.assertIsNotNone(reviewed[0]['reviewed_at'])
            self.assertEqual(srv.call('/api/disease_feedback?status=reviewed', token=an)[1]['items'][0]['id'], item['id'])
            self.assertEqual(srv.call('/api/admin/disease_feedback/status', {'id': item['id'], 'status': 'new'}, admin)[0], 200)
            self.assertIsNone(srv.call('/api/admin/disease_feedback?status=all', token=admin)[1]['items'][-1]['reviewed_at'])
            code, err = srv.call('/api/admin/disease_feedback/status', {'id': 'khongco00000', 'status': 'reviewed'}, admin)
            self.assertEqual((code, err['error']), (422, 'Không tìm thấy ghi nhận này.'))
            self.assertEqual(srv.call('/api/admin/disease_feedback/status', {'id': item['id'], 'status': 'gone'}, admin)[0], 422)
            # export: every item regardless of status, as training cases
            self.assertEqual(srv.call('/api/admin/disease_feedback/status', {'id': item['id'], 'status': 'reviewed'}, admin)[0], 200)
            status, headers, body = raw_fetch(srv.base, '/api/admin/disease_feedback/export', admin)
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
            self.assertRegex(headers['Content-Disposition'], r'^attachment; filename="fe2gen_benh_dung_\d{8}\.json"$')
            data = json.loads(body)
            self.assertEqual((data['count'], len(data['cases'])), (4, 4))
            case = next(c for c in data['cases'] if c['id'] == item['id'])
            self.assertEqual(case['hpos'], [{'id': 'HP:0001674', 'status': 'CÓ'}, {'id': 'HP:0001511', 'status': 'NGHI NGỜ'}])
            self.assertEqual((case['correct_disease_id'], case['correct_disease_name'], case['wrong_disease_ids'], case['genes'], case['case_code']),
                             ('OMIM:190685', 'Down syndrome', ['ORPHA:3380'], ['DSCAM'], 'HV-2026-0123'))
            self.assertEqual(case['ai_suggestions'][0], {'disease_id': 'ORPHA:3380', 'rank': 1, 'overall_rank': 1, 'group': 'NST', 'match_percentage': 41.5})
            self.assertIn('BÍ-MẬT-456', case['note'])
            self.assertEqual(srv.call('/api/admin/disease_feedback/delete', {'id': item['id']}, admin)[1], {'ok': True})
            self.assertEqual(srv.call('/api/admin/disease_feedback?status=all', token=admin)[1]['counts'], {'new': 3, 'reviewed': 0})
            self.assertEqual(srv.call('/api/admin/disease_feedback/delete', {'id': item['id']}, admin)[0], 422)
            # the catalog search is open to every signed-in role
            self.assertEqual(srv.call('/api/disease_search?q=down', token=lab)[1], [{'disease_id': 'OMIM:190685', 'disease_name': 'Down syndrome', 'group': None}])
            self.assertEqual(srv.call('/api/disease_search?q=', token=lab)[1], [])
        # the audit log: ids, status and counts only, never the case content
        log = json.dumps(self.audit.tail(), ensure_ascii=False)
        for secret in ('BÍ-MẬT', 'HV-2026', 'HP:0001674', 'DSCAM', 'Down', 'ORPHA:3380', 'Hội chứng lạ'):
            self.assertNotIn(secret, log)
        submitted = [e for e in self.audit.tail() if e['event'] == 'disease_feedback_submitted']
        self.assertEqual(len(submitted), 4)
        self.assertEqual(set(submitted[0]) - {'time', 'event', 'user', 'prev'}, {'feedback_id'})
        self.assertIn(item['id'], [e['feedback_id'] for e in submitted])
        events = [(e['event'], e.get('feedback_id'), e.get('status'), e.get('count')) for e in self.audit.tail()]
        self.assertIn(('disease_feedback_updated', item['id'], 'reviewed', None), events)
        self.assertIn(('disease_feedback_updated', item['id'], 'new', None), events)
        self.assertIn(('disease_feedback_deleted', item['id'], None, None), events)
        self.assertIn(('disease_feedback_exported', None, None, 4), events)

    def test_doctor_on_admin_routes_gets_one_403(self):
        with self.server() as srv:
            an = self.login(srv, 'bs.an')
            for path in ('/api/admin/disease_feedback', '/api/admin/disease_feedback/export'):
                raw = raw_get(srv.base, path, an)
                self.assertEqual(raw.count(b'HTTP/1.0 '), 1, raw)
                self.assertTrue(raw.startswith(b'HTTP/1.0 403'), raw[:40])
                self.assertEqual(raw.count(b'"error"'), 1)

    def test_corrupt_store_answers_500_not_422(self):
        self.feedback.path.write_text('{"items": [', encoding='utf-8')
        with self.server() as srv:
            an, admin = self.login(srv, 'bs.an'), self.login(srv, 'quan.tri')
            code, err = srv.call('/api/disease_feedback', self.BODY, an)
            self.assertEqual((code, err['error']), (500, 'Chưa lưu được ghi nhận. Liên hệ quản trị viên.'))
            code, err = srv.call('/api/disease_feedback', token=an)
            self.assertEqual((code, err['error']), (500, 'Không đọc được danh sách ghi nhận. Liên hệ quản trị viên.'))
            code, err = srv.call('/api/admin/disease_feedback', token=admin)
            self.assertEqual((code, err['error']), (500, 'Không đọc được dữ liệu đã lưu. Liên hệ quản trị viên.'))
            self.assertEqual(srv.call('/api/admin/disease_feedback/export', token=admin)[0], 500)
            code, err = srv.call('/api/admin/disease_feedback/status', {'id': 'a' * 12, 'status': 'reviewed'}, admin)
            self.assertEqual((code, err['error']), (500, 'Chưa lưu được thay đổi. Liên hệ quản trị viên.'))
            self.assertEqual(srv.call('/api/admin/disease_feedback/delete', {'id': 'a' * 12}, admin)[0], 500)
            code, err = srv.call('/api/disease_feedback', {**self.BODY, 'hpos': []}, an)  # validation still 422
            self.assertEqual((code, err['error']), (422, 'Chọn ít nhất một HPO của ca.'))
            time.sleep(0.02)
            self.feedback.path.write_text('{"items": "x"}', encoding='utf-8')  # parses, wrong shape
            self.assertEqual(srv.call('/api/disease_feedback', self.BODY, an)[0], 500)
            self.feedback.path.unlink()
            self.assertEqual(srv.call('/api/disease_feedback', self.BODY, an)[0], 200)  # a missing file is an empty store
        self.assertEqual(sum(1 for e in self.audit.tail() if e['event'] == 'disease_feedback_submitted'), 1)

    def test_rate_groups_and_store_limits(self):
        import disease_feedback
        import serve_web
        for group in ('disease_search', 'disease_feedback'):
            self.assertIn(group, serve_web.RATES)
            self.assertIn(group, fresh_rates())
        with self.server(RATES=fresh_rates(disease_feedback=2)) as srv:
            an = self.login(srv, 'bs.an')
            codes = [srv.call('/api/disease_feedback', self.BODY, an)[0] for _ in range(3)]
            self.assertEqual(codes, [200, 200, 429])
            self.assertEqual(srv.call('/api/disease_feedback', token=an)[0], 429)  # the GET shares the group
        self.assertIn(('rate_limited', 'bs.an', 'disease_feedback'), [(e['event'], e['user'], e.get('group')) for e in self.audit.tail()])
        for _ in range(3):
            self.feedback.add('bs.binh', 'BS. Bình', {**self.BODY, 'genes': [], 'correct': {'disease_id': None, 'disease_name': 'x'}})
        with patch.object(disease_feedback, 'MAX_ITEMS', 2):
            items, counts = self.feedback.listing('all')
            self.assertEqual((len(items), counts), (2, {'new': 5, 'reviewed': 0}))  # the newest 2, counts complete
            self.assertEqual(len(self.feedback.own('bs.binh')), 2)
        self.assertEqual(len(self.feedback.listing('new', 'bs.an')[0]), 2)

    def raw_post(self, srv, path, body, token):
        """POST a pre-serialised body (what srv.call cannot send: NaN is not json.dumps-able into valid JSON)."""
        req = urllib.request.Request(srv.base + path, data=body.encode(),
                                     headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_rejects_nan_infinity_and_unbounded_numbers(self):
        from disease_feedback import _number_or_none
        with self.server() as srv:
            an = self.login(srv, 'bs.an')
            # NaN / Infinity pass json.loads by default and would be written to disk as invalid JSON: 400, not stored
            base = json.dumps(self.BODY, ensure_ascii=False)
            for constant in ('NaN', 'Infinity', '-Infinity'):
                body = base.replace('"match_percentage": 41.5', '"match_percentage": ' + constant)
                self.assertIn(constant, body)
                code, err = self.raw_post(srv, '/api/disease_feedback', body, an)
                self.assertEqual((code, err['error']), (400, 'JSON không hợp lệ.'), constant)
            code, err = self.raw_post(srv, '/api/login', '{"username": NaN, "password": "x"}', an)  # the hook is global
            self.assertEqual((code, err['error']), (400, 'JSON không hợp lệ.'))
            # finite but out of bounds, or not a number at all
            for field, value in (('rank', 1e308), ('rank', -1), ('rank', 100001), ('rank', 1.5), ('rank', True),
                                 ('overall_rank', 1e308), ('match_percentage', 100.5), ('match_percentage', -0.1),
                                 ('match_percentage', False), ('match_percentage', '41.5')):
                body = {**self.BODY, 'suggestions': [{**self.SUGGESTIONS[0], field: value}, self.SUGGESTIONS[1]]}
                code, err = srv.call('/api/disease_feedback', body, an)
                self.assertEqual((code, err['error']), (422, 'Danh sách gợi ý của AI không hợp lệ.'), (field, value))
            # the bounds themselves are accepted
            body = {**self.BODY, 'suggestions': [{**self.SUGGESTIONS[0], 'rank': 0, 'overall_rank': 100000, 'match_percentage': 100},
                                                 self.SUGGESTIONS[1]]}
            code, out = srv.call('/api/disease_feedback', body, an)
            self.assertEqual(code, 200, out)
            self.assertEqual((out['item']['suggestions'][0]['rank'], out['item']['suggestions'][0]['overall_rank'],
                              out['item']['suggestions'][0]['match_percentage']), (0, 100000, 100.0))
            self.assertEqual(len(srv.call('/api/disease_feedback', token=an)[1]['items']), 1)  # nothing of the refused ones
        for value in (float('nan'), float('inf'), float('-inf'), 10 ** 400, True):
            with self.assertRaises(ValueError):
                _number_or_none(value)
        self.assertIsNone(_number_or_none(None))
        self.assertEqual(_number_or_none(3.0, integer=True), 3)

    def test_caps_per_user_and_file_size(self):
        import disease_feedback
        self.feedback.per_user = 1
        with self.server() as srv:
            an, admin = self.login(srv, 'bs.an'), self.login(srv, 'quan.tri')
            code, first = srv.call('/api/disease_feedback', self.BODY, an)
            self.assertEqual(code, 200, first)
            code, err = srv.call('/api/disease_feedback', self.BODY, an)
            self.assertEqual((code, err['error']), (422, 'Bác sĩ đã có 1 ghi nhận chưa xem; chờ quản trị xem trước khi ghi thêm.'))
            self.assertEqual(srv.call('/api/disease_feedback', self.BODY, self.login(srv, 'bs.binh'))[0], 200)  # per user
            self.assertEqual(srv.call('/api/admin/disease_feedback/status', {'id': first['item']['id'], 'status': 'reviewed'}, admin)[0], 200)
            self.assertEqual(srv.call('/api/disease_feedback', self.BODY, an)[0], 200)  # reviewed ones do not count
            # total size: the JSON text of the stored items plus the new one
            self.feedback.per_user = 0
            self.feedback.max_bytes = len(self.feedback.path.read_text(encoding='utf-8')) + 100
            code, err = srv.call('/api/disease_feedback', self.BODY, an)
            self.assertEqual((code, err['error']), (422, 'Kho ghi nhận bệnh đúng đã đầy; nhờ quản trị dọn bớt.'))
            self.assertEqual(srv.call('/api/admin/disease_feedback?status=all', token=admin)[1]['counts'], {'new': 2, 'reviewed': 1})
            self.feedback.max_bytes = 0  # cap off
            self.assertEqual(srv.call('/api/disease_feedback', self.BODY, an)[0], 200)
        with patch.dict(os.environ, {'WEB_DISEASE_FEEDBACK_PER_USER': '3', 'WEB_DISEASE_FEEDBACK_MAX_MB': '2'}):
            store = DiseaseFeedbackStore(Path(self.dir.name, 'x.json'))
        self.assertEqual((store.per_user, store.max_bytes), (3, 2000000))
        with patch.dict(os.environ, {'WEB_DISEASE_FEEDBACK_PER_USER': '', 'WEB_DISEASE_FEEDBACK_MAX_MB': 'abc'}):
            store = DiseaseFeedbackStore(Path(self.dir.name, 'x.json'))
        self.assertEqual((store.per_user, store.max_bytes), (50, 50000000))  # defaults
        self.assertEqual((disease_feedback.DEFAULT_PER_USER, disease_feedback.DEFAULT_MAX_MB), (50, 50))

    def test_failed_save_leaves_no_phantom_item(self):
        import corrections
        import disease_feedback
        with self.server() as srv:
            an, admin = self.login(srv, 'bs.an'), self.login(srv, 'quan.tri')
            with patch.object(disease_feedback, 'write_private_json', side_effect=OSError('disk full')):
                code, err = srv.call('/api/disease_feedback', self.BODY, an)
            self.assertEqual((code, err['error']), (500, 'Chưa lưu được ghi nhận. Liên hệ quản trị viên.'))
            self.assertFalse(self.feedback.path.exists())
            self.assertEqual(srv.call('/api/disease_feedback', token=an)[1]['items'], [])  # not in memory either
            self.assertEqual(srv.call('/api/admin/disease_feedback?status=all', token=admin)[1]['counts'], {'new': 0, 'reviewed': 0})
            code, out = srv.call('/api/disease_feedback', self.BODY, an)  # the store works again afterwards
            self.assertEqual(code, 200, out)
            self.assertEqual([i['id'] for i in srv.call('/api/disease_feedback', token=an)[1]['items']], [out['item']['id']])
        self.assertEqual(sum(1 for e in self.audit.tail() if e['event'] == 'disease_feedback_submitted'), 1)
        # the same for corrections.json
        store = CorrectionStore(Path(self.dir.name, 'corrections.json'))
        fields = {'phrase': 'thai to', 'right': {'id': 'HP:0001520', 'vi': 'Thai to', 'en': 'LGA'}, 'wrong': [],
                  'context': '', 'note': '', 'source': 'text'}
        with patch.object(corrections, 'write_private_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                store.add('bs.an', 'BS. An', fields)
        self.assertEqual((store.items, store.own('bs.an'), store.path.exists()), ([], [], False))
        item, duplicate = store.add('bs.an', 'BS. An', fields)
        self.assertEqual((duplicate, [i['id'] for i in store.own('bs.an')]), (False, [item['id']]))

    def test_case_code_limit_matches_the_page(self):
        with self.server() as srv:
            an = self.login(srv, 'bs.an')
            code, out = srv.call('/api/disease_feedback', {**self.BODY, 'case_code': 'C' * 80}, an)
            self.assertEqual((code, out['item']['case_code']), (200, 'C' * 80), out)
            code, err = srv.call('/api/disease_feedback', {**self.BODY, 'case_code': 'C' * 81}, an)
            self.assertEqual((code, err['error']), (422, 'Trường "case_code" dài quá 80 ký tự.'))
        page = Path(__file__).resolve().parent.parent / 'docs' / 'index.html'
        if page.exists():
            self.assertIn('id="case-id" maxlength="80"', page.read_text(encoding='utf-8'))

if __name__ == '__main__':
    unittest.main(verbosity=2)
