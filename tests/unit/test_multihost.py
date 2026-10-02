"""Three hospitals as three separate OS processes on this machine's real network address.

The stand-in for three laptops: each node is its own `python -m node` process, bound to
the host's LAN address (not loopback, not Docker DNS) on its own port. Its YAML holds
unresolvable placeholder peer URLs; the real addresses arrive only through
$MEDCHAIN_PEER_<ID>_URL and $MEDCHAIN_ADVERTISE_URL, so the run only works if the
override mechanism works and the packets cross real sockets. scripts/verify_multihost.py
then checks the run from outside, exactly as it would across real machines.
"""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

import verify_multihost
from chain_harness import ROOT, SECRET
from node.config import ADVERTISE_URL_ENV, peer_url_env

CHAIN = ["hospital-a", "hospital-b", "hospital-c"]



def host_address() -> str:
    """This machine's outward-facing IPv4 address (a UDP connect sends nothing), else loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed, only used to pick the interface
            ip = s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
    try:
        with socket.socket() as probe:
            probe.bind((ip, 0))
    except OSError:
        return "127.0.0.1"
    return ip


def free_ports_on(ip: str, n: int) -> list[int]:
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind((ip, 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


@pytest.fixture
def processes(tmp_path):
    ip = host_address()
    ports = dict(zip(CHAIN, free_ports_on(ip, len(CHAIN))))
    real = {n: f"http://{ip}:{ports[n]}" for n in CHAIN}
    procs, logs = {}, {}
    for i, node in enumerate(CHAIN):
        neighbours = [CHAIN[j] for j in (i - 1, i + 1) if 0 <= j < len(CHAIN)]
        cfg = tmp_path / f"{node}.yaml"
        cfg.write_text(f"""node_id: {node}
port: {ports[node]}
host: {ip}
advertise_url: http://{node}.placeholder.invalid:1
chain: [{", ".join(CHAIN)}]
neighbours:
{"".join(f"  - id: {m}\n    url: http://{m}.placeholder.invalid:1\n" for m in neighbours)}shared_secret: {SECRET}
data_slice: {ROOT / "data" / "slices" / f"{node}.csv"}
engine: qgadmm
bits: 8
rho: 24.0
checkpoint_dir: {tmp_path / ("ckpt-" + node)}
training: {{rounds: 120}}
timing: {{heartbeat_interval_s: 5, round_timeout_s: 4, round_pause_s: 0.0}}
""")
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEDCHAIN_") and k != "DATABASE_URL"}
        env |= {"NODE_CONFIG": str(cfg), ADVERTISE_URL_ENV: real[node],
                **{peer_url_env(m): real[m] for m in neighbours}}
        logs[node] = open(tmp_path / f"{node}.log", "w")
        procs[node] = subprocess.Popen([sys.executable, "-m", "node"], cwd=ROOT, env=env,
                                       stdout=logs[node], stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 90
        for node, url in real.items():
            while True:
                if procs[node].poll() is not None:
                    pytest.fail(f"{node} exited: {(tmp_path / f'{node}.log').read_text()[-2000:]}")
                try:
                    if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    pytest.fail(f"{node} did not come up at {url}")
                time.sleep(0.2)
        yield ip, real, procs
    finally:
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for fh in logs.values():
            fh.close()


def test_three_processes_on_real_addresses_train_to_consensus(processes):
    _, real, procs = processes
    assert len({p.pid for p in procs.values()}) == 3
    report = verify_multihost.verify(list(real.values()), SECRET, timeout=180)
    assert report.passed, report.failures
    assert not report.warnings, report.warnings
    assert set(report.nodes) == set(CHAIN)
    for node, info in report.nodes.items():
        # each node reached its neighbours at the real address it got from the environment
        assert all(url == real[peer] for peer, url in info["neighbour_urls"].items())
        assert set(info["last_send"].values()) == {"delivered"}
        assert info["rounds"] > 0 and info["updates_heard"] == info["updates_expected"] > 0
        assert info["converged_round"] is not None and info["final_max_residual"] < 1e-3
    assert {info["rounds"] for info in report.nodes.values()} == {report.nodes["hospital-a"]["rounds"]}


def test_verify_reports_an_unreachable_node_instead_of_raising(tmp_path, capsys):
    (port,) = free_ports_on("127.0.0.1", 1)  # closed again: connecting is refused
    url = f"http://127.0.0.1:{port}"
    out = tmp_path / "report.json"
    assert verify_multihost.main(["--secret", SECRET, "--node", url, "--json", str(out)]) == 1
    printed = capsys.readouterr().out
    assert f"FAIL  {url} unreachable" in printed and "FAILED" in printed
    report = json.loads(out.read_text())
    assert report["passed"] is False and any(url in f for f in report["failures"])
