"""Train and compare the Model 2 mechanism classifier: Naive Bayes, SVM, Random Forest, XGBoost.

Input is the case table from backend/tools/build_mechanism_dataset.py.  Only
numpy, scikit-learn and xgboost are needed, so the script runs unchanged on the
Ubuntu GPU host (XGBoost uses the CPU here; the data are tiny).

Tasks (``--task``):
* ``xet_nghiem`` (default) - which test family the case points to first:
  CMA_KARYOTYPE (NST, CNV or NST_CNV) / WES (DON_GEN) / KHONG_DI_TRUYEN.
* ``co_che`` - NST / CNV / DON_GEN / KHONG_DI_TRUYEN; rows labelled NST_CNV
  (the doctor named both) are dropped because they fit neither class.

Evaluation is repeated stratified k-fold with fixed hyper-parameters; nothing is
tuned on the folds, so the fold scores are not optimistic by selection.  With a
few hundred weak labels the spread across repeats matters as much as the mean,
and both are reported.  Final models are then refit on all rows and saved.

Usage:
    python train_model2_mechanism.py --data dataset_co_che.csv --out ket_qua_xet_nghiem
    python train_model2_mechanism.py --data dataset_co_che.csv --task co_che --out ket_qua_co_che
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import warnings
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.base import BaseEstimator, ClassifierMixin, clone
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import balanced_accuracy_score, f1_score, recall_score
from sklearn.model_selection import RepeatedStratifiedKFold
from sklearn.naive_bayes import GaussianNB
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.utils.class_weight import compute_sample_weight

try:
    from xgboost import XGBClassifier
except ImportError:  # the other three models still run
    XGBClassifier = None

TASKS = {
    "xet_nghiem": {"NST": "CMA_KARYOTYPE", "CNV": "CMA_KARYOTYPE", "NST_CNV": "CMA_KARYOTYPE",
                   "DON_GEN": "WES", "KHONG_DI_TRUYEN": "KHONG_DI_TRUYEN"},
    "co_che": {"NST": "NST", "CNV": "CNV", "DON_GEN": "DON_GEN", "KHONG_DI_TRUYEN": "KHONG_DI_TRUYEN"},
}
META = {"case_id", "label", "label_source"}
SEED = 20260926


def load(path: Path, task: str, exclude: tuple[str, ...] = ()):
    with path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    mapping = TASKS[task]
    features = [name for name in rows[0] if name not in META and not name.startswith(exclude)]
    kept = [r for r in rows if r["label"] in mapping
            and float(r["so_hpo_co"]) + float(r["so_hpo_nghi_ngo"]) > 0]
    X = np.array([[float(r[f]) if r[f] not in ("", "nan") else np.nan for f in features] for r in kept])
    y = np.array([mapping[r["label"]] for r in kept], dtype=str)
    sources = Counter(r["label_source"] for r in kept)
    dropped = Counter(r["label"] or "(trống)" for r in rows if r not in kept)
    return X, y, features, sources, dropped


class Model2Rule(BaseEstimator, ClassifierMixin):
    """Baseline without learning: the mechanism column whose best Model 2 match scores highest.

    It never predicts KHONG_DI_TRUYEN, so it shows what the fixed three-column
    view already gives before any training.
    """

    def __init__(self, features=(), task="xet_nghiem"):
        self.features = features
        self.task = task

    def fit(self, X, y):
        self.classes_ = np.unique(y)
        return self

    def predict(self, X):
        index = {name: list(self.features).index(f"m2_diem_{name}") for name in ("NST", "CNV", "DON_GEN")}
        scores = np.nan_to_num(X[:, [index["NST"], index["CNV"], index["DON_GEN"]]])
        best = np.array(["NST", "CNV", "DON_GEN"])[scores.argmax(axis=1)]
        return np.array([TASKS[self.task][b] for b in best])


def models(features, task):
    impute = lambda: SimpleImputer(strategy="median")  # noqa: E731
    candidates = {
        "naive_bayes": make_pipeline(impute(), StandardScaler(), GaussianNB()),
        "svm": make_pipeline(impute(), StandardScaler(),
                             SVC(kernel="rbf", C=1.0, class_weight="balanced", random_state=SEED)),
        "random_forest": make_pipeline(impute(), RandomForestClassifier(
            n_estimators=500, min_samples_leaf=2, class_weight="balanced_subsample", random_state=SEED, n_jobs=-1)),
    }
    if XGBClassifier is not None:
        candidates["xgboost"] = make_pipeline(impute(), XGBClassifier(
            n_estimators=300, max_depth=3, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
            eval_metric="mlogloss", random_state=SEED, n_jobs=1))
    if "m2_diem_NST" in features:
        candidates["baseline_model2_cot_diem_cao_nhat"] = Model2Rule(tuple(features), task)
    candidates["baseline_lop_dong_nhat"] = DummyClassifier(strategy="most_frequent")
    candidates["baseline_ngau_nhien_theo_ty_le"] = DummyClassifier(strategy="stratified", random_state=SEED)
    return candidates


def fit(model, X, y):
    # XGBoost needs integer labels and explicit balancing weights.
    if hasattr(model, "steps") and model.steps[-1][0] == "xgbclassifier":
        classes = np.unique(y)
        model.fit(X, np.searchsorted(classes, y), xgbclassifier__sample_weight=compute_sample_weight("balanced", y))
        model.classes_decoded_ = classes
    else:
        model.fit(X, y)
    return model


def predict(model, X):
    output = model.predict(X)
    return model.classes_decoded_[output.astype(int)] if hasattr(model, "classes_decoded_") else output


def summarise(values):
    array = np.array(values)
    return {"mean": round(float(array.mean()), 4), "sd": round(float(array.std(ddof=1)), 4),
            "p2_5": round(float(np.percentile(array, 2.5)), 4), "p97_5": round(float(np.percentile(array, 97.5)), 4)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--task", choices=sorted(TASKS), default="xet_nghiem")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--exclude-prefix", nargs="*", default=[],
                        help="drop features by prefix, e.g. m2_ to measure what Model 2 scores add")
    args = parser.parse_args()
    warnings.filterwarnings("ignore", category=UserWarning)

    X, y, features, sources, dropped = load(args.data, args.task, tuple(args.exclude_prefix))
    classes = sorted(set(y))
    counts = Counter(map(str, y))
    folds = min(args.folds, min(counts.values()))
    if folds < 2:
        raise SystemExit(f"Lớp quá ít ca để kiểm định chéo: {dict(counts)}")
    print(f"{len(y)} ca, lớp: {dict(counts)}; nguồn nhãn: {dict(sources)}; {folds}-fold x {args.repeats}")

    splitter = RepeatedStratifiedKFold(n_splits=folds, n_repeats=args.repeats, random_state=SEED)
    splits = list(splitter.split(X, y))
    report = {}
    for name, template in models(features, args.task).items():
        per_repeat = {"macro_f1": [], "balanced_accuracy": []}
        recalls = {c: [] for c in classes}
        oof_pred = np.empty((args.repeats, len(y)), dtype=object)
        for index, (train, test) in enumerate(splits):
            model = fit(clone(template), X[train], y[train])
            oof_pred[index // folds, test] = predict(model, X[test])
        for repeat in range(args.repeats):
            pred = oof_pred[repeat].astype(str)
            per_repeat["macro_f1"].append(f1_score(y, pred, average="macro", zero_division=0))
            per_repeat["balanced_accuracy"].append(balanced_accuracy_score(y, pred))
            for c, value in zip(classes, recall_score(y, pred, labels=classes, average=None, zero_division=0)):
                recalls[c].append(value)
        report[name] = {"macro_f1": summarise(per_repeat["macro_f1"]),
                        "balanced_accuracy": summarise(per_repeat["balanced_accuracy"]),
                        "recall_theo_lop": {c: summarise(v) for c, v in recalls.items()}}
        print(f"  {name:32s} macro-F1 {report[name]['macro_f1']['mean']:.3f} ± {report[name]['macro_f1']['sd']:.3f}"
              f"   balanced acc {report[name]['balanced_accuracy']['mean']:.3f}")

    args.out.mkdir(parents=True, exist_ok=True)
    importance = {}
    for name, template in models(features, args.task).items():
        if name.startswith("baseline"):
            continue
        model = fit(clone(template), X, y)
        joblib.dump({"model": model, "features": features, "classes": classes, "task": args.task},
                    args.out / f"{name}.joblib")
        if name == "random_forest":
            forest = model.steps[-1][1]
            importance = dict(sorted(zip(features, map(float, forest.feature_importances_)),
                                     key=lambda item: -item[1]))
    metadata = {
        "ngay_chay": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task": args.task, "bo_dac_trung": args.exclude_prefix, "so_ca": len(y), "lop": dict(counts), "nguon_nhan": dict(sources),
        "ca_bi_loai": dict(dropped), "folds": folds, "repeats": args.repeats, "seed": SEED,
        "canh_bao_nhan": ("Nhãn goi_y_bac_si là nhóm suy từ bệnh bác sĩ gợi ý đầu tiên, không phải kết quả "
                          "karyotype/CMA/WES. Số đo cho biết mô hình bắt chước gợi ý của bác sĩ tốt đến đâu, "
                          "không phải độ chính xác chẩn đoán.") if sources.get("goi_y_bac_si") else "",
        "moi_truong": {"python": platform.python_version(), "sklearn": sklearn.__version__,
                       "numpy": np.__version__, "xgboost": XGBClassifier is not None},
        "ket_qua": report,
        "do_quan_trong_random_forest": {k: round(v, 4) for k, v in importance.items()},
    }
    (args.out / "metrics.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Đã lưu {args.out / 'metrics.json'} và mô hình *.joblib")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
