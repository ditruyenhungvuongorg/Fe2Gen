"""HTTP web entrypoint; exposes static UI and reviewed APIs only."""
import json
import os
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import BoundedSemaphore, Thread
from urllib.parse import urlparse, parse_qs
from auth import Auth
from model1_process import ModelUnavailable
from web_service import WebSystem

ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / 'static' if (ROOT / 'static').is_dir() else ROOT.parent / 'docs'
SYSTEM = WebSystem()
AUTH = Auth()
WORK = BoundedSemaphore(1)
QUEUE_SECONDS = 90
# On logout Model 1 stays loaded while another doctor was active in the last 10 minutes; otherwise it is released.
LOGOUT_GRACE_SECONDS = 600

class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, '.woff2': 'font/woff2'}

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
        super().end_headers()

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if not self.allowed_origin():
            self.send_json({'error': 'Nguồn truy cập chưa được cấu hình.'}, 403)
            return False
        ok, self.session = AUTH.check(self.headers.get('Authorization', ''))
        if not ok:
            accounts = AUTH.mode == 'accounts'
            self.send_json({'error': 'Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.' if accounts
                            else 'Nhập mã truy cập do quản trị viên cấp.', 'auth_mode': AUTH.mode}, 401)
            return False
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

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == '/api/status':
            mode = AUTH.mode
            return self.send_json({'status': 'online', 'profiles_count': len(SYSTEM.matcher.profiles),
                'hpo_terms_count': len(SYSTEM.canonical), 'auth_required': mode != 'none', 'auth_mode': mode,
                **self.model_status()})
        if path.path.startswith('/api/'):
            if not self.authorized():
                return
            if path.path == '/api/session':
                user = {'username': self.session['user'], 'name': self.session['name']} if self.session else None
                return self.send_json({'user': user, **self.model_status()})
            if path.path == '/api/hpo_search':
                return self.send_json(SYSTEM.search_hpo(parse_qs(path.query).get('q', [''])[0][:200]))
            return self.send_json({'error': 'Không có endpoint này.'}, 404)
        if path.path not in ('/', '/index.html', '/styles.css', '/app.js', '/config.js', '/case-state.js', '/fonts/BeVietnamPro-Regular.ttf', '/fonts/BeVietnamPro-SemiBold.ttf', '/fonts/OFL.txt', '/fonts/Inter-vietnamese.woff2', '/fonts/Inter-latin-ext.woff2', '/fonts/Inter-latin.woff2', '/fonts/OFL-Inter.txt'):
            return self.send_error(404)
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/login':
            return self.login()
        if not self.authorized():
            return
        if path == '/api/logout':
            return self.logout()
        if path == '/api/warmup':  # the doctor opened text extraction: load Model 1 in the background
            SYSTEM.runner.start()
            return self.send_json(self.model_status())
        payload = self.read_json()
        if payload is None:
            return
        if path not in ('/api/extract_hpo', '/api/match_diseases'):
            return self.send_json({'error': 'Không có endpoint này.'}, 404)
        if path.endswith('extract_hpo'):
            try:  # wait for a released model to load before queueing on the GPU
                SYSTEM.runner.ensure_ready()
            except ModelUnavailable as exc:
                return self.send_json({'error': str(exc), **self.model_status()}, 503)
        # One request at a time on the GPU; later requests queue instead of failing at once.
        if not WORK.acquire(timeout=QUEUE_SECONDS):
            return self.send_json({'error': 'Hệ thống đang xử lý ca khác. Vui lòng thử lại.'}, 503)
        try:
            data = SYSTEM.extract(payload.get('text')) if path.endswith('extract_hpo') else SYSTEM.match(payload)
            self.send_json(data)
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

    def login(self):
        if not self.allowed_origin():
            return self.send_json({'error': 'Nguồn truy cập chưa được cấu hình.'}, 403)
        if AUTH.mode != 'accounts':
            return self.send_json({'error': 'Máy chủ chưa bật tài khoản; dùng mã truy cập.', 'auth_mode': AUTH.mode}, 400)
        payload = self.read_json()
        if payload is None:
            return
        try:
            token, user = AUTH.login(payload.get('username'), payload.get('password'))
        except PermissionError as exc:
            return self.send_json({'error': str(exc)}, 401)
        print('Đăng nhập:', user['username'], file=sys.stderr, flush=True)
        SYSTEM.runner.start()  # someone is working now: load Model 1 while they enter the case
        self.send_json({'token': token, 'user': user, **self.model_status()})

    def logout(self):
        header = self.headers.get('Authorization', '')
        AUTH.logout(header[7:] if header.startswith('Bearer ') else '')
        if AUTH.mode == 'accounts' and not AUTH.active(LOGOUT_GRACE_SECONDS):
            Thread(target=SYSTEM.runner.stop, args=('không còn bác sĩ đăng nhập',), daemon=True).start()
        self.send_json({'ok': True})

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

