"""Index of the "Tìm từng HPO" box: HPO names plus the wording doctors really use.

The wording comes from the private Model 1 bundle (memory.jsonl: reviewed findings + dictionaries, usage.json: how often
doctors chose each code), so a bundle change reaches the search too. Without a bundle only the catalog is searched.
Exact accents rank above accent-free matches, so "mắt nhỏ" is not "mặt nhỏ" and "tim" is not "tím"; a query typed without
accents still reaches every term in the accent-free tiers. Within a tier the codes doctors use most come first.
"""
import json
import re
import unicodedata
from pathlib import Path

from model1_v6.contract import normalize_query
from server import normalize_text

_VI_LETTER = 'a-zàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ'
# Old-style tone placement -> new style at the end of a syllable ("hoà" = "hòa", "thuỷ" = "thủy").
_TONE_PLACEMENT = [(re.compile(old + '(?![' + _VI_LETTER + '])'), new) for old, new in (
    ('oà', 'òa'), ('oá', 'óa'), ('oả', 'ỏa'), ('oã', 'õa'), ('oạ', 'ọa'), ('oè', 'òe'), ('oé', 'óe'), ('oẻ', 'ỏe'),
    ('oẽ', 'õe'), ('oẹ', 'ọe'), ('uỳ', 'ùy'), ('uý', 'úy'), ('uỷ', 'ủy'), ('uỹ', 'ũy'), ('uỵ', 'ụy'))]
_DAN = re.compile(r'\bdãn\b')  # doctors write "dãn" and "giãn" for the same finding
_DAN_FOLDED = re.compile(r'\bdan\b')

# Abbreviations in common prenatal use. They appear in neither HPO nor the dictionaries; the doctor still sees the term name.
ABBREVIATIONS = {'tlt': 'HP:0001629', 'vsd': 'HP:0001629', 'tln': 'HP:0001631', 'asd': 'HP:0001631', 'tof': 'HP:0001636',
                 'avsd': 'HP:0006695', 'tga': 'HP:0001669', 'hlhs': 'HP:0004383', 'cdh': 'HP:0000776', 'sga': 'HP:0001518',
                 'lga': 'HP:0001520', 'fgr': 'HP:0001511', 'iugr': 'HP:0001511', 'sua': 'HP:0001195', 'acc': 'HP:0001274',
                 'dwm': 'HP:0001305', 'tbs': 'HP:0001627', 'nt tăng': 'HP:0010880', 'nt dày': 'HP:0010880',
                 'kssg tăng': 'HP:0010880', 'kssg dày': 'HP:0010880'}
# Tiers, best first. _tier gives exact (0), start of the term (1), whole words inside it (2), word prefixes (3).
ACCENT_TIERS = (0, 2, 3, 7)  # same accents
FOLDED_TIERS = (4, 5, 6, 8)  # accent-free; ahead of same-accent word prefixes so "dan nao that" finds "giãn não thất"
EN_EXACT, EN_PART, SYNONYM = 1, 9, 10
TYPED_ID = -10
STRONG = (TYPED_ID, 0, 1, 2, 4, 5)  # the query is the term or starts it: no need to ask the retriever


def akey(text):
    """Accent-preserving key: "thuỷ" = "thủy", "Thận (P)" = "thận p", "dãn" = "giãn", but "mắt" != "mặt"."""
    t = unicodedata.normalize('NFC', normalize_query(text)).casefold()
    for old, new in _TONE_PLACEMENT:
        t = old.sub(new, t)
    return _DAN.sub('giãn', ' '.join(re.sub(r'[^\w:+]+|_', ' ', t).split()))


def fkey(text):
    """Accent-free key, for queries and terms compared without diacritics."""
    return _DAN_FOLDED.sub('gian', normalize_text(normalize_query(text)))


def _tier(q, q_words, key, words):
    if key == q:
        return 0
    if key.startswith(q):
        return 1
    if (' ' + q + ' ') in (' ' + key + ' '):
        return 2
    if all(any(w.startswith(p) for w in words) for p in q_words):
        return 3
    return None


def read_bundle(bundle):
    """(query_text, hpo_id) pairs and {hpo_id: times chosen} of a Model 1 bundle; empty without one."""
    root = Path(bundle).expanduser() if bundle else None
    if not root or not (root / 'memory.jsonl').is_file():
        return [], {}
    with (root / 'memory.jsonl').open(encoding='utf-8') as f:
        phrases = [(row['query_text'], row['hpo_id']) for row in map(json.loads, f)]
    usage = root / 'usage.json'
    return phrases, json.loads(usage.read_text(encoding='utf-8')) if usage.is_file() else {}


class HpoSearch:
    def __init__(self, names, phrases=(), usage=None):
        """names: (vi, en, synonyms, id) of the catalog; phrases: (doctors' wording, id)."""
        self.usage = usage or {}
        rows, self.catalog = {}, {}
        phrases = [(akey(text), hid) for text, hid in phrases]
        with_accents = {(fkey(key), hid) for key, hid in phrases if key != fkey(key)}
        for key, hid in phrases:  # the bundle also holds an accent-free copy of each phrase: "tim" for "tím"
            if (key, hid) not in with_accents:
                rows.setdefault((key, hid), 0)
        for vi, en, synonyms, hid in names:
            rows.setdefault((akey(vi), hid), 1)
            ens, known = self.catalog.setdefault(hid, (set(), []))  # the two source catalogs differ in places
            ens.add(normalize_text(en))
            known.append(normalize_text(synonyms))
        self.terms = [(key, key.split(), fold, fold.split(), hid, source) for (key, hid), source in rows.items() if key
                      for fold in [fkey(key)]]

    def rank(self, query, requested=None):
        """(HPO ids for a query, best first; the tier of the first one, None without results)."""
        q, fq = akey(query), fkey(query)
        q_words, fq_words = q.split(), fq.split()
        best = {}

        def offer(tier, hid, source, length):
            value = (tier, -self.usage.get(hid, 0), source, length, hid)
            if hid not in best or value < best[hid]:
                best[hid] = value
        for key, words, fold, fold_words, hid, source in self.terms:
            tier = _tier(q, q_words, key, words) if q else None
            if tier is not None:
                offer(ACCENT_TIERS[tier], hid, source, len(key))
                continue
            tier = _tier(fq, fq_words, fold, fold_words)
            if tier is not None:
                offer(FOLDED_TIERS[tier], hid, source, len(key))
        for hid, (ens, known) in self.catalog.items():
            if hid == requested:
                offer(TYPED_ID, hid, 1, 0)
            elif fq in ens:
                offer(EN_EXACT, hid, 1, len(fq))
            elif any(fq in en for en in ens) or (requested and hid.startswith(requested)):
                offer(EN_PART, hid, 1, min(map(len, ens)))
            elif any(fq in text for text in known):
                offer(SYNONYM, hid, 1, min(map(len, ens)))
        ranked = sorted(best.values())
        return [v[-1] for v in ranked], ranked[0][0] if ranked else None
