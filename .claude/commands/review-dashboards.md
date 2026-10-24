---
description: Parallel-review both Grafana dashboard JSONs against live Prometheus/Loki before any edits
---

Launch two agents in parallel (single message, two Agent tool calls), each `subagent_type: "general-purpose"`:

1. **Overview reviewer** — reviews `claude-code-dashboard.json` only.
2. **Session reviewer** — reviews `claude-code-session-dashboard.json` only.

Each agent's brief must require it to check, for every panel in its assigned file:
- Units (e.g. tokens vs. bytes, seconds vs. ms) match what the metric actually reports.
- Interval/step settings are sane for the query's range and resolution.
- Field/label names referenced in queries exist on the real metrics/log streams — verify against live Prometheus (`http://localhost:9090/api/v1/label/__name__/values`, `/api/v1/series`) and Loki labels, using the `grafana` MCP server if available, not docs or README assumptions.
- Delta-temporality correctness: Claude Code metrics use delta temporality — flag any `increase()`/`rate()` usage that assumes cumulative counters, and flag any session/range total built from `increase(metric[$__range])` + a sum reduction instead of `sum(increase(...[$__range]))` with an instant query or a 'last' reduction.

Each agent returns a numbered list of findings (panel name, problem, live-data evidence, suggested fix) — read-only, no edits.

After both return, merge their numbered lists into one combined numbered list (continue numbering across files, noting which dashboard each item belongs to) and present it to the user.

Do not edit either dashboard file. Wait for the user to approve each numbered item individually before making any change. When editing is later approved, edit the dashboard JSON with targeted inserts/replacements only — never reformat or re-serialize the whole file — and restart Grafana (`docker compose restart grafana`) after changes, then commit.
