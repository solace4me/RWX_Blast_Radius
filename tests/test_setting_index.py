import unittest

from setting_index import SettingIndex, edited_setting


class SettingIndexTests(unittest.TestCase):
    def test_recognizes_only_a_single_literal_setting_edit(self):
        original = "class DialectName(Dialect):\n    OFFSET = -1\n    OTHER = True\n"
        revised = original.replace("-1", "-2")
        self.assertEqual(edited_setting(original, revised), ("DialectName", "OFFSET", None))
        self.assertIsNone(edited_setting(original, revised.replace("OTHER = True", "OTHER = False")))
        self.assertIsNone(edited_setting(original, original.replace("-1", "new_offset()")))

    def test_dictionary_edit_requires_one_existing_key(self):
        original = "class DialectName(Dialect):\n    TABLE = {'unit': 1, 'week': 2}\n"
        self.assertEqual(
            edited_setting(original, original.replace("'week': 2", "'week': 3")),
            ("DialectName", "TABLE", "week"),
        )
        self.assertIsNone(edited_setting(original, original.replace("'week': 2", "'month': 2")))

    def test_narrowing_keeps_unverified_and_whole_setting_readers(self):
        index = SettingIndex(
            verified={1, 2, 3},
            settings={
                "example.py": {
                    ("DialectName", "TABLE"): ({1}, {"week": {2}, "unit": {3}}),
                }
            },
        )
        narrowed = index.narrow("example.py", ("DialectName", "TABLE", "week"), {1, 2, 3, 9})
        self.assertEqual(narrowed, {1, 2, 9})
        self.assertIsNone(index.narrow("example.py", ("DialectName", "MISSING", None), {1, 2}))

    def test_observed_reader_outside_original_selection_disables_narrowing(self):
        index = SettingIndex(
            verified={1, 2},
            settings={"example.py": {("DialectName", "OFFSET"): ({2}, {})}},
        )
        self.assertIsNone(index.narrow("example.py", ("DialectName", "OFFSET", None), {1}))
