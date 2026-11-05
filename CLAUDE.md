# Claude Code Dashboards

## Grafana Dashboards
- Before you call any panel, label, or metric a 'bug' or delete it, check it against live data. Query Prometheus at http://localhost:9090/api/v1/label/__name__/values or /api/v1/series, and query Loki labels. Live data wins over docs or README.
- Edit dashboard JSON with targeted inserts or replacements. Never reformat or re-serialize the whole file.
- For session or range totals of counters, avoid `increase(metric[$__range])` combined with a sum reduction. Use `sum(increase(...[$__range]))` with an instant query or a 'last' reduction so totals are not inflated.
- Claude Code metrics use delta temporality. Account for this when writing rate or increase queries.
- After changing dashboards, restart Grafana (`docker compose restart grafana`), then commit.
Add under a


## Telemetry Stack Facts
- The stack runs on Colima/Docker. Port forwards can fail, so check `colima status` and the published ports first.
- In container configs (Tempo, OTel collector), services must bind to 0.0.0.0, not 127.0.0.1.
- In docker-compose, volumes belong under `volumes:`, never under `environment:`.
- Real skill and tool names require OTEL_LOG_TOOL_DETAILS=1. Env or flag changes apply only to NEW Claude sessions.
- Only MCP tools carry cost attribution. Built-in tools do not.
- There is no local Datadog exporter.
- Export Claude Code telemetry over OTLP HTTP (`http/protobuf`, `http://localhost:4318`). gRPC on 4317 connects but delivers nothing, with no client error. `env` in `~/.claude/settings.json` overrides shell exports.
- Port 8889 is not published to the host, so `curl localhost:8889/metrics` always fails. Check Prometheus (`localhost:9090`) or Loki instead.
- Per-request and per-session cost totals from Loki `api_request` events (`cost_usd`) exceed Prometheus `max_over_time` totals; Prometheus undercounts. Use Loki for per-request cost.
- `make up` also starts `scripts/cache_monitor.py` on the host (not in Docker). It polls Loki at `localhost:3100` and sends macOS notifications for cache misses, so check `colima status` and the published Loki port first if it logs "Loki unreachable". Tests: `python3 -m unittest discover -s tests`. State: `.cache-monitor.pid` and `.cache-monitor.log` in the repo root.


## Working Agreements
- When applying a list of fixes (review findings, hardening items), confirm each item with me before applying it, and record it in CHANGELOG.
- Commit after each completed logical step with a descriptive message.
- Use parallel agents: one reviews the overview dashboard JSON and one reviews the session dashboard JSON. Each checks units, intervals, field names, and delta-temporality correctness, and validates queries against live Prometheus. Merge the results into a numbered list. Don't edit anything until I approve each item.
- Query Prometheus, Loki, and Grafana directly instead of relying on docs by using MCP servers. Ask me to enable the MCP servers

