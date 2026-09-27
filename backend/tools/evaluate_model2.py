"""Measure Model 2 as the web shows it: overall ranks and the three mechanism columns.

Input: JSONL cases with ``observations`` [{hpo_id, status}], ``gold_disease_ids``
and optionally ``gold_gene_ids`` (the prenatal validation format).  A disease hit
counts OMIM/ORPHA IDs of the same MONDO concept as one disease, in both modes.

Modes:
* ``web``        the matcher exactly as web_service builds it today.
* ``web_mondo``  the same matcher with the MONDO OMIM/ORPHA merge enabled.

Usage (from backend/):
    python tools/evaluate_model2.py --cases <cases.jsonl> --xref data/disease_xref_mondo_20260917.tsv --out <dir>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hpo_agents.agent2_matcher import HPOAgent2Matcher  # noqa: E402
from hpo_agents.agent2_schema import (  # noqa: E402
    PhenotypeObservation, PhenotypeStatus, RankingStrategy, load_disease_xref,
)
from web_service import BASE_DIR, GROUPS, WebSystem  # noqa: E402

STATUS = {"present": PhenotypeStatus.PRESENT, "suspected": PhenotypeStatus.SUSPECTED,
          "absent": PhenotypeStatus.ABSENT, "CÓ": PhenotypeStatus.PRESENT,
          "NGHI NGỜ": PhenotypeStatus.SUSPECTED, "KHÔNG": PhenotypeStatus.ABSENT}
CUTOFFS = (1, 5, 10, 20, 100)


def columns(ranked, categories, concept):
    """Replicates web_service.match: (group, rank in group, overall rank) per listed disease."""
    limits, taken, shown = dict(GROUPS), {name: 0 for name, _ in GROUPS}, {}
    for overall, candidate in enumerate(ranked, 1):
        group = categories.get(candidate.disease_id, ("CHUA_RO", False))[0]
        if candidate.ic_weighted_coverage > 0 and taken[group] < limits[group]:
            taken[group] += 1
            shown.setdefault(concept(candidate.disease_id), (group, taken[group], overall))
    return shown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--xref", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    system = WebSystem()
    system.initialize()
    resolver = load_disease_xref(args.xref)
    concept = resolver.resolve
    merged = HPOAgent2Matcher.from_files(BASE_DIR / "data/phenotype.hpoa", BASE_DIR / "data/hp.obo",
                                         disease_xref=args.xref)
    matchers = {"web": system.matcher, "web_mondo": merged}
    genes_of = system.disease_to_genes

    cases = [json.loads(line) for line in args.cases.open(encoding="utf-8")]
    details = {mode: [] for mode in matchers}
    for index, case in enumerate(cases, 1):
        observations, seen = [], set()
        for obs in case["observations"]:
            if obs["hpo_id"] not in seen and obs.get("status") in STATUS:
                seen.add(obs["hpo_id"])
                observations.append(PhenotypeObservation(obs["hpo_id"], STATUS[obs["status"]]))
        gold = {concept(d) for d in case.get("gold_disease_ids") or []}
        gold_genes = set(case.get("gold_gene_ids") or [])
        for mode, matcher in matchers.items():
            ranked = matcher.rank(observations, top_k=None, ranking_strategy=RankingStrategy.IC_COVERAGE)
            first = next((i for i, c in enumerate(ranked, 1) if concept(c.disease_id) in gold), None) if gold else None
            shown = columns(ranked, system.categories, concept)
            hit = next((shown[g] for g in gold if g in shown), None)
            shown_genes = set()
            listed = [c for c in ranked if concept(c.disease_id) in shown]
            for candidate in listed:
                ids = [candidate.disease_id, *candidate.provenance.get("equivalent_disease_ids", [])]
                for disease in ids:
                    shown_genes.update(genes_of.get(disease, []))
            gold_group = system.categories.get(next(iter(case.get("gold_disease_ids") or []), ""), (None,))[0]
            details[mode].append({
                "case_id": case["case_id"], "so_hpo": len(observations), "co_ma_benh": bool(gold),
                "gold_nhom": gold_group,
                "hang_chung": first, "hien_tren_web": hit is not None,
                "cot": hit[0] if hit else None, "hang_trong_cot": hit[1] if hit else None,
                "gene_dung_hien_tren_web": bool(gold_genes & shown_genes) if gold_genes else None,
                "so_benh_hien_tren_web": len(shown),
            })
        print(f"{index}/{len(cases)} {case['case_id']}", flush=True)

    summary = {}
    for mode, rows in details.items():
        disease_rows = [r for r in rows if r["co_ma_benh"]]
        gene_rows = [r for r in rows if r["gene_dung_hien_tren_web"] is not None]
        n = len(disease_rows)
        summary[mode] = {
            "so_ca_co_ma_benh": n,
            **{f"top{k}_hang_chung": f"{sum(1 for r in disease_rows if r['hang_chung'] and r['hang_chung'] <= k)}/{n}"
               for k in CUTOFFS},
            "benh_dung_hien_tren_web": f"{sum(r['hien_tren_web'] for r in disease_rows)}/{n}",
            "benh_dung_top3_trong_cot": f"{sum(1 for r in disease_rows if r['hang_trong_cot'] and r['hang_trong_cot'] <= 3)}/{n}",
            "so_ca_co_gene": len(gene_rows),
            "gene_dung_hien_tren_web": f"{sum(r['gene_dung_hien_tren_web'] for r in gene_rows)}/{len(gene_rows)}",
            "trung_binh_benh_hien_tren_web": round(sum(r["so_benh_hien_tren_web"] for r in rows) / len(rows), 1),
            "nhom_cua_benh_dung": {g: sum(1 for r in disease_rows if (r["gold_nhom"] or "khong_co_trong_bang") == g) for g in
                                   sorted({r["gold_nhom"] or "khong_co_trong_bang" for r in disease_rows})},
        }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "details.json").write_text(json.dumps(details, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
