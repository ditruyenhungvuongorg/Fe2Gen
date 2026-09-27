"""Assign each phenotype.hpoa disease to a mechanism group for the three-column view.

Groups, in the order the web shows them (left to right), which follows the usual
prenatal test sequence QF-PCR/karyotype -> CMA -> exome:

* ``NST``      whole-chromosome aneuploidy, polyploidy, ring chromosomes.
* ``CNV``      partial deletion/duplication syndromes and other segmental
               chromosomal disorders (CMA territory).
* ``DON_GEN``  a germline Mendelian gene is recorded for the disease.
* ``CHUA_RO``  none of the above could be established from the sources.

Evidence order: MONDO ancestry (via OMIM/Orphanet xrefs), then the disease name,
then gene tables.  A chromosomal/CNV disease that also has a Mendelian gene is
flagged ``co_che_hon_hop=1`` (e.g. Sotos: NSD1 point variants or 5q35 deletion).
The group is a property of the disease label, not of a patient's result.

Usage:
    python tools/build_disease_category.py --hpoa data/phenotype.hpoa \
        --mondo <mondo.obo> --orpha-genes <en_product6.xml> \
        --genes data/genes_to_disease.txt --out data/disease_category.tsv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

GROUPS = ("NST", "CNV", "DON_GEN", "CHUA_RO")

MONDO_CHROMOSOMAL = "MONDO:0019040"
MONDO_NST_ROOTS = {
    "MONDO:0700064",  # aneuploidy
    "MONDO:0700065",  # trisomy
    "MONDO:0020050",  # autosomal trisomy
    "MONDO:0020053",  # total autosomal monosomy
    "MONDO:0020639",  # monosomy
    "MONDO:0019934",  # polyploidy
    "MONDO:0018186",  # ring chromosome
}
MONDO_CNV_ROOTS = {
    "MONDO:0000761",  # syndrome caused by partial chromosomal deletion
    "MONDO:0000762",  # syndrome caused by partial chromosomal duplication
    "MONDO:0020052",  # partial autosomal trisomy/tetrasomy
    "MONDO:0020054",  # partial autosomal monosomy
}

PARTIAL = re.compile(r"partial|distal|proximal|\d+\s*[pq]\s*\d|[pq](ter|arm)|deletion|duplication|microdeletion|microduplication|mosaic.*partial", re.I)
NST_NAME = re.compile(
    r"^(trisomy|tetrasomy|pentasomy|monosomy)\s+(\d+|X|Y)\b(?!\s*[pq])|down syndrome|(?<!wilson-)turner syndrome|"
    r"klinefelter|triploidy|tetraploidy|polyploidy|\b4[7-9],\s*X|\bXXX\b|\bXYY\b|\bXXYY\b|ring chromosome", re.I)
CNV_NAME = re.compile(
    r"\b\d{1,2}\s*[pq]\s*\d[\d.]*\b.*(deletion|duplication|microdeletion|microduplication|monosomy|trisomy)|"
    r"(deletion|duplication|microdeletion|microduplication)\s+(syndrome|of chromosome)|"
    r"(partial|distal|proximal)\s+(trisomy|monosomy|deletion|duplication)|chromosome\s+\d+[pq]", re.I)
# Somatic/haematological entities named after a chromosome; not fetal karyotype findings.
NOT_FETAL_CHROMOSOMAL = re.compile(r"myelodysplasia|leuk[a]?emia|lymphoma|tumou?r|cancer|carcinoma", re.I)
# Recessive single-gene disorders whose phenotype is (mosaic) aneuploidy.
MONOGENIC_ANEUPLOIDY = re.compile(r"mosaic variegated aneuploidy", re.I)
GERMLINE = re.compile(r"^Disease-causing germline mutation", re.I)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_hpoa(path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        rows = csv.DictReader((line for line in handle if not line.startswith("#")), delimiter="\t")
        for row in rows:
            names.setdefault(row["database_id"], row["disease_name"])
    return names


def read_mondo(path: Path) -> tuple[dict[str, set[str]], dict[str, set[str]], str]:
    parents: dict[str, set[str]] = defaultdict(set)
    xref_to_mondo: dict[str, set[str]] = defaultdict(set)
    version, current, obsolete = "", None, False
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line.startswith("data-version:"):
                version = line.split(":", 1)[1].strip()
            if line.startswith("["):
                current, obsolete = None, False
                if line == "[Term]":
                    current = ""
                continue
            if current is None:
                continue
            if line.startswith("id: "):
                current = line[4:]
            elif line.startswith("is_obsolete: true"):
                obsolete = True
                parents.pop(current, None)
            elif obsolete:
                continue
            elif line.startswith("is_a: "):
                parents[current].add(line[6:].split()[0])
            elif line.startswith("xref: "):
                xref = line[6:].split()[0].replace("Orphanet:", "ORPHA:")
                # Only exact-equivalence xrefs; MONDO marks them with MONDO:equivalentTo.
                if xref.startswith(("OMIM:", "ORPHA:")) and "equivalentTo" in line:
                    xref_to_mondo[xref].add(current)
    return parents, xref_to_mondo, version


def ancestors(term: str, parents: dict[str, set[str]], cache: dict[str, frozenset[str]]) -> frozenset[str]:
    if term in cache:
        return cache[term]
    cache[term] = frozenset()  # cycle guard
    found: set[str] = set()
    for parent in parents.get(term, ()):
        found.add(parent)
        found |= ancestors(parent, parents, cache)
    cache[term] = frozenset(found)
    return cache[term]


def read_genes(genes_path: Path, orpha_xml: Path) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Germline Mendelian genes per disease ID; Orphanet version string."""
    genes: dict[str, set[str]] = defaultdict(set)
    with genes_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if (row["association_type"] == "MENDELIAN" and row["disease_id"].startswith("OMIM:")
                    and row["gene_symbol"] not in ("", "-")):
                genes[row["disease_id"]].add(row["gene_symbol"])
    root = ET.parse(orpha_xml).getroot()
    for disorder in root.iter("Disorder"):
        code = disorder.findtext("OrphaCode")
        for assoc in disorder.iter("DisorderGeneAssociation"):
            kind = assoc.findtext("DisorderGeneAssociationType/Name") or ""
            symbol = assoc.findtext("Gene/Symbol")
            if code and symbol and GERMLINE.match(kind):
                genes[f"ORPHA:{code}"].add(symbol)
    return genes, {"orphanet_date": root.get("date", "")}


def classify(disease_id, name, mondo_ids, parents, cache, genes, own_genes):
    anc: set[str] = set()
    for term in mondo_ids:
        anc |= {term} | ancestors(term, parents, cache)
    chromosomal = MONDO_CHROMOSOMAL in anc
    has_gene = bool(genes.get(disease_id))
    if has_gene and (MONOGENIC_ANEUPLOIDY.search(name) or NOT_FETAL_CHROMOSOMAL.search(name)):
        return "DON_GEN", "gene_germline", 1
    if anc & MONDO_NST_ROOTS and not (anc & MONDO_CNV_ROOTS or PARTIAL.search(name)):
        group, basis = "NST", "mondo_aneuploidy"
    elif anc & MONDO_CNV_ROOTS:
        group, basis = "CNV", "mondo_partial_del_dup"
    elif disease_id.startswith("DECIPHER:"):
        # DECIPHER's disorder list is made of recurrent CNV syndromes.
        group, basis = "CNV", "decipher"
    elif NST_NAME.search(name) and not PARTIAL.search(name) and not NOT_FETAL_CHROMOSOMAL.search(name):
        group, basis = "NST", "ten_benh"
    elif CNV_NAME.search(name):
        group, basis = "CNV", "ten_benh"
    elif chromosomal and len(own_genes.get(disease_id, ())) == 1:
        # One causal gene recorded for this very entry (Schaaf-Yang/MAGEL2,
        # Silver-Russell 3-5): a single-gene subtype of a chromosomal-region disorder.
        group, basis = "DON_GEN", "mondo_chromosomal_mot_gene"
    elif chromosomal:
        # Remaining segmental disorders (imprinting regions such as 15q11-q13,
        # marker chromosomes, UPD); a listed gene only marks the mechanism as mixed.
        group, basis = "CNV", "mondo_chromosomal_khac"
    elif has_gene:
        group, basis = "DON_GEN", "gene_germline"
    else:
        group, basis = "CHUA_RO", "khong_du_nguon"
    mixed = int(group in ("NST", "CNV") and has_gene or basis == "mondo_chromosomal_mot_gene")
    return group, basis, mixed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--hpoa", type=Path, required=True)
    parser.add_argument("--mondo", type=Path, required=True)
    parser.add_argument("--orpha-genes", type=Path, required=True)
    parser.add_argument("--genes", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    names = read_hpoa(args.hpoa)
    parents, xref_to_mondo, mondo_version = read_mondo(args.mondo)
    genes, orpha_meta = read_genes(args.genes, args.orpha_genes)
    cache: dict[str, frozenset[str]] = {}
    # OMIM and ORPHA entries for the same MONDO concept share their genes, since
    # each source often records the gene under only one of the two IDs.
    members: dict[str, set[str]] = defaultdict(set)
    for disease_id in names:
        for term in xref_to_mondo.get(disease_id, ()):
            members[term].add(disease_id)
    shared = {
        disease_id: set().union(genes.get(disease_id, set()),
                                *(genes.get(other, set()) for term in xref_to_mondo.get(disease_id, ())
                                  for other in members[term]))
        for disease_id in names
    }
    own_genes, genes = genes, shared

    rows, counts, basis_counts = [], Counter(), Counter()
    for disease_id, name in sorted(names.items()):
        mondo_ids = sorted(xref_to_mondo.get(disease_id, ()))
        group, basis, mixed = classify(disease_id, name, mondo_ids, parents, cache, genes, own_genes)
        counts[group] += 1
        basis_counts[basis] += 1
        rows.append({
            "disease_id": disease_id, "disease_name": name, "nhom": group, "can_cu": basis,
            "co_che_hon_hop": mixed, "mondo_ids": "|".join(mondo_ids),
            "gene_germline": "|".join(sorted(genes.get(disease_id, ()))),
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        handle.write(f"# mondo={mondo_version} sha256={sha256(args.mondo)}\n")
        handle.write(f"# orphanet_product6={orpha_meta['orphanet_date']} sha256={sha256(args.orpha_genes)}\n")
        handle.write(f"# hpoa sha256={sha256(args.hpoa)}; genes_to_disease sha256={sha256(args.genes)}\n")
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} benh -> {args.out}")
    for group in GROUPS:
        print(f"  {group:8s} {counts[group]:6d}")
    print("  can_cu:", dict(basis_counts))
    print("  co_che_hon_hop:", sum(r["co_che_hon_hop"] for r in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
