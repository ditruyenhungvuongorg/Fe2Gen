"""Model 1 process security: error messages never echo the report (B23). Runs without GPU, torch or HPO data."""
import json
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from model1_process import (GENERIC_INPUT_ERROR, KNOWN_INPUT_ERRORS, Model1Process, ModelProcessError, safe_input_error)

BASE = Path(__file__).resolve().parent
SECRET = 'BÍ-MẬT-123'
CHILD = [sys.executable, '-c', 'import sys, model1_process, test_model_integrity; '
         'sys.exit(model1_process.worker_main(test_model_integrity.SecretRunner))']
# An older child that forwards raw exception text: the parent must filter it as well.
RAW_CHILD = [sys.executable, '-c', 'import json, sys\n'
             'print(json.dumps({"event": "ready"}), flush=True)\n'
             'for line in sys.stdin:\n'
             '    r = json.loads(line)\n'
             '    print(json.dumps({"id": r["id"], "ok": False, "error_type": r["text"], "message": "leak BÍ-MẬT-123"}), flush=True)\n']


class SecretRunner:
    """Raises errors whose text quotes the report, as contract.align would with model output."""
    manifest = {'model_version': 'vtest'}

    def load_model(self):
        pass

    def extract(self, text):
        if 'known' in text:
            raise ValueError('Đoạn quá dài cho một lần trích.')
        if 'runtime' in text:
            raise RuntimeError(f'CUDA error near "{text}"')
        if 'other' in text:
            raise KeyError(text)
        if 'warn' in text:
            return {'findings': [], 'errors': [f'Unknown assertion: {text!r}', 'all ensemble members invalid'], 'seconds': 0.0}
        raise ValueError(f'Finding text absent from report: "{text}" ({SECRET})')


def model(**kw):
    kw.setdefault('pause_file', None)
    kw.setdefault('free_mib', lambda: None)
    kw.setdefault('command', CHILD)
    return Model1Process('v6', Path('/bundle'), **kw)


class ErrorMessageTests(unittest.TestCase):
    """B23: only fixed, known ValueError messages reach the doctor; the report never reaches stderr."""

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

    def test_known_list_is_fixed_text(self):
        self.assertIn('Đoạn quá dài cho một lần trích.', KNOWN_INPUT_ERRORS)
        self.assertIn('Nhập đoạn mô tả từ 1 đến 6.000 ký tự.', KNOWN_INPUT_ERRORS)
        self.assertFalse([m for m in KNOWN_INPUT_ERRORS if m.rstrip().endswith(':')])  # no "prefix: <data>" messages
        self.assertEqual(safe_input_error('Duplicate JSON key: ' + SECRET), GENERIC_INPUT_ERROR)
        self.assertEqual(safe_input_error(None), GENERIC_INPUT_ERROR)

    def test_worker_replies_and_stderr_do_not_contain_the_report(self):
        requests = [{'id': i, 'text': f'Thai {SECRET} {kind}'} for i, kind in enumerate(['', 'known', 'runtime', 'other'])]
        out = subprocess.run(CHILD, cwd=BASE, input=''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in requests),
                             capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        replies = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(replies[0]['event'], 'ready')
        by_id = {r['id']: r for r in replies[1:]}
        self.assertEqual(by_id[0], {'id': 0, 'ok': False, 'error_type': 'ValueError', 'message': GENERIC_INPUT_ERROR})
        self.assertEqual(by_id[1]['message'], 'Đoạn quá dài cho một lần trích.')  # known message passes verbatim
        self.assertEqual(by_id[2]['error_type'], 'RuntimeError')
        self.assertEqual(by_id[3]['error_type'], 'Exception')
        self.assertIn('KeyError', out.stderr)  # the class name is logged ...
        for text in (out.stdout, out.stderr):
            self.assertNotIn(SECRET, text)  # ... never the message or the request
            self.assertNotIn(SECRET.encode('unicode_escape').decode(), text)

    def test_parent_raises_only_safe_messages(self):
        m = self.make()
        with self.assertRaises(ValueError) as caught:
            m.extract(f'Thai {SECRET}')
        self.assertEqual(str(caught.exception), GENERIC_INPUT_ERROR)
        with self.assertRaisesRegex(ValueError, '^Đoạn quá dài cho một lần trích.$'):
            m.extract('known')
        with self.assertRaises(RuntimeError) as caught:
            m.extract(f'runtime {SECRET}')
        self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(ModelProcessError) as caught:
            m.extract(f'other {SECRET}')
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SECRET, self.log.getvalue())

    def test_parent_filters_an_older_child(self):
        m = self.make(command=RAW_CHILD)
        for kind, error in (('ValueError', ValueError), ('RuntimeError', RuntimeError), ('Other', ModelProcessError)):
            with self.assertRaises(error) as caught:
                m.extract(kind)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_web_service_filters_in_process_runner_and_warnings(self):
        from web_service import WebSystem
        system = types.SimpleNamespace(runner=SecretRunner())
        with self.assertRaises(ValueError) as caught:
            WebSystem.extract_v6(system, f'Thai {SECRET}')
        self.assertEqual(str(caught.exception), GENERIC_INPUT_ERROR)
        with self.assertRaisesRegex(ValueError, '^Đoạn quá dài cho một lần trích.$'):
            WebSystem.extract_v6(system, 'known')
        warnings = WebSystem.extract_v6(system, f'warn {SECRET}')['warnings']
        self.assertEqual(warnings, [GENERIC_INPUT_ERROR, 'all ensemble members invalid'])
        v38 = types.SimpleNamespace(runner=types.SimpleNamespace(extract_spans=SecretRunner().extract))
        with self.assertRaises(ValueError) as caught:
            WebSystem.extract(v38, f'Thai {SECRET}')
        self.assertEqual(str(caught.exception), GENERIC_INPUT_ERROR)


if __name__ == '__main__':
    unittest.main()
