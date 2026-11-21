import unittest
from unittest import mock

from selector import check_tpcds_inputs


def result(stdout, code=0):
    return mock.Mock(returncode=code, stdout=stdout)


class TpcdsCheckTests(unittest.TestCase):
    def test_starts_at_the_first_affected_query(self):
        with mock.patch("selector.subprocess.run", return_value=result("SAME\n")) as run:
            self.assertTrue(check_tpcds_inputs(["probe"], {}, 23))
        self.assertEqual(run.call_args.args[0], ["probe", "--from", "23"])

    def test_without_line_evidence_replays_everything(self):
        for first in (None, 0):
            with mock.patch("selector.subprocess.run", return_value=result("SAME\n")) as run:
                self.assertTrue(check_tpcds_inputs(["probe"], {}, first))
            self.assertEqual(run.call_args.args[0], ["probe"])

    def test_difference_or_failure_keeps_the_test(self):
        for r in (result("DIFF\n"), result("SAME\n", code=1), result("")):
            with mock.patch("selector.subprocess.run", return_value=r):
                self.assertFalse(check_tpcds_inputs(["probe"], {}, 5))
