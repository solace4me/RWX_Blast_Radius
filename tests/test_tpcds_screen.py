import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from selector import check_tpcds_inputs, scheduled_cost_ms


class TpcdsScreenTests(unittest.TestCase):
    def test_changed_query_stops_before_the_full_check(self):
        with patch("selector.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="DIFF\n")) as run:
            self.assertFalse(check_tpcds_inputs(["probe"], {}, (23,)))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["probe", "--queries", "23"])

    def test_matching_query_still_needs_the_ordered_check(self):
        results = [SimpleNamespace(returncode=0, stdout=text) for text in ("SAME\n", "DIFF\n")]
        with patch("selector.subprocess.run", side_effect=results) as run:
            self.assertFalse(check_tpcds_inputs(["probe"], {}, (23,)))
        self.assertEqual(run.call_args.args[0], ["probe"])

    def test_timeout_in_screen_does_not_change_the_full_result(self):
        with patch("selector.subprocess.run", side_effect=[
            subprocess.TimeoutExpired(["probe"], 1),
            SimpleNamespace(returncode=0, stdout="SAME\n"),
        ]):
            self.assertTrue(check_tpcds_inputs(["probe"], {}, (23,)))

    def test_empty_schedule_and_longest_worker_use_the_scorer_cost(self):
        self.assertEqual(scheduled_cost_ms([], {}), 0)
        self.assertEqual(scheduled_cost_ms([[1], [2, 3]], {1: 5000, 2: 2000, 3: 1000}), 9000)
