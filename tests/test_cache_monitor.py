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

    def test_high_hit_rate_event_is_not_significant(self):
        # 90% hit rate on 100k prompt tokens.
        event = cm.Event(cache_read=90000, cache_creation=10000, input_tokens=0)
        self.assertFalse(cm.is_significant(event))

    def test_event_below_minimum_prompt_size_is_not_significant(self):
        # 50% hit rate but only 10k prompt tokens: too small to matter.
        event = cm.Event(cache_read=5000, cache_creation=5000, input_tokens=0)
        self.assertFalse(cm.is_significant(event))

    # Boundary guards: added after the implementation, so they were never RED.
    def test_exactly_80_percent_hit_rate_is_not_significant(self):
        event = cm.Event(cache_read=16000, cache_creation=4000, input_tokens=0)
        self.assertFalse(cm.is_significant(event))

    def test_exactly_20k_prompt_tokens_is_significant(self):
        event = cm.Event(cache_read=10000, cache_creation=10000, input_tokens=0)
        self.assertTrue(cm.is_significant(event))


def miss(session, seq):
    return cm.Event(cache_read=0, cache_creation=30000, input_tokens=0,
                    session_id=session, event_sequence=seq)


class ExemptionTrackerTest(unittest.TestCase):
    def test_first_event_of_a_session_is_exempt_and_second_is_not(self):
        first, second = miss("s1", 1), miss("s1", 2)
        self.assertEqual(cm.ExemptionTracker().non_exempt([first, second]), [second])

    def test_first_event_of_each_session_is_exempt_independently(self):
        a1, b1, a2, b2 = miss("s1", 1), miss("s2", 2), miss("s1", 3), miss("s2", 4)
        self.assertEqual(cm.ExemptionTracker().non_exempt([a1, b1, a2, b2]), [a2, b2])

    def test_first_event_after_a_successful_compaction_is_exempt(self):
        e1, e2, e3 = miss("s1", 1), miss("s1", 3), miss("s1", 4)
        compaction = cm.Compaction(session_id="s1", event_sequence=2, success=True)
        self.assertEqual(cm.ExemptionTracker().non_exempt([e1, compaction, e2, e3]), [e3])


if __name__ == "__main__":
    unittest.main()
