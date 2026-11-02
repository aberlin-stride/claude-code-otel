import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import cache_monitor as cm


class HitRateTest(unittest.TestCase):
    def test_hit_rate_is_none_when_event_has_no_prompt_tokens(self):
        event = cm.Event(cache_read=0, cache_creation=0, input_tokens=0)
        self.assertIsNone(cm.hit_rate(event))

    def test_hit_rate_is_cache_read_share_of_prompt_tokens(self):
        # Token counts from a real Loki api_request event; 0.97046... is
        # 113161 / (113161 + 3442 + 2) worked by hand.
        event = cm.Event(cache_read=113161, cache_creation=3442, input_tokens=2)
        rate = cm.hit_rate(event)
        self.assertIsNotNone(rate)
        self.assertAlmostEqual(rate, 0.9704643883195403, places=6)


class IsSignificantTest(unittest.TestCase):
    def test_large_low_hit_rate_event_is_significant(self):
        # 50% hit rate on 40k prompt tokens.
        event = cm.Event(cache_read=20000, cache_creation=20000, input_tokens=0)
        self.assertTrue(cm.is_significant(event))


if __name__ == "__main__":
    unittest.main()
