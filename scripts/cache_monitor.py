"""Host-side monitor: alerts on macOS when a Claude Code turn misses the prompt cache."""
from dataclasses import dataclass


HIT_RATE_THRESHOLD = 0.8


@dataclass(frozen=True)
class Event:
    cache_read: int
    cache_creation: int
    input_tokens: int


def prompt_tokens(event):
    return event.cache_read + event.cache_creation + event.input_tokens


def hit_rate(event):
    total = prompt_tokens(event)
    if total == 0:
        return None
    return event.cache_read / total


def is_significant(event):
    return hit_rate(event) < HIT_RATE_THRESHOLD
