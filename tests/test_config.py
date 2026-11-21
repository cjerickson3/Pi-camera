from dataclasses import replace

import pytest

from picam.config import DEFAULT_PATH, Config, from_dict, load

# Site-specific tuning in config.toml that intentionally differs from the code defaults.
SITE_MOTION = ("include", "exclude", "min_area")


def test_repo_config_loads_and_matches_defaults():
    cfg = load(DEFAULT_PATH)
    defaults = Config()
    site = {k: getattr(cfg.motion, k) for k in SITE_MOTION}
    assert replace(cfg, motion=replace(cfg.motion, **{k: getattr(defaults.motion, k) for k in SITE_MOTION}),
                   camera=replace(cfg.camera, source=defaults.camera.source)) == defaults
    assert site["include"] != ((0.0, 0.0, 1.0, 1.0),)   # the area has been set for the burrow


def test_lists_become_tuples():
    cfg = from_dict({"motion": {"include": [[0.1, 0.2, 0.5, 0.6]]}})
    assert cfg.motion.include == ((0.1, 0.2, 0.5, 0.6),)


@pytest.mark.parametrize("data, msg", [
    ({"camra": {}}, "unknown config section"),
    ({"motion": {"treshold": 3}}, "unknown key"),
    ({"motion": {"include": [[0.5, 0.0, 0.2, 1.0]]}}, "bad rectangle"),
    ({"motion": {"include": [[0.0, 0.0, 1.2, 1.0]]}}, "bad rectangle"),
    ({"motion": {"include": []}}, "at least one rectangle"),
    ({"clips": {"max_clip_s": 3}}, "max_clip_s"),
    ({"camera": {"lores_size": [321, 240]}}, "even"),
    ({"camera": {"source": "usb"}}, "source"),
    ({"tapo": {"motion_size": [321, 180]}}, "motion_size"),
    ({"tapo": {"segment_s": 4, "stall_s": 5}}, "stall_s"),
])
def test_bad_config_is_rejected(data, msg):
    with pytest.raises(ValueError, match=msg):
        from_dict(data)


def test_motion_size_follows_source():
    assert from_dict({}).motion_size == (320, 240)
    assert from_dict({"camera": {"source": "tapo"}}).motion_size == (320, 180)


def test_tapo_login_from_env_file(tmp_path, monkeypatch):
    from picam.config import tapo_login
    for k in ("TAPO_HOST", "TAPO_USER", "TAPO_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text('# comment\nTAPO_HOST=10.0.0.3\nTAPO_USER="cam"\nTAPO_PASSWORD=p@ss:w/rd\n')
    login = tapo_login(env)
    assert (login.host, login.user, login.password) == ("10.0.0.3", "cam", "p@ss:w/rd")
    monkeypatch.setenv("TAPO_HOST", "10.0.0.9")
    assert tapo_login(env).host == "10.0.0.9"              # environment wins
    with pytest.raises(ValueError, match="TAPO_USER"):
        tapo_login(tmp_path / "missing.env")
