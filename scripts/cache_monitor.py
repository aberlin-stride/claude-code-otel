"""Host-side monitor: alerts on macOS when a Claude Code turn misses the prompt cache."""
import json
import logging
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass


HIT_RATE_THRESHOLD = 0.8
MIN_PROMPT_TOKENS = 20000


@dataclass(frozen=True)
class Event:
    cache_read: int
    cache_creation: int
    input_tokens: int
    session_id: str = ""
    event_sequence: int = 0
    query_source: str = ""


@dataclass(frozen=True)
class Compaction:
    session_id: str
    event_sequence: int
    success: bool


def prompt_tokens(event):
    return event.cache_read + event.cache_creation + event.input_tokens


def hit_rate(event):
    total = prompt_tokens(event)
    if total == 0:
        return None
    return event.cache_read / total


def is_significant(event):
    if prompt_tokens(event) < MIN_PROMPT_TOKENS:
        return False
    return hit_rate(event) < HIT_RATE_THRESHOLD


class ExemptionTracker:
    def __init__(self):
        self._seen_sessions = set()
        self._post_compaction = set()

    def non_exempt(self, items):
        result = []
        for event in sorted(items, key=lambda item: item.event_sequence):
            if isinstance(event, Compaction):
                if event.success:
                    self._post_compaction.add(event.session_id)
                continue
            if event.session_id in self._post_compaction:
                self._post_compaction.discard(event.session_id)
                continue
            if event.session_id not in self._seen_sessions:
                self._seen_sessions.add(event.session_id)
                continue
            result.append(event)
        return result


def _parse_stream(labels):
    name = labels["event_name"]
    if name == "compaction":
        return Compaction(
            session_id=labels["session_id"],
            event_sequence=int(labels["event_sequence"]),
            success=labels["success"] == "true",
        )
    if name == "api_request":
        return Event(
            cache_read=int(labels["cache_read_tokens"]),
            cache_creation=int(labels["cache_creation_tokens"]),
            input_tokens=int(labels["input_tokens"]),
            session_id=labels["session_id"],
            event_sequence=int(labels["event_sequence"]),
            query_source=labels.get("query_source", ""),
        )
    return None


def parse_events(loki_response):
    items = []
    for result in loki_response["data"]["result"]:
        try:
            item = _parse_stream(result["stream"])
        except (KeyError, ValueError):
            continue
        if item is not None:
            items.append(item)
    return items


class Cooldown:
    def __init__(self, window_seconds=300, clock=time.monotonic):
        self._window = window_seconds
        self._clock = clock
        self._last_alert = {}

    def allow(self, session_id):
        now = self._clock()
        last = self._last_alert.get(session_id)
        if last is not None and now - last < self._window:
            return False
        self._last_alert[session_id] = now
        return True


class Dedup:
    def __init__(self):
        self._seen = set()

    def fresh(self, items):
        result = []
        for item in items:
            key = (item.session_id, item.event_sequence)
            if key not in self._seen:
                self._seen.add(key)
                result.append(item)
        return result


class Backoff:
    def __init__(self, base=5, cap=60):
        self._base = base
        self._cap = cap
        self._delay = base
        self._in_outage = False

    def failed(self):
        self._in_outage = True
        delay = self._delay
        self._delay = min(self._delay * 2, self._cap)
        return delay

    @property
    def in_outage(self):
        return self._in_outage

    def succeeded(self):
        self._in_outage = False
        self._delay = self._base


def _run_command(argv):
    subprocess.run(argv, check=False, timeout=5)


_APPLESCRIPT = (
    "on run argv",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "end run",
)


def notify(title, message, runner=_run_command):
    argv = ["osascript"]
    for line in _APPLESCRIPT:
        argv += ["-e", line]
    try:
        runner(argv + [message, title])
    except (OSError, subprocess.SubprocessError) as error:
        logging.getLogger(__name__).warning("notification failed: %s", error)


def format_alert(event):
    title = f"Cache miss: {hit_rate(event):.0%} hit rate"
    message = (f"{event.query_source} · {prompt_tokens(event):,} prompt tokens"
               f" · session {event.session_id[:8]}")
    return (title, message)


LOKI_QUERY = '{service_name="claude-code"} | event_name=~"api_request|compaction"'


def loki_fetch(base_url):
    def fetch(start_ns):
        params = urllib.parse.urlencode({
            "query": LOKI_QUERY, "start": start_ns,
            "limit": 1000, "direction": "forward",
        })
        url = f"{base_url}/loki/api/v1/query_range?{params}"
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.load(response)
    return fetch


OVERLAP_NS = 60 * 10**9


class Monitor:
    def __init__(self, fetch, notify, now_ns=time.time_ns):
        self._fetch = fetch
        self._notify = notify
        self._now_ns = now_ns
        self._since = now_ns()
        self._dedup = Dedup()
        self._exemptions = ExemptionTracker()
        self._cooldown = Cooldown()

    def poll(self):
        started = self._now_ns()
        items = parse_events(self._fetch(self._since))
        self._since = started - OVERLAP_NS
        for event in self._exemptions.non_exempt(self._dedup.fresh(items)):
            if is_significant(event) and self._cooldown.allow(event.session_id):
                self._notify(*format_alert(event))
        return 0
