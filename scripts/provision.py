"""Generate MedChain kits: one folder (optionally one zip) per hospital, plus one for the hub.

A hospital kit is everything one collaborator needs to run their node on their own
laptop, laid out for deploy/node.yml (docs/MULTI_LAPTOP.md):

    deploy/node.yml     the one-service compose file (a copy of the repo's)
    .env                this hospital's settings, filled in: NODE_ID, NODE_PORT, its address and
                        the hub's (a Tailscale block, and a hotspot block to switch to), the network secret
    pki/                only its own TLS and signing keys and directory credential, plus the public
                        material it pins (CA, the directory's and the observer's certificates, the
                        directory's document key) and the observer's ingest token
    data/               its data slice; the node keeps its checkpoints, saved membership and
                        audit.db here too
    config/node.yaml    its display name and the engine settings (from config/<id>.yaml)
    README.md           the commands

Unzip, check `.env`, run `docker compose --project-directory . -f deploy/node.yml up`. The
directory kit is the hub's: the directory's config and keys, the network CA's key (it
issues certificates to hospitals that enroll with a join code instead of a kit), the
observer's keys, and a filled `hub.env` for `make hub` (deploy/hub.yml) from the repository.

    python scripts/provision.py \\
        --peers hospital-a=100.101.1.10:8001,hospital-b=100.101.1.11:8002,hospital-c=100.101.1.12:8003 \\
        --directory 100.101.1.99:8100 --zip

`--peers` lists every hospital with the address its neighbours reach it at (a LAN IP, a
Tailscale MagicDNS name, a domain); the order is the directory's placement order while
no run is in progress. One hospital is enough (the organiser's own) when the others will
enroll with join codes from the dashboard.

Only Docker installed (no Python)? Run it inside the published node image, from the
repository (or its source zip) folder; the kits land in ./kits as usual:

    docker run --rm -v "${PWD}:/src" -w /src ghcr.io/denimworld12/medchain-node:v1.1.0 \\
        python scripts/provision.py --zip --peers ... --directory ...

Every link is HTTPS: each address goes into that hospital's
certificate, issued by the network CA kept in `--pki` (default kits/.pki). Nodes push
their telemetry to the observer (`--observer`, default the directory's host on port
8200) with its token and pin its key; no kit holds database credentials. Re-running
with the same `--pki` reuses the CA and every existing key, so a hospital added later
(`--hospital hospital-e`) joins the same network: copy the refreshed
kits/directory/pki/directory/hospitals.yaml to the directory machine (it is re-read on
every registration, no restart) and start the new kit; the directory appends it.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
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
from node.config import build_config, engine_settings  # noqa: E402
from node.settings import NodeSettings, apply_env  # noqa: E402

NODE_COMPOSE = ROOT / "deploy" / "node.yml"
UP = "docker compose --project-directory . -f deploy/node.yml"
IMAGE_VERSION = "v1.1.0"  # the published node image's tag (.github/workflows/image.yml)
DIRECTORY = "directory"
OBSERVER = "observer"
OBSERVER_PORT = 8200
DEFAULT_TEMPLATE = ROOT / "config" / "hospital-a.yaml"
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


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
        if node_id in peers or node_id in (DIRECTORY, OBSERVER):
            raise ValueError(f"--peers lists {node_id} twice" if node_id in peers else f"{node_id!r} is reserved")
        peers[node_id] = parse_url(node_id, addr)
    if not peers:
        raise ValueError("--peers needs at least one hospital (your own; join codes add the others later)")
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


def engine_yaml(node_id: str, template: Path) -> dict:
    """The kit's config/node.yaml: the hospital's name and the engine settings, nothing about where it runs.

    Identity, addresses, keys and the secret come from the kit's `.env` and deploy/node.yml instead."""
    raw, own = base_config(node_id, template)
    raw = engine_settings(raw)
    raw["display_name"] = raw.get("display_name") if own else node_id
    return raw


def hostport(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)


def split_image(image: str | None) -> tuple[str | None, str]:
    """`--image repo[:tag]` -> (MEDCHAIN_IMAGE, IMAGE_VERSION); none -> the published image's default tag."""
    if not image:
        return None, IMAGE_VERSION
    repo, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:  # no tag (a ':' in a registry port is not one)
        return image, "latest"
    return repo, tag


def node_env(node_id: str, port: int, url: str, directory_url: str, observer_url: str, secret: str,
             image: str | None) -> str:
    """The kit's `.env`, laid out like deploy/node.env.example: a Tailscale block and a hotspot block."""
    repo, version = split_image(image)
    d, o = urlsplit(directory_url), urlsplit(observer_url)
    return textwrap.dedent(f"""\
        # MedChain hospital node settings for {node_id}, generated by scripts/provision.py.
        # Read by deploy/node.yml (and by `make node-native`). docs/MULTI_LAPTOP.md walks through it.

        # ---- Which hospital this laptop is: check it matches what the organiser told you ----
        NODE_ID={node_id}
        NODE_PORT={port}

        # ======== Tailscale (the normal way) ========
        # This laptop's MagicDNS name (or Tailscale IP, `tailscale ip -4`), with NODE_PORT:
        ADVERTISE_ADDR={hostport(url)}
        # The organiser's laptop (the hub), which runs the directory and the observer:
        DIRECTORY_URL={directory_url}
        OBSERVER_URL={observer_url}

        # ======== Phone hotspot (fallback) ========
        # To switch: put a # in front of the three Tailscale lines above, remove the # from the
        # three lines below, and put in the IP addresses the hotspot gave each laptop.
        #ADVERTISE_ADDR=THIS-LAPTOPS-HOTSPOT-IP:{port}
        #DIRECTORY_URL=https://HUB-HOTSPOT-IP:{d.port or 443}
        #OBSERVER_URL=https://HUB-HOTSPOT-IP:{o.port or 443}

        # ---- The network: filled in by the kit, the same on every laptop. Don't change. ----
        SHARED_SECRET={secret}
        IMAGE_VERSION={version}
        """) + (f"MEDCHAIN_IMAGE={repo}\n" if repo else "") + textwrap.dedent("""\
        # Rounds over a real network take longer than on one machine: give each one time.
        ROUND_TIMEOUT_S=15
        HTTP_CONNECT_TIMEOUT_S=5
        HTTP_READ_TIMEOUT_S=10
        """)


def read_env(text: str) -> dict[str, str]:
    """The variables an `.env` sets (KEY=value lines; # comments and blank lines skipped)."""
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def interpolate(value: str, env: dict[str, str]) -> str:
    """Compose's ${VAR}, ${VAR:-default} and ${VAR:?message} against `env`."""
    def sub(m: re.Match) -> str:
        name, op, arg = m.group(1), m.group(2), m.group(3) or ""
        if env.get(name):
            return env[name]
        if op == ":?":
            raise ValueError(f"{name} is not set: {arg}")
        return arg if op == ":-" else ""
    return re.sub(r"\$\{(\w+)(?:(:-|:\?)([^}]*))?\}", sub, value)


def container_env(env: dict[str, str]) -> dict[str, str]:
    """What the node sees inside deploy/node.yml's container: the `.env` plus the file's own `environment:`."""
    service = yaml.safe_load(NODE_COMPOSE.read_text())["services"]["node"]
    return {**env, **{k: interpolate(str(v), env) for k, v in (service.get("environment") or {}).items()}}


def check_kit(kit: Path, env: dict[str, str]) -> None:
    """Validate the kit as its node will load it in deploy/node.yml (container paths, not read here)."""
    settings = NodeSettings(**container_env(env))
    raw = yaml.safe_load((kit / "config" / "node.yaml").read_text()) or {}
    build_config(apply_env(raw, settings, str(kit / ".env")), str(kit / ".env"))


def ensure_observer_token(pki_dir: Path, token: str | None) -> None:
    """The observer's ingest token, kept with the network's keys so every kit from this network shares it."""
    path = pki_dir / OBSERVER / "token"
    if token or not path.is_file():
        path.write_text((token or secrets.token_urlsafe(24)) + "\n")
        path.chmod(0o600)


def copy_keys(pki_dir: Path, kit: Path, party: str) -> None:
    """Only this party's own private keys, plus the public material every member needs."""
    dst = kit / "pki"
    shutil.copytree(pki_dir / party, dst / party)
    shutil.copyfile(pki_dir / "ca.crt", dst / "ca.crt")
    (dst / DIRECTORY).mkdir(exist_ok=True)
    shutil.copyfile(pki_dir / DIRECTORY / "signing.pub", dst / DIRECTORY / "signing.pub")
    if party != DIRECTORY:  # the directory's certificate: this kit pins its key (out of band, not from the directory)
        shutil.copyfile(pki_dir / DIRECTORY / "tls.crt", dst / DIRECTORY / "tls.crt")
    else:  # the hub issues certificates to hospitals that enroll with a join code (directory/enroll.py)
        shutil.copyfile(pki_dir / "ca.key", dst / "ca.key")
        (dst / "ca.key").chmod(0o600)
    # the observer's certificate (pinned) and ingest token; the hub kit also gets its TLS key to run it
    (dst / OBSERVER).mkdir()
    for f in ("tls.crt", "token", *(("tls.key",) if party == DIRECTORY else ())):
        shutil.copyfile(pki_dir / OBSERVER / f, dst / OBSERVER / f)


def fresh(out: Path, name: str, force: bool) -> Path:
    kit = out / name
    if kit.exists():
        if not force:
            raise FileExistsError(f"{kit} already exists (pass --force to replace it)")
        shutil.rmtree(kit)
    kit.mkdir(parents=True)
    return kit


def kit_readme(node_id: str, display: str, url: str, directory_url: str, port: int, secret: str,
               observer_url: str) -> str:
    curl = f"curl --cacert pki/ca.crt https://localhost:{port}"
    return textwrap.dedent("""\
        # MedChain node kit: {display} (`{node_id}`)

        This folder runs one MedChain hospital node on this laptop. The step-by-step guide,
        written for someone who has never used Docker, is docs/MULTI_LAPTOP.md in the
        MedChain repository (the organiser can send it to you). The short version:

        1. Install Docker Desktop and Tailscale, and join the organiser's tailnet.
        2. Open `.env` in a text editor and check `NODE_ID` and the addresses
           (`ADVERTISE_ADDR`, `DIRECTORY_URL`, `OBSERVER_URL`) against what the organiser told you.
        3. In a terminal, in this folder:

           ```sh
           {up} up -d
           {up} exec node python scripts/preflight.py
           {up} logs -f
           ```

        The node trains on `data/{node_id}.csv`, which never leaves this laptop; only model
        updates travel, only to this node's chain neighbours, over TLS, each signed with
        this node's key (`pki/{node_id}/signing.key`). Its place in the chain comes from
        the directory at `{directory_url}`; its telemetry goes to the observer at
        `{observer_url}`. Training never waits for either of them.

        This kit contains this hospital's private keys, its directory credential and the
        network's shared secret. Treat it like a password.

        ## Check it

        ```sh
        {curl}/health
        {curl}/status      # chain, position, head/tail group, neighbours
        ```

        ## Start training (any one hospital; it floods to the others)

        ```sh
        {curl}/round/start -X POST -H 'Content-Type: application/json' \\
          -H 'X-MedChain-Secret: {secret}' -d '{{}}'
        ```

        ## Leave the chain, then stop

        ```sh
        {up} exec node python -m node.leave
        {up} down
        ```

        The directory takes this hospital out at the next round boundary and its two
        neighbours bridge to each other. Stopping without leaving counts as a crash: the
        neighbours skip it and wait for it to come back. `data/` keeps its checkpoints.

        ## Switching to a phone hotspot

        Edit only `.env`: comment out the three Tailscale address lines and fill in the
        hotspot block (this laptop's hotspot IP, and the hub's), then run the `up` command
        again. Peers pin this node's TLS key, not its address, so nothing else changes.
        This node is reachable at `{url}` on Tailscale.
        """).format(display=display, node_id=node_id, url=url, directory_url=directory_url, port=port, up=UP,
                    curl=curl, secret=secret, observer_url=observer_url)


def build_kit(node_id: str, url: str, out: Path, *, directory_url: str, pki_dir: Path, secret: str, data: Path,
              template: Path, image: str | None, observer_url: str, make_zip: bool, force: bool) -> Path:
    kit = fresh(out, node_id, force)
    for d in ("deploy", "config", "data"):
        (kit / d).mkdir()
    shutil.copyfile(NODE_COMPOSE, kit / "deploy" / "node.yml")
    port = listen_port(node_id, url, template)
    cfg = engine_yaml(node_id, template)
    (kit / "config" / "node.yaml").write_text(
        f"# {node_id}'s name and engine settings, generated by scripts/provision.py. Where it runs, its keys\n"
        "# and the network secret are in ../.env (node/settings.py lists every variable).\n"
        + yaml.safe_dump(cfg, sort_keys=False))
    env = node_env(node_id, port, url, directory_url, observer_url, secret, image)
    (kit / ".env").write_text(env)
    check_kit(kit, read_env(env))  # the same validation the node runs at start
    shutil.copyfile(data, kit / "data" / f"{node_id}.csv")
    copy_keys(pki_dir, kit, node_id)
    (kit / "README.md").write_text(kit_readme(node_id, str(cfg["display_name"]), url, directory_url, port, secret,
                                              observer_url))
    if make_zip:
        shutil.make_archive(str(out / node_id), "zip", root_dir=out, base_dir=node_id)
    return kit


def hub_env(kit: Path, directory_url: str, observer_url: str, secret: str, image: str | None,
            bind_ip: str | None) -> str:
    """The directory kit's `hub.env` for deploy/hub.yml, laid out like deploy/hub.env.example."""
    repo, version = split_image(image)
    host = urlsplit(directory_url).hostname or ""
    try:
        tailscale_ip = host if ipaddress.ip_address(host) in TAILSCALE else None
    except ValueError:
        tailscale_ip = None
    kit_path = "./" + os.path.relpath(kit.resolve(), ROOT) if kit.resolve().is_relative_to(ROOT) else str(kit.resolve())
    tailscale = (f"HUB_BIND_IP={bind_ip}" if bind_ip else f"HUB_BIND_IP={tailscale_ip}" if tailscale_ip else
                 "# (the directory's address is not a Tailscale IP: put `tailscale ip -4` here to listen on the\n"
                 "# tailnet only)\nHUB_BIND_IP=0.0.0.0")
    return textwrap.dedent(f"""\
        # MedChain hub settings, generated by scripts/provision.py. From the repository root: make hub
        # (deploy/hub.yml; docs/MULTI_LAPTOP.md walks through it).

        # This directory kit (its config/ and pki/): ./ and relative to the repo root, or absolute
        HUB_KIT={kit_path}
        # The network's shared secret (the dashboard's Start / Pause relay sends it)
        MEDCHAIN_SHARED_SECRET={secret}
        IMAGE_VERSION={version}
        """) + (f"MEDCHAIN_IMAGE={repo}\n" if repo else "") + textwrap.dedent(f"""\
        # Where the hospitals reach this hub: the dashboard's install commands (join codes) use it
        HUB_ADDRESS={host}
        # The ports in the directory's and the observer's addresses
        DIRECTORY_PORT={urlsplit(directory_url).port or 443}
        OBSERVER_PORT={urlsplit(observer_url).port or OBSERVER_PORT}
        # The dashboard, on this laptop only: http://localhost:8080
        DASHBOARD_BIND_IP=127.0.0.1
        DASHBOARD_PORT=8080
        # The dashboard's admin password (user admin, at least 10 characters): without it nobody can log in.
        # Restart the dashboard after setting it.
        MEDCHAIN_ADMIN_PASSWORD=

        # ======== Tailscale (the normal way) ========
        # Listen for the hospitals on this laptop's Tailscale IP only. 0.0.0.0 (every interface) also works.
        """) + tailscale + textwrap.dedent("""

        # ======== Phone hotspot (fallback) ========
        # To switch: put a # in front of the Tailscale line above and remove it below. The hotspot
        # hands out addresses that can change, so listen on every interface.
        #HUB_BIND_IP=0.0.0.0
        """)


def build_directory_kit(url: str, out: Path, *, pki_dir: Path, observer_url: str, secret: str, image: str | None,
                        bind_ip: str | None, make_zip: bool, force: bool) -> Path:
    kit = fresh(out, DIRECTORY, force)
    (kit / "config").mkdir()
    port = urlsplit(url).port or 443
    cfg = yaml.safe_load((ROOT / "config" / "directory.yaml").read_text())
    cfg.update(port=port, host="0.0.0.0", db_path="/data/directory.sqlite")
    if cfg.get("enrollment"):
        cfg["enrollment"]["observer_port"] = urlsplit(observer_url).port or OBSERVER_PORT
    (kit / "config" / "directory.yaml").write_text("# MedChain directory config, generated by scripts/provision.py.\n"
                                                   + yaml.safe_dump(cfg, sort_keys=False))
    copy_keys(pki_dir, kit, DIRECTORY)
    (kit / "hub.env").write_text(hub_env(kit, url, observer_url, secret, image, bind_ip))
    (kit / "README.md").write_text(textwrap.dedent(f"""\
        # MedChain hub kit (directory + observer)

        The organiser's laptop runs the hub from the MedChain repository, with this kit's
        settings (`hub.env`):

        ```sh
        make hub    # = docker compose --project-directory . --env-file {os.path.relpath(kit / "hub.env", ROOT) if kit.resolve().is_relative_to(ROOT) else kit / "hub.env"} -f deploy/hub.yml up -d --wait
        ```

        - The directory, at `{url}`, decides where each hospital sits in the chain and
          tells hospitals who to connect to when one joins or leaves. It never receives
          model weights. If it is down, hospitals already in the chain keep training.
        - The observer, at `{observer_url}`, takes the hospitals' pushed telemetry into
          Postgres for the dashboard (http://localhost:8080). Training never waits for it.
        - Postgres listens on this laptop's loopback only.

        `pki/directory/hospitals.yaml` lists which hospitals may register (an id and the
        SHA-256 of that hospital's credential, in placement order). It is re-read on
        every registration: to admit a new hospital, replace it with the refreshed file
        from the provisioning run. Or skip kits altogether: create a join code on the
        dashboard's admin page and the hospital enrolls itself with it (its install
        script runs `python -m node.enroll`). Keep `pki/ca.key` (the directory signs
        enrolled hospitals' certificates with it), `pki/directory/signing.key` (it signs
        every membership document) and `pki/observer/tls.key` private.
        """))
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
    ap.add_argument("--image", help=f"the node image, repo[:tag] (default: the published "
                                    f"ghcr.io/denimworld12/medchain-node:{IMAGE_VERSION})")
    ap.add_argument("--observer", help="the observer's address, host:port (https; default: the directory's host, "
                                       f"port {OBSERVER_PORT})")
    ap.add_argument("--observer-token", help="the observer's ingest token (default: the network's, else a new one)")
    ap.add_argument("--bind-ip", help="the hub listens for the hospitals on this IP only (HUB_BIND_IP in hub.env; "
                                      "default: the directory's address when it is a Tailscale IP)")
    ap.add_argument("--zip", action="store_true", help="also write <out>/<id>.zip for each kit")
    ap.add_argument("--force", action="store_true", help="replace kits that already exist")
    args = ap.parse_args(argv)

    try:
        peers = parse_peers(args.peers)
        directory_url = parse_url(DIRECTORY, args.directory)
        observer_url = parse_url(OBSERVER, args.observer or f"{urlsplit(directory_url).hostname}:{OBSERVER_PORT}")
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
        pki.issue(pki_dir, (DIRECTORY, host(directory_url)), [(h, host(u)) for h, u in peers.items()],
                  [(OBSERVER, host(observer_url))])
        ensure_observer_token(pki_dir, args.observer_token)
        secret = args.secret or secrets.token_urlsafe(24)
        for h in hospitals:
            kit = build_kit(h, peers[h], args.out, directory_url=directory_url, pki_dir=pki_dir, secret=secret,
                            data=data[h], template=args.template, image=args.image, observer_url=observer_url,
                            make_zip=args.zip, force=args.force)
            print(f"{h}: {kit}{' (+ ' + kit.name + '.zip)' if args.zip else ''}")
        if DIRECTORY in wanted:
            kit = build_directory_kit(directory_url, args.out, pki_dir=pki_dir, observer_url=observer_url,
                                      secret=secret, image=args.image,
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
