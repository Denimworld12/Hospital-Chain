"""scripts/preflight.py: is each neighbour reached directly or through a Tailscale DERP relay?"""

import preflight

STATUS = {"Peer": {
    "nodekey:1": {"HostName": "alice-laptop", "DNSName": "alice-laptop.tail1234.ts.net.",
                  "TailscaleIPs": ["100.101.1.10", "fd7a::1"], "CurAddr": "192.168.1.5:41641", "Relay": "fra"},
    "nodekey:2": {"HostName": "bob-laptop", "DNSName": "bob-laptop.tail1234.ts.net.",
                  "TailscaleIPs": ["100.101.1.11"], "CurAddr": "", "Relay": "lhr"},
}}


def test_ping_output_says_direct_or_relayed():
    direct = "pong from alice-laptop (100.101.1.10) via 192.168.1.5:41641 in 4ms\n"
    relayed = "pong from bob-laptop (100.101.1.11) via DERP(lhr) in 61ms\n"
    upgraded = relayed.replace("bob", "alice") + direct  # the first pong may still be relayed
    assert preflight.classify_ping(direct) == ("direct", "192.168.1.5:41641")
    assert preflight.classify_ping(relayed) == ("relayed", "DERP lhr")
    assert preflight.classify_ping(upgraded) == ("direct", "192.168.1.5:41641")
    assert preflight.classify_ping("timeout waiting for pong\n") is None


def test_status_finds_the_peer_by_ip_or_magicdns_name_and_reads_its_path():
    for host in ("100.101.1.10", "alice-laptop.tail1234.ts.net", "alice-laptop"):
        assert preflight.classify_status(preflight.find_peer(STATUS, host)) == ("direct", "192.168.1.5:41641")
    assert preflight.classify_status(preflight.find_peer(STATUS, "100.101.1.11")) == ("relayed", "DERP lhr")
    assert preflight.find_peer(STATUS, "192.168.1.20") is None
    assert preflight.is_tailscale_ip("100.101.1.11") and not preflight.is_tailscale_ip("192.168.1.20")


def test_inside_the_node_container_it_needs_no_arguments_and_says_why_a_neighbour_is_unreachable(monkeypatch, capsys):
    """In deploy/node.yml's container there is no tailscale command; NODE_PORT and TLS_CA name the node and CA."""
    import httpx

    monkeypatch.setenv("NODE_PORT", "8002")
    monkeypatch.delenv("TLS_CA", raising=False)
    monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
    calls = []

    def get(url, **_):
        calls.append(url)
        if url == "https://localhost:8002/status":
            return httpx.Response(200, json={"node_id": "hospital-b", "chain": ["hospital-a", "hospital-b"],
                                             "neighbours": [{"id": "hospital-a", "url": "https://a.tail1234.ts.net:8001"}]})
        raise httpx.ConnectError("[Errno -2] Name or service not known")

    monkeypatch.setattr(preflight.httpx, "get", get)
    assert preflight.main([]) == 1
    out = capsys.readouterr().out
    assert calls[0] == "https://localhost:8002/status"
    assert "unreachable" in out and "Name or service not known" in out
    assert preflight.path_to("100.101.1.10", ping=False)[0] == "unknown"
