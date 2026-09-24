"""Client IP resolution for rate limiting (pure unit tests; no database needed).

Run standalone with:  uv run pytest --noconftest tests/test_client_ip.py
"""

import pytest

from app.common.rate_limit import resolve_client_ip

PEER = "10.0.0.5"  # the proxy that connects to the app


def test_zero_hops_ignores_forwarded_for_entirely() -> None:
    assert resolve_client_ip(PEER, ["203.0.113.9"], 0) == PEER
    assert resolve_client_ip(PEER, ["1.2.3.4, 203.0.113.9"], 0) == PEER


def test_one_hop_uses_rightmost_entry() -> None:
    assert resolve_client_ip(PEER, ["203.0.113.9"], 1) == "203.0.113.9"


def test_spoofed_entries_on_the_left_are_ignored() -> None:
    # Client sends "X-Forwarded-For: 6.6.6.6"; the trusted proxy appends the real address.
    assert resolve_client_ip(PEER, ["6.6.6.6, 203.0.113.9"], 1) == "203.0.113.9"
    assert resolve_client_ip(PEER, ["6.6.6.6", "203.0.113.9"], 1) == "203.0.113.9"


def test_two_hops_skips_the_inner_proxy() -> None:
    # client -> proxy A (appends client) -> proxy B (appends A) -> app
    assert resolve_client_ip(PEER, ["6.6.6.6, 203.0.113.9, 10.1.1.1"], 2) == "203.0.113.9"


@pytest.mark.parametrize(
    ("header", "hops"),
    [
        ([], 1),  # header missing
        ([""], 1),  # empty header
        (["203.0.113.9"], 2),  # fewer entries than trusted hops
        (["not-an-ip"], 1),  # garbage
        (["203.0.113.9, <script>"], 1),
    ],
)
def test_fails_safe_to_peer(header: list[str], hops: int) -> None:
    assert resolve_client_ip(PEER, header, hops) == PEER


def test_normalises_ipv6() -> None:
    assert resolve_client_ip(PEER, ["2001:DB8::1"], 1) == "2001:db8::1"


def test_unknown_peer() -> None:
    assert resolve_client_ip(None, [], 0) == "unknown"
