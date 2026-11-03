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


if __name__ == "__main__":
    unittest.main()
