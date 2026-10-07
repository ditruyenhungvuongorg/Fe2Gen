import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch
from web_service import WebSystem
from model1_v38_package.core import align

class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.system = WebSystem()
        cls.system.initialize()

    def test_search_id_alias_and_vietnamese(self):
        s = self.system
        self.assertEqual(s.search_hpo('HP:0009729')[0]['id'], 'HP:0009729')
        self.assertEqual(s.search_hpo('HP:0001366')[0]['id'], 'HP:0000252')
        self.assertTrue(s.search_hpo('dau nho'))
        self.assertEqual(len({t['id'] for t in s.search_hpo('tim')}), len(s.search_hpo('tim')))

    def test_input_validation(self):
        for payload in ({'hpos': []}, {'hpos': [{'id': 'HP:9999999', 'status': 'CÓ'}]},
                        {'hpos': [{'id': 'HP:0000252', 'status': 'KHÔNG'}]},
                        {'hpos': [{'id': 'HP:0000252', 'status': 'MAYBE'}]}):
            with self.assertRaises(ValueError):
                self.system.match(payload)

    def test_real_ranking_and_inheritance(self):
        result = self.system.match({'hpos': [{'id': 'HP:0009729', 'status': 'CÓ'}]})
        self.assertTrue(result['grouped'])
        self.assertTrue(result['candidates'])
        self.assertTrue(any(c['inheritance_modes'] for c in result['candidates']))
        for c in result['candidates']:
            if c['disease_id'] == 'OMIM:109400':
                self.assertEqual(c['model3_recommended_tests'], '')
        for c in result['candidates']:
            self.assertTrue(0 <= c['match_percentage'] <= 100)
            self.assertTrue(all(p['relation'] not in ('no_match', 'ignored') for p in c['matched_phenotypes']))
            self.assertNotIn('HP:0009729', {p['id'] for p in c['clinical_features_to_check']})

    def test_groups_follow_test_sequence(self):
        # Trisomy 18 pattern from gold case 2: AVSD, corpus callosum agenesis,
        # omphalocele, IUGR.
        hpos = [{'id': h, 'status': 'CÓ'} for h in ('HP:0001674', 'HP:0001274', 'HP:0001539', 'HP:0001511')]
        result = self.system.match({'hpos': hpos})
        order = ['NST', 'CNV', 'DON_GEN', 'CHUA_RO']
        groups = [c['group'] for c in result['candidates']]
        self.assertEqual(groups, sorted(groups, key=order.index))
        for name, limit in (('NST', 10), ('CNV', 10), ('DON_GEN', 10), ('CHUA_RO', 5)):
            items = [c for c in result['candidates'] if c['group'] == name]
            self.assertLessEqual(len(items), limit)
            self.assertEqual([c['rank'] for c in items], list(range(1, len(items) + 1)))
            overall = [c['overall_rank'] for c in items]
            self.assertEqual(overall, sorted(overall))
            self.assertTrue(all(c['matched_phenotypes'] for c in items))
        nst = [c['disease_id'] for c in result['candidates'] if c['group'] == 'NST']
        self.assertIn('ORPHA:3380', nst)

    def test_equivalent_ids_are_listed_once(self):
        # 4 HPO of the trisomy 18 pattern; the MONDO table merges OMIM/ORPHA pairs.
        hpos = [{'id': h, 'status': 'CÓ'} for h in ('HP:0001674', 'HP:0001274', 'HP:0001539', 'HP:0001511')]
        result = self.system.match({'hpos': hpos})
        resolve = self.system.resolver.resolve
        concepts = [resolve(c['disease_id']) for c in result['candidates']]
        self.assertEqual(len(concepts), len(set(concepts)))
        merged = [c for c in result['candidates'] if c['equivalent_ids']]
        self.assertTrue(merged)
        for c in merged:
            self.assertTrue(all(resolve(e) == resolve(c['disease_id']) for e in c['equivalent_ids']))

    def test_merge_matches_matcher_resolver(self):
        # Same result as HPOAgent2Matcher.rank with the resolver loaded, on the top-100.
        from hpo_agents.agent2_matcher import HPOAgent2Matcher
        from hpo_agents.agent2_schema import PhenotypeObservation, PhenotypeStatus, RankingStrategy
        obs = [PhenotypeObservation(h, PhenotypeStatus.PRESENT) for h in ('HP:0001636', 'HP:0012020')]
        s = self.system
        merged_here, _ = s.merge_equivalents(s.matcher.rank(obs, top_k=None, ranking_strategy=RankingStrategy.IC_COVERAGE))
        reference = HPOAgent2Matcher(s.matcher.profiles, s.matcher.ontology, disease_resolver=s.resolver)
        expected = reference.rank(obs, top_k=100, ranking_strategy=RankingStrategy.IC_COVERAGE)
        self.assertEqual([c.disease_id for c in merged_here[:100]], [c.disease_id for c in expected])

    def test_reranker_keeps_columns_and_top_overall(self):
        hpos = [{'id': h, 'status': 'CÓ'} for h in ('HP:0001674', 'HP:0001274', 'HP:0001539', 'HP:0001511')]
        result = self.system.match({'hpos': hpos})
        self.assertTrue(result['reranked'])
        ids = [c['disease_id'] for c in result['candidates']]
        self.assertTrue(1 <= len(result['top_overall']) <= 5)
        self.assertTrue(set(result['top_overall']) <= set(ids))
        order = ['NST', 'CNV', 'DON_GEN', 'CHUA_RO']
        groups = [c['group'] for c in result['candidates']]
        self.assertEqual(groups, sorted(groups, key=order.index))
        self.assertEqual(result['top_overall'][0], 'ORPHA:3380')  # trisomy 18 pattern

    def test_reranker_json_matches_feature_list(self):
        from model2_reranker import FEATURES, Reranker
        from web_service import RERANKER_FILES
        model = Reranker.load(RERANKER_FILES[-1])
        self.assertEqual(tuple(model.spec['features']), FEATURES)
        self.assertIsInstance(model.score([0.5] * len(FEATURES)), float)

    def test_wes_genes_rank_listed_genes_by_phenotype(self):
        # Cardiac rhabdomyoma with a WES list holding TSC2 among unrelated genes.
        result = self.system.match({'hpos': [{'id': 'HP:0009729', 'status': 'CÓ'}],
                                    'genes': 'BRCA2, TSC2, MYH7 , ttn; FGFR3 NOTAGENE1'})
        wes = result['wes']
        self.assertEqual(wes['genes_ranked'][0], 'TSC2')
        self.assertIn('NOTAGENE1', wes['unknown_genes'])
        by_id = {c['disease_id']: c for c in result['candidates']}
        first = by_id[wes['diseases'][0]]
        self.assertEqual(first['wes_rank'], 1)
        self.assertIn('TSC2', first['wes_genes'])
        columns = [c for c in result['candidates'] if c['in_column']]
        self.assertTrue(all(c['rank'] is not None for c in columns))
        with self.assertRaises(ValueError):
            self.system.match({'hpos': [{'id': 'HP:0009729', 'status': 'CÓ'}], 'genes': 'TSC2 <script>'})

    def test_no_genes_means_no_wes_block(self):
        result = self.system.match({'hpos': [{'id': 'HP:0009729', 'status': 'CÓ'}]})
        self.assertIsNone(result['wes'])
        self.assertTrue(all(c['in_column'] for c in result['candidates']))

    def test_requests_queue_instead_of_failing(self):
        import serve_web
        self.assertGreaterEqual(serve_web.QUEUE_SECONDS, 30)

    def test_model1_preload_failure_keeps_service_up(self):
        # 01-07/10/2026 the NVIDIA driver was missing after a reboot; the preload error stopped the
        # whole service, though search and matching need no GPU.
        import serve_web
        broken = Mock()
        broken.runner.load_model.side_effect = RuntimeError('Model 1 v6 cần GPU CUDA.')
        log = io.StringIO()
        with patch.object(serve_web, 'SYSTEM', broken), redirect_stderr(log):
            serve_web.preload_model1()
        self.assertIn('RuntimeError', log.getvalue())

    def test_v6_runner_without_gpu_raises_runtime_error(self):
        # unsloth's device check raises NotImplementedError at import when CUDA is unusable; its message
        # blames the AMD iGPU and advises reinstalling PyTorch for ROCm, which would break the NVIDIA setup.
        from model1_v6.runner import Model1V6Runner

        class NoGpuUnsloth(types.ModuleType):
            def __getattr__(self, name):
                if name.startswith('__'):
                    raise AttributeError(name)
                raise NotImplementedError('Unsloth detected signs of an AMD ROCm GPU')

        with tempfile.TemporaryDirectory() as bundle:
            Path(bundle, 'manifest.json').write_text('{}', encoding='utf-8')
            with patch.dict(sys.modules, {'unsloth': NoGpuUnsloth('unsloth')}):
                with self.assertRaisesRegex(RuntimeError, 'CUDA'):
                    Model1V6Runner(bundle).load_model()

    def test_shipped_category_table(self):
        from web_service import CATEGORY_FILES, load_categories
        table, version = load_categories(CATEGORY_FILES[-1])
        self.assertIn('mondo=', version)
        expected = {'ORPHA:870': 'NST', 'ORPHA:3380': 'NST', 'ORPHA:881': 'NST', 'ORPHA:567': 'CNV',
                    'ORPHA:904': 'CNV', 'OMIM:163950': 'DON_GEN', 'OMIM:309585': 'DON_GEN'}
        for disease, group in expected.items():
            self.assertEqual(table[disease][0], group, disease)
        self.assertTrue(table['ORPHA:567'][1])  # 22q11.2 deletion: mixed mechanism (TBX1)

    def test_extraction_requires_review_and_preserves_spans(self):
        s = self.system
        original = s.runner
        text = 'Không ghi nhận đầu nhỏ.'
        start = text.index('đầu nhỏ')
        s.runner = Mock()
        s.runner.extract_spans.return_value = [{'mention_text': 'đầu nhỏ', 'span_start': start, 'span_end': start + 7}]
        try:
            output = s.extract(text)
            self.assertTrue(output['review_required'])
            self.assertTrue(output['mentions'][0]['context_review'])
            self.assertFalse(output['mentions'][0]['approved'])
            self.assertTrue(output['mentions'][0]['candidates'])
        finally:
            s.runner = original

    def test_strict_alignment(self):
        with self.assertRaises(ValueError):
            align('đầu nhỏ, đầu nhỏ', '{"phrases":["đầu nhỏ"]}')
        with self.assertRaises(ValueError):
            align('tim bình thường', '{"phrases":["đầu nhỏ"]}')

    def test_v6_extraction_keeps_review_gate_and_ranked_candidates(self):
        from model1_v6.runner import Model1V6Runner
        s = self.system
        original = s.runner
        text = 'Thai 22 tuần, TD hẹp eo ĐMC, không thấy dạ dày'
        a, b = text.index('hẹp eo ĐMC'), text.index('dạ dày')
        s.runner = Mock(spec=Model1V6Runner)
        s.runner.extract.return_value = {'findings': [
            {'mention_text': 'hẹp eo ĐMC', 'span_start': a, 'span_end': a + 10, 'assertion': 'suspected',
             'hpo_ranked': [{'id': 'HP:0001680', 'retriever_cosine': 0.99, 'reranker_logprob': -0.01},
                            {'id': 'HP:9999999', 'retriever_cosine': 0.5, 'reranker_logprob': -9.0}]},
            {'mention_text': 'dạ dày', 'span_start': b, 'span_end': b + 6, 'assertion': 'present', 'alignment': 'unique',
             'hpo_ranked': [{'id': 'HP:9999999', 'retriever_cosine': 0.4, 'reranker_logprob': -1.0}]}],
            'errors': [], 'seconds': 1.0}
        try:
            out = s.extract(text)
            self.assertTrue(out['review_required'])
            self.assertEqual(out['engine'], 'model1_v6_ensemble')
            first, second = out['mentions']
            self.assertEqual((first['status'], first['approved']), ('NGHI NGỜ', False))
            self.assertEqual([c['id'] for c in first['candidates']], ['HP:0001680'])  # unknown IDs are dropped
            self.assertEqual(text[first['span_start']:first['span_end']], first['mention_text'])
            self.assertTrue(second['context_review'])  # "không" before the finding: doctor must choose the status
            self.assertEqual(second['mapping'], 'search_suggestion')  # no catalogue ID left -> dictionary search
        finally:
            s.runner = original

    def test_extraction_names_the_bundle_version(self):
        # The served bundle model1_v7_prod runs on the v6 runner; doctors saw "Model 1 v6" until 07/10/2026.
        from model1_v6.runner import Model1V6Runner
        s = self.system
        original = s.runner
        text = 'Thai đầu nhỏ.'
        start = text.index('đầu nhỏ')
        try:
            for manifest, expected in (({'model_version': 'v7'}, 'v7'), (None, 'v6')):
                s.runner = Mock(spec=Model1V6Runner)
                if manifest is not None:
                    s.runner.manifest = manifest
                s.runner.extract.return_value = {'findings': [
                    {'mention_text': 'đầu nhỏ', 'span_start': start, 'span_end': start + 7, 'assertion': 'present',
                     'hpo_ranked': [{'id': 'HP:0000252', 'retriever_cosine': 0.9, 'reranker_logprob': -0.1}]}],
                    'errors': [], 'seconds': 1.0}
                out = s.extract(text)
                self.assertEqual(out['model_version'], expected)
                self.assertTrue(out['mentions'][0]['explanation'].startswith('Model 1 ' + expected + ' '))
                self.assertEqual(out['engine'], 'model1_v6_ensemble')
        finally:
            s.runner = original

    def test_doctor_reviewed_phrase_is_listed_first(self):
        # 07/10/2026: "thai to" (reviewed as HP:0001520 large for gestational age) is outside the v7 retriever's
        # top 10, and the reranker put HP:0001518 small for gestational age first.
        from unittest.mock import patch
        from model1_v6.runner import Model1V6Runner
        from web_service import BASE_DIR
        s = self.system
        if (BASE_DIR / 'data/doctor_clinical_phrases.json').is_file():
            self.assertEqual(s.doctor_phrases.get('thai to'), ['HP:0001520'])
            # Organ-only fragments of "không thấy ..." sentences mean absence only in their sentence.
            self.assertLessEqual({'thận (p)', 'thận (t)', 'túi mật', 'xương mũi'}, s.context_phrases)
            self.assertNotIn('thai to', s.context_phrases)
        original = s.runner
        text = 'Thai 36 tuần, Thai to. Túi mật bình thường, đa ối.'
        a, g, b = text.index('Thai to'), text.index('Túi mật'), text.index('đa ối')
        s.runner = Mock(spec=Model1V6Runner)
        s.runner.extract.return_value = {'findings': [
            {'mention_text': 'Thai to', 'span_start': a, 'span_end': a + 7, 'assertion': 'present',
             'hpo_ranked': [{'id': 'HP:0001518', 'retriever_cosine': 0.5, 'reranker_logprob': -0.02},
                            {'id': 'HP:0001640', 'retriever_cosine': 0.68, 'reranker_logprob': -5.8}]},
            {'mention_text': 'Túi mật', 'span_start': g, 'span_end': g + 7, 'assertion': 'present',
             'hpo_ranked': [{'id': 'HP:0002240', 'retriever_cosine': 0.6, 'reranker_logprob': -0.5},
                            {'id': 'HP:0001640', 'retriever_cosine': 0.5, 'reranker_logprob': -3.0}]},
            {'mention_text': 'đa ối', 'span_start': b, 'span_end': b + 5, 'assertion': 'present',
             'hpo_ranked': [{'id': 'HP:0001561', 'retriever_cosine': 1.0, 'reranker_logprob': 0.0}]}],
            'errors': [], 'seconds': 1.0}
        try:
            with patch.object(s, 'doctor_phrases', {'thai to': ['HP:0001520'], 'túi mật': ['HP:0011467']}), \
                 patch.object(s, 'context_phrases', {'túi mật'}):
                big, gall, poly = s.extract(text)['mentions']
            self.assertEqual([c['id'] for c in big['candidates']], ['HP:0001520', 'HP:0001518', 'HP:0001640'])
            self.assertEqual(big['mapping'], 'doctor_reviewed_phrase')
            self.assertIn('bác sĩ đã duyệt', big['explanation'])
            # A fragment never goes first: "Túi mật bình thường" must not open with absent gallbladder.
            self.assertEqual([c['id'] for c in gall['candidates']], ['HP:0002240', 'HP:0001640', 'HP:0011467'])
            self.assertTrue(gall['context_review'])
            self.assertEqual(gall['mapping'], 'model1_v6_retriever_reranker')
            self.assertNotIn('bác sĩ đã duyệt', gall['explanation'])
            self.assertEqual([c['id'] for c in poly['candidates']], ['HP:0001561'])  # not reviewed: model order kept
            self.assertEqual(poly['mapping'], 'model1_v6_retriever_reranker')
        finally:
            s.runner = original

    def test_v6_contract_alignment_and_chunks(self):
        from model1_v6 import contract as C
        with self.assertRaises(ValueError):
            C.align('đầu nhỏ, đầu nhỏ', '{"findings":[{"text":"đầu nhỏ","assertion":"present"}]}')
        got = C.lenient_align('đầu nhỏ, đầu nhỏ', '{"findings":[{"text":"đầu nhỏ","assertion":"present"}]}')
        self.assertEqual((got[0]['span_start'], got[0]['alignment']), (0, 'ambiguous_first_occurrence'))
        text = 'Thai 30 tuần. ' + 'Tim: hẹp eo ĐMC, thông liên thất. ' * 60
        self.assertTrue(all(text[o:o + len(c)] == c for o, c in C.chunks(text, 1200)))

if __name__ == '__main__':
    unittest.main(verbosity=2)
