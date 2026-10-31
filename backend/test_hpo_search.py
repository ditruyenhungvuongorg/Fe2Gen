import unittest
from unittest.mock import Mock

from web_service import WebSystem


class HpoSearchTests(unittest.TestCase):
    def test_search_prefers_doctor_wording_exact_accents_and_usage(self):
        from hpo_search import HpoSearch
        names = [('Tím', 'Cyanosis', '', 'HP:1'), ('Bất thường hình thái tim', 'Abnormal heart morphology', '', 'HP:2'),
                 ('Mặt nhỏ', 'Small face', '', 'HP:3'), ('Mắt nhỏ', 'Microphthalmia', '', 'HP:4'),
                 ('Lớn so với tuổi thai', 'Large for gestational age', '', 'HP:5'), ('Hình thái tổn thương da', 'Skin lesion', '', 'HP:6'),
                 ('Giãn não thất', 'Ventriculomegaly', '', 'HP:7'), ('Não úng thủy', 'Hydrocephalus', 'water on the brain', 'HP:8')]
        search = HpoSearch(names, [('thai to', 'HP:5'), ('thai tô', 'HP:6'), ('tím', 'HP:1'), ('tim', 'HP:1')], {'HP:5': 9})
        self.assertEqual(search.rank('thai to')[0][0], 'HP:5')
        self.assertEqual(search.rank('tim')[0][0], 'HP:2')  # the accent-free 'tim' of 'tím' is not the word 'tim'
        self.assertEqual(search.rank('mắt nhỏ')[0][:2], ['HP:4', 'HP:3'])
        self.assertEqual(search.rank('mat nho')[0][0] in ('HP:3', 'HP:4'), True)
        self.assertEqual(search.rank('dãn não thất')[0][0], 'HP:7')
        self.assertEqual(search.rank('dan nao that')[0][0], 'HP:7')
        self.assertEqual(search.rank('não úng thuỷ')[0][0], 'HP:8')
        self.assertEqual(search.rank('hydrocephalus'), (['HP:8'], 1))
        self.assertEqual(search.rank('water on')[0], ['HP:8'])
        self.assertEqual(search.rank('HP:4', 'HP:4')[1], -10)
        self.assertEqual(search.rank('khong co gi'), ([], None))

    def test_search_drops_the_accent_free_copy_of_a_phrase(self):
        from hpo_search import HpoSearch
        search = HpoSearch([('Tim to', 'Cardiomegaly', '', 'HP:2')], [('tím', 'HP:1'), ('tim', 'HP:1')])
        self.assertEqual([t[0] for t in search.terms if t[4] == 'HP:1'], ['tím'])

    def test_search_asks_the_retriever_only_when_text_is_weak(self):
        s = WebSystem()
        s.canonical = {h: {'vi': h, 'en': h} for h in ('HP:1', 'HP:2', 'HP:3')}
        s.labels, s.aliases, s.corrections_search = {}, {}, {}
        s.hpo_search = Mock(rank=Mock(return_value=(['HP:3'], 7)))
        s.runner = Mock(similar=Mock(return_value=[dict(id='HP:1', cosine=0.9), dict(id='HP:2', cosine=0.5)]))
        self.assertEqual([t['id'] for t in s.search_hpo('não úng nước')], ['HP:1', 'HP:3', 'HP:2'])
        s.runner.similar.return_value = None  # model not loaded or busy extracting
        self.assertEqual([t['id'] for t in s.search_hpo('não úng nước')], ['HP:3'])
        s.hpo_search.rank.return_value = (['HP:3'], 0)
        s.runner.similar.reset_mock()
        self.assertEqual([t['id'] for t in s.search_hpo('thai to')], ['HP:3'])
        s.runner.similar.assert_not_called()

    def test_similar_never_loads_or_waits(self):
        from model1_process import Model1Process
        runner = Model1Process('v6', '/nonexistent')
        self.assertIsNone(runner.similar('thai to'))  # not loaded
        runner.state = 'ready'
        runner.call_lock.acquire()
        try:
            self.assertIsNone(runner.similar('thai to'))  # an extraction is running
        finally:
            runner.call_lock.release()


if __name__ == '__main__':
    unittest.main()
