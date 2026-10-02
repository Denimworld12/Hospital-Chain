"""Generate per-hospital MedChain node kits (one folder, optionally one zip, per hospital).

A kit is everything one collaborator needs to run their hospital's node on their own
machine: its config pre-filled with the real peer addresses, a compose file that runs
only that node, its own data slice, the node's build context (or a prebuilt image
reference) and a README with the exact commands. Unpack it, run one command.

    python scripts/provision.py \\
        --peers hospital-a=http://100.101.1.10:8001,hospital-b=http://100.101.1.11:8002,hospital-c=http://100.101.1.12:8003 \\
        --zip

`--peers` lists every hospital in chain order with the address its neighbours reach
it at (a LAN IP, a Tailscale MagicDNS name, a domain). Each node only talks to its
chain neighbours; there is no central server, so nothing else needs an address.
One invocation provisions every hospital in `--peers` (or only `--hospital ...`) with
one shared secret; pass the same `--secret` when provisioning hospitals separately.
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from node.config import load_config, peer_url_env  # noqa: E402

COMPOSE_FILE = "docker-compose.hospital.yaml"
DEFAULT_TEMPLATE = ROOT / "config" / "hospital-a.yaml"
# the node image's build context, laid out as node/Dockerfile expects it
SOURCE_FILES = ("node", "models/ecg-resnet18.onnx", "data/cardio/cardio_train.csv")


def parse_peers(spec: str) -> dict[str, str]:
    """`id=URL,id=URL,...` in chain order -> {id: URL}; a bare host:port gets http://."""
    peers: dict[str, str] = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        node_id, sep, addr = item.partition("=")
        node_id, addr = node_id.strip(), addr.strip()
        if not sep or not node_id or not addr:
            raise ValueError(f"--peers entry {item!r} must look like hospital-a=http://host:8001")
        if node_id in peers:
            raise ValueError(f"--peers lists {node_id} twice")
        if "://" not in addr:
            addr = f"http://{addr}"
        parts = urlsplit(addr)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"--peers address for {node_id} is not an http(s) URL: {addr!r}")
        peers[node_id] = addr.rstrip("/")
    if len(peers) < 2:
        raise ValueError("--peers needs at least two hospitals (the chain)")
    return peers


def base_config(node_id: str, template: Path) -> tuple[dict, bool]:
    """The hospital's own config/<id>.yaml when the repo has one, else the template."""
    own = ROOT / "config" / f"{node_id}.yaml"
    return yaml.safe_load((own if own.is_file() else template).read_text()), own.is_file()


def listen_port(node_id: str, url: str, template: Path) -> int:
    """The port in the hospital's address, else the port its repo config uses."""
    port = urlsplit(url).port
    if port is not None:
        return port
    raw, own = base_config(node_id, template)
    if not own:
        raise ValueError(f"the address for {node_id} needs an explicit port ({url})")
    return int(raw["port"])


def node_yaml(node_id: str, peers: dict[str, str], secret: str, template: Path) -> dict:
    raw, own = base_config(node_id, template)
    chain = list(peers)
    i = chain.index(node_id)
    raw.update(
        node_id=node_id,
        display_name=raw.get("display_name") if own else node_id,
        port=listen_port(node_id, peers[node_id], template),
        host="0.0.0.0",
        advertise_url=peers[node_id],
        chain=chain,
        neighbours=[{"id": chain[j], "url": peers[chain[j]]} for j in (i - 1, i + 1) if 0 <= j < len(chain)],
        shared_secret=secret,
        data_slice=f"/data/{node_id}.csv",
    )
    return raw


def compose_yaml(node_id: str, port: int, *, image: str | None, observer_db: str | None, bind_ip: str | None) -> dict:
    env = {"NODE_CONFIG": f"/config/{node_id}.yaml"}
    if observer_db:
        env["DATABASE_URL"] = observer_db
    service: dict = {}
    if image:
        service["image"] = image
    else:
        service["build"] = {"context": "./source", "dockerfile": "node/Dockerfile"}
        service["image"] = "medchain-node:latest"
    service.update(
        environment=env,
        volumes=[f"./config/{node_id}.yaml:/config/{node_id}.yaml:ro",
                 f"./data/{node_id}.csv:/data/{node_id}.csv:ro",
                 "checkpoints:/checkpoints"],
        ports=[f"{bind_ip}:{port}:{port}" if bind_ip else f"{port}:{port}"],
        restart="unless-stopped",
    )
    return {"name": f"medchain-{node_id}", "services": {node_id: service}, "volumes": {"checkpoints": {}}}


def kit_readme(node_id: str, display: str, peers: dict[str, str], port: int, secret: str, *,
               image: str | None, observer_db: str | None) -> str:
    chain = list(peers)
    links = " - ".join(f"**{n} (this kit)**" if n == node_id else n for n in chain)
    rows = "\n".join(f"| {n} | {peers[n]} | {'this node' if n == node_id else ''} |" for n in chain)
    i = chain.index(node_id)
    neighbours = [chain[j] for j in (i - 1, i + 1) if 0 <= j < len(chain)]
    up = f"docker compose -f {COMPOSE_FILE} up -d{'' if image else ' --build'} --wait"
    observer = (f"Training telemetry goes to the observer database at `{observer_db}` (read by the\n"
                "MedChain dashboard). It only observes; training does not depend on it."
                if observer_db else
                "No observer database is configured, so this node keeps its telemetry to itself\n"
                "(`/status` and `/metrics` still work). Re-provision with `--observer-db` to feed a dashboard.")
    env_lines = "\n".join(f"      {peer_url_env(n)}: http://new-address-of-{n}:PORT" for n in neighbours)
    # dedent before filling in: the multi-line values are not indented like the template
    return textwrap.dedent("""\
        # MedChain node kit: {display} (`{node_id}`)

        This folder runs one MedChain hospital node on this machine. It trains on
        `data/{node_id}.csv`, which never leaves this machine; only model updates
        travel, and only to this node's chain neighbours. There is no central server.

        Chain: {links}

        | Hospital | Address its neighbours use | |
        | --- | --- | --- |
        {rows}

        This kit contains the network's shared secret (in `config/{node_id}.yaml`).
        Treat it like a password: send it to {display}'s operator only, over a private channel.

        ## Before you start

        - Docker with the Compose v2 plugin.
        - Port {port} free on this machine, and reachable at `{address}` from
          {neighbour_names}. The simplest way is a private mesh network such as
          Tailscale on every hospital machine: it works through NAT, encrypts the
          traffic, and needs no port forwarding.
        - To train on your own patients instead of the demo slice, replace
          `data/{node_id}.csv` with a file that has the same columns.

        ## Run

        ```sh
        {up}
        ```

        Check it is up and sees its neighbours (each neighbour's `last_send` fills in once training starts):

        ```sh
        curl http://localhost:{port}/health
        curl http://localhost:{port}/status
        ```

        ## Start training

        Once every hospital's node is up, start a run from any one of them; it floods
        the start to the others:

        ```sh
        curl -X POST http://localhost:{port}/round/start \\
          -H 'Content-Type: application/json' \\
          -H 'X-MedChain-Secret: {secret}' \\
          -d '{{}}'
        curl http://localhost:{port}/metrics    # per-round loss and neighbours heard
        ```

        {observer}

        ## Stop

        ```sh
        docker compose -f {COMPOSE_FILE} down        # add -v to also delete this node's checkpoints
        ```

        ## If an address changes

        Edit the neighbour `url` in `config/{node_id}.yaml`, or override it without
        touching the file by adding to the `environment:` block in `{COMPOSE_FILE}`:

        ```yaml
        {env_lines}
              MEDCHAIN_ADVERTISE_URL: http://new-address-of-{node_id}:{port}
        ```

        Then run the `up` command again.
        """).format(display=display, node_id=node_id, links=links, rows=rows, port=port, address=peers[node_id],
                    neighbour_names=" and ".join(neighbours), up=up, secret=secret, observer=observer,
                    env_lines=env_lines, COMPOSE_FILE=COMPOSE_FILE)


def build_kit(node_id: str, peers: dict[str, str], out: Path, *, secret: str, data: Path, template: Path,
              image: str | None, observer_db: str | None, bind_ip: str | None, make_zip: bool, force: bool) -> Path:
    kit = out / node_id
    if kit.exists():
        if not force:
            raise FileExistsError(f"{kit} already exists (pass --force to replace it)")
        shutil.rmtree(kit)
    (kit / "config").mkdir(parents=True)
    (kit / "data").mkdir()

    cfg = node_yaml(node_id, peers, secret, template)
    cfg_path = kit / "config" / f"{node_id}.yaml"
    cfg_path.write_text(f"# MedChain node config for {node_id}, generated by scripts/provision.py.\n"
                        + yaml.safe_dump(cfg, sort_keys=False))
    load_config(cfg_path)  # the same validation the node runs at start (chain, neighbours, engine)
    shutil.copyfile(data, kit / "data" / f"{node_id}.csv")

    port = cfg["port"]
    compose = compose_yaml(node_id, port, image=image, observer_db=observer_db, bind_ip=bind_ip)
    (kit / COMPOSE_FILE).write_text(
        f"# Runs only {node_id}. See README.md:  docker compose -f {COMPOSE_FILE} up -d{'' if image else ' --build'} --wait\n"
        + yaml.safe_dump(compose, sort_keys=False))
    (kit / "README.md").write_text(kit_readme(node_id, str(cfg["display_name"]), peers, port, secret,
                                              image=image, observer_db=observer_db))
    if not image:
        for rel in SOURCE_FILES:
            src, dst = ROOT / rel, kit / "source" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copyfile(src, dst)
    if make_zip:
        shutil.make_archive(str(out / node_id), "zip", root_dir=out, base_dir=node_id)
    return kit


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--peers", required=True,
                    help="every hospital in chain order: id=URL,id=URL,... (URL = how its neighbours reach it)")
    ap.add_argument("--hospital", action="append", default=[],
                    help="provision only this hospital (repeatable); default: every hospital in --peers")
    ap.add_argument("--secret", help="shared secret for X-MedChain-Secret (default: a new random one, printed)")
    ap.add_argument("--out", type=Path, default=ROOT / "kits", help="output directory (default: kits/)")
    ap.add_argument("--data", action="append", default=[], metavar="ID=PATH",
                    help="this hospital's data CSV (repeatable; default: data/slices/<id>.csv)")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                    help="engine settings for a hospital without its own config/<id>.yaml")
    ap.add_argument("--image", help="use this prebuilt node image instead of bundling the build context")
    ap.add_argument("--observer-db", help="postgresql:// URL of the dashboard's database (optional, observe-only)")
    ap.add_argument("--bind-ip", help="publish the node port on this host IP only (e.g. the machine's Tailscale IP)")
    ap.add_argument("--zip", action="store_true", help="also write <out>/<id>.zip for each kit")
    ap.add_argument("--force", action="store_true", help="replace kits that already exist")
    args = ap.parse_args(argv)

    try:
        peers = parse_peers(args.peers)
        hospitals = args.hospital or list(peers)
        unknown = [h for h in hospitals if h not in peers]
        if unknown:
            raise ValueError(f"--hospital {unknown} not in --peers {list(peers)}")
        data = {h: ROOT / "data" / "slices" / f"{h}.csv" for h in hospitals}
        for item in args.data:
            h, _, p = item.partition("=")
            if h not in data or not p:
                raise ValueError(f"--data {item!r} must be ID=PATH for a provisioned hospital")
            data[h] = Path(p)
        missing = [str(p) for p in data.values() if not p.is_file()]
        if missing:
            raise ValueError(f"data file(s) not found: {missing} (pass --data ID=PATH)")
        secret = args.secret or secrets.token_urlsafe(24)
        for h in hospitals:
            kit = build_kit(h, peers, args.out, secret=secret, data=data[h], template=args.template,
                            image=args.image, observer_db=args.observer_db, bind_ip=args.bind_ip,
                            make_zip=args.zip, force=args.force)
            print(f"{h}: {kit}{' (+ ' + kit.name + '.zip)' if args.zip else ''}")
    except (ValueError, FileExistsError) as exc:
        print(f"provision: {exc}", file=sys.stderr)
        return 2
    if not args.secret:
        print(f"shared secret (generated): {secret}\n"
              "Pass --secret with this value if you provision more hospitals for this network later.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
