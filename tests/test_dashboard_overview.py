import json
import os
import subprocess
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
DASHBOARD = os.path.join(ROOT, "claude-code-dashboard.json")


def prometheus_is_up():
    try:
        out = subprocess.run(
            ["docker", "compose", "ps", "--status", "running", "--services"],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "prometheus" in out.stdout.split()


def promtool_daily_average(expr, spends, expected):
    """Run `expr` through promtool on a synthetic cost counter.

    `expected` is the hand-computed value the expression must return.
    spends[i] is the cost of local day i+1 (None = no data that day). Local
    days end 05:00 UTC (EST), so hour h belongs to day floor((h + 18) / 24).
    The counter ramps evenly through each day, which makes increase() exact.
    Returns promtool's exit code and combined output.
    """
    values, cumulative = [], 0.0
    for h in range(24 * len(spends) + 6):
        day = (h + 18) // 24
        spend = spends[day - 1] if 1 <= day <= len(spends) else None
        if spend is None:
            values.append("_")
        else:
            cumulative += spend / 24
            values.append(repr(cumulative))
    case = {
        "evaluation_interval": "1m",
        "tests": [{
            "interval": "1h",
            "input_series": [{
                "series": 'claude_code_cost_usage_USD_total{job="otel-collector"}',
                "values": " ".join(values),
            }],
            "promql_expr_test": [{
                # promtool compares exactly; round away increase() float noise
                "expr": "round(%s, 1)" % expr,
                "eval_time": "%dh" % (24 * (len(spends) + 1)),
                "exp_samples": [{"labels": "{}", "value": expected}],
            }],
        }],
    }
    done = subprocess.run(
        ["docker", "compose", "exec", "-T", "prometheus", "sh", "-c",
         "cat > /tmp/dash_test.json && promtool test rules /tmp/dash_test.json"],
        cwd=ROOT, input=json.dumps(case), capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout + done.stderr


def load_panels():
    with open(DASHBOARD) as f:
        return {p["id"]: p for p in json.load(f)["panels"] if "id" in p}


class OverviewLayoutTest(unittest.TestCase):
    def test_existing_overview_stats_are_four_wide_in_one_row(self):
        panels = load_panels()
        grid = [panels[i]["gridPos"] for i in (1, 2, 3, 4)]
        self.assertEqual([g["w"] for g in grid], [4, 4, 4, 4])
        self.assertEqual([g["x"] for g in grid], [0, 4, 8, 12])
        self.assertEqual({g["y"] for g in grid}, {1})


class DailyAveragePanelTest(unittest.TestCase):
    def test_daily_average_panel_is_a_stat_next_to_the_four(self):
        panels = load_panels()
        self.assertIn(44, panels)
        panel = panels[44]
        self.assertEqual(panel["type"], "stat")
        self.assertEqual(panel["title"], "Avg Cost / Day (all time)")
        self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "currencyUSD")
        self.assertEqual(
            panel["gridPos"], {"h": 4, "w": 4, "x": 16, "y": 1})
        self.assertEqual(panel["options"]["reduceOptions"]["calcs"], ["lastNotNull"])
        (target,) = panel["targets"]
        self.assertTrue(target["instant"])
        self.assertEqual(target["datasource"]["uid"], "prometheus")


class WeeklyAveragePanelTest(unittest.TestCase):
    def test_weekly_average_panel_completes_the_row_of_six(self):
        panels = load_panels()
        self.assertIn(45, panels)
        panel = panels[45]
        self.assertEqual(panel["type"], "stat")
        self.assertEqual(panel["title"], "Avg Cost / Week (30d)")
        self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "currencyUSD")
        self.assertEqual(
            panel["gridPos"], {"h": 4, "w": 4, "x": 20, "y": 1})
        self.assertEqual(panel["options"]["reduceOptions"]["calcs"], ["lastNotNull"])
        (target,) = panel["targets"]
        self.assertTrue(target["instant"])
        self.assertEqual(target["datasource"]["uid"], "prometheus")


@unittest.skipUnless(prometheus_is_up(), "prometheus container is not running")
class DailyAverageQueryTest(unittest.TestCase):
    def daily_expr(self):
        (target,) = load_panels()[44]["targets"]
        return target["expr"]

    def test_daily_average_counts_only_days_with_spend(self):
        code, output = promtool_daily_average(
            self.daily_expr(), [100, 200, None, 300], expected=200)
        self.assertEqual(code, 0, output)

    def test_daily_average_drops_days_under_fifteen_percent_of_median(self):
        code, output = promtool_daily_average(
            self.daily_expr(), [100] * 6 + [10], expected=100)
        self.assertEqual(code, 0, output)

    def test_weekly_average_is_the_mean_of_rolling_seven_day_sums(self):
        (target,) = load_panels()[45]["targets"]
        # 30 daily steps: 10 windows of 700, transition windows 800..1300,
        # 14 windows of 1400 -> 32900 / 30 = 1096.67
        code, output = promtool_daily_average(
            target["expr"], [100] * 20 + [200] * 20, expected=1097)
        self.assertEqual(code, 0, output)

    def test_daily_average_keeps_days_over_fifteen_percent_of_median(self):
        # 20 sits between the 15% cutoff (15) and a 25% cutoff (25)
        code, output = promtool_daily_average(
            self.daily_expr(), [100, 100, 100, 20], expected=80)
        self.assertEqual(code, 0, output)


def promtool_last_sample_age(expr, values, eval_time, expected):
    """Run `expr` through promtool on a synthetic token counter, one sample per minute."""
    case = {
        "evaluation_interval": "1m",
        "tests": [{
            "interval": "1m",
            "input_series": [{
                "series": 'claude_code_token_usage_tokens_total{job="otel-collector"}',
                "values": values,
            }],
            "promql_expr_test": [{
                "expr": expr,
                "eval_time": eval_time,
                "exp_samples": [{"labels": "{}", "value": expected}],
            }],
        }],
    }
    done = subprocess.run(
        ["docker", "compose", "exec", "-T", "prometheus", "sh", "-c",
         "cat > /tmp/stale_test.json && promtool test rules /tmp/stale_test.json"],
        cwd=ROOT, input=json.dumps(case), capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout + done.stderr


class LastTelemetryPanelTest(unittest.TestCase):
    def test_panel_is_a_full_width_stat_below_the_overview_row(self):
        panel = load_panels()[46]
        self.assertEqual(panel["type"], "stat")
        self.assertEqual(panel["title"], "Last Telemetry Received")
        self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "s")
        self.assertEqual(panel["gridPos"], {"h": 3, "w": 24, "x": 0, "y": 5})
        self.assertEqual(panel["options"]["reduceOptions"]["calcs"], ["lastNotNull"])
        (target,) = panel["targets"]
        self.assertTrue(target["instant"])
        self.assertEqual(target["datasource"]["uid"], "prometheus")

    def test_thresholds_turn_red_after_four_hours(self):
        steps = load_panels()[46]["fieldConfig"]["defaults"]["thresholds"]["steps"]
        self.assertEqual(
            [(s["color"], s["value"]) for s in steps],
            [("green", None), ("yellow", 3600), ("red", 14400)])

    def test_no_panels_overlap(self):
        with open(DASHBOARD) as f:
            boxes = [(p.get("title"), p["gridPos"]) for p in json.load(f)["panels"]]
        for i, (a_title, a) in enumerate(boxes):
            for b_title, b in boxes[i + 1:]:
                overlap = (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                           and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])
                self.assertFalse(overlap, "%s overlaps %s" % (a_title, b_title))


@unittest.skipUnless(prometheus_is_up(), "prometheus container is not running")
class LastTelemetryQueryTest(unittest.TestCase):
    def expr(self):
        (target,) = load_panels()[46]["targets"]
        return target["expr"]

    def test_age_is_seconds_since_the_newest_sample(self):
        # samples at minutes 0-2, then silence; at minute 60 the age is 58 min
        code, output = promtool_last_sample_age(
            self.expr(), "1 2 3 _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _",
            "60m", expected=3480)
        self.assertEqual(code, 0, output)

    def test_age_survives_a_gap_longer_than_the_five_minute_staleness_window(self):
        # a plain timestamp() would return nothing here; the subquery must not
        code, output = promtool_last_sample_age(
            self.expr(), "1 2 3", "40m", expected=2280)
        self.assertEqual(code, 0, output)


if __name__ == "__main__":
    unittest.main()
