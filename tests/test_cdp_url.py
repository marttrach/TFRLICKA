import socket

import pytest

from tra_sniper.automation import cdp_url_over_ipv4


@pytest.fixture
def fake_dns(monkeypatch):
    def resolve(host, port, family, socktype):
        assert family is socket.AF_INET, "must not ask for the AAAA record"
        if host == "tra-sniper-browser":
            return [(family, socktype, 6, "", ("172.18.0.3", port))]
        raise OSError(f"no A record for {host}")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def test_container_name_becomes_ipv4(fake_dns) -> None:
    assert cdp_url_over_ipv4("http://tra-sniper-browser:9222") == "http://172.18.0.3:9222"


def test_path_and_default_port_survive(fake_dns) -> None:
    assert cdp_url_over_ipv4("http://tra-sniper-browser/json") == "http://172.18.0.3/json"


def test_unresolvable_host_is_left_alone(fake_dns) -> None:
    assert cdp_url_over_ipv4("http://nope:9222") == "http://nope:9222"
