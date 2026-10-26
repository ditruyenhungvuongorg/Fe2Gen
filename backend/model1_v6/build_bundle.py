"""Assemble a Model 1 v6 bundle on the GPU server (never committed: weights + doctor-reviewed memory).

  --source prod : models retrained on all 226 reviewed cases (for serving)
  --source eval : the exact v6 models evaluated on the held-out test (to verify this runner reproduces the reported numbers)

Usage (Ubuntu):
  python -m model1_v6.build_bundle --source prod --research ~/model1_improve_ubuntu_v5 \
      --prod-run ~/model1_improve_ubuntu_v5/runs/prod_all226_20260926T224213Z --out ~/fe2gen_bundles/model1_v6_prod

manifest.json lists the SHA-256 of every other bundle file; the runner checks them before loading (model1_v6/integrity.py).
Add or refresh the hashes of an existing bundle (e.g. model1_v7_prod built before 26/10/2026) after checking its files:
  python -m model1_v6.build_bundle --rehash ~/fe2gen_bundles/model1_v7_prod
"""
import argparse
import collections
import json
import os
import shutil
import tempfile
from pathlib import Path

from .contract import normalize_query
from .integrity import MANIFEST, file_hashes, sha256_file

MEMBERS = [['A_r16_lr1e-4', [4, 5, 6]], ['B_r32_lr2e-4', [4, 5, 6]], ['C_r16_lr1e-4_syn', [3, 4, 5]],
           ['C_seed2', [3, 4, 5]], ['C_seed3', [3, 4, 5]]]


def read(path):
    return [json.loads(s) for s in Path(path).read_text(encoding='utf-8').splitlines() if s.strip()]


def copy_adapter(src, dst):
    dst.mkdir(parents=True, exist_ok=True)
    for name in ('adapter_config.json', 'adapter_model.safetensors'):
        shutil.copy2(src / name, dst / name)


def write_manifest(bundle, manifest):
    """manifest.json with the SHA-256 of every other bundle file, written atomically (same mode as before)."""
    bundle = Path(bundle)
    manifest = dict(manifest, sha256=file_hashes(bundle))
    target = bundle / MANIFEST
    fd, tmp = tempfile.mkstemp(dir=bundle, prefix='.manifest.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(manifest, ensure_ascii=False, indent=2))
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return manifest


def rehash(bundle):
    bundle = Path(bundle).expanduser()
    manifest = json.loads((bundle / MANIFEST).read_text(encoding='utf-8'))
    manifest = write_manifest(bundle, manifest)
    print(json.dumps({'bundle': str(bundle), 'files': len(manifest['sha256']),
                      'MODEL1_MANIFEST_SHA256': sha256_file(bundle / MANIFEST)}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--rehash', type=Path, metavar='BUNDLE',
                   help='only (re)write the sha256 table of an existing bundle; check its files first')
    p.add_argument('--source', choices=['prod', 'eval'])
    p.add_argument('--research', type=Path, help='model1_improve_ubuntu_v5 package (data/, tokenizer/)')
    p.add_argument('--prod-run', type=Path)
    p.add_argument('--v2run', type=Path)
    p.add_argument('--v3run', type=Path)
    p.add_argument('--v4run', type=Path)
    p.add_argument('--out', type=Path)
    a = p.parse_args()
    if a.rehash:
        return rehash(a.rehash)
    if not (a.source and a.research and a.out):
        p.error('--source, --research and --out are required (or use --rehash BUNDLE)')
    out = a.out.expanduser()
    if out.exists():
        raise SystemExit(f'{out} already exists; choose a new directory')
    from huggingface_hub import snapshot_download
    base = snapshot_download('unsloth/Qwen3.5-4B', revision='3764fa359b9082ea5a1e4a5e3ac3aaf6e9671636', local_files_only=True)
    research = a.research.expanduser()
    dev = read(research / 'data/dev.jsonl')
    test = read(research / 'data/test.jsonl')
    adapters = {}
    if a.source == 'prod':
        run = a.prod_run.expanduser()
        card = json.loads((run / 'MODEL_CARD.json').read_text(encoding='utf-8'))
        src = {c: run / 'extractors' / c for c, _ in MEMBERS}
        reranker, encoder, reviewed = run / 'reranker' / f'epoch{card["reranker_epochs"]}', run / 'retriever/encoder', dev + test
    else:
        v2, v3, v4 = (x.expanduser() for x in (a.v2run, a.v3run, a.v4run))
        src = {'A_r16_lr1e-4': v3 / 'final/extractors/A_r16_lr1e-4', 'B_r32_lr2e-4': v3 / 'final/extractors/B_r32_lr2e-4',
               'C_r16_lr1e-4_syn': v4 / 'final/extractors/C_r16_lr1e-4_syn', 'C_seed2': v4 / 'final/extractors/C_seed2',
               'C_seed3': v4 / 'final/extractors/C_seed3'}
        chk = json.loads((v3 / 'choice_reranker_v3.json').read_text())
        reranker, encoder, reviewed = v3 / 'final/reranker' / f'epoch{chk["epoch"]}', v2 / 'final/retriever/encoder', dev
    for config, epochs in MEMBERS:
        for e in epochs:
            name = f'{config}__epoch{e}'
            copy_adapter(src[config] / f'epoch{e}', out / 'adapters' / name)
            adapters[name] = f'adapters/{name}'
    copy_adapter(reranker, out / 'adapters/reranker')
    adapters['reranker'] = 'adapters/reranker'
    shutil.copytree(encoder, out / 'retriever')
    shutil.copytree(research / 'tokenizer', out / 'tokenizer')
    shutil.copy2(research / 'data/hpo_catalog.jsonl', out / 'hpo_catalog.jsonl')
    memory = [dict(query_text=normalize_query(m['mention_text']), hpo_id=m['hpo_id']) for r in reviewed for m in r['findings']]
    (out / 'memory.jsonl').write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in memory), encoding='utf-8')
    usage = collections.Counter(m['hpo_id'] for r in reviewed for m in r['findings'])
    (out / 'usage.json').write_text(json.dumps(usage, ensure_ascii=False), encoding='utf-8')
    manifest = dict(schema='fe2gen-model1-v6-bundle', source=a.source, base_model_dir=base,
                    extractor=dict(members=MEMBERS, need=3, extend=True), adapters=adapters, reranker_adapter='reranker',
                    retriever=dict(mode='memory', max_length=96, top_k_candidates=10), chunk_chars=1200,
                    reviewed_cases=len(reviewed), memory_findings=len(memory),
                    note='Private: contains doctor-reviewed findings (memory.jsonl, usage.json). Do not publish.')
    manifest = write_manifest(out, manifest)
    print(json.dumps({**{k: manifest[k] for k in ('source', 'reviewed_cases', 'memory_findings')}, 'files': len(manifest['sha256']),
                      'MODEL1_MANIFEST_SHA256': sha256_file(out / MANIFEST)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
