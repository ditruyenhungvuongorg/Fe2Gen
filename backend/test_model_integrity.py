"""Model 1 process security: error messages never echo the report (B23), bundle SHA-256 check before loading (B22).
Runs without GPU, torch or HPO data."""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from model1_process import (GENERIC_INPUT_ERROR, KNOWN_INPUT_ERRORS, Model1Process, ModelProcessError, safe_input_error)
from model1_v6.build_bundle import write_manifest
from model1_v6.integrity import file_hashes, sha256_file, verify_bundle

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


def make_bundle(root):
    root = Path(root)
    (root / 'adapters/a').mkdir(parents=True)
    (root / 'tokenizer').mkdir()
    (root / 'adapters/a/adapter_model.safetensors').write_bytes(b'\x00weights' * 100)
    (root / 'adapters/a/adapter_config.json').write_text('{"r": 16, "lora_alpha": 32}', encoding='utf-8')
    (root / 'tokenizer/tokenizer.json').write_text('{}', encoding='utf-8')
    manifest = {'schema': 'fe2gen-model1-v6-bundle', 'base_model_dir': '/base', 'adapters': {'a': 'adapters/a'},
                'extractor': {'members': [], 'need': 3, 'extend': True}}
    (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return write_manifest(root, manifest)


class BundleIntegrityTests(unittest.TestCase):
    """B22: every bundle file is checked against manifest.json before weights load."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.manifest = make_bundle(self.dir)
        self.env = patch.dict(os.environ, {'MODEL1_REQUIRE_HASH': '1', 'MODEL1_MANIFEST_SHA256': ''})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def stored(self):
        return json.loads((self.dir / 'manifest.json').read_text(encoding='utf-8'))

    def test_build_writes_every_file_but_the_manifest(self):
        self.assertEqual(sorted(self.stored()['sha256']), ['adapters/a/adapter_config.json',
                                                           'adapters/a/adapter_model.safetensors', 'tokenizer/tokenizer.json'])
        self.assertEqual(self.stored()['sha256']['tokenizer/tokenizer.json'], sha256_file(self.dir / 'tokenizer/tokenizer.json'))
        self.assertEqual(self.stored()['base_model_dir'], '/base')
        self.assertFalse(list(self.dir.glob('.manifest.*')))  # no temporary file left

    def test_matching_bundle_passes(self):
        self.assertEqual(verify_bundle(self.dir, self.stored())['checked'], 3)

    def test_one_changed_byte_names_the_file(self):
        path = self.dir / 'adapters/a/adapter_model.safetensors'
        data = bytearray(path.read_bytes())
        data[3] ^= 1
        path.write_bytes(bytes(data))
        with self.assertRaises(RuntimeError) as caught:
            verify_bundle(self.dir, self.stored())
        self.assertEqual(str(caught.exception), 'Bundle không khớp manifest: adapters/a/adapter_model.safetensors')

    def test_missing_file_and_bad_path(self):
        (self.dir / 'tokenizer/tokenizer.json').unlink()
        with self.assertRaisesRegex(RuntimeError, 'không khớp manifest: tokenizer/tokenizer.json$'):
            verify_bundle(self.dir, self.stored())
        with self.assertRaisesRegex(RuntimeError, r'không khớp manifest: \.\./outside$'):
            verify_bundle(self.dir, {'sha256': {'../outside': '0' * 64}})

    def test_missing_hash_key_depends_on_mode(self):
        old = {k: v for k, v in self.stored().items() if k != 'sha256'}
        with self.assertRaisesRegex(RuntimeError, 'không khớp manifest: manifest.json'):
            verify_bundle(self.dir, old)
        log = StringIO()
        with patch.dict(os.environ, {'MODEL1_REQUIRE_HASH': '0'}), redirect_stderr(log):
            self.assertEqual(verify_bundle(self.dir, old)['problems'], ['manifest.json'])
            (self.dir / 'tokenizer/tokenizer.json').write_text('{"changed": 1}', encoding='utf-8')
            self.assertEqual(verify_bundle(self.dir, self.stored())['problems'], ['tokenizer/tokenizer.json'])
        self.assertIn('chưa có khóa sha256', log.getvalue())
        self.assertIn('tokenizer/tokenizer.json', log.getvalue())

    def test_unlisted_file_warns_or_stops_in_strict_mode(self):
        (self.dir / 'tokenizer/extra.py').write_text('print(1)', encoding='utf-8')
        log = StringIO()
        with redirect_stderr(log):
            self.assertEqual(verify_bundle(self.dir, self.stored())['unlisted'], ['tokenizer/extra.py'])
        self.assertIn('tokenizer/extra.py', log.getvalue())
        with patch.dict(os.environ, {'MODEL1_REQUIRE_HASH': 'strict'}):
            with self.assertRaisesRegex(RuntimeError, 'không khớp manifest: tokenizer/extra.py'):
                verify_bundle(self.dir, self.stored())

    def test_pinned_manifest_hash(self):
        good = sha256_file(self.dir / 'manifest.json')
        with patch.dict(os.environ, {'MODEL1_MANIFEST_SHA256': good}):
            verify_bundle(self.dir, self.stored())
        with patch.dict(os.environ, {'MODEL1_MANIFEST_SHA256': '0' * 64}):
            with self.assertRaisesRegex(RuntimeError, 'không khớp manifest: manifest.json'):
                verify_bundle(self.dir, self.stored())

    def test_rehash_command_adds_hashes_to_an_old_bundle(self):
        old = {k: v for k, v in self.stored().items() if k != 'sha256'}
        (self.dir / 'manifest.json').write_text(json.dumps(old), encoding='utf-8')
        out = subprocess.run([sys.executable, '-m', 'model1_v6.build_bundle', '--rehash', str(self.dir)], cwd=BASE,
                             capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.stored()['sha256'], file_hashes(self.dir))
        self.assertEqual(self.stored()['adapters'], {'a': 'adapters/a'})
        self.assertEqual(json.loads(out.stdout)['MODEL1_MANIFEST_SHA256'], sha256_file(self.dir / 'manifest.json'))
        verify_bundle(self.dir, self.stored())

    def test_runner_checks_before_loading_weights(self):
        from model1_v6.runner import Model1V6Runner

        class WeightsRead(Exception):
            pass
        unsloth = types.ModuleType('unsloth')
        unsloth.FastLanguageModel = MagicMock()
        unsloth.FastLanguageModel.from_pretrained.side_effect = WeightsRead
        torch = types.ModuleType('torch')
        torch.cuda = types.SimpleNamespace(is_available=lambda: True)
        torch.bfloat16 = 'bf16'
        transformers = types.ModuleType('transformers')
        transformers.AutoModel = transformers.AutoTokenizer = MagicMock()
        peft = types.ModuleType('peft')
        peft.get_peft_model_state_dict = MagicMock()
        save_and_load = types.ModuleType('peft.utils.save_and_load')
        save_and_load.load_peft_weights = MagicMock()
        fake = {'unsloth': unsloth, 'torch': torch, 'transformers': transformers, 'peft': peft,
                'peft.utils': types.ModuleType('peft.utils'), 'peft.utils.save_and_load': save_and_load}
        with patch.dict(sys.modules, fake), redirect_stderr(StringIO()):
            with self.assertRaises(WeightsRead):  # intact bundle: the check passes and loading starts
                Model1V6Runner(self.dir).load_model()
            unsloth.FastLanguageModel.from_pretrained.reset_mock()
            (self.dir / 'adapters/a/adapter_config.json').write_text('{"r": 32}', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'không khớp manifest: adapters/a/adapter_config.json'):
                Model1V6Runner(self.dir).load_model()
            unsloth.FastLanguageModel.from_pretrained.assert_not_called()
            save_and_load.load_peft_weights.assert_not_called()


if __name__ == '__main__':
    unittest.main()
