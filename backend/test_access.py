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
from unittest.mock import patch

# Tests also run on the production host (deployment step 3): keep serve_web's module-level stores away from the
# real users.json, audit log, access log and lockout file. Child processes inherit these variables.
if 'FE2GEN_TEST_DIR' not in os.environ:
    os.environ['FE2GEN_TEST_DIR'] = tempfile.mkdtemp(prefix='fe2gen-test-')
    for _name, _file in (('WEB_USERS_FILE', 'users.json'), ('WEB_AUDIT_FILE', 'audit.jsonl'),
                         ('WEB_ACCESS_LOG', 'access.log'), ('WEB_LOGIN_FAILURES_FILE', 'login_failures.json'),
                         ('MODEL1_STATE_FILE', 'model1_state.json')):
        os.environ[_name] = str(Path(os.environ['FE2GEN_TEST_DIR'], _file))

from accesslog import AccessLog
from audit import AuditLog
from auth import Auth, UserStore, check_password, hash_password, password_problem, public_user, temporary_password
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
        self.assertEqual((code, status), (200, {'status': 'online', 'auth_required': True, 'auth_mode': 'accounts'}))
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
    groups = ('hpo_search', 'match_diseases', 'extract_hpo', 'login', 'login_user', 'admin')
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
            self.assertEqual([(e['method'], e['path'], e['status']) for e in lines],
                             [('POST', '/api/login', 200), ('GET', '/api/hpo_search', 200),
                              ('POST', '/api/extract_hpo', 200), ('GET', '/api/nope%01', 404), ('GET', '/app.js', 200)])
            self.assertEqual([e['user'] for e in lines], ['-', 'bs.an', 'bs.an', 'bs.an', '-'])
            self.assertEqual(set(lines[0]), {'time', 'method', 'path', 'status', 'user', 'ms', 'bytes'})
            self.assertGreater(lines[-1]['bytes'], 1000)
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
        self.assertTrue(totp.uri('bs.an', 'ABC').startswith('otpauth://totp/Fe2Gen%3Abs.an?secret=ABC&issuer=Fe2Gen'))
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
            self.assertTrue(admin['user']['needs_2fa'])
            token = admin['token']
            code, data = srv.call('/api/admin/users', token=token)
            self.assertEqual((code, data.get('needs_2fa')), (403, True))
            self.assertEqual(srv.call('/api/admin/users/create', {'username': 'x.y', 'role': 'lab'}, token)[0], 403)
            self.assertEqual(srv.call('/api/hpo_search?q=a', token=token)[0], 403)
            self.assertEqual(srv.call('/api/session', token=token)[0], 200)
            secret, backup = self.enable(srv, token)
            code, users = srv.call('/api/admin/users', token=token)  # the same session is now verified
            self.assertEqual(code, 200)
            self.assertNotIn(secret, json.dumps(users))                   # the secret never leaves again
            self.assertNotIn('backup', json.dumps(users))
            self.assertTrue(next(u for u in users['users'] if u['username'] == 'quan.tri')['totp'])
            # next login: password, then code
            code, first = srv.call('/api/login', {'username': 'quan.tri', 'password': 'mat-khau-quan.tri'})
            self.assertEqual((code, first['second_factor']), (200, True))
            self.assertNotIn('token', first)
            self.assertEqual(srv.call('/api/admin/users', token=first['pending'])[0], 401)  # pending is no session
            code, bad = srv.call('/api/login/totp', {'pending': first['pending'], 'code': '000000'})
            self.assertEqual(code, 401)
            # the code used to enable cannot be replayed; wait for the next step is too slow, use a backup code
            code, ok = srv.call('/api/login/totp', {'pending': first['pending'], 'code': backup[0]})
            self.assertEqual(code, 200, ok)
            self.assertFalse(ok['user']['needs_2fa'])
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
            self.assertFalse(doctor['user']['needs_2fa'])
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
