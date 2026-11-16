"""When to run: from sunset + offset until sunrise + offset, local time."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sun

from .config import ScheduleConfig


class NightSchedule:
    def __init__(self, cfg: ScheduleConfig, always_on: bool = False):
        self.tz = ZoneInfo(cfg.timezone)
        self.always_on = always_on
        self._observer = Observer(latitude=cfg.latitude, longitude=cfg.longitude)
        self._start = timedelta(minutes=cfg.start_after_sunset_min)
        self._stop = timedelta(minutes=cfg.stop_after_sunrise_min)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def window(self, evening: date) -> tuple[datetime, datetime]:
        """Active window for the night that starts on the evening of `evening`."""
        start = sun(self._observer, date=evening, tzinfo=self.tz)["sunset"] + self._start
        end = sun(self._observer, date=evening + timedelta(days=1), tzinfo=self.tz)["sunrise"] + self._stop
        return start, end

    def is_active(self, now: datetime) -> bool:
        if self.always_on:
            return True
        now = now.astimezone(self.tz)
        return any(s <= now < e for s, e in (self.window(now.date() - timedelta(days=1)),
                                              self.window(now.date())))

    def next_change(self, now: datetime) -> datetime:
        """Next start (if inactive) or end (if active) of the window."""
        now = now.astimezone(self.tz)
        for d in (-1, 0, 1):
            s, e = self.window(now.date() + timedelta(days=d))
            if now < s:
                return s
            if now < e:
                return e
        raise RuntimeError("no upcoming window")  # unreachable at Plano's latitude

    def night_label(self, t: datetime) -> str:
        """The evening's date, so one night is one folder even past midnight."""
        return (t.astimezone(self.tz) - timedelta(hours=12)).date().isoformat()
