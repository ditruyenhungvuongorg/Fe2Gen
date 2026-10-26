"""HTTP web entrypoint; exposes static UI and reviewed APIs only."""
import hashlib
import json
import os
import socket
import sys
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import BoundedSemaphore, Thread, Timer
from urllib.parse import urlparse, parse_qs
from accesslog import AccessLog
from audit import EVENTS, AuditLog
from auth import GPU_ROLES, LOCKED_MESSAGE, ROLES, USERNAME, Auth, LoginRefused, public_user
from model1_process import ModelUnavailable, free_vram_mib
from ratelimit import RateLimiter, WorkQueue, env_int
from web_service import WebSystem

ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / 'static' if (ROOT / 'static').is_dir() else ROOT.parent / 'docs'
STATIC_FILES = ('/', '/index.html', '/styles.css', '/app.js', '/admin.js', '/config.js', '/case-state.js', '/idle.js', '/twofactor.js',
                '/fonts/BeVietnamPro-Regular.ttf', '/fonts/BeVietnamPro-SemiBold.ttf', '/fonts/OFL.txt',
                '/fonts/Inter-vietnamese.woff2', '/fonts/Inter-latin-ext.woff2', '/fonts/Inter-latin.woff2', '/fonts/OFL-Inter.txt')
SYSTEM = WebSystem()
AUTH = Auth()
AUDIT = AuditLog()
ACCESS = AccessLog()
AUTH.store.on_problem = lambda kind: AUDIT.write('auth_store_error', '', kind=kind)
QUEUE_SECONDS = 90
# One request at a time on the GPU/CPU; at most WEB_QUEUE_MAX more wait (up to QUEUE_SECONDS), the next gets 503.
WORK = WorkQueue(env_int('WEB_QUEUE_MAX', 5))
# Requests per minute (token bucket, burst = one minute's worth); 0 turns a limit off. Keys: the session token's
# hash; for login the connection source and, separately, the username.
RATES = {'hpo_search': RateLimiter(env_int('WEB_RATE_HPO_SEARCH', 60)),
         'match_diseases': RateLimiter(env_int('WEB_RATE_MATCH', 10)),
         'extract_hpo': RateLimiter(env_int('WEB_RATE_EXTRACT', 10)),
         'login': RateLimiter(env_int('WEB_RATE_LOGIN', 10)),
         'login_user': RateLimiter(env_int('WEB_RATE_LOGIN', 10)),
         'admin': RateLimiter(env_int('WEB_RATE_ADMIN', 60))}
# Behind Tailscale Funnel every connection comes from 127.0.0.1. Only with WEB_TRUST_PROXY=1 is the last
# X-Forwarded-For entry (the one the local proxy appended) used as the source; never trust it otherwise.
TRUST_PROXY = os.getenv('WEB_TRUST_PROXY', '0') == '1'
MAX_CONNECTIONS = env_int('WEB_MAX_CONNECTIONS', 64)
HEADER_SECONDS = env_int('WEB_HEADER_TIMEOUT', 15)   # whole request line + headers, against slowloris
REQUEST_SECONDS = 180                                 # each socket read/write once the headers are in
# On logout Model 1 stays loaded while another doctor was active in the last 10 minutes; otherwise it is released.
LOGOUT_GRACE_SECONDS = 600
# Only the page's own files run; inline style attributes are used by the lab sheet.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
PERMISSIONS_POLICY = 'camera=(), microphone=(), geolocation=(), payment=(), usb=()'
HSTS = os.getenv('WEB_HSTS', '1') != '0'
# With a temporary password, only these work until the user sets a new one.
FIRST_LOGIN_PATHS = ('/api/session', '/api/change_password', '/api/logout')
# An admin without a verified second factor (WEB_ADMIN_2FA_REQUIRED=1) can only set one up (or change the password).
# A temporary password is changed first (FIRST_LOGIN_PATHS), then this applies.
SECOND_FACTOR_PATHS = ('/api/session', '/api/totp/setup', '/api/totp/enable', '/api/change_password', '/api/logout')

class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, '.woff2': 'font/woff2'}
    session = status_sent = deadline = None
    bytes_sent = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_ROOT), **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(HEADER_SECONDS)

    def header_deadline(self):
        """The request line and headers did not arrive within HEADER_SECONDS (slowloris): drop the connection."""
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def parse_request(self):
        ok = super().parse_request()  # reads the request line and headers
        if self.deadline:
            self.deadline.cancel()
        self.connection.settimeout(REQUEST_SECONDS)
        return ok

    def log_message(self, *args):
        pass  # stderr stays quiet; one line per request goes to ACCESS (no query, body or token)

    def handle_one_request(self):
        self.session, self.status_sent, self.bytes_sent, started = None, None, 0, time.monotonic()
        self.connection.settimeout(HEADER_SECONDS)
        self.deadline = Timer(HEADER_SECONDS, self.header_deadline)
        self.deadline.daemon = True
        self.deadline.start()
        try:
            super().handle_one_request()
        finally:
            self.deadline.cancel()
        if self.status_sent is not None:
            ACCESS.write(self.command, self.path, self.status_sent, self.session['user'] if self.session else '-',
                         (time.monotonic() - started) * 1000, self.bytes_sent)

    def send_response(self, code, message=None):
        if self.status_sent is None:
            self.status_sent = code
        super().send_response(code, message)

    def send_header(self, keyword, value):
        if keyword.lower() == 'content-length':
            try:
                self.bytes_sent = int(value)
            except ValueError:
                pass
        super().send_header(keyword, value)

    def allowed_origin(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        return not origin or origin in os.getenv('WEB_ALLOWED_ORIGINS', '').split(',') or origin in ('http://' + host, 'https://' + host)

    def end_headers(self):
        origin = self.headers.get('Origin')
        if origin and self.allowed_origin():
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Expose-Headers', 'Retry-After')  # the page says how long to wait on 429
            self.send_header('Vary', 'Origin')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Frame-Options', 'DENY')
        if HSTS:  # Tailscale Funnel terminates TLS but does not add HSTS; browsers ignore it on plain http
            self.send_header('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        self.send_header('Permissions-Policy', PERMISSIONS_POLICY)
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        # CORP only applies to no-cors loads (<img>, <script> from another site). The GitHub Pages UI reads the
        # API with CORS-mode fetch, which CORP does not block (Fetch standard), so it is safe on /api/* as well.
        self.send_header('Cross-Origin-Resource-Policy', 'same-site')
        super().end_headers()

    def send_json(self, data, code=200, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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
        if self.session and not self.session['must_change'] and AUTH.needs_2fa(self.session) \
                and path not in SECOND_FACTOR_PATHS:
            self.send_json({'error': self.second_factor_message(), 'totp_required': True, 'needs_2fa': True}, 403)
            return False
        if self.role in GPU_ROLES:
            SYSTEM.runner.touch()  # a doctor is working: postpone releasing the GPU
        return True

    def second_factor_message(self):
        if self.session.get('totp'):  # enabled, but this session was opened before (e.g. promoted to admin)
            return 'Đăng xuất rồi đăng nhập lại bằng mã xác thực 2 lớp để dùng chức năng quản trị.'
        return 'Tài khoản quản trị cần bật xác thực 2 lớp trước khi tiếp tục (Tài khoản → Xác thực 2 lớp).'

    def source(self):
        """Where the connection comes from, for login limits (see TRUST_PROXY)."""
        if TRUST_PROXY:
            forwarded = self.headers.get('X-Forwarded-For', '').split(',')[-1].strip()
            if forwarded:
                return forwarded[:64]
        return self.client_address[0]

    def rate_key(self):
        token = self.token()
        return 'session:' + hashlib.sha256(token.encode()).hexdigest() if token else 'source:' + self.source()

    def limited(self, group, key=None, who=None):
        """Answer 429 with Retry-After when key used up group's budget; True when refused."""
        limiter, key = RATES[group], key or self.rate_key()
        wait = limiter.take(key)
        if not wait:
            return False
        if limiter.should_report(key):
            AUDIT.write('rate_limited', who or self.user, group=group)
        self.send_json({'error': f'Quá nhiều yêu cầu. Vui lòng thử lại sau {wait} giây.', 'retry_after': wait}, 429,
                       {'Retry-After': str(wait)})
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
        status = dict(SYSTEM.runner.status())
        status['gpu_idle_minutes'] = status.pop('idle_minutes', None)  # 'idle_minutes' is the session's limit
        return {'model_configured': bool(SYSTEM.runner.adapter_path), **status}

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
        if path.path == '/api/status':  # public: only what the sign-in page needs; details in /api/session
            return self.send_json({'status': 'online', 'auth_mode': AUTH.mode})
        if path.path.startswith('/api/'):
            if not self.authorized(path.path):
                return
            if path.path == '/api/session':
                user = AUTH.describe(self.session) if self.session else None
                return self.send_json({'user': user, 'role': self.role, **AUTH.session_fields(self.session),
                                       'profiles_count': len(SYSTEM.matcher.profiles),
                                       'hpo_terms_count': len(SYSTEM.canonical), **self.model_status()})
            if path.path == '/api/hpo_search':
                if self.limited('hpo_search'):
                    return
                return self.send_json(SYSTEM.search_hpo(parse_qs(path.query).get('q', [''])[0][:200]))
            if path.path.startswith('/api/admin/'):
                if not self.admin_only():  # admin_only already answered 403; a second reply would desync HTTP
                    return
                if self.limited('admin'):
                    return
                return self.admin_get(path.path, parse_qs(path.query))
            return self.send_json({'error': 'Không có endpoint này.'}, 404)
        if path.path not in STATIC_FILES:
            return self.send_error(404)
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if self.locked():
            return
        if path in ('/api/login', '/api/login/totp'):
            return self.login(second=path.endswith('totp'))
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
        if path in ('/api/totp/setup', '/api/totp/enable', '/api/totp/disable'):
            return self.totp(path, payload)
        if path.startswith('/api/admin/'):
            if self.admin_only() and not self.limited('admin'):
                self.admin_post(path, payload)
            return
        if path not in ('/api/extract_hpo', '/api/match_diseases'):
            return self.send_json({'error': 'Không có endpoint này.'}, 404)
        extract = path.endswith('extract_hpo')
        if extract and self.role not in GPU_ROLES:
            return self.send_json({'error': 'Tài khoản Phòng Lab không dùng tách đoạn văn.'}, 403)
        if self.limited(path[5:]):
            return
        if extract:
            try:  # wait for a released model to load before queueing on the GPU
                SYSTEM.runner.ensure_ready()
            except ModelUnavailable as exc:
                return self.send_json({'error': str(exc), **self.model_status()}, 503)
        # One request at a time on the GPU; later requests queue instead of failing at once.
        waited = WORK.acquire(QUEUE_SECONDS)
        if waited != 'ok':
            return self.send_json({'error': 'Hệ thống đang xử lý ca khác. Vui lòng thử lại.' if waited == 'timeout'
                                   else 'Đang có nhiều ca chờ xử lý. Vui lòng thử lại sau ít phút.'}, 503,
                                  {'Retry-After': '30'})
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
    def login(self, second=False):
        """/api/login {username, password}; with 2FA it answers {second_factor, pending} and the page sends
        /api/login/totp {pending, code} (a 6-digit code or a backup code) within 5 minutes."""
        if not self.allowed_origin():
            return self.send_json({'error': 'Nguồn truy cập chưa được cấu hình.'}, 403)
        if AUTH.mode != 'accounts':
            return self.send_json({'error': 'Máy chủ chưa bật tài khoản; dùng mã truy cập.', 'auth_mode': AUTH.mode}, 400)
        payload = self.read_json()
        if payload is None:
            return
        if second:
            pending = AUTH.pending.get(AUTH._key(str(payload.get('pending') or '')))
            name = pending['user'] if pending else ''
        else:
            name = str(payload.get('username') or '').strip().lower()
        who = name if USERNAME.match(name) else '(tên không hợp lệ)'
        source_key, user_key = 'source:' + self.source(), 'user:' + name[:64]
        if self.limited('login', source_key, who) or self.limited('login_user', user_key, who):
            return
        backup = False
        try:
            if second:
                token, user, backup = AUTH.login_totp(payload.get('pending'), payload.get('code'))
            else:
                token, user = AUTH.login(name, payload.get('password'))
        except LoginRefused as exc:
            AUDIT.write(exc.event, who)
            code = 401 if exc.event in ('login_fail', 'totp_fail', 'totp_expired') else 403 if exc.event == 'temp_password_expired' else 429
            # pending_invalid: the second step was cancelled (5 wrong codes) or expired; the page starts from the password
            return self.send_json({'error': str(exc), **({'pending_invalid': True} if exc.pending_invalid else {})}, code)
        RATES['login'].give_back(source_key)  # only failed attempts use up the source's budget
        RATES['login_user'].give_back(user_key)
        if token is None:  # password right; the code comes next
            return self.send_json(user)
        if backup:
            AUDIT.write('totp_backup_used', user['username'])
        AUDIT.write('login_ok', user['username'], role=user['role'], **({'mfa': True} if second else {}))
        session = AUTH.session(token)
        if user['role'] == 'doctor' and not user['must_change']:
            SYSTEM.runner.start()  # a doctor is working now: load Model 1 while they enter the case
        self.send_json({'token': token, 'user': AUTH.describe(session) if session else user,
                        **AUTH.session_fields(session), **self.model_status()})

    def totp(self, path, payload):
        """Turn on the second factor for the logged-in account: setup gives a secret, enable confirms it."""
        if not self.session:
            return self.send_json({'error': 'Chỉ tài khoản cá nhân mới bật được xác thực 2 lớp.'}, 400)
        try:
            if path.endswith('setup'):
                secret, uri = AUTH.totp_setup(self.token())
                return self.send_json({'secret': secret, 'uri': uri})
            if path.endswith('disable'):
                AUTH.totp_disable(self.token(), payload.get('password'), payload.get('code'))
                AUDIT.write('totp_disabled', self.user)
                return self.send_json({'ok': True, 'user': AUTH.describe(self.session), **AUTH.session_fields(self.session)})
            codes = AUTH.totp_enable(self.token(), payload.get('code'))
        except ValueError as exc:
            return self.send_json({'error': str(exc)}, 422)
        AUDIT.write('totp_enabled', self.user)
        self.send_json({'ok': True, 'backup_codes': codes, 'user': AUTH.describe(self.session),
                        **AUTH.session_fields(self.session)})

    def logout(self):
        AUTH.logout(self.token())
        if self.session:
            AUDIT.write('logout', self.user)
        if AUTH.mode == 'accounts' and not AUTH.active(LOGOUT_GRACE_SECONDS, GPU_ROLES):
            Thread(target=SYSTEM.runner.stop, args=('không còn bác sĩ đăng nhập',), daemon=True).start()
        # Shared hospital computers: drop anything the browser cached from this origin ("storage" would also
        # erase the saved server address, so only the cache).
        self.send_json({'ok': True}, headers={'Clear-Site-Data': '"cache"'})

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
        self.send_json({'ok': True, 'user': AUTH.describe(self.session)})

    # ------------------------------------------------------------------ admin page
    def admin_only(self):
        if self.session and self.role == 'admin' and not AUTH.needs_2fa(self.session):
            return True
        self.send_json({'error': 'Chỉ tài khoản quản trị dùng được chức năng này.'}, 403)
        return False

    def gpu_status(self):
        runner = SYSTEM.runner
        model = self.model_status()
        return {**model, 'idle_minutes': model['gpu_idle_minutes'], 'paused': runner.paused(), 'free_mib': free_vram_mib(),
                'doctors_active': AUTH.active(LOGOUT_GRACE_SECONDS, GPU_ROLES), 'pause_file': str(runner.pause_file or '')}

    def admin_get(self, path, query):
        if path == '/api/admin/users':
            def lock(name):
                until = AUTH.locked_until(name)
                return {'login_locked': bool(until),  # ISO 8601 with the offset: Date.parse reads it in every browser
                        'locked_until': datetime.fromtimestamp(until).astimezone().isoformat(timespec='seconds') if until else ''}
            users = [{**public_user(name, u), **lock(name)} for name, u in sorted(AUTH.store.all().items())]
            return self.send_json({'users': users, 'roles': ROLES})
        if path == '/api/admin/sessions':
            return self.send_json({'sessions': AUTH.list_sessions(), 'idle_minutes': round(AUTH.idle_seconds / 60),
                                   'session_hours': round(AUTH.session_seconds / 3600, 1)})
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
            if path == '/api/admin/users/totp_reset':
                AUTH.totp_reset(self.user, target)
                AUDIT.write('totp_reset', self.user, target=target)
                return self.send_json({'ok': True})
            if path == '/api/admin/users/unlock':
                if target not in AUTH.store.all():
                    raise ValueError('Không có tài khoản ' + target)
                was = AUTH.unlock(target)
                RATES['login_user'].reset('user:' + target)
                AUDIT.write('login_unlocked', self.user, target=target, was_locked=was)
                return self.send_json({'ok': True, 'was_locked': was})
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

class Server(ThreadingHTTPServer):
    """At most max_connections connections are served at once; the next ones get a short 503 and are closed,
    so a flood cannot start an unbounded number of threads."""
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, handler, max_connections=None):
        self.slots = BoundedSemaphore(max_connections or MAX_CONNECTIONS)
        body = json.dumps({'error': 'Máy chủ đang quá tải. Vui lòng thử lại sau ít giây.'}, ensure_ascii=False).encode()
        self.busy = (b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n'
                     b'Retry-After: 5\r\nConnection: close\r\nContent-Length: %d\r\n\r\n' % len(body)) + body
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(2)
                request.sendall(self.busy)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


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
    Server(('127.0.0.1', port), Handler).serve_forever()
