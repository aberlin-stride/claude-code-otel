import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import cache_monitor as cm

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


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

    def test_failed_compaction_does_not_exempt_the_next_event(self):
        e1, e2 = miss("s1", 1), miss("s1", 3)
        compaction = cm.Compaction(session_id="s1", event_sequence=2, success=False)
        self.assertEqual(cm.ExemptionTracker().non_exempt([e1, compaction, e2]), [e2])

    def test_events_are_evaluated_in_event_sequence_order(self):
        earlier, later = miss("s1", 1), miss("s1", 2)
        self.assertEqual(cm.ExemptionTracker().non_exempt([later, earlier]), [later])

    # Guard added after the implementation (state is per instance), never RED.
    def test_exemptions_carry_over_between_batches(self):
        tracker = cm.ExemptionTracker()
        first, second = miss("s1", 1), miss("s1", 2)
        self.assertEqual(tracker.non_exempt([first]), [])
        self.assertEqual(tracker.non_exempt([second]), [second])


class ParseEventsTest(unittest.TestCase):
    def test_parses_api_request_fixture_into_events(self):
        events = cm.parse_events(load_fixture("loki_api_request.json"))
        self.assertEqual(len(events), 6)
        by_seq = {e.event_sequence: e for e in events}
        self.assertEqual(by_seq[261], cm.Event(
            cache_read=95637, cache_creation=1458, input_tokens=2,
            session_id="116076d4-cf41-45e7-83bc-086223d5666e", event_sequence=261))

    def test_parses_compaction_fixture_into_compaction(self):
        items = cm.parse_events(load_fixture("loki_compaction.json"))
        self.assertEqual(items, [cm.Compaction(
            session_id="4da3f278-2caa-4b96-a720-ae55585c03f3",
            event_sequence=3115, success=True)])

    def test_malformed_streams_are_skipped_without_losing_the_rest(self):
        response = load_fixture("loki_api_request.json")
        streams = [r["stream"] for r in response["data"]["result"]]
        del streams[0]["cache_read_tokens"]
        streams[1]["input_tokens"] = "not-a-number"
        streams[2]["event_name"] = "tool_result"
        self.assertEqual(len(cm.parse_events(response)), 3)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CooldownTest(unittest.TestCase):
    def test_first_alert_for_a_session_is_allowed(self):
        self.assertTrue(cm.Cooldown(300, FakeClock()).allow("s1"))

    def test_second_alert_inside_the_window_is_suppressed(self):
        clock = FakeClock()
        cooldown = cm.Cooldown(300, clock)
        self.assertTrue(cooldown.allow("s1"))
        clock.now += 60
        self.assertFalse(cooldown.allow("s1"))

    def test_alert_is_allowed_again_after_the_window_passes(self):
        clock = FakeClock()
        cooldown = cm.Cooldown(300, clock)
        self.assertTrue(cooldown.allow("s1"))
        clock.now += 301
        self.assertTrue(cooldown.allow("s1"))

    # Guard added after the implementation (state is keyed by session), never RED.
    def test_cooldown_is_independent_per_session(self):
        cooldown = cm.Cooldown(300, FakeClock())
        self.assertTrue(cooldown.allow("s1"))
        self.assertTrue(cooldown.allow("s2"))


class DedupTest(unittest.TestCase):
    def test_unseen_items_are_returned(self):
        items = [miss("s1", 1), miss("s1", 2)]
        self.assertEqual(cm.Dedup().fresh(items), items)

    def test_items_seen_in_an_earlier_poll_are_dropped(self):
        dedup = cm.Dedup()
        first, second = miss("s1", 1), miss("s1", 2)
        dedup.fresh([first])
        self.assertEqual(dedup.fresh([first, second]), [second])

    # Guard added after the implementation (same key for both types), never RED.
    def test_a_repeated_compaction_is_dropped(self):
        dedup = cm.Dedup()
        compaction = cm.Compaction(session_id="s1", event_sequence=5, success=True)
        self.assertEqual(dedup.fresh([compaction]), [compaction])
        self.assertEqual(dedup.fresh([compaction]), [])


class BackoffTest(unittest.TestCase):
    def test_first_failure_retries_after_the_base_delay(self):
        self.assertEqual(cm.Backoff(5, 60).failed(), 5)

    def test_repeated_failures_double_up_to_the_cap(self):
        backoff = cm.Backoff(5, 60)
        self.assertEqual([backoff.failed() for _ in range(6)], [5, 10, 20, 40, 60, 60])

    def test_success_resets_the_delay_to_the_base(self):
        backoff = cm.Backoff(5, 60)
        backoff.failed()
        backoff.failed()
        backoff.succeeded()
        self.assertEqual(backoff.failed(), 5)

    def test_in_outage_is_true_between_a_failure_and_the_next_success(self):
        backoff = cm.Backoff(5, 60)
        self.assertFalse(backoff.in_outage)
        backoff.failed()
        self.assertTrue(backoff.in_outage)
        backoff.succeeded()
        self.assertFalse(backoff.in_outage)


class NotifyTest(unittest.TestCase):
    def test_notify_runs_osascript_with_title_and_message_verbatim(self):
        calls = []
        cm.notify('Cache "miss" \\ 42%', 'He said "hi" \\ and left', calls.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "osascript")
        self.assertEqual(calls[0][-2:], ['He said "hi" \\ and left', 'Cache "miss" \\ 42%'])

    def test_notify_swallows_runner_failures(self):
        def broken_runner(argv):
            raise OSError("osascript missing")

        cm.notify("title", "message", broken_runner)


if __name__ == "__main__":
    unittest.main()
