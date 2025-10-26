import unittest
from index import WordIndex

WORDS = [
    'hello',
    'hallo',
    'hxllo',
    'heLLo',
    'world',
    'hell',
    'shell',
]


class TestWordIndexMatching(unittest.TestCase):
    def setUp(self):
        self.index = WordIndex(list(WORDS))

    def test_exact_question_marks(self):
        res = self.index.find_exact('h?llo')
        self.assertEqual(res, ['hello', 'hallo', 'hxllo', 'heLLo'])
        cnt = self.index.count_exact('h?llo')
        self.assertEqual(cnt, 4)

    def test_exact_with_star(self):
        # '^h.*o$' anchored, should match the four 'h?llo' variants
        res = self.index.find_exact('h*o')
        self.assertEqual(res, ['hello', 'hallo', 'hxllo', 'heLLo'])
        self.assertEqual(self.index.count_exact('h*o'), 4)

    def test_exact_no_match(self):
        self.assertEqual(self.index.find_exact('abc'), [])
        self.assertEqual(self.index.count_exact('abc'), 0)

    def test_partial_substring(self):
        res = self.index.find_partial('ell')
        self.assertEqual(sorted(res), sorted(['hello', 'heLLo', 'hell', 'shell']))
        self.assertEqual(self.index.count_partial('ell'), 4)

    def test_partial_all_questions(self):
        # '??' matches any 2-length substring; any word with len>=2 qualifies
        res = self.index.find_partial('??')
        expected = list(WORDS)  # all words in this fixture have len>=2
        self.assertEqual(res, expected)
        self.assertEqual(self.index.count_partial('??'), len(expected))


if __name__ == '__main__':
    unittest.main()
