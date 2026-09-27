"""Build the case table for the Model 2 mechanism classifier (NST / CNV / đơn gen ...).

One row per case: reviewed HPO findings summarised by organ system, the best
Model 2 match in each mechanism column, gestational age, and a label.

The label written here is *derived automatically from the doctor's first listed
suggestion* (``label_source=goi_y_bac_si``).  It is a weak label: a suggestion,
not a karyotype/CMA/exome result.  A companion CSV lists every derived label
with the sentence it came from so doctors can confirm or correct it; confirmed
labels are read back with ``--confirmed-labels``.

Usage (from backend/):
    python tools/build_mechanism_dataset.py --cases ../nghien_cuu/gold_234/gold_234.jsonl \
        --out-dir ../nghien_cuu/model2/co_che
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hpo_agents.agent2_schema import PhenotypeObservation, PhenotypeStatus, RankingStrategy  # noqa: E402
from web_service import GROUPS, WebSystem  # noqa: E402

LABELS = ("NST", "CNV", "NST_CNV", "DON_GEN", "KHONG_DI_TRUYEN")
STATUS = {"CÓ": PhenotypeStatus.PRESENT, "NGHI NGỜ": PhenotypeStatus.SUSPECTED, "KHÔNG": PhenotypeStatus.ABSENT}

# Direct children of HP:0000118 in hp.obo v2026-06-23 that occur prenatally.
SYSTEMS = {
    "HP:0001626": "tim_mach", "HP:0000707": "than_kinh", "HP:0000152": "dau_co",
    "HP:0025031": "tieu_hoa", "HP:0000119": "tiet_nieu_sinh_duc", "HP:0033127": "co_xuong",
    "HP:0040064": "chi", "HP:0002086": "ho_hap", "HP:0045027": "long_nguc",
    "HP:0001197": "phat_trien_truoc_sinh", "HP:0001507": "tang_truong", "HP:0000478": "mat",
    "HP:0000598": "tai", "HP:0001574": "da", "HP:0001871": "mau", "HP:0002664": "khoi_u",
    "HP:0001939": "chuyen_hoa",
}

# Patterns for the doctor's first suggestion.  The earliest match in the
# sentence decides; NST and CNV named together become NST_CNV.
PATTERNS = {
    "NST": r"trisomy|tam nhiễm|turner|klinefelter|triploid|tam bội|aneuploid|dị bội|monosomy x|45,\s*x|47,\s*x|"
           r"\bnst\b|nhiễm sắc thể|chromosomal",
    "CNV": r"\bcnv\b|22q11|microdeletion|microduplication|deletion|duplication|mất đoạn|lặp đoạn|williams|"
           r"wolf-hirschhorn|cri.du.chat|1p36|prader|smith-magenis|\b\d{1,2}[pq]\d",
    "DON_GEN": r"-related|monogenic|đơn gen|gene-associated|ciliopathy|rasopathy|noonan|tuberous sclerosis|"
               r"joubert|meckel|skeletal dysplasia|loạn sản xương",
    "KHONG_DI_TRUYEN": r"đơn độc|isolated|không hội chứng|nhiễm |cmv|toxoplasma|mắc phải|không đặc hiệu|"
                       r"chức năng|sequence|association|amniotic band|chưa thể gợi ý|mạch máu|teratoma|sporadic|"
                       r"lymphatic|cpam|shunt|venous|tắc ruột|meconium|nhịp|sulcation|tác động thai nhỏ|"
                       r"nonsyndromic|multifactorial|không mendelian|diabetic|placental|destructive|hemorrhage|ischemia",
}
# Sentences that say no genetic syndrome fits are labelled KHONG_DI_TRUYEN outright,
# even when they go on to name a gene ("Chưa có một hội chứng đơn gen nào phù hợp").
NO_SYNDROME = re.compile(r"^(chưa có|không có|không thể|chưa thể)", re.I)
# Gene symbols (case-sensitive), excluding upper-case abbreviations that are not genes.
GENE_SYMBOL = re.compile(
    r"\b(?!(?:CNV|NST|CHD|CDH|CMV|CPAM|CAKUT|IUGR|FGR|MRI|ARSA|VSD|ASD|AVSD|TOF|DORV|HLHS|CNS|CPC|UPJ|"
    r"VACTERL|ACMG|AMP|CoA|NT|SD|BPV|TD|EA|PA|IVS|PLAP)\b)[A-Z][A-Z0-9]{2,}\d*\b")


def derive_label(syndromes: str) -> tuple[str, str]:
    lines = [line.strip() for line in (syndromes or "").splitlines() if line.strip()]
    first = re.sub(r"^\d+\)\s*", "", lines[0]) if lines else ""
    if NO_SYNDROME.match(first):
        return "KHONG_DI_TRUYEN", first
    hits = []
    for label, pattern in PATTERNS.items():
        match = re.search(pattern, first, re.I)
        if match:
            hits.append((match.start(), label))
    gene = GENE_SYMBOL.search(first)
    if gene:
        hits.append((gene.start(), "DON_GEN"))
    if not hits:
        return "", first
    hits.sort()
    first_label = hits[0][1]
    if first_label in ("NST", "CNV"):
        near = {label for pos, label in hits if pos - hits[0][0] <= 25}
        if {"NST", "CNV"} <= near:
            return "NST_CNV", first
    return first_label, first


def gestational_weeks(text: str) -> float:
    match = re.search(r"(\d{1,2})\s*tuần(?:\s*(\d)\s*ngày)?", text)
    if not match:
        return math.nan
    return int(match.group(1)) + (int(match.group(2)) / 7 if match.group(2) else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--confirmed-labels", type=Path,
                        help="CSV with case_id,nhan_xac_nhan from doctor review; overrides derived labels")
    args = parser.parse_args()

    system = WebSystem()
    system.initialize()
    ontology = system.matcher.ontology
    confirmed = {}
    if args.confirmed_labels:
        with args.confirmed_labels.open(encoding="utf-8-sig") as handle:
            confirmed = {r["case_id"]: r["nhan_xac_nhan"].strip() for r in csv.DictReader(handle)
                         if r.get("nhan_xac_nhan", "").strip()}

    cases = [json.loads(line) for line in args.cases.open(encoding="utf-8")]
    rows, review = [], []
    for index, case in enumerate(cases, 1):
        case_id = str(case["case_id"])
        observations, seen = [], set()
        for mention in case["mentions"]:
            hpo = ontology.resolve(mention["hpo_id"]) if mention.get("hpo_id") else None
            if hpo and ontology.contains(hpo) and hpo not in seen and mention.get("status") in STATUS:
                seen.add(hpo)
                observations.append(PhenotypeObservation(hpo, STATUS[mention["status"]]))
        derived, sentence = derive_label(case.get("syndromes", ""))
        label = confirmed.get(case_id, derived)
        review.append({"case_id": case_id, "nhan_tu_dong": derived, "cau_goi_y_dau_tien": sentence,
                       "nhan_xac_nhan": confirmed.get(case_id, ""), "ghi_chu": ""})
        positive = [o for o in observations if o.status is not PhenotypeStatus.ABSENT]
        row = {"case_id": case_id, "label": label,
               "label_source": "bac_si_xac_nhan" if case_id in confirmed else "goi_y_bac_si",
               "tuoi_thai_tuan": round(gestational_weeks(case.get("text", "")), 2),
               "so_hpo_co": sum(o.status is PhenotypeStatus.PRESENT for o in observations),
               "so_hpo_nghi_ngo": sum(o.status is PhenotypeStatus.SUSPECTED for o in observations),
               "so_hpo_khong": sum(o.status is PhenotypeStatus.ABSENT for o in observations)}
        touched = set()
        for system_id, name in SYSTEMS.items():
            weight = 0.0
            for obs in positive:
                if obs.hpo_id == system_id or ontology.is_ancestor(system_id, obs.hpo_id):
                    weight += 1.0 if obs.status is PhenotypeStatus.PRESENT else 0.5
            row[f"he_{name}"] = weight
            if weight:
                touched.add(name)
        row["so_he_co_quan"] = len(touched)
        # Best Model 2 similarity per mechanism column (0 when no finding is shared).
        best = {name: 0.0 for name, _ in GROUPS}
        best_rank = {name: math.nan for name, _ in GROUPS}
        if positive:
            ranked = system.matcher.rank(observations, top_k=None, ranking_strategy=RankingStrategy.IC_COVERAGE)
            for overall, candidate in enumerate(ranked, 1):
                group = system.categories.get(candidate.disease_id, ("CHUA_RO", False))[0]
                if math.isnan(best_rank[group]) and candidate.ic_weighted_coverage > 0:
                    best[group] = round(candidate.ic_weighted_coverage, 4)
                    best_rank[group] = overall
        for name, _ in GROUPS:
            row[f"m2_diem_{name}"] = best[name]
            row[f"m2_log_hang_{name}"] = round(math.log10(best_rank[name]), 4) if not math.isnan(best_rank[name]) else 5.0
        rows.append(row)
        print(f"{index}/{len(cases)} ca {case_id}: nhan={label or '-'} hpo={len(observations)}", flush=True)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (("dataset_co_che.csv", rows), ("nhan_can_bac_si_duyet.csv", review)):
        with (args.out_dir / name).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)
    counts = {}
    for row in rows:
        counts[row["label"] or "(không gán được)"] = counts.get(row["label"] or "(không gán được)", 0) + 1
    print("Nhãn:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
