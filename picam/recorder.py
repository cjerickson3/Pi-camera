"""Clip start/stop decisions from per-frame motion. Pure logic on frame times."""

from __future__ import annotations

from enum import Enum


class Action(Enum):
    START = "start"
    STOP = "stop"


class Recorder:
    """Idle until `trigger_frames` consecutive motion frames, then record until
    `postroll_s` without motion or `max_clip_s` total, whichever comes first."""

    def __init__(self, trigger_frames: int, postroll_s: float, max_clip_s: float):
        self.trigger_frames = trigger_frames
        self.postroll_s = postroll_s
        self.max_clip_s = max_clip_s
        self.recording = False
        self._run = 0
        self._run_start = 0.0
        self.first_motion_t = 0.0   # first frame of the run that triggered the current/last clip
        self.last_motion_t = 0.0
        self.start_t = 0.0          # when the clip was started (trigger confirmed)

    def update(self, t: float, motion: bool) -> Action | None:
        if motion:
            if self._run == 0:
                self._run_start = t
            self._run += 1
        else:
            self._run = 0

        if not self.recording:
            if self._run >= self.trigger_frames:
                self.recording = True
                self.first_motion_t = self._run_start
                self.last_motion_t = self.start_t = t
                return Action.START
            return None

        if motion:
            self.last_motion_t = t
        if t - self.last_motion_t >= self.postroll_s or t - self.start_t >= self.max_clip_s:
            self.stop()
            return Action.STOP
        return None

    def stop(self):
        """End the current clip (also used for shutdown and end of night)."""
        self.recording = False
        self._run = 0
