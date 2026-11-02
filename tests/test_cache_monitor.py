import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import cache_monitor as cm

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def stream(session, seq, read, creation, input_tokens=2, event_name="api_request"):
    return {"stream": {
        "event_name": event_name, "session_id": session,
        "event_sequence": str(seq), "cache_read_tokens": str(read),
        "cache_creation_tokens": str(creation), "input_tokens": str(input_tokens),
        "query_source": "repl_main_thread",
    }, "values": [[str(seq), "claude_code.api_request"]]}


def loki_response(*streams):
    return {"status": "success",
            "data": {"resultType": "streams", "result": list(streams)}}


def closed_port_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return "http://127.0.0.1:%d" % port


class FakeLoki:
    """Real HTTP server on an ephemeral port serving a fixed Loki response."""

    def __init__(self, body, port=0, strict=True):
        self.body = body
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                request = urlparse(self.path)
                owner.requests.append(request)
                # Live Loki answers 400 when start is within ~10ms of its own
                # "now" (measured: 10ms -> 400, 100ms -> 200).
                start = int(parse_qs(request.query)["start"][0])
                too_recent = start > time.time_ns() - 50 * 10**6
                payload = (owner.body if isinstance(owner.body, bytes)
                           else json.dumps(owner.body).encode())
                self.send_response(400 if strict and too_recent else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self.url = "http://127.0.0.1:%d" % self._server.server_port
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


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


def miss(session, seq, source="repl_main_thread"):
    return cm.Event(cache_read=0, cache_creation=30000, input_tokens=0,
                    session_id=session, event_sequence=seq, query_source=source)


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

    def test_non_main_thread_request_does_not_spend_the_first_request_exemption(self):
        title = miss("s1", 1, "generate_session_title")
        main = miss("s1", 2)
        self.assertEqual(cm.ExemptionTracker().non_exempt([title, main]), [title])

    # Guard added after the implementation (same branch as the test above), never RED.
    def test_non_main_thread_request_does_not_spend_the_post_compaction_exemption(self):
        first, away = miss("s1", 1), miss("s1", 3, "away_summary")
        after = miss("s1", 4)
        compaction = cm.Compaction(session_id="s1", event_sequence=2, success=True)
        self.assertEqual(
            cm.ExemptionTracker().non_exempt([first, compaction, away, after]), [away])

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
            session_id="116076d4-cf41-45e7-83bc-086223d5666e", event_sequence=261,
            query_source="repl_main_thread"))

    def test_parsed_event_carries_query_source(self):
        events = cm.parse_events(load_fixture("loki_api_request.json"))
        by_seq = {e.event_sequence: e for e in events}
        self.assertEqual(by_seq[261].query_source, "repl_main_thread")

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


class FormatAlertTest(unittest.TestCase):
    def test_format_alert_summarizes_hit_rate_size_source_and_session(self):
        event = cm.Event(
            cache_read=10000, cache_creation=30000, input_tokens=2,
            session_id="116076d4-cf41-45e7-83bc-086223d5666e",
            event_sequence=7, query_source="repl_main_thread")
        self.assertEqual(cm.format_alert(event), (
            "Cache miss: 24% hit rate",
            "repl_main_thread · 40,002 prompt tokens · session 116076d4"))


class LokiFetchTest(unittest.TestCase):
    def test_loki_fetch_queries_range_endpoint_and_returns_json(self):
        fixture = load_fixture("loki_api_request.json")
        with FakeLoki(fixture) as loki:
            result = cm.loki_fetch(loki.url)(1793634000000000000)
        self.assertEqual(result, fixture)
        request = loki.requests[0]
        self.assertEqual(request.path, "/loki/api/v1/query_range")
        params = parse_qs(request.query)
        self.assertEqual(params["start"], ["1793634000000000000"])
        self.assertIn("api_request", params["query"][0])
        self.assertIn("compaction", params["query"][0])


class MonitorTest(unittest.TestCase):
    def test_poll_notifies_once_for_a_miss_after_the_exempt_first_event(self):
        body = loki_response(stream("a" * 36, 1, 0, 30000), stream("a" * 36, 2, 0, 30000))
        notified = []
        with FakeLoki(body) as loki:
            monitor = cm.Monitor(cm.loki_fetch(loki.url), lambda t, m: notified.append((t, m)))
            monitor.poll()
        self.assertEqual(notified, [(
            "Cache miss: 0% hit rate",
            "repl_main_thread · 30,002 prompt tokens · session aaaaaaaa")])

    # Guards added after the pipeline existed, so never RED.
    def test_healthy_cache_hits_do_not_notify(self):
        body = loki_response(stream("b" * 36, 1, 0, 30000), stream("b" * 36, 2, 90000, 1000))
        notified = []
        with FakeLoki(body) as loki:
            cm.Monitor(cm.loki_fetch(loki.url), lambda t, m: notified.append(t)).poll()
        self.assertEqual(notified, [])

    def test_polling_the_same_events_twice_notifies_once(self):
        body = loki_response(stream("c" * 36, 1, 0, 30000), stream("c" * 36, 2, 0, 30000))
        notified = []
        with FakeLoki(body) as loki:
            monitor = cm.Monitor(cm.loki_fetch(loki.url), lambda t, m: notified.append(t))
            monitor.poll()
            monitor.poll()
        self.assertEqual(len(notified), 1)

    def test_poll_survives_loki_being_down_and_returns_the_first_backoff_delay(self):
        monitor = cm.Monitor(cm.loki_fetch(closed_port_url()), lambda t, m: None)
        self.assertEqual(monitor.poll(), 5)

    def test_poll_recovers_after_an_outage_and_resets_the_backoff(self):
        url = closed_port_url()
        port = int(url.rsplit(":", 1)[1])
        monitor = cm.Monitor(cm.loki_fetch(url), lambda t, m: None)
        self.assertEqual([monitor.poll(), monitor.poll()], [5, 10])
        with FakeLoki(loki_response(), port):
            self.assertEqual(monitor.poll(), 5)
        self.assertEqual(monitor.poll(), 5)

    def test_an_outage_is_logged_once_not_on_every_retry(self):
        monitor = cm.Monitor(cm.loki_fetch(closed_port_url()), lambda t, m: None)
        with self.assertLogs("cache_monitor", "WARNING") as logs:
            for _ in range(3):
                monitor.poll()
        self.assertEqual(len(logs.records), 1)


class RunTest(unittest.TestCase):
    def test_run_sleeps_for_each_delay_poll_returns(self):
        monitor = cm.Monitor(cm.loki_fetch(closed_port_url()), lambda t, m: None)
        sleeps = []
        cm.run(monitor, sleeps.append, lambda: len(sleeps) >= 3)
        self.assertEqual(sleeps, [5, 10, 20])

    def test_first_poll_does_not_start_inside_lokis_rejection_window(self):
        body = loki_response(stream("d" * 36, 1, 0, 30000), stream("d" * 36, 2, 0, 30000))
        notified = []
        with FakeLoki(body) as loki:
            cm.Monitor(cm.loki_fetch(loki.url), lambda t, m: notified.append((t, m))).poll()
        self.assertEqual(notified, [(
            "Cache miss: 0% hit rate",
            "repl_main_thread · 30,002 prompt tokens · session dddddddd")])

    def test_alert_title_never_rounds_a_miss_up_to_the_threshold(self):
        event = cm.Event(cache_read=79600, cache_creation=20400, input_tokens=0,
                         session_id="s" * 36, query_source="repl_main_thread")
        self.assertEqual(cm.format_alert(event)[0], "Cache miss: 79% hit rate")


def scripted_fetch(*responses):
    """Fake fetch boundary: returns each response in turn (or raises it) and records start."""
    starts = []
    queue = list(responses)

    def fetch(start_ns):
        starts.append(start_ns)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return fetch, starts


def miss_stream(seq, session="s" * 36):
    return stream(session, seq, 0, 30000)


class MonitorWiringTest(unittest.TestCase):
    # These cover behavior that already worked; each was verified by mutation
    # (see CHANGELOG), not by a RED run.
    def setUp(self):
        self.notified = []
        self.clock = FakeClock()
        self.cooldown = cm.Cooldown(300, self.clock)

    def monitor(self, fetch, **kwargs):
        return cm.Monitor(fetch, lambda t, m: self.notified.append((t, m)),
                          cooldown=self.cooldown, **kwargs)

    def test_dedup_alone_prevents_a_repeat_alert_after_the_cooldown_expires(self):
        response = loki_response(miss_stream(1), miss_stream(2))
        monitor = self.monitor(scripted_fetch(response, response)[0])
        monitor.poll()
        self.clock.now += 400
        monitor.poll()
        self.assertEqual(len(self.notified), 1)

    def test_first_request_exemption_persists_across_polls(self):
        fetch, _ = scripted_fetch(
            loki_response(miss_stream(1)),
            loki_response(miss_stream(1), miss_stream(2)))
        monitor = self.monitor(fetch)
        monitor.poll()
        self.assertEqual(self.notified, [])
        monitor.poll()
        self.assertEqual(len(self.notified), 1)

    def test_compaction_exemption_applies_to_a_request_in_a_later_poll(self):
        compaction = stream("s" * 36, 2, 0, 0, event_name="compaction")
        compaction["stream"]["success"] = "true"
        first = loki_response(miss_stream(1), compaction)
        second = loki_response(miss_stream(1), compaction, miss_stream(3))
        third = loki_response(miss_stream(1), compaction, miss_stream(3), miss_stream(4))
        monitor = self.monitor(scripted_fetch(first, second, third)[0])
        for _ in range(3):
            monitor.poll()
            self.clock.now += 400
        # seq 3 is exempt (first after the compaction); only seq 4 alerts.
        self.assertEqual(len(self.notified), 1)

    def test_cooldown_suppresses_a_new_miss_in_a_later_poll(self):
        fetch, _ = scripted_fetch(
            loki_response(miss_stream(1), miss_stream(2)),
            loki_response(miss_stream(1), miss_stream(2), miss_stream(3)))
        monitor = self.monitor(fetch)
        monitor.poll()
        self.clock.now += 60
        monitor.poll()
        self.assertEqual(len(self.notified), 1)

    def test_a_healthy_request_does_not_start_the_cooldown(self):
        response = loki_response(
            miss_stream(1), stream("s" * 36, 2, 90000, 1000), miss_stream(3))
        self.monitor(scripted_fetch(response)[0]).poll()
        self.assertEqual(len(self.notified), 1)

    def test_poll_start_uses_launch_margin_then_overlap_and_holds_during_an_outage(self):
        launch, first, second, third = 1000 * 10**9, 1100 * 10**9, 1200 * 10**9, 1300 * 10**9
        times = iter([launch, first, second, third])
        fetch, starts = scripted_fetch(
            loki_response(), ConnectionRefusedError(), loki_response())
        monitor = self.monitor(fetch, now_ns=lambda: next(times))
        for _ in range(3):
            monitor.poll()
        self.assertEqual(starts, [
            launch - 5 * 10**9,    # first poll: launch margin
            first - 60 * 10**9,    # after success: 60s overlap
            first - 60 * 10**9])   # after a failed poll: unchanged

    def test_a_non_json_200_response_backs_off_instead_of_crashing(self):
        with FakeLoki(b"not json at all") as loki:
            monitor = cm.Monitor(cm.loki_fetch(loki.url), lambda t, m: None)
            self.assertEqual([monitor.poll(), monitor.poll()], [5, 10])

    def test_query_asks_for_forward_order_and_a_limit_of_1000(self):
        with FakeLoki(loki_response()) as loki:
            cm.loki_fetch(loki.url)(1793634000000000000)
        params = parse_qs(loki.requests[0].query)
        self.assertEqual(params["limit"], ["1000"])
        self.assertEqual(params["direction"], ["forward"])

    def test_cooldown_allows_an_alert_at_exactly_the_window_boundary(self):
        clock = FakeClock()
        cooldown = cm.Cooldown(300, clock)
        self.assertTrue(cooldown.allow("s1"))
        clock.now += 300
        self.assertTrue(cooldown.allow("s1"))

    def test_dedup_keys_per_session_so_equal_sequence_numbers_both_pass(self):
        a, b = miss("s1", 5), miss("s2", 5)
        self.assertEqual(cm.Dedup().fresh([a, b]), [a, b])


if __name__ == "__main__":
    unittest.main()
