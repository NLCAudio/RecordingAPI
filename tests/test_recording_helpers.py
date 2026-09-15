"""Pure string/path helpers in recording.py: safe_name, free_path, timing."""

import pytest

from recording import (
    FLOOR_LEVEL_DB,
    format_time,
    free_path,
    parse_db,
    safe_name,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sanctuary", "sanctuary"),
        ("9am", "9am"),
        ("LR FOH", "LR FOH"),
        ("a/b", "a_b"),
        ("a:b", "a_b"),
        ('a"b', "a_b"),
        ("a<b>c", "a_b_c"),
        ("a|b?c*d", "a_b_c_d"),
        ("back\\slash", "back_slash"),
        ("trailing.", "trailing"),
        (".leading", "leading"),
        ("   ", "fallback"),  # nothing usable left
        ("..", "fallback"),  # would walk out of the recording folder
        ("", "fallback"),
        ("AUX", "_AUX"),  # reserved DOS device name, prefixed not replaced
        ("con", "_con"),
        ("LPT3", "_LPT3"),
        ("COM9", "_COM9"),
        ("COM10", "COM10"),  # only COM1..COM9 are reserved
    ],
)
def test_safe_name(raw, expected):
    assert safe_name(raw, fallback="fallback") == expected


def test_free_path_returns_original_when_free(tmp_path):
    target = tmp_path / "a.mp3"
    assert free_path(target) == target


def test_free_path_numbers_collisions(tmp_path):
    target = tmp_path / "a.mp3"
    target.write_bytes(b"x")
    (tmp_path / "a_2.mp3").write_bytes(b"x")
    assert free_path(target) == tmp_path / "a_3.mp3"


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00:00:00"),
        (59, "00:00:59"),
        (60, "00:01:00"),
        (142, "00:02:22"),
        (3661, "01:01:01"),
    ],
)
def test_format_time(seconds, expected):
    assert format_time(seconds) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"-18.4", -18.4),
        (b"0", 0.0),
        (b"-100", FLOOR_LEVEL_DB),  # below the floor, clamped
        (b"inf", FLOOR_LEVEL_DB),
        (b"-inf", FLOOR_LEVEL_DB),
        (b"nan", FLOOR_LEVEL_DB),
        (b"not-a-number", FLOOR_LEVEL_DB),
    ],
)
def test_parse_db(raw, expected):
    assert parse_db(raw) == expected