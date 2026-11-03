# Changelog

Notable config decisions for this stack. Newest first.

## 2026-11-03

### Added average cost per day and per week to the overview
The overview row is now six 4-wide stats. The two new panels (ids 44 and 45) are covered by `tests/test_dashboard_overview.py`, which runs the real panel queries through `promtool` in the Prometheus container on synthetic cost counters (the query tests skip when that container is not running).

- **Avg Cost / Day (all time):** mean of local days that had spend. Days under 15% of the median day are dropped (low outliers only, your choice). The median is pinned once with `@ end()` so every step uses the same cutoff. On live data the cutoff drops nothing yet: the lowest day (10.6) sits just above it (10.2).
- **Avg Cost / Week (30d):** mean of the rolling 7-day sums, evaluated daily over 30 days. No outlier filtering.
- **30-day cap on "all time":** Prometheus retention is `--storage.tsdb.retention.time=30d` (`docker-compose.yml`). Once data is older than 30 days it drops out, so the daily panel covers at most the last 30 days. Raising retention is a separate change.
- **Weekly bias:** only 30 days are kept, so once history exceeds 30 days the oldest 7-day windows are truncated and pull the weekly average down. A `[23d:1d]` range would avoid that; `[30d:1d]` was kept as chosen.
- **Fixed EST day boundary:** days are aligned to local midnight with `offset 19h` because Prometheus cannot read the dashboard timezone. **Follow-up:** change `19h` to `18h` in both panels when daylight saving time starts on 2027-03-08, and back to `19h` in November. The boundary is also wrong when you are in another timezone.
- **Partial first day:** data starts at 22:00 on 2026-10-23, so that day is a partial day.

## 2026-11-02

### Fixed cache-miss monitor after an adversarial review (Opus critic)
Each item below was confirmed one at a time and fixed test-first on `fix/cache-monitor-critic-findings`:

1. **Exemptions were spent by helper requests.** `generate_session_title`, `away_summary` and similar requests share the session id and arrive first, so the first-request and post-compaction exemptions were used up and the real first request alerted (verified by the critic on live Loki). Only `repl_main_thread` requests now take or grant an exemption.
2. **Subagent bursts spent the cooldown.** One session would have produced about 50 alerts, almost all subagent cold starts. Significant misses from one session in one poll are now a single grouped notification, and the cooldown applies per session group. (Your choice: group instead of exempting subagents.)
3. **Reused PID.** A stale pidfile naming an unrelated live process made `make monitor` skip the start and `make stop-monitor` kill that process. Both now require the process command line to contain `cache_monitor.py`.
4. **Failed start reported success.** `make monitor` now waits a second, and exits 1 with the log tail if the process died. `make up` ignores that failure on purpose (previously untested; now tested).
5. **Truncated or malformed Loki responses killed the monitor.** `IncompleteRead`, `BadStatusLine` and an unexpected response shape now back off like any outage.
6. **Alert title rounded up.** 79.6% showed as "80%". It is now floored with integer arithmetic.
7. **Tests that could not fail.** The critic showed 12 mutations the suite missed (no dedup, fresh dedup/tracker/cooldown per poll, cooldown before the significance check, no overlap, `limit`/`direction` removed, only `OSError` caught, `<=` cooldown, dedup keyed on sequence only). The new `MonitorWiringTest` tests isolate each mechanism. A mutation harness over the 13 mutations (the critic's plus one it surfaced, a fresh `Cooldown` per poll) now kills all of them. The critic's backoff-order mutation (M17) was not added.
8. **Docs.** README exemption claim corrected, `make status` no longer says Loki is internal only (it is published on `127.0.0.1:3100`), and the "one per turn" and "cumulative" wording above.

Process notes: two of my tests were wrong the first time. A zombie `sleep` child made `os.kill(pid, 0)` report a killed process as alive, so that test passed against the buggy recipe until it used `Popen.poll()`. A shell negation (`! A && B`) in the new start check left its test red for one commit (`d54111a`).

Accepted limitations (recorded, not fixed):
- Events are skipped when a Loki outage leaves more than 1000 unread events (no pagination, no warning).
- A host and Colima VM clock skew above about 60s would cause the HTTP 400 described below, logged as "unreachable".
- The seen-event and per-session state is never pruned (negligible at the live rate).
- `make down` then `make up` within about 10s can briefly run two monitors.
- A possible spurious "Loki unreachable" line right after `make up`, while Loki is still starting (not verified).

### Added a macOS cache-miss notification monitor
`scripts/cache_monitor.py` (stdlib Python, runs on the host), started by `make up` and stopped by `make down`. New targets: `monitor`, `stop-monitor`, `logs-monitor`. Tests in `tests/` (`python3 -m unittest discover -s tests`).

Decisions, confirmed one at a time:

1. **Trigger:** per-request hit rate below 80% on at least 20,000 prompt tokens. The 20k floor was a default, not a number you gave.
2. **Source:** Loki `api_request` events (one per API call), not Prometheus. Prometheus metrics are delta counters aggregated by label, so a single request's hit rate cannot be recovered from them.
3. **Exemptions:** first request per session and first after a successful `compaction` event, ordered by `event_sequence`. A TTL-expired cache still alerts.
4. **Cooldown 5 minutes per session; poll every 5 seconds; Loki outage backs off 5s, 10s, 20s, 40s, 60s (capped) and resets on success.**
5. **Runs outside Docker** because `osascript` only exists on the host. Title and message are passed to `osascript` as `argv`, so session text is never parsed as AppleScript.

**Real bug found by running against live Loki:** a first poll with `start` equal to now gets `HTTP Error 400: end timestamp must not be before or equal to start time` (measured: 10ms margin gives 400, 100ms gives 200). The first poll now starts 5 seconds before launch, and the fake Loki in the tests enforces the same rule.

Known limitations: a session already running at monitor start has its next request exempted; a `compaction` event that reaches Loki after the following `api_request` could cause one false alert; notifications are not clickable and carry no trace link.

## 2026-10-28

### Added pdca/pdlc skill cost breakdown panels
`claude-code-dashboard.json` — 3 new panels under a "🧵 PDCA / PDLC Cost Breakdown" row (ids 41-43), confirmed against live Prometheus:

1. **Cost by Skill — pdca/pdlc Normalized** — the existing "Cost by Skill" panel splits pdca into 7 separate bars (`pdca-framework`, `pdca:act`, `pdca:check`, `pdca:cycle`, `pdca:do`, `pdca:pdca-framework`, `pdca:plan`), so pdca's total never shows as one number. This panel rolls all `pdca.*` into one `pdca` series.
2. **pdca vs pdlc Cost: Main vs Subagent** — same rollup split by `query_source` (main vs subagent fan-out).
3. **PDLC Cost by Agent** — pdlc cost by `agent_name` (`pdlc-developer-agent` vs `pdlc-quality-agent`).

**Real bug found while building these:** `pdlc-developer-agent` subagent cost rows carry no `skill_name` label at all (confirmed live: `claude_code_cost_usage_USD_total{agent_name="pdlc-developer-agent"}` has no `skill_name` key). The existing "Cost by Skill" panel was undercounting pdlc by roughly 90% as a result (7d actual: $39.30 total pdlc cost vs ~$3.40 visible under `skill_name="pdlc"`). All 3 new panels fall back to `agent_name=~"pdlc-.*"` to recover the missing cost. Not fixed upstream in the original panel — left as-is per working agreement (confirm before changing existing panels); only new panels added this round.

`beads` has no cost attribution at all — it's a plain Bash-invoked CLI, not an MCP server or skill, and built-in tool calls carry no cost split (confirmed: no `beads` value in `skill_name`, `mcp_server_name`, or `plugin_name`). No dashboard fix possible without instrumenting it as an MCP server.

## 2026-10-24

### Parallel dashboard review — 6 findings fixed
Dual-agent review (overview + session dashboard, each verified against live Prometheus/Loki) found and fixed, confirmed one at a time:

1. `claude-code-dashboard.json` panel 12 ("Development Activity (Hourly)") — dropped the Pull Requests series; `claude_code_pull_request_count_total` doesn't exist in Prometheus, series always fell back to `vector(0)`.
2. `claude-code-dashboard.json` panel 7 ("Tool Usage Rate Over Time") — `sum by (name)` → `sum by (tool_name)`; `name` isn't a real field on `tool_result` logs.
3. `claude-code-dashboard.json` panel 14 ("Cumulative Tool Usage") — same `name` → `tool_name` fix.
4. `claude-code-dashboard.json` panel 8 ("Tool Success Rate") — same `name` → `tool_name` fix, both numerator and denominator.
5. `claude-code-dashboard.json` panel 13 ("Tool Execution Events") — line_format `{{.name}}` → `{{.tool_name}}`.
6. `claude-code-session-dashboard.json` panel 102 ("Session Event Timeline") — dropped dead `{{if .name}}...{{end}}` clause from line_format; `tool_name` clause already covers it.

Everything else in both files checked out against live data: units, delta-temporality handling (`sum(increase(...))` instant/lastNotNull pattern), label names, scrape interval.

## 2026-10-23

### Added Tempo for trace storage
- `tempo.yaml`: local-filesystem backend, OTLP receiver.
- `docker-compose.yml`: `tempo` service.
- `collector-config.yaml`: `traces` pipeline added, exports via `otlp/tempo`.
- `grafana-datasources.yml`: Tempo datasource.
- **Gotcha hit:** Tempo's OTLP gRPC receiver defaults to `127.0.0.1` — unreachable from other containers. Fixed by setting `distributor.receivers.otlp.protocols.grpc.endpoint: 0.0.0.0:4317` (and http equivalent) in `tempo.yaml`.

### Added persistent volumes
Named volumes so metric/log/trace/dashboard data survives container recreation:
- `prometheus-data` → `/prometheus`
- `loki-data` → `/loki`
- `tempo-data` → `/tmp/tempo`
- `grafana-data` → `/var/lib/grafana`

### Opus-reviewed hardening pass
Full review ranked 10 findings; applied in order, decided one at a time:

1. **Localhost-only ports.** All exposed ports were bound `0.0.0.0`/`[::]` — readable/writable by anyone on the LAN (telemetry includes prompts, `user.email`, cost data). Bound `4317`, `4318`, `9090`, `3000` to `127.0.0.1`; dropped `3100`/`3200`/`8889` from the host entirely (reached internally over `otel-network`).
2. **Skipped.** Grafana admin password still hardcoded in `docker-compose.yml` — known stale since it only applies on first volume init. Revisit later.
3. **Retention caps** to stop unbounded disk growth:
   - Loki: pulled the image's default config to `loki-config.yaml` (now version-controlled and mounted explicitly instead of relying on the image's built-in copy), added `compactor` + `limits_config.retention_period: 720h` (30 days).
   - Prometheus: explicit `command:` with `--storage.tsdb.retention.time=30d --storage.tsdb.retention.size=5GB`.
4. **Removed `debug` exporter** from all three collector pipelines (metrics/logs/traces) — it was dumping every telemetry record, including prompt text, to container stdout with no retention control of its own. Added Docker log rotation (`json-file`, `max-size: 10m`, `max-file: 3`) to every service via a shared `x-logging` anchor.
5. **Skipped.** Images still pinned to `:latest` (Tempo had already silently jumped to v3.0, which changed internals). Revisit later — pin to currently-running versions.
6. **Memory protection.** No `memory_limiter`/`batch` processor existed, and no container had a memory cap — a downstream outage (Loki/Tempo down) could grow the collector's queues/heap until Docker's VM OOMs.
   - `collector-config.yaml`: added `memory_limiter` (512 MiB limit / 128 MiB spike) and `batch` processors, first in every pipeline.
   - `docker-compose.yml` `mem_limit`s, sized for a single-developer's month of accumulated data (disk volume already bounded by the retention settings above — these caps cover compaction/WAL overhead, not raw data volume): collector `768m`, Prometheus `2g`, Loki `1536m`, Tempo `1g`.
7. **Trace↔log↔metric correlation.** No datasource UIDs existed (dashboard JSON hardcoded `"uid":"prometheus"`/`"loki"`, only worked via Grafana's name-fallback); no linking between signals.
   - `grafana-datasources.yml`: added explicit `uid`s, Prometheus `exemplarTraceIdDestinations` → Tempo, Loki `derivedFields` (trace_id → Tempo), Tempo `tracesToLogsV2`/`tracesToMetrics`/`serviceMap`.
   - Full scope chosen, including service graph: Tempo's `metrics_generator` enabled (`service-graphs`, `span-metrics` processors) remote-writing to Prometheus; Prometheus given `--web.enable-remote-write-receiver --enable-feature=exemplar-storage`.
8. **Removed dead `alertmanager` datasource** from `grafana-datasources.yml` — no such service exists in `docker-compose.yml`, health check was failing for nothing. Real alerting deferred to later.
9. **Traces were never configured client-side.** Added `OTEL_TRACES_EXPORTER=otlp` to `make setup-claude` and both README snippets. Added Tempo to `make status` output and README's architecture diagram/component table; updated port listings to reflect finding #1 (localhost-only / internal-only).
10. **Misc cleanup:**
    - `resource` processor: `environment: "production"` → `"development"`; added `resource_to_telemetry_conversion: enabled: true` on the Prometheus exporter so the attribute actually surfaces as a label (it wasn't reaching Prometheus before).
    - `make clean`: dropped `docker system prune -f` (was host-wide, not scoped to this stack) — `docker compose down -v` already covers it.
    - `.gitignore`: removed stale `prometheus_data/`, `grafana_data/`, `loki_data/` entries left over from before named volumes.
    - `grafana-dashboards.yml`: `allowUiUpdates` → `false` (was `true` on a dashboard mounted `:ro` — UI edits were silently discarded on reload anyway).

**Still open:** pinning image versions off `:latest` (finding #5, skipped), and Grafana admin password hardcoding (finding #2, skipped — note the running password was since changed by hand directly in `docker-compose.yml`, outside this review).
