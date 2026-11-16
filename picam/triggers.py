"""Trigger sources besides camera motion. Hook for a PIR sensor on a GPIO pin."""

from __future__ import annotations

from typing import Protocol

from .config import Config


class Trigger(Protocol):
    name: str

    def active(self) -> bool: ...

    def close(self) -> None: ...


class PirTrigger:
    """HC-SR501-style PIR on a GPIO input (BCM numbering). Needs python3-gpiozero."""

    name = "pir"

    def __init__(self, pin: int):
        from gpiozero import MotionSensor  # only on the Pi, only when enabled
        self._sensor = MotionSensor(pin)

    def active(self) -> bool:
        return bool(self._sensor.motion_detected)

    def close(self) -> None:
        self._sensor.close()


def build_triggers(cfg: Config) -> list[Trigger]:
    return [PirTrigger(cfg.pir.gpio_pin)] if cfg.pir.enabled else []
