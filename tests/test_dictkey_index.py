import unittest

from dictkey_index import edited_dict_value

BASE = '''class D:
    class Tokenizer:
        ESC = {"x": (16, 2), "u": (16, 4)}
    MAP = {"a": "b"}
'''


class EditedDictValueTests(unittest.TestCase):
    def test_one_value_in_a_nested_class(self):
        after = BASE.replace('"u": (16, 4)', '"u": (17, 4)')
        self.assertEqual(edited_dict_value(BASE, after), ("D.Tokenizer", "ESC", "u"))

    def test_key_changes_are_not_value_edits(self):
        self.assertIsNone(edited_dict_value(BASE, BASE.replace('"u":', '"v":')))
        self.assertIsNone(edited_dict_value(BASE, BASE.replace('"a": "b"', '"a": "b", "c": "d"')))

    def test_two_values_or_other_code_is_rejected(self):
        two = BASE.replace('(16, 2)', '(8, 2)').replace('(16, 4)', '(8, 4)')
        self.assertIsNone(edited_dict_value(BASE, two))
        other = BASE.replace('"u": (16, 4)', '"u": (17, 4)') + "X = 1\n"
        self.assertIsNone(edited_dict_value(BASE, other))
