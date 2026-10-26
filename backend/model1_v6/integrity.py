"""SHA-256 check of a Model 1 bundle before any weight is loaded (no torch import: testable without a GPU).

manifest.json carries {"sha256": {relative_path: hex}} for every file of the bundle except manifest.json itself
(written by build_bundle.py; add it to an older bundle with `python -m model1_v6.build_bundle --rehash <dir>`).

MODEL1_REQUIRE_HASH:
  1 (default) - a missing "sha256" key, a missing file or a different hash stops the load (RuntimeError)
  strict      - as 1, and a file present in the bundle but not listed also stops the load
  0           - only warn on stderr (older bundles such as model1_v7_prod before --rehash)
MODEL1_MANIFEST_SHA256 (optional): expected hash of manifest.json itself, so that a changed manifest (other hashes,
another base_model_dir) is caught too. Print it with --rehash.
"""
import hashlib
import os
import sys
from pathlib import Path, PurePosixPath

MANIFEST = 'manifest.json'
CHUNK = 1 << 20


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def bundle_files(bundle_dir):
    """Relative POSIX paths of every regular file in the bundle except manifest.json, sorted."""
    root = Path(bundle_dir)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p.relative_to(root).as_posix() != MANIFEST)


def file_hashes(bundle_dir):
    root = Path(bundle_dir)
    return {rel: sha256_file(root / rel) for rel in bundle_files(root)}


def require_mode():
    mode = os.getenv('MODEL1_REQUIRE_HASH', '1').strip().lower() or '1'
    return mode if mode in ('0', '1', 'strict') else '1'


def warn(message):
    print('[Model 1] Cảnh báo toàn vẹn bundle:', message, file=sys.stderr, flush=True)


def safe_relative(rel):
    p = PurePosixPath(rel) if isinstance(rel, str) else None
    return bool(p) and not p.is_absolute() and '..' not in p.parts and '\\' not in rel and ':' not in rel


def verify_bundle(bundle_dir, manifest, mode=None):
    """Raise RuntimeError('Bundle không khớp manifest: <file>') unless every listed file exists with its hash.

    Returns {'checked': n, 'unlisted': [...], 'problems': [...]} (problems are only non-empty with mode '0')."""
    root = Path(bundle_dir)
    mode = mode or require_mode()
    problems = []

    def fail(name, why):
        if mode == '0':
            warn(f'{name}: {why} (MODEL1_REQUIRE_HASH=0, vẫn nạp)')
            problems.append(name)
        else:
            raise RuntimeError(f'Bundle không khớp manifest: {name}')

    pinned = os.getenv('MODEL1_MANIFEST_SHA256', '').strip().lower()
    if pinned and sha256_file(root / MANIFEST) != pinned:
        fail(MANIFEST, 'khác MODEL1_MANIFEST_SHA256')
    hashes = manifest.get('sha256') if isinstance(manifest, dict) else None
    if not isinstance(hashes, dict) or not hashes:
        fail(MANIFEST, 'chưa có khóa sha256; thêm bằng python -m model1_v6.build_bundle --rehash <bundle>')
        return {'checked': 0, 'unlisted': [], 'problems': problems}
    checked = 0
    for rel in sorted(hashes):
        if not safe_relative(rel) or rel == MANIFEST:
            fail(str(rel), 'đường dẫn không hợp lệ trong manifest')
            continue
        path = root / rel
        if not path.is_file():
            fail(rel, 'thiếu file')
            continue
        if sha256_file(path) != str(hashes[rel]).lower():
            fail(rel, 'SHA-256 khác manifest')
            continue
        checked += 1
    unlisted = [rel for rel in bundle_files(root) if rel not in hashes]
    if unlisted:
        if mode == 'strict':
            raise RuntimeError(f'Bundle không khớp manifest: {unlisted[0]}')
        warn(f'{len(unlisted)} file không có trong manifest (chưa được kiểm, vẫn nạp): ' + ', '.join(unlisted[:5]))
    return {'checked': checked, 'unlisted': unlisted, 'problems': problems}
