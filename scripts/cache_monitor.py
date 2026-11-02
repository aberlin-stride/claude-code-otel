"""Host-side monitor: alerts on macOS when a Claude Code turn misses the prompt cache."""
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
    def non_exempt(self, items):
        return items[1:]
