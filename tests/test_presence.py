"""Tests for the relay URL parser and the auto-join presence logic."""

from __future__ import annotations

import pytest

from dota_voice.presence import parse_relay_url


@pytest.mark.parametrize(
    "url,default,expected",
    [
        ("", 12000, ("127.0.0.1", 12000)),
        ("udp://10.0.0.5:9000", 12000, ("10.0.0.5", 9000)),
        ("10.0.0.5:9000", 12000, ("10.0.0.5", 9000)),
        ("udp://example.local", 12000, ("example.local", 12000)),
        ("example.local", 12000, ("example.local", 12000)),
    ],
)
def test_parse_relay_url(url, default, expected):
    assert parse_relay_url(url, default) == expected


def test_parse_relay_url_defaults_host():
    host, port = parse_relay_url(":9999", 12000)
    assert host == "127.0.0.1"
    assert port == 9999
