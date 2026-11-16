import pytest

from picam.config import DEFAULT_PATH, Config, from_dict, load


def test_repo_config_loads_and_matches_defaults():
    assert load(DEFAULT_PATH) == Config()


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
