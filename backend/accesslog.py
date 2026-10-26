"""Minimal access log: one JSON line per HTTP request, for investigating incidents and noticing scans.

WEB_ACCESS_LOG (default ~/prenatal_pubcasefinder/.deployment/access.log, mode 600; empty or "off" disables it).
Each line: time, method, path (query string removed), status, user ("-" without a session), ms, bytes.
Never the request body, the Authorization header, a token or the query (HPO searches would leak case content).
Above 20 MB the file rotates to access.log.1 .. access.log.3 (three older files kept).
A failure to write never breaks the request.
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

DEFAULT_ACCESS_LOG = Path.home() / 'prenatal_pubcasefinder/.deployment/access.log'
MAX_BYTES = 20 * 1024 * 1024
KEEP = 3
UNSAFE = re.compile(r'[^\x21-\x7e]')  # control characters, spaces and non-ASCII in a hostile path


class AccessLog:
    def __init__(self, path=None, max_bytes=MAX_BYTES, keep=KEEP):
        value = os.getenv('WEB_ACCESS_LOG') if path is None else str(path)
        self.path = None if value in ('', 'off', '0') else Path(value or DEFAULT_ACCESS_LOG)
        self.max_bytes, self.keep = max_bytes, keep
        self.lock = threading.Lock()
        self.warned = False

    @staticmethod
    def clean_path(raw):
        path = str(raw or '').split('?', 1)[0].split('#', 1)[0]
        return UNSAFE.sub('_', path)[:200] or '-'

    def write(self, method, path, status, user='-', ms=0, size=0):
        if not self.path:
            return
        entry = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'method': UNSAFE.sub('_', str(method or '-'))[:10],
                 'path': self.clean_path(path), 'status': int(status or 0), 'user': user or '-',
                 'ms': int(ms), 'bytes': int(size or 0)}
        line = json.dumps(entry, ensure_ascii=True) + '\n'
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'a', encoding='utf-8') as f:
                    f.write(line)
        except OSError as exc:  # the log must never break a request
            if not self.warned:
                self.warned = True
                print('Access log not written:', type(exc).__name__, file=sys.stderr, flush=True)

    def _rotate(self):
        for i in range(self.keep - 1, 0, -1):
            older = self.path.with_name(f'{self.path.name}.{i}')
            if older.exists():
                os.replace(older, self.path.with_name(f'{self.path.name}.{i + 1}'))
        os.replace(self.path, self.path.with_name(self.path.name + '.1'))
