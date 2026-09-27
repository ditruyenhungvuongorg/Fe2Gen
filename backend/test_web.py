import unittest
from unittest.mock import Mock
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

    def test_requests_queue_instead_of_failing(self):
        import serve_web
        self.assertGreaterEqual(serve_web.QUEUE_SECONDS, 30)

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
