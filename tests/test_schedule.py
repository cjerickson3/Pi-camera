from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from picam.config import ScheduleConfig
from picam.schedule import NightSchedule

TZ = ZoneInfo("America/Chicago")
S = NightSchedule(ScheduleConfig())


def at(*args):
    return datetime(*args, tzinfo=TZ)


@pytest.mark.parametrize("when, active", [
    # Plano, mid-November: sunset ~17:23, sunrise ~07:04 (CST)
    (at(2026, 11, 16, 12, 0), False),
    (at(2026, 11, 16, 18, 0), False),   # after sunset, before sunset + 60 min
    (at(2026, 11, 16, 18, 45), True),
    (at(2026, 11, 16, 23, 59), True),
    (at(2026, 11, 17, 3, 0), True),
    (at(2026, 11, 17, 6, 20), True),
    (at(2026, 11, 17, 6, 45), False),   # stop 30 min before sunrise
    (at(2026, 11, 17, 7, 30), False),
    # DST starts 2026-03-08 02:00; night of Mar 7 -> 8 (sunrise ~07:50 CDT)
    (at(2026, 3, 8, 3, 30), True),
    (at(2026, 3, 8, 8, 30), False),
])
def test_is_active(when, active):
    assert S.is_active(when) is active


def test_window_bounds():
    start, end = S.window(at(2026, 11, 16).date())
    assert at(2026, 11, 16, 18, 15) < start < at(2026, 11, 16, 18, 35)
    assert at(2026, 11, 17, 6, 25) < end < at(2026, 11, 17, 6, 45)   # sunrise ~07:04 - 30 min


def test_next_change():
    start, end = S.window(at(2026, 11, 16).date())
    assert S.next_change(at(2026, 11, 16, 12, 0)) == start
    assert S.next_change(at(2026, 11, 16, 23, 0)) == end
    assert S.next_change(at(2026, 11, 17, 2, 0)) == end


def test_night_label_spans_midnight():
    assert S.night_label(at(2026, 11, 16, 22, 0)) == "2026-11-16"
    assert S.night_label(at(2026, 11, 17, 4, 0)) == "2026-11-16"


def test_always_on():
    assert NightSchedule(ScheduleConfig(), always_on=True).is_active(at(2026, 11, 16, 12, 0))

