"""Model 1 in its own process, started on demand and stopped when idle.

The web process never imports torch or touches CUDA. Model 1 (v6/v7 bundle or v3.8 worker) loads in a child
process started when a doctor logs in, opens text extraction or sends a report; after MODEL1_IDLE_MINUTES
without activity, or while the pause file exists (training, local AI), the child exits and the driver gets
back all of its VRAM (about 12.4 GB for model1_v7_prod). Stopping a process frees memory that
torch.cuda.empty_cache() inside a long-lived process keeps (CUDA context, allocator pools).

Protocol: JSON lines. The parent writes {"id", "text"} to the child's stdin; the child answers
{"id", "ok", "result"} or {"id", "ok": false, "error_type", "message"} on a private copy of its stdout, after
an {"event": "ready"} or {"event": "failed"} line. The child's own stdout goes to stderr (journal), so library
banners cannot break the protocol. The child exits when stdin closes, also if the web process dies.

Error messages cross to the doctor only when they are fixed strings listed below: a message built from model
output or exception text may quote part of the report. The child never writes request text to stderr.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PAUSE_FILE = Path.home() / 'prenatal_pubcasefinder/.deployment/gpu_pause'


# Fixed ValueError messages that may reach the doctor (grep "raise ValueError" in model1_v6/, model1_v38_package/ and
# web_service.py). Messages that append data (a JSON key, an assertion value, a gene symbol, a sample id) are left out
# on purpose: that data comes from the report or the model output.
KNOWN_INPUT_ERRORS = frozenset({
    # web_service.py
    'Nhập đoạn mô tả từ 1 đến 6.000 ký tự.',
    'Danh sách gene WES không hợp lệ (tối đa 1.000 gene).',
    'Chọn từ 1 đến 100 HPO đã duyệt.',
    'HPO không hợp lệ.',
    'Mã HPO, trạng thái không hợp lệ hoặc bị trùng.',
    'Cần ít nhất một HPO Có hoặc Nghi ngờ để đối chiếu.',
    # model1_v6/runner.py
    'Đoạn quá dài cho một lần trích.',
    # model1_v6/contract.py
    'Expected exactly one findings list',
    'Too many findings',
    'Each finding needs exactly text and assertion',
    'Finding text must be a nonempty trimmed string',
    'Finding text absent from report or cuts a word',
    'Ambiguous repeated occurrences',
    'Overlapping or out-of-order findings',
    'not a findings list',
    'bad item',
    'finding text absent or out of order',
    'all ensemble members invalid',  # contract.vote error, returned as a warning
    # model1_v38_package/core.py
    'Expected exactly one phrases field',
    'phrases must be a list with at most 40 entries',
    'Every phrase must be a nonempty exact string',
    'Phrase absent from source or partial word',
    'Overlapping or out-of-order phrases',
    'Empty text or external whitespace',
})
KNOWN_RUNTIME_ERRORS = frozenset({'Model đang bận.'})
GENERIC_INPUT_ERROR = 'Đoạn văn không xử lý được; kiểm tra lại nội dung.'
GENERIC_RUNTIME_ERROR = 'Model gặp lỗi khi xử lý.'


def safe_input_error(message):
    """The message itself when it is a known fixed input error, else a generic one (never echoes report text)."""
    return message if isinstance(message, str) and message in KNOWN_INPUT_ERRORS else GENERIC_INPUT_ERROR


def safe_runtime_error(message):
    return message if isinstance(message, str) and message in KNOWN_RUNTIME_ERRORS else GENERIC_RUNTIME_ERROR


class ModelUnavailable(RuntimeError):
    """Model 1 cannot serve now; the message is shown to the doctor."""


class ModelProcessError(Exception):
    """Unexpected error inside the model process."""


def env_number(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def free_vram_mib():
    """Free memory of the first visible GPU from nvidia-smi, or None when it cannot be read."""
    index = os.getenv('CUDA_VISIBLE_DEVICES', '0').split(',')[0].strip() or '0'
    try:
        out = subprocess.run(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits', '-i', index],
                             capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip().splitlines()[0]) if out.returncode == 0 else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


class Model1Process:
    def __init__(self, kind, adapter_path, idle_seconds=3600, load_wait=120, request_seconds=300,
                 min_free_mib=13000, pause_file=None, command=None, free_mib=free_vram_mib):
        self.kind = kind                    # 'v6' (v6/v7 bundle) or 'v38'
        self.adapter_path = adapter_path    # configured bundle/adapter, for /api/status
        self.idle_seconds = idle_seconds
        self.load_wait = load_wait
        self.request_seconds = request_seconds
        self.min_free_mib = min_free_mib
        self.pause_file = Path(pause_file) if pause_file else None
        self.command = command or [sys.executable, '-m', 'model1_process']
        self.free_mib = free_mib
        self.cond = threading.Condition()
        self.call_lock = threading.Lock()
        self.proc = self.responses = None
        self.state, self.error, self.manifest = 'stopped', '', {}
        self.load_seconds = None
        self.last_used = time.monotonic()
        self.next_id = 0
        self.busy = False
        threading.Thread(target=self._watch, daemon=True).start()

    @classmethod
    def from_env(cls):
        bundle = os.getenv('MODEL1_V6_BUNDLE')
        adapter = bundle or os.getenv('MODEL1_ADAPTER')
        return cls('v6' if bundle else 'v38', Path(adapter) if adapter else None,
                   idle_seconds=env_number('MODEL1_IDLE_MINUTES', 60) * 60,
                   load_wait=env_number('MODEL1_LOAD_WAIT_SECONDS', 120),
                   min_free_mib=env_number('MODEL1_MIN_FREE_MIB', 13000),
                   pause_file=os.getenv('MODEL1_PAUSE_FILE') or DEFAULT_PAUSE_FILE)

    # ------------------------------------------------------------------ state
    @property
    def is_loaded(self):
        return self.state == 'ready'

    def paused(self):
        return bool(self.pause_file and self.pause_file.exists())

    def touch(self):
        """Doctor activity: postpones the idle stop (does not start the model)."""
        self.last_used = time.monotonic()

    def status(self):
        with self.cond:
            state = 'paused' if self.paused() and self.state in ('stopped', 'failed') else self.state
            return {'model_state': state, 'model_ready': state == 'ready', 'model_error': self.error if state == 'failed' else '',
                    'model_load_seconds': self.load_seconds, 'idle_minutes': round(self.idle_seconds / 60)}

    def start(self):
        """Start loading in the background unless already loading/ready; returns the state."""
        self.touch()
        with self.cond:
            if self.state in ('loading', 'ready'):
                return self.state
            if not self.adapter_path:
                self.state, self.error = 'failed', 'Model trích văn bản chưa được cấu hình.'
                return self.state
            if self.paused():
                self.state, self.error = 'stopped', ''
                return 'paused'
            free = self.free_mib() if self.min_free_mib > 0 else None
            if free is not None and free < self.min_free_mib:
                self.state = 'failed'
                self.error = (f'GPU chỉ còn {free:,} MiB trống, Model 1 cần khoảng {int(self.min_free_mib):,} MiB. '
                              'GPU có thể đang dùng để train hoặc chạy AI cục bộ; thử lại sau.')
                print('[Model 1] Không khởi động:', self.error, file=sys.stderr, flush=True)
                return self.state
            try:
                proc = subprocess.Popen(self.command, cwd=str(BASE_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, encoding='utf-8', bufsize=1)
            except OSError as exc:
                self.state, self.error = 'failed', f'Không chạy được tiến trình model: {exc}'
                return self.state
            self.proc, self.responses = proc, queue.Queue()
            self.state, self.error, self.started = 'loading', '', time.monotonic()
            print('[Model 1] Đang nạp model trong tiến trình', proc.pid, file=sys.stderr, flush=True)
            threading.Thread(target=self._read, args=(proc, self.responses), daemon=True).start()
            return self.state

    def wait_ready(self, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.state != 'loading', timeout)
            return self.state == 'ready'

    def ensure_ready(self, timeout=None):
        """Start if needed and wait for the model; raises ModelUnavailable with a message for the doctor."""
        state = self.start()
        if state == 'paused':
            raise ModelUnavailable('GPU đang tạm dành cho việc khác (train/AI cục bộ). Tra cứu và đối chiếu vẫn dùng được.')
        if not self.wait_ready(self.load_wait if timeout is None else timeout):
            with self.cond:
                if self.state == 'loading':
                    raise ModelUnavailable('Model đang khởi động. Vui lòng thử lại sau ít giây.')
                if self.state == 'stopped' and self.paused():
                    raise ModelUnavailable('GPU đang tạm dành cho việc khác (train/AI cục bộ). Tra cứu và đối chiếu vẫn dùng được.')
                raise ModelUnavailable(self.error or 'Model chưa sẵn sàng. Liên hệ quản trị viên.')

    def load_model(self):
        """Blocking load (MODEL1_PRELOAD=1); RuntimeError when the model cannot serve."""
        self.ensure_ready(timeout=max(self.load_wait, 600))

    def stop(self, reason=''):
        with self.cond:
            proc, self.proc = self.proc, None
            if self.state in ('loading', 'ready'):
                self.state = 'stopped'
            self.cond.notify_all()
        if not proc:
            return
        print('[Model 1] Giải phóng GPU' + (f' ({reason})' if reason else ''), file=sys.stderr, flush=True)
        try:
            proc.stdin.close()  # the child leaves its request loop and exits
        except OSError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    # ------------------------------------------------------------------ requests
    def extract(self, text):
        return self.call(text)

    def extract_spans(self, text):
        return self.call(text)

    def call(self, text):
        self.ensure_ready()
        with self.call_lock:
            with self.cond:
                proc, responses = self.proc, self.responses
                if self.state != 'ready' or proc is None:
                    raise ModelUnavailable('Model vừa được giải phóng. Vui lòng thử lại.')
                self.next_id += 1
                rid = self.next_id
                self.busy = True
            try:
                try:
                    proc.stdin.write(json.dumps({'id': rid, 'text': text}, ensure_ascii=False) + '\n')
                    proc.stdin.flush()
                except (OSError, ValueError):
                    raise ModelUnavailable('Tiến trình model đã dừng. Vui lòng thử lại.')
                deadline = time.monotonic() + self.request_seconds
                while True:
                    try:
                        msg = responses.get(timeout=max(0.1, deadline - time.monotonic()))
                    except queue.Empty:
                        self.stop('quá thời gian xử lý')
                        raise ModelUnavailable('Model xử lý quá lâu và đã được khởi động lại. Vui lòng thử lại.')
                    if msg is None:
                        raise ModelUnavailable('Tiến trình model đã dừng. Vui lòng thử lại.')
                    if msg.get('id') == rid:
                        break
            finally:
                self.busy = False
                self.touch()
        if msg.get('ok'):
            return msg['result']
        kind = msg.get('error_type')  # filtered again here: the child may be an older build
        if kind == 'ValueError':
            raise ValueError(safe_input_error(msg.get('message')))
        if kind == 'RuntimeError':
            raise RuntimeError(safe_runtime_error(msg.get('message')))
        raise ModelProcessError(GENERIC_RUNTIME_ERROR)

    # ------------------------------------------------------------------ threads
    def _read(self, proc, responses):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            event = msg.get('event')
            if event:
                with self.cond:
                    if self.proc is proc:
                        if event == 'ready':
                            self.state, self.manifest = 'ready', msg.get('manifest') or {}
                            self.load_seconds = round(time.monotonic() - self.started, 1)
                            print(f'[Model 1] Sẵn sàng sau {self.load_seconds} giây', file=sys.stderr, flush=True)
                        elif event == 'failed':
                            self.state, self.error = 'failed', msg.get('message') or 'Model không nạp được.'
                        self.cond.notify_all()
            else:
                responses.put(msg)
        code = proc.wait()
        responses.put(None)
        with self.cond:
            if self.proc is proc:  # exited on its own (crash, CUDA error), not through stop()
                self.proc = None
                if self.state != 'failed':
                    self.state, self.error = 'failed', f'Tiến trình model dừng bất thường (mã {code}).'
                print('[Model 1]', self.error, file=sys.stderr, flush=True)
            self.cond.notify_all()

    def _watch(self, every=10):
        while True:
            time.sleep(every)
            self.check_idle()

    def check_idle(self):
        if self.proc is None:
            return
        if self.paused():
            self.stop('GPU tạm dành cho việc khác')
        elif self.state == 'ready' and not self.busy and time.monotonic() - self.last_used > self.idle_seconds:
            self.stop(f'không dùng {round(self.idle_seconds / 60)} phút')


# ---------------------------------------------------------------------- child process
def build_runner():
    bundle = os.getenv('MODEL1_V6_BUNDLE')
    if bundle:
        from model1_v6.runner import Model1V6Runner
        return Model1V6Runner(bundle)
    from model1_v38_package.model1_runner import Model1V38Runner
    return Model1V38Runner(os.getenv('MODEL1_ADAPTER'), os.getenv('MODEL1_WORKER_DIR'))


def worker_main(runner_factory=build_runner):
    proto = os.fdopen(os.dup(1), 'w', encoding='utf-8', buffering=1)
    os.dup2(2, 1)  # anything printed by libraries goes to stderr
    sys.stdin.reconfigure(encoding='utf-8')  # systemd may start the service without a UTF-8 locale

    def send(msg):
        proto.write(json.dumps(msg, ensure_ascii=False) + '\n')
        proto.flush()

    t0 = time.monotonic()
    try:
        runner = runner_factory()
        runner.load_model()
    except Exception as exc:
        print('[Model 1] Nạp model thất bại:', type(exc).__name__, exc, file=sys.stderr, flush=True)
        send({'event': 'failed', 'message': str(exc) if isinstance(exc, RuntimeError) else
              f'Model không nạp được ({type(exc).__name__}). Kiểm tra nvidia-smi và nhật ký dịch vụ.'})
        return 1
    version = (getattr(runner, 'manifest', None) or {}).get('model_version')
    print(f'[Model 1] Nạp xong trong {time.monotonic() - t0:.1f} giây', file=sys.stderr, flush=True)
    send({'event': 'ready', 'manifest': {'model_version': version} if version else {}})
    extract = runner.extract if hasattr(runner, 'extract') else runner.extract_spans
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        try:
            send({'id': request.get('id'), 'ok': True, 'result': extract(request.get('text'))})
        except Exception as exc:  # the class name only: str(exc) may quote the report
            if isinstance(exc, ValueError):
                kind, message = 'ValueError', safe_input_error(str(exc))
            elif isinstance(exc, RuntimeError):
                kind, message = 'RuntimeError', safe_runtime_error(str(exc))
            else:
                kind, message = 'Exception', GENERIC_RUNTIME_ERROR
            if kind != 'ValueError':
                print('[Model 1] Lỗi xử lý:', type(exc).__name__, file=sys.stderr, flush=True)
            send({'id': request.get('id'), 'ok': False, 'error_type': kind, 'message': message})
    return 0


if __name__ == '__main__':
    sys.exit(worker_main())
