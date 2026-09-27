"""Check that the web runner reproduces the evaluated v6 pipeline (use an --source eval bundle).

Runs the runner on the held-out test reports, then scores with the research package's own scorer and compares the
extracted findings with the v6 run's saved predictions. Research data stays on the server.

  python -m model1_v6.verify_on_test --bundle <eval bundle> --research ~/model1_improve_ubuntu_v5 \
      --v6-final ~/model1_improve_ubuntu_v5/runs/v6_20260926T214022Z/final
"""
import argparse
import json
import sys
import time
from pathlib import Path

from .runner import Model1V6Runner


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--research', type=Path, required=True)
    p.add_argument('--v6-final', type=Path, required=True)
    a = p.parse_args()
    research = a.research.expanduser()
    sys.path.insert(0, str(research))
    from core import aggregate, read, score_case  # research scorer (same as the reported numbers)
    test = read(research / 'data/test.jsonl')
    ref = {x['sample_id']: x for x in json.loads((a.v6_final.expanduser() / 'test_extraction_final.json').read_text(encoding='utf-8'))}
    runner = Model1V6Runner(a.bundle.expanduser())
    t0 = time.monotonic(); runner.load_model(); load_s = time.monotonic() - t0
    e2e, ext, same, seconds = [], [], 0, []
    for r in test:
        out = runner.extract(r['text'])
        seconds.append(out['seconds'])
        preds = [dict(f, hpo_id=f['hpo_ranked'][0]['id']) for f in out['findings']]
        err = '; '.join(out['errors']) or None if not preds else None
        e2e.append(score_case(r, preds, 'span_assertion_hpo', err))
        ext.append(score_case(r, preds, 'span_assertion', err))
        key = lambda fs: sorted((f['span_start'], f['span_end'], f['assertion']) for f in fs)
        same += key(out['findings']) == key(ref[r['sample_id']]['predicted'])
    m, x = aggregate(e2e, bootstrap=0), aggregate(ext, bootstrap=0)
    report = dict(cases=len(test), identical_extraction_to_v6_run=same, load_seconds=round(load_s, 1),
                  seconds_per_report=dict(mean=round(sum(seconds) / len(seconds), 1), max=max(seconds)),
                  e2e_strict={k: round(m[k], 4) for k in ('precision', 'recall', 'f1', 'exact_match_rate')},
                  extraction={k: round(x[k], 4) for k in ('precision', 'recall', 'f1', 'exact_match_rate')},
                  reported_v6_test=dict(e2e_f1=0.6197, extraction_f1=0.7042))
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
