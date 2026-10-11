"""deploy/install/install.ps1, run for real by PowerShell 7 (pwsh) with docker and tailscale stubbed on PATH.

The docker stub records the exact arguments each call receives, one argv entry per field, and answers
`compose ... up` the way Docker Compose does: anything after `up` that is not a flag it knows is a service
name, and an unknown service fails with "no such service". A splatting slip that hands Docker a string one
character at a time therefore fails here exactly as it does on a real laptop.
"""

import os
import shutil
import socket
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "deploy" / "install" / "install.ps1"
CODE = "ABCD-EFGH-JKMN-PQRS-TVWX"

pytestmark = pytest.mark.skipif(shutil.which("pwsh") is None or shutil.which("bash") is None,
                                reason="needs pwsh (PowerShell 7) and bash")

SEP = "\x1f"
STUBS = {
    "docker": f"""\
        {{ printf '%s{SEP}' "$@"; printf '\\n'; }} >>"$STUB_LOG"
        case " $* " in *" exec "*) echo "registration"; exit "${{REGISTER_EXIT:-0}}" ;; esac
        case " $* " in *" down "*) exit 0 ;; esac
        if [ "$1" = compose ]; then
          seen_up=""
          for a in "$@"; do
            if [ -n "$seen_up" ]; then
              case "$a" in -d|--detach|--force-recreate|--wait) ;; *) echo "no such service: $a" >&2; exit 1 ;; esac
            fi
            [ "$a" = up ] && seen_up=1
          done
          exit 0
        fi
        case "$1" in
          run)
            kit=$(printf '%s\\n' "$@" | sed -n 's/:\\/kit$//p')
            if [ "${{ENROLL_EXIT:-0}}" = 0 ]; then
              mkdir -p "$kit/pki/$ENROLLED_ID" && echo cert >"$kit/pki/$ENROLLED_ID/tls.crt"
              printf 'NODE_ID=%s\\nNODE_PORT=8000\\n' "$ENROLLED_ID" >"$kit/.env"
            fi
            exit "${{ENROLL_EXIT:-0}}" ;;
        esac
        exit 0""",
    "tailscale": """\
        echo '{"BackendState": "Running", "Self": {"TailscaleIPs": ["100.101.1.7", "fd7a::7"],
               "DNSName": "friend-laptop.tail1234.ts.net.", "HostName": "friend-laptop"}}'""",
}


@pytest.fixture
def hub():
    """Something listening where the hub would be: the installer only checks that it answers a TCP connect."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    yield f"127.0.0.1:{s.getsockname()[1]}"
    s.close()


def stub_env(tmp_path, hub, env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        (bin_dir / name).write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body) + "\n")
        (bin_dir / name).chmod(0o755)
    clean = {k: v for k, v in os.environ.items() if not k.startswith("MEDCHAIN_")}
    return {**clean, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path),
            "STUB_LOG": str(tmp_path / "calls.log"), "MEDCHAIN_DIR": str(tmp_path / "MedChain"), "MEDCHAIN_HUB": hub,
            "MEDCHAIN_REGISTER_TRIES": "1", "ENROLLED_ID": "st-marys-3f9a", **env}


def calls(tmp_path):
    log = tmp_path / "calls.log"
    return [line.split(SEP)[:-1] for line in log.read_text().splitlines()] if log.exists() else []


def run_installer(tmp_path, hub, *, how=("-File", str(INSTALLER)), **env):
    r = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", *how], env=stub_env(tmp_path, hub, env),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    return r, calls(tmp_path)


def enrolled_kit(tmp_path):
    kit = tmp_path / "MedChain"
    (kit / "pki" / "st-marys-3f9a").mkdir(parents=True)
    (kit / "pki" / "st-marys-3f9a" / "tls.crt").write_text("cert")
    (kit / ".env").write_text("NODE_ID=st-marys-3f9a\nNODE_PORT=8000\nSHARED_SECRET_FILE=/pki/x\n")
    return kit


def compose_up(argvs):
    ups = [a for a in argvs if a[:1] == ["compose"] and "up" in a]
    assert len(ups) == 1, argvs
    return ups[0][ups[0].index("up"):]


def test_a_rerun_passes_force_recreate_to_docker_as_one_argument(tmp_path, hub):
    """The bug from issue #11: `--force-recreate` reached Docker as - - f o r c e ... and every re-run failed."""
    kit = enrolled_kit(tmp_path)
    r, argvs = run_installer(tmp_path, hub)
    assert r.returncode == 0, r.stdout + r.stderr
    assert compose_up(argvs) == ["up", "-d", "--force-recreate"]
    assert "no such service" not in r.stdout + r.stderr and "Done. This laptop is running hospital st-marys-3f9a" in r.stdout
    assert not any(a[:1] == ["run"] for a in argvs), "no new enrollment"
    env = dict(line.split("=", 1) for line in (kit / ".env").read_text().splitlines())
    assert env["ADVERTISE_ADDR"] == "100.101.1.7:8000" and env["SHARED_SECRET_FILE"] == "/pki/x"


def test_a_first_install_enrolls_then_starts_without_recreating(tmp_path, hub):
    r, argvs = run_installer(tmp_path, hub, MEDCHAIN_DISPLAY_NAME="St Mary's", MEDCHAIN_JOIN_CODE=CODE)
    assert r.returncode == 0, r.stdout + r.stderr
    assert compose_up(argvs) == ["up", "-d"]
    verbs = [a[0] for a in argvs]
    assert verbs.index("pull") < verbs.index("run"), "it downloads the image it enrolls with"
    assert "Done. This laptop is running hospital st-marys-3f9a" in r.stdout
    assert CODE not in r.stdout + r.stderr, "it never prints the join code"


def test_a_removed_laptop_rerun_is_told_so_and_stopped(tmp_path, hub):
    enrolled_kit(tmp_path)
    r, argvs = run_installer(tmp_path, hub, REGISTER_EXIT="3")
    assert r.returncode == 1, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "organiser removed this laptop" in out and "new join code" in out and "Done" not in out
    assert "has been stopped" in out and "Nothing has been changed" not in out
    up = next(i for i, a in enumerate(argvs) if a[:1] == ["compose"] and "up" in a)
    check = next(i for i, a in enumerate(argvs) if "node.registered" in a)
    assert "--force-recreate" in argvs[up] and up < check
    assert argvs[-1][-1] == "down", "the refused node is stopped, not left retrying"


@pytest.mark.parametrize("env, says", [
    ({"MEDCHAIN_DISPLAY_NAME": "St Mary's"}, "No join code given"),
    ({"MEDCHAIN_DISPLAY_NAME": "St Mary's", "MEDCHAIN_JOIN_CODE": "ABCD-EFGH"}, "That is not a join code"),
    ({"MEDCHAIN_DISPLAY_NAME": "S", "MEDCHAIN_JOIN_CODE": CODE}, "2 to 60 characters"),
])
def test_a_bad_name_or_code_fails_before_the_download(tmp_path, hub, env, says):
    r, argvs = run_installer(tmp_path, hub, **env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert says in r.stdout + r.stderr
    assert not any(a[:1] in (["pull"], ["run"]) for a in argvs), "nothing is downloaded or enrolled"
    assert not (tmp_path / "MedChain").exists()


def test_every_failure_exits_non_zero(tmp_path, hub):
    r, argvs = run_installer(tmp_path, hub, MEDCHAIN_DISPLAY_NAME="St Mary's", MEDCHAIN_JOIN_CODE=CODE, ENROLL_EXIT="1")
    assert r.returncode == 1 and "Enrollment failed" in r.stdout + r.stderr
    assert not any(a[:1] == ["compose"] and "up" in a for a in argvs), "nothing is started"
    # a one-off `pwsh -Command` piping it into iex, as a script would run the dashboard's command
    r, _ = run_installer(tmp_path, hub, how=("-Command", f"Get-Content -Raw '{INSTALLER}' | Invoke-Expression"),
                         MEDCHAIN_DISPLAY_NAME="St Mary's")
    assert r.returncode == 1 and "No join code given" in r.stdout + r.stderr


def test_pasted_into_an_open_window_it_sets_lastexitcode_instead_of_closing_it(tmp_path, hub):
    r = subprocess.run(["pwsh", "-NoProfile", "-NoExit", "-Command",
                        f"Get-Content -Raw '{INSTALLER}' | Invoke-Expression; Write-Host \"still open, LASTEXITCODE=$LASTEXITCODE\""],
                       env=stub_env(tmp_path, hub, {"MEDCHAIN_DISPLAY_NAME": "St Mary's"}),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert "No join code given" in r.stdout + r.stderr
    assert "still open, LASTEXITCODE=1" in r.stdout, r.stdout + r.stderr
