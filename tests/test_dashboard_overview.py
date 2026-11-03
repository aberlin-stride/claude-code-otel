import json
import os
import unittest

DASHBOARD = os.path.join(os.path.dirname(__file__), "..", "claude-code-dashboard.json")


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


if __name__ == "__main__":
    unittest.main()
