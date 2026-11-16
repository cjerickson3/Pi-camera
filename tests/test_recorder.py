from picam.recorder import Action, Recorder

DT = 0.1


def feed(rec, pattern, t0=0.0):
    """pattern: string of '1' (motion) / '0' frames. Returns {frame_index: action}."""
    out = {}
    for i, c in enumerate(pattern):
        a = rec.update(t0 + i * DT, c == "1")
        if a:
            out[i] = a
    return out


def test_needs_consecutive_frames_to_start():
    rec = Recorder(trigger_frames=3, postroll_s=1.0, max_clip_s=10)
    assert feed(rec, "1101101") == {}
    assert not rec.recording


def test_starts_on_third_frame_and_remembers_first_motion():
    rec = Recorder(trigger_frames=3, postroll_s=1.0, max_clip_s=10)
    assert feed(rec, "00111") == {4: Action.START}
    assert rec.first_motion_t == 0.2


def test_stops_after_postroll_without_motion():
    rec = Recorder(trigger_frames=3, postroll_s=1.0, max_clip_s=10)
    # start at frame 2, last motion at frame 5 (t=0.5), stop when t - 0.5 >= 1.0 -> frame 15
    acts = feed(rec, "111111" + "0" * 12)
    assert acts == {2: Action.START, 15: Action.STOP}
    assert rec.last_motion_t == 0.5


def test_motion_during_postroll_extends_clip():
    rec = Recorder(trigger_frames=3, postroll_s=1.0, max_clip_s=10)
    acts = feed(rec, "111" + "00000001" + "0" * 12)
    assert acts[2] is Action.START
    assert list(acts.values()).count(Action.STOP) == 1
    assert max(acts) == 20  # last motion frame 10 (t=1.0) + 1.0 s


def test_max_clip_cap_then_retrigger():
    rec = Recorder(trigger_frames=3, postroll_s=1.0, max_clip_s=2.0)
    acts = feed(rec, "1" * 40)
    starts = [i for i, a in acts.items() if a is Action.START]
    stops = [i for i, a in acts.items() if a is Action.STOP]
    assert starts[0] == 2 and stops[0] == 22   # 2.0 s after start at t=0.2
    assert starts[1] == 25                     # three more motion frames
