"""Generate MedChain kits: one folder (optionally one zip) per hospital, plus one for the directory.

A hospital kit is everything one collaborator needs to run their node on their own
machine: its config (the directory's address, its own credential, TLS and signing
keys), a compose file that runs only that node, its own data slice, its own keys and
certificate, the node's build context (or a prebuilt image reference) and a README with
the exact commands. The directory kit runs the membership directory. Unpack, run one command.

    python scripts/provision.py \\
        --peers hospital-a=100.101.1.10:8001,hospital-b=100.101.1.11:8002,hospital-c=100.101.1.12:8003 \\
        --directory 100.101.1.99:8100 --zip

`--peers` lists every hospital with the address its neighbours reach it at (a LAN IP, a
Tailscale MagicDNS name, a domain); the order is the directory's placement order while
no run is in progress. Every link is HTTPS: each address goes into that hospital's
certificate, issued by the network CA kept in `--pki` (default kits/.pki). Re-running
with the same `--pki` reuses the CA and every existing key, so a hospital added later
(`--hospital hospital-e`) joins the same network: copy the refreshed
kits/directory/pki/directory/hospitals.yaml to the directory machine (it is re-read on
every registration, no restart) and start the new kit; the directory appends it.
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
sys.path.insert(0, str(ROOT / "scripts"))

import pki  # noqa: E402
from node.config import ADVERTISE_URL_ENV, DIRECTORY_URL_ENV, load_config  # noqa: E402

COMPOSE_FILE = "docker-compose.hospital.yaml"
DIRECTORY = "directory"
DEFAULT_TEMPLATE = ROOT / "config" / "hospital-a.yaml"
# the node image's build context, laid out as node/Dockerfile expects it (the same image runs the directory)
SOURCE_FILES = ("node", "directory", "observer", "scripts/pki.py", "models/ecg-resnet18.onnx", "data/cardio/cardio_train.csv")


def parse_url(name: str, addr: str) -> str:
    """A hospital's or the directory's address: https only (bare host:port gets https://)."""
    if "://" not in addr:
        addr = f"https://{addr}"
    parts = urlsplit(addr)
    if parts.scheme == "http":
        raise ValueError(f"the address for {name} uses http, but every MedChain link is HTTPS (TLS); use https://")
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"the address for {name} is not an https URL: {addr!r}")
    return addr.rstrip("/")


def parse_peers(spec: str) -> dict[str, str]:
    """`id=URL,id=URL,...` in placement order -> {id: URL}."""
    peers: dict[str, str] = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        node_id, sep, addr = item.partition("=")
        node_id, addr = node_id.strip(), addr.strip()
        if not sep or not node_id or not addr:
            raise ValueError(f"--peers entry {item!r} must look like hospital-a=host:8001")
        if node_id in peers or node_id == DIRECTORY:
            raise ValueError(f"--peers lists {node_id} twice" if node_id in peers else f"{node_id!r} is reserved")
        peers[node_id] = parse_url(node_id, addr)
    if len(peers) < 2:
        raise ValueError("--peers needs at least two hospitals (a chain)")
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


def node_yaml(node_id: str, url: str, directory_url: str, secret: str, template: Path) -> dict:
    raw, own = base_config(node_id, template)
    for k in ("chain", "neighbours"):
        raw.pop(k, None)
    raw.update(
        node_id=node_id,
        display_name=raw.get("display_name") if own else node_id,
        port=listen_port(node_id, url, template),
        host="0.0.0.0",
        advertise_url=url,
        directory={"url": directory_url, "credential_file": f"/pki/{node_id}/credential",
                   "public_key": "file:/pki/directory/signing.pub", "tls_pin": "file:/pki/directory/tls.crt"},
        tls={"cert": f"/pki/{node_id}/tls.crt", "key": f"/pki/{node_id}/tls.key", "ca": "/pki/ca.crt"},
        signing_key=f"/pki/{node_id}/signing.key",
        shared_secret=secret,
        data_slice=f"/data/{node_id}.csv",
    )
    return raw


def copy_keys(pki_dir: Path, kit: Path, party: str) -> None:
    """Only this party's own private keys, plus the public material every member needs."""
    dst = kit / "pki"
    shutil.copytree(pki_dir / party, dst / party)
    shutil.copyfile(pki_dir / "ca.crt", dst / "ca.crt")
    (dst / DIRECTORY).mkdir(exist_ok=True)
    shutil.copyfile(pki_dir / DIRECTORY / "signing.pub", dst / DIRECTORY / "signing.pub")
    if party != DIRECTORY:  # the directory's certificate: this kit pins its key (out of band, not from the directory)
        shutil.copyfile(pki_dir / DIRECTORY / "tls.crt", dst / DIRECTORY / "tls.crt")


def compose_yaml(name: str, port: int, *, image: str | None, env: dict, volumes: list[str], bind_ip: str | None,
                 command: list[str] | None = None, extra_volumes: dict | None = None) -> dict:
    service: dict = {}
    if image:
        service["image"] = image
    else:
        service["build"] = {"context": "./source", "dockerfile": "node/Dockerfile"}
        service["image"] = "medchain-node:latest"
    if command:
        service["command"] = command
    service.update(environment=env, volumes=volumes,
                   ports=[f"{bind_ip}:{port}:{port}" if bind_ip else f"{port}:{port}"], restart="unless-stopped")
    return {"name": f"medchain-{name}", "services": {name: service}, "volumes": extra_volumes or {}}


def copy_source(kit: Path) -> None:
    for rel in SOURCE_FILES:
        src, dst = ROOT / rel, kit / "source" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copyfile(src, dst)


def fresh(out: Path, name: str, force: bool) -> Path:
    kit = out / name
    if kit.exists():
        if not force:
            raise FileExistsError(f"{kit} already exists (pass --force to replace it)")
        shutil.rmtree(kit)
    kit.mkdir(parents=True)
    return kit


def kit_readme(node_id: str, display: str, url: str, directory_url: str, port: int, secret: str, *,
               image: str | None, observer_db: str | None) -> str:
    up = f"docker compose -f {COMPOSE_FILE} up -d{'' if image else ' --build'} --wait"
    observer = (f"Training telemetry goes to the observer database at `{observer_db}` (read by the\n"
                "MedChain dashboard). It only observes; training does not depend on it."
                if observer_db else
                "No observer database is configured, so this node keeps its telemetry to itself\n"
                "(`/status` and `/metrics` still work). Re-provision with `--observer-db` to feed a dashboard.")
    curl = f"curl --cacert pki/ca.crt https://localhost:{port}"
    return textwrap.dedent("""\
        # MedChain node kit: {display} (`{node_id}`)

        This folder runs one MedChain hospital node on this machine. It trains on
        `data/{node_id}.csv`, which never leaves this machine; only model updates
        travel, only to this node's chain neighbours, over TLS, each one signed with
        this node's key (`pki/{node_id}/signing.key`). There is no central training
        server.

        Where this node sits in the chain is decided by the MedChain directory at
        `{directory_url}`: on start the node registers there with its own credential
        (`pki/{node_id}/credential`), and the directory tells it, and its new
        neighbours, who to connect to. The directory only coordinates membership. If it
        is down, a node already in the chain keeps training; only joining and leaving
        wait for it.

        This kit contains this hospital's private keys, its directory credential and the
        network's shared secret. Treat it like a password: send it to {display}'s
        operator only, over a private channel.

        ## Before you start

        - Docker with the Compose v2 plugin.
        - Port {port} free on this machine, and reachable at `{url}` from the other
          hospitals and the directory. The simplest way is a private mesh network such as
          Tailscale on every machine: it works through NAT and needs no port forwarding.
          Peers pin this node's TLS key (registered with the directory), not its address.
        - To train on your own patients instead of the demo slice, replace
          `data/{node_id}.csv` with a file that has the same columns.

        ## Run (this is also how the hospital joins)

        ```sh
        {up}
        ```

        The node registers with the directory. If a training run is in progress, the
        directory appends this hospital at the end of the chain from a round boundary a
        few rounds ahead; it then exchanges one full model snapshot with its new
        neighbour and joins the run. Check it is up and placed:

        ```sh
        {curl}/health
        {curl}/status      # chain, position, head/tail group, neighbours
        ```

        ## Start training

        Once the hospitals are up, start a run from any one of them; it floods the
        start to the others:

        ```sh
        {curl}/round/start -X POST \\
          -H 'Content-Type: application/json' \\
          -H 'X-MedChain-Secret: {secret}' \\
          -d '{{}}'
        {curl}/metrics    # per-round loss and neighbours heard
        ```

        {observer}

        ## Leave the chain (planned)

        ```sh
        docker compose -f {COMPOSE_FILE} exec {node_id} python -m node.leave
        ```

        The directory takes this hospital out at the next round boundary and tells its
        two neighbours, which bridge directly to each other (a new link, a fresh
        snapshot exchange) so the chain heals instead of splitting. Then stop the node.
        (Stopping it without leaving is treated as a crash: its neighbours skip it and
        wait for it to come back.)

        ## Stop

        ```sh
        docker compose -f {COMPOSE_FILE} down        # add -v to also delete this node's checkpoints
        ```

        ## If an address changes

        Override it without touching the config by adding to the `environment:` block in
        `{COMPOSE_FILE}`:

        ```yaml
              {ADVERTISE}: https://new-address-of-{node_id}:{port}
              {DIRECTORY_ENV}: https://new-address-of-the-directory:PORT
        ```

        Then run the `up` command again; the node re-registers its new address and the
        directory passes it to its neighbours. Other hospitals pin this node's TLS key,
        not its address, so their links keep working. Only the dashboard checks the
        address: for it, ask whoever provisioned the network to re-run
        `scripts/provision.py` for this hospital with the new address (same key).
        """).format(display=display, node_id=node_id, url=url, directory_url=directory_url, port=port, up=up,
                    curl=curl, secret=secret, observer=observer, ADVERTISE=ADVERTISE_URL_ENV,
                    DIRECTORY_ENV=DIRECTORY_URL_ENV, COMPOSE_FILE=COMPOSE_FILE)


def build_kit(node_id: str, url: str, out: Path, *, directory_url: str, pki_dir: Path, secret: str, data: Path,
              template: Path, image: str | None, observer_db: str | None, bind_ip: str | None, make_zip: bool,
              force: bool) -> Path:
    kit = fresh(out, node_id, force)
    (kit / "config").mkdir()
    (kit / "data").mkdir()
    cfg = node_yaml(node_id, url, directory_url, secret, template)
    cfg_path = kit / "config" / f"{node_id}.yaml"
    cfg_path.write_text(f"# MedChain node config for {node_id}, generated by scripts/provision.py.\n"
                        + yaml.safe_dump(cfg, sort_keys=False))
    load_config(cfg_path)  # the same validation the node runs at start
    shutil.copyfile(data, kit / "data" / f"{node_id}.csv")
    copy_keys(pki_dir, kit, node_id)

    port = cfg["port"]
    env = {"NODE_CONFIG": f"/config/{node_id}.yaml"} | ({"DATABASE_URL": observer_db} if observer_db else {})
    compose = compose_yaml(node_id, port, image=image, env=env, bind_ip=bind_ip,
                           volumes=[f"./config/{node_id}.yaml:/config/{node_id}.yaml:ro",
                                    f"./data/{node_id}.csv:/data/{node_id}.csv:ro", "./pki:/pki:ro",
                                    "checkpoints:/checkpoints"], extra_volumes={"checkpoints": {}})
    (kit / COMPOSE_FILE).write_text(
        f"# Runs only {node_id}. See README.md:  docker compose -f {COMPOSE_FILE} up -d{'' if image else ' --build'} --wait\n"
        + yaml.safe_dump(compose, sort_keys=False))
    (kit / "README.md").write_text(kit_readme(node_id, str(cfg["display_name"]), url, directory_url, port, secret,
                                              image=image, observer_db=observer_db))
    if not image:
        copy_source(kit)
    if make_zip:
        shutil.make_archive(str(out / node_id), "zip", root_dir=out, base_dir=node_id)
    return kit


def build_directory_kit(url: str, out: Path, *, pki_dir: Path, image: str | None, bind_ip: str | None,
                        make_zip: bool, force: bool) -> Path:
    kit = fresh(out, DIRECTORY, force)
    (kit / "config").mkdir()
    port = urlsplit(url).port or 8100
    cfg = yaml.safe_load((ROOT / "config" / "directory.yaml").read_text())
    cfg.update(port=port, host="0.0.0.0", db_path="/data/directory.sqlite")
    (kit / "config" / "directory.yaml").write_text("# MedChain directory config, generated by scripts/provision.py.\n"
                                                   + yaml.safe_dump(cfg, sort_keys=False))
    copy_keys(pki_dir, kit, DIRECTORY)
    compose = compose_yaml(DIRECTORY, port, image=image, env={"DIRECTORY_CONFIG": "/config/directory.yaml"},
                           bind_ip=bind_ip, command=["python", "-m", "directory"],
                           volumes=["./config/directory.yaml:/config/directory.yaml:ro", "./pki:/pki:ro",
                                    "directory-data:/data"], extra_volumes={"directory-data": {}})
    (kit / COMPOSE_FILE.replace("hospital", "directory")).write_text(
        "# Runs the MedChain membership directory. See README.md.\n" + yaml.safe_dump(compose, sort_keys=False))
    (kit / "README.md").write_text(textwrap.dedent(f"""\
        # MedChain directory kit

        Runs the membership directory at `{url}`. It decides where each hospital sits in
        the chain and tells hospitals who to connect to when one joins or leaves. It
        never receives model weights and never runs or influences training. It is a
        single point of failure for joins and leaves only: if it is down, hospitals
        already in the chain keep training.

        ```sh
        docker compose -f docker-compose.directory.yaml up -d{'' if image else ' --build'} --wait
        ```

        Its state lives in its own SQLite file (the `directory-data` volume), separate
        from the dashboard's Postgres. `pki/directory/hospitals.yaml` lists which
        hospitals may register (an id and the SHA-256 of that hospital's own
        credential, in placement order). It is re-read on every registration: to admit
        a new hospital, replace it with the refreshed file from the provisioning machine.
        Keep `pki/directory/signing.key` private: it signs every membership document.
        """))
    if not image:
        copy_source(kit)
    if make_zip:
        shutil.make_archive(str(out / DIRECTORY), "zip", root_dir=out, base_dir=DIRECTORY)
    return kit


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--peers", required=True,
                    help="every hospital in placement order: id=host:port,... (how the others reach it)")
    ap.add_argument("--directory", required=True, help="the directory's address, host:port (https)")
    ap.add_argument("--hospital", action="append", default=[],
                    help="provision only this hospital, or `directory` (repeatable); default: all of them")
    ap.add_argument("--secret", help="shared secret for X-MedChain-Secret (default: a new random one, printed)")
    ap.add_argument("--out", type=Path, default=ROOT / "kits", help="output directory (default: kits/)")
    ap.add_argument("--pki", type=Path, help="the network's CA and keys, reused across runs (default: <out>/.pki)")
    ap.add_argument("--data", action="append", default=[], metavar="ID=PATH",
                    help="this hospital's data CSV (repeatable; default: data/slices/<id>.csv)")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                    help="engine settings for a hospital without its own config/<id>.yaml")
    ap.add_argument("--image", help="use this prebuilt node image instead of bundling the build context")
    ap.add_argument("--observer-db", help="postgresql:// URL of the dashboard's database (optional, observe-only)")
    ap.add_argument("--bind-ip", help="publish the port on this host IP only (e.g. the machine's Tailscale IP)")
    ap.add_argument("--zip", action="store_true", help="also write <out>/<id>.zip for each kit")
    ap.add_argument("--force", action="store_true", help="replace kits that already exist")
    args = ap.parse_args(argv)

    try:
        peers = parse_peers(args.peers)
        directory_url = parse_url(DIRECTORY, args.directory)
        wanted = args.hospital or [*peers, DIRECTORY]
        unknown = [h for h in wanted if h not in peers and h != DIRECTORY]
        if unknown:
            raise ValueError(f"--hospital {unknown} not in --peers {list(peers)}")
        hospitals = [h for h in wanted if h != DIRECTORY]
        data = {h: ROOT / "data" / "slices" / f"{h}.csv" for h in hospitals}
        for item in args.data:
            h, _, p = item.partition("=")
            if h not in data or not p:
                raise ValueError(f"--data {item!r} must be ID=PATH for a provisioned hospital")
            data[h] = Path(p)
        missing = [str(p) for p in data.values() if not p.is_file()]
        if missing:
            raise ValueError(f"data file(s) not found: {missing} (pass --data ID=PATH)")
        for h in hospitals:
            listen_port(h, peers[h], args.template)
        pki_dir = args.pki or args.out / ".pki"
        host = lambda u: [urlsplit(u).hostname, "localhost", "127.0.0.1"]  # noqa: E731 - + local checks
        pki.issue(pki_dir, (DIRECTORY, host(directory_url)), [(h, host(u)) for h, u in peers.items()])
        secret = args.secret or secrets.token_urlsafe(24)
        for h in hospitals:
            kit = build_kit(h, peers[h], args.out, directory_url=directory_url, pki_dir=pki_dir, secret=secret,
                            data=data[h], template=args.template, image=args.image, observer_db=args.observer_db,
                            bind_ip=args.bind_ip, make_zip=args.zip, force=args.force)
            print(f"{h}: {kit}{' (+ ' + kit.name + '.zip)' if args.zip else ''}")
        if DIRECTORY in wanted:
            kit = build_directory_kit(directory_url, args.out, pki_dir=pki_dir, image=args.image,
                                      bind_ip=args.bind_ip, make_zip=args.zip, force=args.force)
            print(f"{DIRECTORY}: {kit}{' (+ directory.zip)' if args.zip else ''}")
        else:
            print(f"admitted {', '.join(hospitals)}: copy {pki_dir / DIRECTORY / 'hospitals.yaml'} to the directory "
                  "machine's pki/directory/ (re-read on every registration)")
    except (ValueError, FileExistsError) as exc:
        print(f"provision: {exc}", file=sys.stderr)
        return 2
    if not args.secret:
        print(f"shared secret (generated): {secret}\n"
              "Pass --secret with this value if you provision more hospitals for this network later.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
