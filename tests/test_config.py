from dataclasses import replace

import pytest

from picam.config import DEFAULT_PATH, Config, from_dict, load

# Site-specific tuning in config.toml that intentionally differs from the code defaults.
SITE_MOTION = ("include", "exclude", "min_area")


def test_repo_config_loads_and_matches_defaults():
    cfg = load(DEFAULT_PATH)
    defaults = Config()
    site = {k: getattr(cfg.motion, k) for k in SITE_MOTION}
    assert replace(cfg, motion=replace(cfg.motion, **{k: getattr(defaults.motion, k) for k in SITE_MOTION})) == defaults
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
])
def test_bad_config_is_rejected(data, msg):
    with pytest.raises(ValueError, match=msg):
        from_dict(data)
