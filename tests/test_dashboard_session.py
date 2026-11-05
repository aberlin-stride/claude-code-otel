import json
import os
import time
import unittest
import urllib.parse
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
DASHBOARD = os.path.join(ROOT, "claude-code-session-dashboard.json")
LOKI = "http://localhost:3100"
WEEK_NS = 7 * 24 * 3600 * 10**9


def load_panels():
    with open(DASHBOARD) as f:
        return {p["id"]: p for p in json.load(f)["panels"] if "id" in p}


def loki_get(path, **params):
    url = "%s%s?%s" % (LOKI, path, urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)["data"]["result"]


def loki_is_up():
    try:
        with urllib.request.urlopen(LOKI + "/ready", timeout=5) as resp:
            return resp.status == 200
    except OSError:
        return False


def real_session_id():
    now = time.time_ns()
    result = loki_get(
        "/loki/api/v1/query_range",
        query='{service_name=~".*claude-code.*"} | event_name="api_request"',
        limit=1, start=now - WEEK_NS, end=now)
    return result[0]["stream"]["session_id"]


def expr_for(panel_id, session_id):
    (target,) = load_panels()[panel_id]["targets"]
    return target["expr"].replace("$__range", "7d").replace("$session_id", session_id)


class CostPerRequestPanelsTest(unittest.TestCase):
    def test_table_lists_each_request_with_its_cost(self):
        panel = load_panels()[104]
        self.assertEqual(panel["type"], "table")
        self.assertEqual(panel["title"], "Cost per Request (Selected Session)")
        self.assertEqual(panel["datasource"]["uid"], "loki")
        (target,) = panel["targets"]
        self.assertIn('event_name="api_request"', target["expr"])
        self.assertIn('session_id="$session_id"', target["expr"])
        for field in ("request_id", "cost_usd", "model", "query_source"):
            self.assertIn(field, target["expr"])
        self.assertEqual(
            [t["id"] for t in panel["transformations"]][:2],
            ["extractFields", "convertFieldType"])

    def test_table_is_ordered_by_cost_most_expensive_first(self):
        sort = [t for t in load_panels()[104]["transformations"]
                if t["id"] == "sortBy"]
        self.assertEqual(len(sort), 1)
        self.assertEqual(
            sort[0]["options"]["sort"], [{"field": "cost_usd", "desc": True}])

    def test_trace_id_column_links_to_tempo(self):
        panel = load_panels()[104]
        (target,) = panel["targets"]
        self.assertIn("trace_id", target["expr"])
        (override,) = [o for o in panel["fieldConfig"]["overrides"]
                       if o["matcher"]["options"] == "trace_id"]
        (prop,) = [p for p in override["properties"] if p["id"] == "links"]
        self.assertIn("tempo", prop["value"][0]["url"])
        self.assertIn("${__value.raw}", prop["value"][0]["url"])

    def test_stat_shows_average_cost_per_request_as_one_loki_query(self):
        panel = load_panels()[105]
        self.assertEqual(panel["type"], "stat")
        self.assertEqual(panel["title"], "Avg Cost / Request (Selected Session)")
        self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "currencyUSD")
        (target,) = panel["targets"]
        self.assertTrue(target["instant"])
        self.assertEqual(target["datasource"]["uid"], "loki")
        self.assertIn("unwrap cost_usd", target["expr"])

    def test_panels_sit_above_the_timeline_without_overlap(self):
        panels = load_panels()
        table, stat = panels[104]["gridPos"], panels[105]["gridPos"]
        self.assertEqual((table["x"], table["y"], table["w"]), (0, 33, 18))
        self.assertEqual((stat["x"], stat["y"], stat["w"]), (18, 33, 6))
        self.assertEqual(panels[101]["gridPos"]["y"], table["y"] + table["h"])
        self.assertEqual(panels[102]["gridPos"]["y"], panels[101]["gridPos"]["y"] + 1)


@unittest.skipUnless(loki_is_up(), "loki is not reachable")
class CostPerRequestLiveDataTest(unittest.TestCase):
    def setUp(self):
        self.sid = real_session_id()
        now = time.time_ns()
        events = loki_get(
            "/loki/api/v1/query_range",
            query='{service_name=~".*claude-code.*"} | event_name="api_request" '
                  '| session_id="%s"' % self.sid,
            limit=5000, start=now - WEEK_NS, end=now)
        self.costs = [float(r["stream"]["cost_usd"])
                      for r in events for _ in r["values"]]

    def test_average_matches_mean_of_raw_event_costs(self):
        (row,) = loki_get(
            "/loki/api/v1/query", query=expr_for(105, self.sid), time=time.time_ns())
        self.assertAlmostEqual(
            float(row["value"][1]), sum(self.costs) / len(self.costs), places=6)

    def test_table_query_returns_one_line_per_request_as_json(self):
        now = time.time_ns()
        rows = loki_get(
            "/loki/api/v1/query_range", query=expr_for(104, self.sid),
            limit=5000, start=now - WEEK_NS, end=now)
        lines = [json.loads(v[1]) for r in rows for v in r["values"]]
        self.assertEqual(len(lines), len(self.costs))
        self.assertAlmostEqual(
            sum(float(l["cost_usd"]) for l in lines), sum(self.costs), places=6)
        self.assertTrue(all(l["request_id"].startswith("req_") for l in lines))


if __name__ == "__main__":
    unittest.main()
