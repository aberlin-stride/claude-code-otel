# Changelog

Notable config decisions for this stack. Newest first.

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
