import unittest
from unittest.mock import Mock

from lab.cloudflare import benchmark
from lab.runner import Failure


class CloudflareTests(unittest.TestCase):
    def test_median_and_larger_download_attempt(self):
        measure = Mock(side_effect=[
            {"ok": True, "response_ms": value} for value in (40, 500, 30, 35, 25)
        ] + [
            {"ok": True, "transfer_ms": 200},
            {"ok": True, "transfer_ms": 800},
            {"ok": False, "error": "timeout"},
            {"ok": True, "transfer_ms": 1000},
            {"ok": True, "transfer_ms": 500},
            {"ok": True, "transfer_ms": 1600},
            {"ok": False, "error": "timeout"},
            {"ok": True, "transfer_ms": 2000},
        ])
        port = object()
        result = benchmark(measure, port, 5_000_000, 1_000_000, lambda: False)
        self.assertTrue(all(call.args[1] is port for call in measure.call_args_list))
        self.assertEqual([call.args[3] for call in measure.call_args_list[5:]],
                         [250_000, 1_000_000, 5_000_000, 1_000_000, 1_000_000, 250_000, 250_000, 250_000])
        self.assertEqual((result["ping_ms"], result["ping_sample_ok"]), (35, 5))
        self.assertEqual((result["mbps"], result["sample_ok"], result["sample_total"]), (10, 4, 5))
        self.assertEqual((result["upload"]["mbps"], result["upload"]["sample_ok"]), (1.125, 2))

    def test_cancellation_stops_new_requests(self):
        measure = Mock(side_effect=Failure("cancelled"))
        with self.assertRaisesRegex(Failure, "cancelled"):
            benchmark(measure, object(), 1_000_000, 1_000_000, lambda: True)
        self.assertEqual(measure.call_count, 1)
