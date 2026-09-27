"""Learned re-ranker for Model 2 candidates (numpy only; no xgboost/sklearn at serving time).

The model reorders the candidates that ic_coverage already found (the top POOL of
each mechanism column).  It is trained offline by
nghien_cuu/model2/do_chinh_xac/train_reranker.py and saved as JSON: either
gradient-boosted trees exported from XGBoost or a logistic regression.  Scores are
ranking scores, not probabilities.

``candidate_features`` is shared with training so both sides compute the same inputs.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np

GROUPS = ("NST", "CNV", "DON_GEN", "CHUA_RO")
POOL = 100
FEATURES = (
    "ic_coverage", "exact_coverage", "likelihood_score", "freq_weighted_coverage", "log10_profile_size",
    "n_findings", "n_matched", "frac_matched", "n_exact", "max_ic_matched", "mean_freq_matched",
    "frac_freq_known", "log10_column_rank", "is_NST", "is_CNV", "is_DON_GEN", "is_CHUA_RO",
    "gencc_gene_max", "gencc_disease_max", "log1p_n_genes",
)


def candidate_features(ic, ex, sc, n_profile, evidence, group, genes, ids, column_rank, gene_validity, disease_validity):
    """evidence: (status_weight, IC of finding, semantic similarity, disease frequency or None) per positive finding."""
    matched = [e for e in evidence if e[2] > 0]
    freqs = [e[3] for e in matched if e[3] is not None]
    den = sum(w * info for w, info, _, _ in evidence) or 1.0
    fwc = sum(w * info * sim * (f if f is not None else 0.5) for w, info, sim, f in evidence) / den
    return [
        ic, ex, sc, fwc, math.log10(max(n_profile, 1)), len(evidence), len(matched),
        len(matched) / max(len(evidence), 1), sum(1 for e in evidence if e[2] >= 0.999),
        max((e[1] for e in matched), default=0.0), sum(freqs) / len(freqs) if freqs else -1.0,
        len(freqs) / max(len(matched), 1), math.log10(column_rank),
        *[1.0 if group == g else 0.0 for g in GROUPS],
        max((gene_validity.get(g, 0.0) for g in genes), default=0.0),
        max((disease_validity.get(d, 0.0) for d in ids), default=0.0), math.log1p(len(genes)),
    ]


def load_validity(path: Path):
    """GenCC gene-disease validity (Definitive 4 ... Limited 1), max per gene and per disease ID."""
    gene, disease = {}, {}
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader((l for l in handle if not l.startswith("#")), delimiter="\t"):
                target = gene if row["kind"] == "gene" else disease
                target[row["id"]] = float(row["validity"])
    return gene, disease


class Reranker:
    def __init__(self, spec: dict):
        if tuple(spec["features"]) != FEATURES:
            raise ValueError("re-ranker feature list does not match this code")
        self.spec = spec
        self.kind = spec["kind"]
        if self.kind == "logreg":
            self.mean = spec["mean"]
            self.std = spec["std"]
            self.coef = spec["coef"]
        elif self.kind == "trees":
            # XGBoost compares float32 features with float32 thresholds; doing the same keeps
            # values equal to a threshold (e.g. log10 of a column rank) on the same branch.
            self.trees = [{**t, "cond32": np.asarray(t["cond"], dtype=np.float32)} for t in spec["trees"]]
        else:
            raise ValueError(f"unknown re-ranker kind {self.kind}")

    @classmethod
    def load(cls, path: Path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def score(self, x) -> float:
        if self.kind == "logreg":
            return sum(c * (v - m) / s for c, v, m, s in zip(self.coef, x, self.mean, self.std))
        x32 = np.asarray(x, dtype=np.float32)
        total = 0.0
        for t in self.trees:
            node = 0
            left, right, split, cond, default_left = t["left"], t["right"], t["split"], t["cond32"], t["default_left"]
            while left[node] != -1:
                value = x32[split[node]]
                if value != value:  # NaN follows the default branch
                    node = left[node] if default_left[node] else right[node]
                else:
                    node = left[node] if value < cond[node] else right[node]
            total += float(cond[node])  # leaves store their value in split_conditions
        return total
