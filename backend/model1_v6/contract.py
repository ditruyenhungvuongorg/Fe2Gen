"""Model 1 v6 contract: prompts, exact alignment, ensemble vote, modifier extension, chunking, reranking helpers.

Copied verbatim from the evaluated research package (outputs/model1_improve_ubuntu_v5); no GPU imports here.
Qwen never outputs HPO IDs or offsets; spans come from deterministic alignment against the report text.
"""
import collections
import json
import re
import unicodedata
from functools import lru_cache

ASSERTIONS = ('present', 'suspected')
MAX_FINDINGS = 40
LETTERS = 'ABCDEFGHIJ'
K = 10
MAX_LENGTH = 2048
MAX_NEW = 384
SYSTEM = 'Đọc báo cáo siêu âm/tiền sản của bác sĩ và trích các cụm biểu hiện (bất thường hình thái, cấu trúc, tăng trưởng, dịch ối...) của thai.\nChỉ trả JSON {"findings":[{"text":"cụm nguyên văn","assertion":"present"}]}, không giải thích.\n- "text": sao chép đúng chữ hoa, dấu và khoảng trắng của cụm trong báo cáo; giữ phần bổ nghĩa giải phẫu, bên, mức độ trong cùng cụm; hai biểu hiện độc lập là hai cụm. Không đưa từ nghi ngờ (TD, theo dõi, nghĩ, chưa loại trừ), lời dẫn hay dấu câu kết thúc vào cụm.\n- "assertion": "present" nếu biểu hiện được ghi nhận; "suspected" nếu được nêu là nghi ngờ/theo dõi/chưa loại trừ.\n- Không trích tên gen, biến thể, tên hội chứng/bệnh chẩn đoán, tuổi thai, ngôi thai, chuyển dạ.\nLiệt kê theo thứ tự xuất hiện; cụm nhắc hai lần thì liệt kê hai lần. Không suy diễn, không sinh mã HPO hay vị trí ký tự. Không có biểu hiện thì trả {"findings":[]}.'
RERANK_SYSTEM = 'Bạn hỗ trợ bác sĩ chọn mã HPO cho một cụm biểu hiện trong báo cáo tiền sản.\nĐọc báo cáo, cụm được đánh dấu và danh sách ứng viên (tên tiếng Việt | tiếng Anh). Chọn ứng viên mô tả đúng nhất cụm đó, xét cả vị trí giải phẫu, bên, mức độ.\nChỉ trả đúng một chữ cái của ứng viên, không giải thích. Đây là gợi ý để bác sĩ duyệt, không phải chẩn đoán.'
FILLER_TOKENS = frozenset({'khi', 'như', 'thì', 'những', 'do', 'đang', 'có', 'là', 'bởi', 'các', 'sẽ', 'ở', 'được', 'và', 'cũng', 'một', 'cho', 'của', 'đã', 'với'})
MOD = re.compile('^\\s*(\\((T|P|trái|phải)\\)|(T|P)\\b|bên\\s+(T|P|trái|phải)\\b|(2|hai)\\s+bên|nặng|nhẹ|trung bình|độ\\s+[IV\\d]+)', re.I)
BOUNDARY = re.compile('(?<=[.;!?])\\s+|\\n+|(?<=/)\\s+')


def normalize_query(text: str) -> str:
    text = unicodedata.normalize("NFC", str(text or "")).strip().lower()
    text = re.sub(r"[–—]", "-", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([,;:/()])\s*", r"\1 ", text).strip()
    tokens = re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE)
    kept = [token for token in tokens if token not in FILLER_TOKENS]
    result = " ".join(kept)
    result = re.sub(r"\s+([,;:/)])", r"\1", result)
    result = re.sub(r"([(])\s+", r"\1", result)
    return result.strip() or text


def occurrences(text, phrase):
    def word(c):
        return c.isalnum() or c == '_' or unicodedata.category(c).startswith('M')
    return [(m.start(1), m.end(1)) for m in re.finditer('(?=(' + re.escape(phrase) + '))', text)
            if (not word(phrase[0]) or m.start(1) == 0 or not word(text[m.start(1) - 1]))
            and (not word(phrase[-1]) or m.end(1) == len(text) or not word(text[m.end(1)]))]


def strict_json(raw):
    def pairs(items):
        obj = {}
        for k, v in items:
            if k in obj:
                raise ValueError('Duplicate JSON key: ' + k)
            obj[k] = v
        return obj
    return json.loads(raw, object_pairs_hook=pairs)


def align(text, raw):
    """Strict JSON, unique monotonic exact alignment; never repairs wording."""
    obj = strict_json(raw)
    if not isinstance(obj, dict) or set(obj) != {'findings'} or not isinstance(obj['findings'], list):
        raise ValueError('Expected exactly one findings list')
    items = obj['findings']
    if len(items) > MAX_FINDINGS:
        raise ValueError('Too many findings')
    for it in items:
        if not isinstance(it, dict) or set(it) != {'text', 'assertion'}:
            raise ValueError('Each finding needs exactly text and assertion')
        if not isinstance(it['text'], str) or not it['text'] or it['text'] != it['text'].strip():
            raise ValueError('Finding text must be a nonempty trimmed string')
        if it['assertion'] not in ASSERTIONS:
            raise ValueError('Unknown assertion: ' + repr(it['assertion']))
    choices = [occurrences(text, it['text']) for it in items]
    if any(not c for c in choices):
        raise ValueError('Finding text absent from report or cuts a word')

    @lru_cache(None)
    def walk(i, previous):
        if i == len(items):
            return ((),)
        solutions = []
        for start, end in choices[i]:
            if start < previous:
                continue
            for tail in walk(i + 1, end):
                solutions.append(((start, end),) + tail)
                if len(solutions) == 2:
                    return tuple(solutions)
        return tuple(solutions)
    solutions = walk(0, 0)
    if len(solutions) != 1:
        raise ValueError('Ambiguous repeated occurrences' if solutions else 'Overlapping or out-of-order findings')
    return [dict(mention_text=it['text'], span_start=a, span_end=b, assertion=it['assertion'])
            for it, (a, b) in zip(items, solutions[0])]


def lenient_align(text, raw):
    """Suggestion-time fallback when strict alignment is ambiguous: earliest in-order occurrence, flagged for the doctor.
    Never used for scoring (evaluation keeps the strict unique alignment)."""
    obj = strict_json(raw)
    items = obj['findings'] if isinstance(obj, dict) and set(obj) == {'findings'} else None
    if not isinstance(items, list) or len(items) > MAX_FINDINGS:
        raise ValueError('not a findings list')
    out, pos = [], 0
    for it in items:
        if not isinstance(it, dict) or set(it) != {'text', 'assertion'} or it['assertion'] not in ASSERTIONS or not it['text'] or it['text'] != it['text'].strip():
            raise ValueError('bad item')
        occ = [o for o in occurrences(text, it['text']) if o[0] >= pos]
        if not occ:
            raise ValueError('finding text absent or out of order')
        a, b = occ[0]
        out.append(dict(mention_text=it['text'], span_start=a, span_end=b, assertion=it['assertion'], alignment='ambiguous_first_occurrence'))
        pos = b
    return out


def vote(pred_lists, need=2):
    """Keep a finding (span + assertion) predicted by >= need of the given runs; invalid runs contribute nothing."""
    counter, keep = collections.Counter(), {}
    for preds in pred_lists:
        if preds.get('error'):
            continue
        for f in preds['predicted']:
            k = (f['span_start'], f['span_end'], f['assertion'])
            counter[k] += 1
            keep[k] = f
    valid = sum(not p.get('error') for p in pred_lists)
    if valid == 0:
        return dict(predicted=[], error='all ensemble members invalid')
    chosen = sorted((keep[k] for k, n in counter.items() if n >= min(need, valid)), key=lambda f: f['span_start'])
    return dict(predicted=chosen)


def extend_modifiers(text, pred):
    """Attach a laterality/severity modifier that immediately follows a finding (never crosses the next finding)."""
    if pred.get('error'):
        return pred
    out = []
    items = sorted(pred['predicted'], key=lambda f: f['span_start'])
    for i, f in enumerate(items):
        m = MOD.match(text[f['span_end']:])
        nxt = items[i + 1]['span_start'] if i + 1 < len(items) else len(text)
        if m and f['span_end'] + m.end() <= nxt:
            e = f['span_end'] + m.end()
            f = dict(f, span_end=e, mention_text=text[f['span_start']:e])
        out.append(f)
    return dict(pred, predicted=out)


def chunks(text, max_chars=400):
    """Greedy packing of boundary-delimited pieces; a single piece longer than max_chars is cut at the last comma/space."""
    pieces, start = [], 0
    for m in BOUNDARY.finditer(text):
        if m.end() > start:
            pieces.append((start, m.start() if m.start() > start else m.end()))
            start = m.end()
    pieces.append((start, len(text)))
    out, cur_s, cur_e = [], None, None
    for s, e in pieces:
        if s >= e:
            continue
        if cur_s is None:
            cur_s, cur_e = s, e
        elif e - cur_s <= max_chars:
            cur_e = e
        else:
            out.append((cur_s, cur_e)); cur_s, cur_e = s, e
    if cur_s is not None:
        out.append((cur_s, cur_e))
    final = []
    for s, e in out:
        while e - s > max_chars:
            comma = text.rfind(',', s + max_chars // 2, s + max_chars)
            cut = comma if comma > s else text.rfind(' ', s, s + max_chars)
            cut = cut + 1 if cut > s else s + max_chars
            final.append((s, cut)); s = cut
        final.append((s, e))
    result = []
    for s, e in final:
        seg = text[s:e]
        lead = len(seg) - len(seg.lstrip())
        seg = seg.strip()
        if seg:
            result.append((s + lead, seg))
    return result


def shift(pred, offset):
    return dict(pred, span_start=pred['span_start'] + offset, span_end=pred['span_end'] + offset)


def apply_rerank(ranked, logprob):
    """Reorder the first K candidates by reranker log-probability; candidates beyond K keep retriever order."""
    head = ranked[:K]
    order = sorted(range(len(head)), key=lambda i: -logprob[i])
    return [head[i] for i in order] + ranked[K:]


def trigrams(s):
    s = ' ' + ' '.join(s.casefold().split()) + ' '
    return {s[i:i + 3] for i in range(len(s) - 2)}


def closest(mention, phrases, n=2):
    """Up to n distinct reviewed phrases most similar to the mention (character-trigram Jaccard)."""
    q, seen, scored = trigrams(mention), set(), []
    for p in phrases:
        if p.casefold() in seen:
            continue
        seen.add(p.casefold())
        t = trigrams(p)
        scored.append((len(q & t) / len(q | t) if q | t else 0.0, p))
    return [p for _, p in sorted(scored, key=lambda x: (-x[0], x[1]))[:n]]


def rerank_user(text, mention, assertion, candidates, catalog, usage=None, exemplars=None):
    """candidates: HPO IDs (<= 10). usage: {hpo: times doctors chose it}. exemplars: {hpo: [reviewed phrases]} (training folds only)."""
    lines = [f'Báo cáo: {text}', f'Cụm cần mã hoá: «{mention}» (trạng thái: {assertion})', 'Ứng viên:']
    for letter, h in zip(LETTERS, candidates):
        c = catalog[h]
        notes = []
        if usage and usage.get(h):
            notes.append(f'bác sĩ đã dùng {usage[h]} lần')
        if exemplars and exemplars.get(h):
            notes.append('ví dụ đã duyệt: ' + ', '.join(f'«{x}»' for x in closest(mention, exemplars[h])))
        lines.append(f'{letter}. {c["name_vi"]} | {c["name_en"]}' + (f' [{"; ".join(notes)}]' if notes else ''))
    return '\n'.join(lines)
