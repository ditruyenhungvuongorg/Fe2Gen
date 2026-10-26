"""HTTP web entrypoint; exposes static UI and reviewed APIs only."""
import json
import os
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import BoundedSemaphore, Thread
from urllib.parse import urlparse, parse_qs
from audit import EVENTS, AuditLog
from auth import GPU_ROLES, LOCKED_MESSAGE, ROLES, USERNAME, Auth, LoginRefused, public_user
from model1_process import ModelUnavailable, free_vram_mib
from web_service import WebSystem

ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / 'static' if (ROOT / 'static').is_dir() else ROOT.parent / 'docs'
STATIC_FILES = ('/', '/index.html', '/styles.css', '/app.js', '/admin.js', '/config.js', '/case-state.js',
                '/fonts/BeVietnamPro-Regular.ttf', '/fonts/BeVietnamPro-SemiBold.ttf', '/fonts/OFL.txt',
                '/fonts/Inter-vietnamese.woff2', '/fonts/Inter-latin-ext.woff2', '/fonts/Inter-latin.woff2', '/fonts/OFL-Inter.txt')
SYSTEM = WebSystem()
AUTH = Auth()
AUDIT = AuditLog()
AUTH.store.on_problem = lambda kind: AUDIT.write('auth_store_error', '', kind=kind)
WORK = BoundedSemaphore(1)
QUEUE_SECONDS = 90
# On logout Model 1 stays loaded while another doctor was active in the last 10 minutes; otherwise it is released.
LOGOUT_GRACE_SECONDS = 600
# Only the page's own files run; inline style attributes are used by the lab sheet.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# With a temporary password, only these work until the user sets a new one.
FIRST_LOGIN_PATHS = ('/api/session', '/api/change_password', '/api/logout')

class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, '.woff2': 'font/woff2'}
    session = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_ROOT), **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(180)

    def log_message(self, *args):
        pass

    def allowed_origin(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        return not origin or origin in os.getenv('WEB_ALLOWED_ORIGINS', '').split(',') or origin in ('http://' + host, 'https://' + host)

    def end_headers(self):
        origin = self.headers.get('Origin')
        if origin and self.allowed_origin():
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Frame-Options', 'DENY')
        super().end_headers()

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ identity
    @property
    def role(self):
        return self.session['role'] if self.session else 'doctor'  # shared code or local use: doctor

    @property
    def user(self):
        if self.session:
            return self.session['user']
        return '(mã chung)' if AUTH.mode == 'token' else '(cục bộ)'

    def token(self):
        header = self.headers.get('Authorization', '')
        return header[7:] if header.startswith('Bearer ') else ''

    def authorized(self, path):
        if not self.allowed_origin():
            self.send_json({'error': 'Nguồn truy cập chưa được cấu hình.'}, 403)
            return False
        ok, self.session = AUTH.check(self.headers.get('Authorization', ''))
        if not ok:
            accounts = AUTH.mode == 'accounts'
            self.send_json({'error': 'Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.' if accounts
                            else 'Nhập mã truy cập do quản trị viên cấp.', 'auth_mode': AUTH.mode}, 401)
            return False
        if self.session and self.session['must_change'] and path not in FIRST_LOGIN_PATHS:
            self.send_json({'error': 'Đặt mật khẩu mới trước khi tiếp tục.', 'must_change': True}, 403)
            return False
        if self.role in GPU_ROLES:
            SYSTEM.runner.touch()  # a doctor is working: postpone releasing the GPU
        return True

    def read_json(self):
        """The request body as a dict, or None after an error reply."""
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 100000:
                self.send_json({'error': 'Yêu cầu quá lớn hoặc rỗng.'}, 413)
                return None
            payload = json.loads(self.rfile.read(size).decode())
            if not isinstance(payload, dict):
                raise ValueError()
            return payload
        except (ValueError, UnicodeError):
            self.send_json({'error': 'JSON không hợp lệ.'}, 400)
            return None

    def model_status(self):
        return {'model_configured': bool(SYSTEM.runner.adapter_path), **SYSTEM.runner.status()}

    def do_OPTIONS(self):
        if not self.allowed_origin():
            return self.send_json({'error': 'Origin denied'}, 403)
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Content-Length', '0')
        self.end_headers()

    # ------------------------------------------------------------------ routes
    def locked(self):
        """Fail closed: without a readable account list (WEB_REQUIRE_ACCOUNTS=1) every API request is refused."""
        if AUTH.mode != 'locked':
            return False
        self.send_json({'error': LOCKED_MESSAGE, 'status': 'locked', 'auth_mode': 'locked'}, 503)
        return True

    def do_GET(self):
        path = urlparse(self.path)
        if path.path.startswith('/api/') and self.locked():
            return
        if path.path == '/api/status':
            mode = AUTH.mode
            return self.send_json({'status': 'online', 'profiles_count': len(SYSTEM.matcher.profiles),
                'hpo_terms_count': len(SYSTEM.canonical), 'auth_required': mode != 'none', 'auth_mode': mode,
                **self.model_status()})
        if path.path.startswith('/api/'):
            if not self.authorized(path.path):
                return
            if path.path == '/api/session':
                user = ({'username': self.session['user'], 'name': self.session['name'], 'role': self.session['role'],
                         'must_change': self.session['must_change']} if self.session else None)
                return self.send_json({'user': user, 'role': self.role, **self.model_status()})
            if path.path == '/api/hpo_search':
                return self.send_json(SYSTEM.search_hpo(parse_qs(path.query).get('q', [''])[0][:200]))
            if path.path.startswith('/api/admin/') and self.admin_only():
                return self.admin_get(path.path, parse_qs(path.query))
            return self.send_json({'error': 'Không có endpoint này.'}, 404)
        if path.path not in STATIC_FILES:
            return self.send_error(404)
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if self.locked():
            return
        if path == '/api/login':
            return self.login()
        if not self.authorized(path):
            return
        if path == '/api/logout':
            return self.logout()
        if path == '/api/warmup':  # the doctor opened text extraction: load Model 1 in the background
            if self.role not in GPU_ROLES:
                return self.send_json({'error': 'Tài khoản Phòng Lab không dùng tách đoạn văn.'}, 403)
            SYSTEM.runner.start()
            return self.send_json(self.model_status())
        payload = self.read_json()
        if payload is None:
            return
        if path == '/api/change_password':
            return self.change_password(payload)
        if path.startswith('/api/admin/'):
            if self.admin_only():
                self.admin_post(path, payload)
            return
        if path not in ('/api/extract_hpo', '/api/match_diseases'):
            return self.send_json({'error': 'Không có endpoint này.'}, 404)
        extract = path.endswith('extract_hpo')
        if extract:
            if self.role not in GPU_ROLES:
                return self.send_json({'error': 'Tài khoản Phòng Lab không dùng tách đoạn văn.'}, 403)
            try:  # wait for a released model to load before queueing on the GPU
                SYSTEM.runner.ensure_ready()
            except ModelUnavailable as exc:
                return self.send_json({'error': str(exc), **self.model_status()}, 503)
        # One request at a time on the GPU; later requests queue instead of failing at once.
        if not WORK.acquire(timeout=QUEUE_SECONDS):
            return self.send_json({'error': 'Hệ thống đang xử lý ca khác. Vui lòng thử lại.'}, 503)
        try:
            data = SYSTEM.extract(payload.get('text')) if extract else SYSTEM.match(payload)
            self.send_json(data)
            # Who and when only: no report text, HPO or genes in the log.
            if extract:
                AUDIT.write('extract', self.user, chars=len(payload.get('text') or ''),
                            findings=len(data.get('mentions', [])), seconds=data.get('seconds'))
            else:
                AUDIT.write('match', self.user, hpo_count=len(payload.get('hpos') or []), wes=bool(payload.get('genes')))
        except ValueError as exc:
            self.send_json({'error': str(exc)}, 422)
        except RuntimeError as exc:
            self.send_json({'error': str(exc) if isinstance(exc, ModelUnavailable)
                            else 'Model chưa sẵn sàng hoặc đang bận. Liên hệ quản trị viên.'}, 503)
        except Exception as exc:
            print('Request failed:', type(exc).__name__, file=sys.stderr, flush=True)
            self.send_json({'error': 'Xử lý thất bại. Chưa tạo kết quả; vui lòng thử lại.'}, 500)
        finally:
            WORK.release()

    # ------------------------------------------------------------------ login, logout, own password
    def login(self):
        if not self.allowed_origin():
            return self.send_json({'error': 'Nguồn truy cập chưa được cấu hình.'}, 403)
        if AUTH.mode != 'accounts':
            return self.send_json({'error': 'Máy chủ chưa bật tài khoản; dùng mã truy cập.', 'auth_mode': AUTH.mode}, 400)
        payload = self.read_json()
        if payload is None:
            return
        name = str(payload.get('username') or '').strip().lower()
        try:
            token, user = AUTH.login(name, payload.get('password'))
        except LoginRefused as exc:
            AUDIT.write(exc.event, name if USERNAME.match(name) else '(tên không hợp lệ)')
            return self.send_json({'error': str(exc)}, 429 if exc.event != 'login_fail' else 401)
        AUDIT.write('login_ok', user['username'], role=user['role'])
        if user['role'] == 'doctor' and not user['must_change']:
            SYSTEM.runner.start()  # a doctor is working now: load Model 1 while they enter the case
        self.send_json({'token': token, 'user': user, **self.model_status()})

    def logout(self):
        AUTH.logout(self.token())
        if self.session:
            AUDIT.write('logout', self.user)
        if AUTH.mode == 'accounts' and not AUTH.active(LOGOUT_GRACE_SECONDS, GPU_ROLES):
            Thread(target=SYSTEM.runner.stop, args=('không còn bác sĩ đăng nhập',), daemon=True).start()
        self.send_json({'ok': True})

    def change_password(self, payload):
        if not self.session:
            return self.send_json({'error': 'Chỉ tài khoản cá nhân mới đổi được mật khẩu.'}, 400)
        try:
            AUTH.change_password(self.token(), payload.get('old_password'), payload.get('new_password'))
        except ValueError as exc:
            return self.send_json({'error': str(exc)}, 422)
        AUDIT.write('password_changed', self.user)
        if self.role == 'doctor':
            SYSTEM.runner.start()
        self.send_json({'ok': True, 'user': {'username': self.session['user'], 'name': self.session['name'],
                                             'role': self.session['role'], 'must_change': False}})

    # ------------------------------------------------------------------ admin page
    def admin_only(self):
        if self.session and self.role == 'admin':
            return True
        self.send_json({'error': 'Chỉ tài khoản quản trị dùng được chức năng này.'}, 403)
        return False

    def gpu_status(self):
        runner = SYSTEM.runner
        return {**self.model_status(), 'paused': runner.paused(), 'free_mib': free_vram_mib(),
                'doctors_active': AUTH.active(LOGOUT_GRACE_SECONDS, GPU_ROLES), 'pause_file': str(runner.pause_file or '')}

    def admin_get(self, path, query):
        if path == '/api/admin/users':
            users = [public_user(name, u) for name, u in sorted(AUTH.store.all().items())]
            return self.send_json({'users': users, 'roles': ROLES})
        if path == '/api/admin/sessions':
            return self.send_json({'sessions': AUTH.list_sessions()})
        if path == '/api/admin/audit':
            first = lambda key: query.get(key, [''])[0]
            try:
                limit = max(1, min(1000, int(first('limit') or 200)))
            except ValueError:
                limit = 200
            return self.send_json({'entries': AUDIT.tail(limit, first('user'), first('event')), 'events': EVENTS})
        if path == '/api/admin/gpu':
            return self.send_json(self.gpu_status())
        self.send_json({'error': 'Không có endpoint này.'}, 404)

    def admin_post(self, path, p):
        target = str(p.get('username') or '').strip().lower()
        try:
            if path == '/api/admin/users/create':
                temp = AUTH.create_user(target, p.get('name'), p.get('role'))
                AUDIT.write('user_created', self.user, target=target, role=p.get('role'))
                return self.send_json({'username': target, 'temp_password': temp})
            if path == '/api/admin/users/update':
                disabled = p.get('disabled')
                changes = AUTH.update_user(self.user, target, name=p.get('name'), role=p.get('role'),
                                           disabled=None if disabled is None else bool(disabled))
                if changes:
                    AUDIT.write('user_updated', self.user, target=target, changes=changes)
                return self.send_json({'changes': changes})
            if path == '/api/admin/users/reset':
                temp = AUTH.reset_password(target)
                AUDIT.write('password_reset', self.user, target=target)
                return self.send_json({'username': target, 'temp_password': temp})
            if path == '/api/admin/users/delete':
                AUTH.delete_user(self.user, target)
                AUDIT.write('user_deleted', self.user, target=target)
                return self.send_json({'ok': True})
            if path == '/api/admin/sessions/end':
                ended = AUTH.end_sessions(target, keep=self.token())
                AUDIT.write('sessions_ended', self.user, target=target, count=ended)
                return self.send_json({'ended': ended})
            if path == '/api/admin/gpu':
                return self.admin_gpu(str(p.get('action') or ''))
        except ValueError as exc:
            return self.send_json({'error': str(exc)}, 422)
        self.send_json({'error': 'Không có endpoint này.'}, 404)

    def admin_gpu(self, action):
        runner = SYSTEM.runner
        if action in ('pause', 'resume') and not runner.pause_file:
            return self.send_json({'error': 'Chưa cấu hình MODEL1_PAUSE_FILE.'}, 422)
        if action == 'pause':  # release now and keep it off (training, local AI)
            runner.pause_file.parent.mkdir(parents=True, exist_ok=True)
            runner.pause_file.touch()
            Thread(target=runner.stop, args=('quản trị tạm dừng GPU',), daemon=True).start()
        elif action == 'resume':
            runner.pause_file.unlink(missing_ok=True)
        elif action == 'release':  # release now; the next doctor activity loads it again
            Thread(target=runner.stop, args=('quản trị giải phóng GPU',), daemon=True).start()
        else:
            return self.send_json({'error': 'Thao tác không hợp lệ.'}, 422)
        AUDIT.write('gpu', self.user, action=action)
        self.send_json(self.gpu_status())

def preload_model1():
    """Load Model 1 at startup (MODEL1_PRELOAD=1; it is still released after MODEL1_IDLE_MINUTES unused).
    HPO search and disease matching need no GPU, so a failure (e.g. no NVIDIA driver after a reboot) must not
    stop the service: /api/status reports model_ready=false and text extraction answers 503; each later
    login or extraction tries to load the model again."""
    try:
        SYSTEM.runner.load_model()
    except Exception as exc:
        print('Model 1 not loaded; text extraction unavailable, search and matching still served:',
              type(exc).__name__, exc, file=sys.stderr, flush=True)

if __name__ == '__main__':
    SYSTEM.initialize()
    if os.getenv('MODEL1_PRELOAD') == '1':
        preload_model1()
    port = int(os.getenv('PORT', '8000'))
    print(f'Web ready on 127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
