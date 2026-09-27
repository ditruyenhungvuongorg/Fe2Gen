"""Web boundary for HPO review, evidence formatting and strict model serving."""
import csv
import os
import re
from collections import defaultdict
from server import Fe2genSystem, normalize_text, BASE_DIR
from hpo_agents.agent2_schema import PhenotypeObservation, PhenotypeStatus, RankingStrategy, load_disease_xref
from hpo_agents.agent3_prenatal_recommender import PRENATAL_SYNDROME_CATALOG
from model1_v38_package.model1_runner import Model1V38Runner
from model1_v6.runner import Model1V6Runner

STATUS = {'CÓ': PhenotypeStatus.PRESENT, 'NGHI NGỜ': PhenotypeStatus.SUSPECTED, 'KHÔNG': PhenotypeStatus.ABSENT}
# Left-to-right order of the result columns, following the usual prenatal test
# sequence QF-PCR/karyotype -> CMA -> exome; CHUA_RO is listed after them.
GROUPS = (('NST', 10), ('CNV', 10), ('DON_GEN', 10), ('CHUA_RO', 5))
# A locally rebuilt table in data/ takes precedence over the copy shipped in the repo.
XREF_FILES = (BASE_DIR / 'data/disease_xref_mondo.tsv', BASE_DIR / 'resources/disease_xref_mondo.tsv')
CATEGORY_FILES = (BASE_DIR / 'data/disease_category.tsv', BASE_DIR / 'resources/disease_category.tsv')


def load_categories(path):
    """disease_id -> (group, mixed_mechanism) from tools/build_disease_category.py output."""
    if not path.is_file():
        return {}, None
    lines = path.read_text(encoding='utf-8').splitlines()
    version = ' | '.join(line[2:] for line in lines if line.startswith('# '))
    rows = csv.DictReader((line for line in lines if not line.startswith('#')), delimiter='	')
    return {r['disease_id']: (r['nhom'], r['co_che_hon_hop'] == '1') for r in rows}, version

class WebSystem(Fe2genSystem):
    def initialize(self):
        super().initialize()
        self.profiles_by_id = {p.disease_id: p for p in self.matcher.profiles}
        self.inheritance = defaultdict(set)
        with (BASE_DIR / 'data/phenotype.hpoa').open(encoding='utf-8') as f:
            for row in csv.DictReader((line for line in f if not line.startswith('#')), delimiter='\t'):
                if row['aspect'] == 'I' and row['qualifier'] != 'NOT':
                    self.inheritance[row['database_id']].add(row['hpo_id'])
        self.labels = {}
        current = None
        for line in (BASE_DIR / 'data/hp.obo').read_text(encoding='utf-8').splitlines():
            if line.startswith('id: HP:'):
                current = line[4:]
            elif line.startswith('name: ') and current:
                self.labels[current] = line[6:]
                current = None
        self.aliases, self.canonical = {}, {}
        self.lexicon = defaultdict(list)
        for term in self.hpo_catalog:
            if 'obsolete' in term.get('en', '').lower():
                continue
            self.canonical[term['id']] = term
            for alias in term.get('aliases', '').split('|'):
                if alias.strip():
                    self.aliases[alias.strip()] = term['id']
            phrases = [term.get('vi', ''), term.get('en', '')] + term.get('synonyms', '').split('|')
            if term.get('vi', '').lower().startswith('tật '):
                phrases.append(term['vi'][4:])
            for phrase in phrases:
                key = normalize_text(phrase)
                if key and term['id'] not in self.lexicon[key]:
                    self.lexicon[key].append(term['id'])
        for vi, en, term in self.search_index:
            if term['id'] in self.canonical:
                for key in (vi, en):
                    if key and term['id'] not in self.lexicon[key]:
                        self.lexicon[key].append(term['id'])
        path = next((p for p in CATEGORY_FILES if p.is_file()), CATEGORY_FILES[-1])
        self.categories, self.category_version = load_categories(path)
        if self.categories:
            print(f'[Model 2] Nạp nhóm cơ chế cho {len(self.categories):,} bệnh từ {path.relative_to(BASE_DIR)}.')
        else:
            print('[Model 2] Chưa có disease_category.tsv: kết quả hiển thị một danh sách chung.')
        # Model 1 v6 (ensemble extractor + HPO retriever/reranker) when its private bundle is configured; else v3.8.
        bundle = os.getenv('MODEL1_V6_BUNDLE')
        xref = next((p for p in XREF_FILES if p.is_file()), None)
        self.resolver = load_disease_xref(xref) if xref else None
        print(f'[Model 2] Gộp mã OMIM/ORPHA cùng bệnh: {xref.name if xref else "không có bảng, không gộp"}.')
        self.runner = Model1V6Runner(bundle) if bundle else Model1V38Runner(os.getenv('MODEL1_ADAPTER'), os.getenv('MODEL1_WORKER_DIR'))

    def term(self, hid):
        t = self.canonical.get(hid, {})
        return {'id': hid, 'vi': t.get('vi', ''), 'en': self.labels.get(hid, t.get('en', hid))}

    def search_hpo(self, query, limit=20):
        q = normalize_text(query)
        if not q:
            return []
        requested = self.aliases.get(query.strip().upper(), query.strip().upper())
        ranked = {}
        for vi, en, item in self.search_index:
            hid = item['id']
            if hid not in self.canonical:
                continue
            if hid == requested:
                score = -10
            elif q == vi:
                score = 0
            elif q == en:
                score = 1
            elif (' ' + q + ' ') in (' ' + vi + ' '):
                score = 2
            elif all(any(w.startswith(p) for w in vi.split()) for p in q.split()):
                score = 3
            elif q in en or hid.startswith(requested):
                score = 4
            elif q in normalize_text(item.get('synonyms', '')):
                score = 5
            else:
                continue
            value = (score, len(vi), hid)
            if hid not in ranked or value < ranked[hid]:
                ranked[hid] = value
        return [self.term(v[2]) for v in sorted(ranked.values())[:limit]]

    def extract(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 6000:
            raise ValueError('Nhập đoạn mô tả từ 1 đến 6.000 ký tự.')
        if isinstance(self.runner, Model1V6Runner):
            return self.extract_v6(text)
        mentions = []
        for span in self.runner.extract_spans(text):
            phrase = span['mention_text']
            state, reason = self.assertion_engine.predict(text, span['span_start'], span['span_end'], phrase)
            context = re.split(r'[.;,\n]', text[:span['span_start']])[-1]
            caution = bool(re.search(r'\b(không|chưa|mẹ|cha|gia đình|tiền sử)\b', context, re.I))
            exact = self.lexicon.get(normalize_text(phrase), [])
            choices = [self.term(h) for h in exact] or self.search_hpo(phrase, 5)
            mentions.append({**span, 'status': state, 'explanation': reason,
                             'context_review': caution, 'candidates': choices,
                             'mapping': 'exact_dictionary' if exact else 'search_suggestion', 'approved': False})
        return {'mentions': mentions, 'engine': 'model1_v3.8_lora', 'review_required': True}

    def extract_v6(self, text):
        """Findings + assertion from the v6 ensemble; HPO candidates in reranked order (ranking scores, not probabilities)."""
        result = self.runner.extract(text)
        labels = {'present': 'CÓ', 'suspected': 'NGHI NGỜ'}
        mentions = []
        for f in result['findings']:
            context = re.split(r'[.;,\n]', text[:f['span_start']])[-1]
            caution = bool(re.search(r'\b(không|chưa|mẹ|cha|gia đình|tiền sử)\b', context, re.I)) or f.get('alignment') == 'ambiguous_first_occurrence'
            choices = [dict(self.term(h['id']), retriever_cosine=h['retriever_cosine'], reranker_logprob=h['reranker_logprob'])
                       for h in f['hpo_ranked'] if h['id'] in self.canonical]
            mentions.append({'mention_text': f['mention_text'], 'span_start': f['span_start'], 'span_end': f['span_end'],
                             'status': labels[f['assertion']],
                             'explanation': 'Model 1 v6 gợi ý trạng thái ' + labels[f['assertion']] + '; bác sĩ xác nhận.',
                             'context_review': caution, 'alignment': f.get('alignment', 'unique'),
                             'candidates': choices or self.search_hpo(f['mention_text'], 5),
                             'mapping': 'model1_v6_retriever_reranker' if choices else 'search_suggestion', 'approved': False})
        return {'mentions': mentions, 'engine': 'model1_v6_ensemble', 'review_required': True,
                'warnings': result.get('errors', []), 'seconds': result.get('seconds')}

    def merge_equivalents(self, ranked):
        """Keep the best-ranked ID of each MONDO disease; the same choice as rank() with a resolver,
        without rescoring every profile."""
        kept, first, others = [], {}, {}
        for candidate in ranked:
            concept = self.resolver.resolve(candidate.disease_id)
            if concept in first:
                others[first[concept]].append(candidate.disease_id)
            else:
                first[concept] = candidate.disease_id
                others[candidate.disease_id] = []
                kept.append(candidate)
        return kept, {k: sorted(v) for k, v in others.items() if v}

    def match(self, payload):
        hpos = payload.get('hpos')
        if not isinstance(hpos, list) or not 1 <= len(hpos) <= 100:
            raise ValueError('Chọn từ 1 đến 100 HPO đã duyệt.')
        observations, seen = [], set()
        for item in hpos:
            if not isinstance(item, dict):
                raise ValueError('HPO không hợp lệ.')
            hid, state = item.get('id'), item.get('status')
            if hid not in self.canonical or state not in STATUS or hid in seen:
                raise ValueError('Mã HPO, trạng thái không hợp lệ hoặc bị trùng.')
            seen.add(hid)
            observations.append(PhenotypeObservation(hid, STATUS[state]))
        positive = [o for o in observations if o.status != PhenotypeStatus.ABSENT]
        if not positive:
            raise ValueError('Cần ít nhất một HPO Có hoặc Nghi ngờ để đối chiếu.')
        # Scoring every profile costs the same as a top-k heap, so rank them all
        # once; overall_rank then shows where a grouped disease stands overall.
        ranked = self.matcher.rank(observations, top_k=None, ranking_strategy=RankingStrategy.IC_COVERAGE)
        if self.resolver:
            ranked, equivalents = self.merge_equivalents(ranked)
        else:
            equivalents = {}
        report = self.decider.analyze_case(case_id='WEB', observations=positive, disease_candidates=ranked[:20], top_k=5)
        if self.categories:
            chosen, taken = [], {name: 0 for name, _ in GROUPS}
            limits = dict(GROUPS)
            for overall, c in enumerate(ranked, 1):
                group = self.categories.get(c.disease_id, ('CHUA_RO', False))[0]
                # A group lists only diseases sharing at least one reviewed finding.
                if c.ic_weighted_coverage > 0 and taken[group] < limits[group]:
                    taken[group] += 1
                    chosen.append((overall, taken[group], group, c))
            position = {name: i for i, (name, _) in enumerate(GROUPS)}
            chosen.sort(key=lambda item: (position[item[2]], item[1]))
        else:
            chosen = [(overall, overall, None, c) for overall, c in enumerate(ranked[:20], 1)]
        output = []
        for overall, rank, group, c in chosen:
            profile = self.profiles_by_id[c.disease_id]
            matched, mids = [], set()
            for ev in c.evidence:
                if ev.status == 'ABSENT' or ev.relation not in ('exact', 'profile_ancestor', 'profile_descendant', 'semantic') or not ev.matched_hpo_id:
                    continue
                if ev.matched_hpo_id not in mids:
                    mids.add(ev.matched_hpo_id)
                    matched.append({**self.term(ev.matched_hpo_id), 'relation': ev.relation, 'observed_id': ev.observed_hpo_id})
            remaining = [self.term(h) for h, f in sorted(profile.positive_frequencies.items(), key=lambda p: (-p[1], p[0]))
                         if h not in seen | mids and h != 'HP:0000118']
            # The legacy recommender also matches generic name tokens such as
            # "syndrome". Only exact catalog IDs may supply disease-specific text.
            exact_profile = next((p for p in PRENATAL_SYNDROME_CATALOG.values()
                                  if p.canonical_id == c.disease_id), None)
            # With the MONDO merge a card stands for its equivalent IDs too.
            same = [c.disease_id, *equivalents.get(c.disease_id, [])]
            genes = list(dict.fromkeys(g for d in same for g in self.disease_to_genes.get(d, [])))
            modes = sorted({h for d in same for h in self.inheritance[d]})
            output.append({'rank': rank, 'overall_rank': overall, 'group': group, 'equivalent_ids': same[1:],
                'mixed_mechanism': self.categories.get(c.disease_id, (None, False))[1],
                'disease_id': c.disease_id, 'disease_name': c.disease_name,
                'match_percentage': round(max(0, min(1, c.ic_weighted_coverage)) * 100, 1),
                'matched_phenotypes': matched,
                'inheritance_modes': [self.labels.get(h, h) for h in modes],
                'causative_genes': genes,
                'clinical_features_to_check': remaining,
                'model3_rationale': ('Hồ sơ gợi ý khớp mã bệnh ' + c.disease_id) if exact_profile else '',
                'model3_recommended_tests': (exact_profile.recommended_first_tier + '. ' + exact_profile.recommended_second_tier) if exact_profile else '',
                'negative_conflict': c.negative_conflict})
        return {'candidates': output, 'grouped': bool(self.categories), 'category_version': self.category_version,
                'clinical_pattern': report.clinical_pattern,
                'score_semantics': 'IC-weighted phenotype similarity; not disease probability', 'observations': hpos}
