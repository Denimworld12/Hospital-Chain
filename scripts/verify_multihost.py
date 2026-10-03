"""Prove a MedChain chain is training across real network addresses, from outside the nodes.

Point it at every hospital node's real address (LAN IPs, Tailscale names, domains). It
checks the nodes agree on the chain, starts one training run through the first node
(the start floods peer to peer; this script never coordinates anything), waits for every
node to finish, and then reports, per hospital and per link, what crossed the network:

    python scripts/verify_multihost.py --secret <shared secret> --ca kits/.pki/ca.crt \\
        --node https://100.101.1.10:8001 --node https://100.101.1.11:8002 --node https://100.101.1.12:8003 \\
        [--database-url postgresql://medchain:medchain@<observer>:5432/medchain] [--json report.json]

Point it at every hospital currently in the chain (any number). `--ca` is the network
CA (scripts/pki.py) that the nodes' HTTPS certificates are checked against.

Without --database-url the evidence comes from each node's own /status and /metrics
(rounds completed, neighbour updates heard on time, the last send to each neighbour,
consensus). With it, every directed link's packet count and exact bytes are read from
the observer database the nodes write to. Exit status 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field

import httpx

H_SECRET = "X-MedChain-Secret"  # node/wire.py; repeated so this script runs with only httpx installed
ACTIVE = ("running", "paused")


@dataclass
class Report:
    run_id: str | None = None
    engine: str | None = None
    nodes: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


class Unreachable(Exception):
    pass


def get(client: httpx.Client, url: str, path: str) -> dict:
    try:
        resp = client.get(f"{url}{path}")
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise Unreachable(f"{url} unreachable ({path}): {exc}") from exc
    return resp.json()


def check_topology(statuses: dict[str, dict], report: Report) -> None:
    ids = {s["node_id"] for s in statuses.values()}
    if len(ids) != len(statuses):
        report.failures.append(f"two --node addresses are the same hospital: {sorted(s['node_id'] for s in statuses.values())}")
    engines = {s["engine"] for s in statuses.values()}
    if len(engines) != 1:
        report.failures.append(f"nodes run different engines: {sorted(engines)}")
    chains = {tuple(s.get("chain") or ()) for s in statuses.values()}
    if len(chains) != 1:
        report.failures.append(f"nodes disagree on the chain order: {sorted(chains)}")
    by_id = {s["node_id"]: s for s in statuses.values()}
    for s in statuses.values():
        for n in s["neighbours"]:
            peer = by_id.get(n["id"])
            if peer is None:
                report.failures.append(f"{s['node_id']} has neighbour {n['id']}, which was not given as a --node")
            elif s["node_id"] not in {m["id"] for m in peer["neighbours"]}:
                report.failures.append(f"{s['node_id']} lists {n['id']} but {n['id']} does not list it back")


def start_run(client: httpx.Client, url: str, secret: str, rounds: int | None) -> dict:
    body = {"initiated_by": "verify_multihost"} | ({"rounds": rounds} if rounds else {})
    resp = client.post(f"{url}/round/start", json=body, headers={H_SECRET: secret})
    if resp.status_code == 401:
        raise SystemExit(f"{url} rejected the shared secret (401)")
    resp.raise_for_status()
    return resp.json()


def wait_finished(client: httpx.Client, urls: list[str], run_id: str, timeout: float) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    while True:
        statuses = {u: get(client, u, "/status") for u in urls}
        if all(s["run_id"] == run_id and s["run_state"] not in ACTIVE for s in statuses.values()):
            return statuses
        if time.monotonic() > deadline:
            rounds = {s["node_id"]: (s["run_id"], s["run_state"], s["current_round"]) for s in statuses.values()}
            raise SystemExit(f"run {run_id} did not finish within {timeout:.0f}s: {rounds}")
        time.sleep(1.0)


def check_run(statuses: dict[str, dict], metrics: dict[str, dict], report: Report) -> None:
    final_rounds = set()
    for url, s in statuses.items():
        node = s["node_id"]
        hist = metrics[url]["history"]
        heard = sum(h["neighbours_heard"] for h in hist)
        expected = sum(h["neighbours_expected"] for h in hist)
        degraded = sum(1 for h in hist if h.get("degraded") or h["neighbours_heard"] < h["neighbours_expected"])
        last_send = {n["id"]: n["last_send"] for n in s["neighbours"]}
        report.nodes[node] = {
            "url": url, "neighbour_urls": {n["id"]: n["url"] for n in s["neighbours"]},
            "run_state": s["run_state"], "rounds": len(hist), "updates_heard": heard, "updates_expected": expected,
            "degraded_rounds": degraded, "last_send": last_send,
            "first_loss": hist[0]["loss"] if hist else None, "final_loss": hist[-1]["loss"] if hist else None,
            "final_accuracy": hist[-1]["accuracy"] if hist else None,
            "final_max_residual": hist[-1].get("max_residual") if hist else None,
            "converged_round": s.get("converged_round"),
        }
        final_rounds.add(s["current_round"])
        if s["run_state"] != "completed":
            report.failures.append(f"{node} ended the run {s['run_state']!r}, not 'completed'")
        if not hist:
            report.failures.append(f"{node} completed no rounds")
            continue
        if heard == 0:
            report.failures.append(f"{node} never heard from a neighbour: nothing crossed the network to it")
        if degraded:
            report.warnings.append(f"{node}: {degraded} of {len(hist)} rounds missed a neighbour's update")
        for peer, status in last_send.items():
            if status != "delivered":
                report.failures.append(f"{node}'s last update to {peer} was {status!r}")
    if len(final_rounds) > 1:
        report.failures.append(f"nodes stopped on different rounds: {sorted(final_rounds)}")
    if any(s.get("chain") for s in statuses.values()):  # ADMM engines: did the chain reach consensus?
        converged = {s["node_id"]: s.get("converged_round") for s in statuses.values()}
        if None in converged.values():
            report.failures.append(f"no consensus: converged_round per node {converged}")


def db_links(dsn: str, run_id: str, report: Report) -> None:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=5) as conn:
        sent = conn.execute(
            """SELECT sender, receiver, count(*) FILTER (WHERE status = 'delivered'), count(*),
                      coalesce(sum(payload_bytes), 0), coalesce(sum(full_precision_bytes), 0)
               FROM updates_meta WHERE run_id = %s GROUP BY sender, receiver ORDER BY sender, receiver""",
            (run_id,)).fetchall()
        received = dict(((r[0], r[1]), r[2]) for r in conn.execute(
            """SELECT sender, receiver, count(*) FROM audit_logs
               WHERE run_id = %s AND event = 'update_received' AND (detail->>'verified')::boolean
               GROUP BY sender, receiver""", (run_id,)).fetchall())
        urls = dict(conn.execute("SELECT node_id, url FROM nodes").fetchall())
    for sender, receiver, delivered, attempts, payload, full in sent:
        got = received.get((sender, receiver), 0)
        report.links.append({"sender": sender, "receiver": receiver, "receiver_url": urls.get(receiver),
                             "delivered": delivered, "attempts": attempts, "received_and_verified": got,
                             "payload_bytes": payload, "float64_bytes": full})
        if delivered == 0 or got == 0:
            report.failures.append(f"no verified update crossed {sender} -> {receiver}")
    expected = {(n, p) for n, info in report.nodes.items() for p in info["last_send"]}
    missing = expected - {(link["sender"], link["receiver"]) for link in report.links}
    for sender, receiver in sorted(missing):
        report.failures.append(f"the observer database has no updates for {sender} -> {receiver}")


def print_report(report: Report) -> None:
    print(f"run {report.run_id}  engine {report.engine}")
    print(f"{'hospital':<12} {'reached at':<28} {'rounds':>6} {'heard':>9} {'degraded':>8} "
          f"{'loss first->final':>18} {'acc':>6} {'residual':>9} {'consensus':>9}")
    for node, n in report.nodes.items():
        loss = f"{n['first_loss']:.4f}->{n['final_loss']:.4f}" if n["rounds"] else "-"
        res = f"{n['final_max_residual']:.2e}" if n["final_max_residual"] is not None else "-"
        acc = f"{n['final_accuracy']:.3f}" if n["final_accuracy"] is not None else "-"
        print(f"{node:<12} {n['url']:<28} {n['rounds']:>6} {n['updates_heard']:>4}/{n['updates_expected']:<4} "
              f"{n['degraded_rounds']:>8} {loss:>18} {acc:>6} {res:>9} {str(n['converged_round'] or '-'):>9}")
        for peer, url in n["neighbour_urls"].items():
            print(f"    -> {peer} at {url}: last send {n['last_send'][peer]}")
    if report.links:
        print(f"\n{'link':<28} {'to address':<28} {'delivered':>9} {'verified':>8} {'bytes':>8} {'float64':>8}")
        for link in report.links:
            print(f"{link['sender'] + ' -> ' + link['receiver']:<28} {str(link['receiver_url']):<28} "
                  f"{link['delivered']:>4}/{link['attempts']:<4} {link['received_and_verified']:>8} "
                  f"{link['payload_bytes']:>8} {link['float64_bytes']:>8}")
    for w in report.warnings:
        print(f"WARN  {w}")
    for f in report.failures:
        print(f"FAIL  {f}")
    print("PASS: every hospital trained and exchanged parameters with its neighbours over the network"
          if report.passed else "FAILED")


def verify(urls: list[str], secret: str, *, rounds: int | None = None, database_url: str | None = None,
           timeout: float = 900, start: bool = True, ca: str | None = None) -> Report:
    report = Report()
    urls = [u.rstrip("/") for u in urls]
    with httpx.Client(timeout=10, verify=ca or True) as client:
        try:
            statuses = {u: get(client, u, "/status") for u in urls}
            check_topology(statuses, report)
            if report.failures:
                return report
            report.engine = next(iter(statuses.values()))["engine"]
            if start:
                report.run_id = start_run(client, urls[0], secret, rounds)["run_id"]
            else:
                report.run_id = next(iter(statuses.values()))["run_id"]
            statuses = wait_finished(client, urls, report.run_id, timeout)
            metrics = {u: get(client, u, "/metrics") for u in urls}
        except Unreachable as exc:
            report.failures.append(str(exc))
            return report
    check_run(statuses, metrics, report)
    if database_url:
        db_links(database_url, report.run_id, report)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--node", action="append", required=True, help="a hospital node's real address (repeat for each)")
    ap.add_argument("--secret", required=True, help="the network's shared secret (to start the run)")
    ap.add_argument("--ca", help="the network CA certificate for https:// nodes (scripts/pki.py)")
    ap.add_argument("--rounds", type=int, help="round limit for the run (default: the nodes' training.rounds)")
    ap.add_argument("--database-url", help="observer Postgres the nodes write to, for per-link byte counts")
    ap.add_argument("--timeout", type=float, default=900, help="seconds to wait for the run to finish")
    ap.add_argument("--no-start", action="store_true", help="verify the nodes' last run instead of starting one")
    ap.add_argument("--json", help="also write the report to this file")
    args = ap.parse_args(argv)
    report = verify(args.node, args.secret, rounds=args.rounds, database_url=args.database_url,
                    timeout=args.timeout, start=not args.no_start, ca=args.ca)
    print_report(report)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(asdict(report) | {"passed": report.passed}, fh, indent=2)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
