"""News filter - placeholder interface, disabled by default.

The bot never fetches news itself (no external calls). To use it, load
high-impact event times into `NewsFilter.events` (e.g. from a CSV you export
from an economic calendar) and set use_news_filter: true. Until then,
is_blocked() always returns (False, "").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config_loader import Settings


@dataclass
class NewsEvent:
    time: datetime          # broker time
    title: str = ""
    impact: str = "high"


@dataclass
class NewsFilter:
    cfg: Settings
    events: list[NewsEvent] = field(default_factory=list)

    def is_blocked(self, now: datetime) -> tuple[bool, str]:
        if not self.cfg.use_news_filter:
            return False, ""
        before = timedelta(minutes=self.cfg.news_block_minutes_before)
        after = timedelta(minutes=self.cfg.news_block_minutes_after)
        for event in self.events:
            if event.time - before <= now <= event.time + after:
                return True, f"news block: {event.title or 'high-impact event'} @ {event.time}"
        return False, ""
