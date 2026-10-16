"""Model 1 on demand (child process, idle release, pause file, free-VRAM check) and doctor accounts.
Runs without GPU or HPO data: a fake runner stands in for Model 1."""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from auth import Auth, UserStore, check_password, hash_password
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

    def test_mode_follows_users_file(self):
        self.assertEqual(Auth(self.store, token='').mode, 'none')
        self.assertEqual(Auth(self.store, token='abc').mode, 'token')
        self.add()
        self.assertEqual(Auth(self.store, token='abc').mode, 'accounts')
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)

    def test_shared_token_mode(self):
        a = Auth(self.store, token='abc')
        self.assertEqual(a.check('Bearer abc'), (True, None))
        self.assertFalse(a.check('Bearer abd')[0])
        self.assertFalse(a.check('Bearer ábc')[0])  # non-ASCII header: refused, no TypeError

    def test_login_session_logout(self):
        self.add()
        a = Auth(self.store, token='abc')
        self.assertFalse(a.check('Bearer abc')[0])  # the shared code stops working once accounts exist
        token, user = a.login(' BS.AN ', 'mat-khau-1')
        self.assertEqual(user, {'username': 'bs.an', 'name': 'BS. An'})
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

    def test_manage_users_cli(self):
        import manage_users
        with patch('getpass.getpass', return_value='mat-khau-2'), redirect_stderr(StringIO()), \
             patch('sys.stdout', StringIO()):
            manage_users.main(['add', 'bs.binh', '--name', 'BS. Bình', '--file', str(self.store.path)])
            self.assertEqual(Auth(self.store).login('bs.binh', 'mat-khau-2')[1]['name'], 'BS. Bình')
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

    def call(self, path, payload=None, token=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={'Content-Type': 'application/json',
                                              **({'Authorization': 'Bearer ' + token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        code, status = self.call('/api/status')
        self.assertEqual((code, status['auth_mode'], status['model_state']), (200, 'accounts', 'stopped'))
        self.assertIsNone(self.runner.proc)  # no VRAM before anyone logs in
        self.assertEqual(self.call('/api/hpo_search?q=a')[0], 401)
        self.assertEqual(self.call('/api/login', {'username': 'bs.an', 'password': 'sai'})[0], 401)
        code, login = self.call('/api/login', {'username': 'bs.an', 'password': 'mat-khau-1'})
        self.assertEqual(code, 200)
        self.assertIn(login['model_state'], ('loading', 'ready'))  # login starts loading Model 1
        token = login['token']
        self.assertEqual(self.call('/api/session', token=token)[1]['user']['name'], 'BS. An')
        self.assertEqual(self.call('/api/hpo_search?q=a', token=token)[1][0]['q'], 'a')
        code, out = self.call('/api/extract_hpo', {'text': 'Thai đầu nhỏ.'}, token)
        self.assertEqual((code, out['echo']), (200, 'Thai đầu nhỏ.'))
        self.assertEqual(self.call('/api/extract_hpo', {'text': 'long'}, token)[0], 422)
        self.assertEqual(self.call('/api/warmup', {}, token)[1]['model_state'], 'ready')
        proc = self.runner.proc
        self.assertEqual(self.call('/api/logout', {}, token)[0], 200)
        proc.wait(timeout=30)  # the last doctor logged out: the model process exits
        self.assertEqual(self.call('/api/session', token=token)[0], 401)


if __name__ == '__main__':
    unittest.main(verbosity=2)
