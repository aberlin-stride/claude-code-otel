import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import cache_monitor as cm


class HitRateTest(unittest.TestCase):
    def test_hit_rate_is_none_when_event_has_no_prompt_tokens(self):
        event = cm.Event(cache_read=0, cache_creation=0, input_tokens=0)
        self.assertIsNone(cm.hit_rate(event))


if __name__ == "__main__":
    unittest.main()
