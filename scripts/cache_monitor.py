"""Host-side monitor: alerts on macOS when a Claude Code turn misses the prompt cache."""
import time
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
    def fresh(self, items):
        return items
