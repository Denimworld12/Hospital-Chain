"""deploy/install/install.sh, run for real with docker, tailscale, curl and df stubbed on PATH."""

import os
import shutil
import subprocess
import textwrap

import pytest

from chain_harness import ROOT

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

STUBS = {
    "docker": """\
        echo "docker $*" >>"$STUB_LOG"
        case " $* " in *" exec "*) echo "registration"; exit "${REGISTER_EXIT:-0}" ;; esac
        case " $* " in *" down "*) exit "${DOWN_EXIT:-0}" ;; esac
        case "$1" in
          run)
            kit=$(printf '%s\\n' "$@" | sed -n 's/:\\/kit$//p')
            [ "${ENROLL_EXIT:-0}" = 0 ] && printf 'NODE_ID=%s\\nNODE_PORT=8000\\n' "$ENROLLED_ID" >"$kit/.env"
            exit "${ENROLL_EXIT:-0}" ;;
        esac
        exit 0""",
    "tailscale": """\
        case "$1" in
          ip) echo 100.101.1.7 ;;
          status) echo '{"Self": {"DNSName": "friend-laptop.tail1234.ts.net."}}' ;;
        esac""",
    "curl": """echo '{"service": "directory"}'""",
    "df": """printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/x 1 1 99999999 1%% /\\n'""",
}


def run_installer(tmp_path, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in STUBS.items():
        (bin_dir / name).write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body) + "\n")
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    full = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "STUB_LOG": str(log),
            "MEDCHAIN_DIR": str(tmp_path / "MedChain"), "MEDCHAIN_HUB": "100.101.1.99",
            "ENROLLED_ID": "st-marys-3f9a", **env}
    r = subprocess.run(["bash", str(ROOT / "deploy/install/install.sh")], env=full, stdin=subprocess.DEVNULL,
                       start_new_session=True, capture_output=True, text=True, timeout=60)
    return r, log.read_text() if log.exists() else ""


def test_an_unattended_install_enrolls_and_starts_the_hospital(tmp_path):
    r, calls = run_installer(tmp_path, MEDCHAIN_DISPLAY_NAME="St Mary's", MEDCHAIN_JOIN_CODE="ABCD-EFGH-JKMN-PQRS-TVWX")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "running hospital st-marys-3f9a" in r.stdout
    assert [c.split()[1] for c in calls.splitlines()].count("run") == 1
    assert any(c.startswith("docker compose") and c.rstrip().endswith("up -d") for c in calls.splitlines())
    assert "ABCD-EFGH" not in r.stdout + r.stderr, "it never prints the join code"


def test_an_unattended_install_reports_why_enrollment_failed(tmp_path):
    r, calls = run_installer(tmp_path, MEDCHAIN_DISPLAY_NAME="St Mary's", MEDCHAIN_JOIN_CODE="ABCD-EFGH-JKMN-PQRS-TVWX",
                             ENROLL_EXIT="1")
    assert r.returncode == 1
    assert "Enrollment failed" in r.stderr and "No free name" not in r.stderr
    assert not any(c.startswith("docker compose") and "up -d" in c for c in calls.splitlines())
    assert not (tmp_path / "MedChain").exists(), "the empty folder it made is gone again"


def test_an_unattended_install_with_a_taken_name_stops_instead_of_asking(tmp_path):
    r, calls = run_installer(tmp_path, MEDCHAIN_DISPLAY_NAME="St Mary's", MEDCHAIN_JOIN_CODE="ABCD-EFGH-JKMN-PQRS-TVWX",
                             ENROLL_EXIT="3")
    assert r.returncode == 1
    assert "That name is taken" in r.stderr and "MEDCHAIN_DISPLAY_NAME" in r.stderr
    assert [c.split()[1] for c in calls.splitlines()].count("run") == 1


@pytest.mark.parametrize("env, says", [
    ({"MEDCHAIN_DISPLAY_NAME": "St Mary's"}, "No join code given"),
    ({"MEDCHAIN_DISPLAY_NAME": "St Mary's", "MEDCHAIN_JOIN_CODE": "ABCD-EFGH"}, "That is not a join code"),
    ({"MEDCHAIN_DISPLAY_NAME": "S", "MEDCHAIN_JOIN_CODE": "ABCD-EFGH-JKMN-PQRS-TVWX"}, "2 to 60 characters"),
    ({"MEDCHAIN_JOIN_CODE": "ABCD-EFGH-JKMN-PQRS-TVWX"}, "No hospital name given"),
])
def test_a_bad_name_or_code_fails_before_the_download(tmp_path, env, says):
    r, calls = run_installer(tmp_path, **env)
    assert r.returncode == 1 and says in r.stderr
    assert not any(c.split()[1] in ("pull", "run") for c in calls.splitlines()), "nothing is downloaded or enrolled"
    assert not (tmp_path / "MedChain").exists()


def test_a_rerun_with_medchain_port_moves_the_enrolled_node_without_a_new_code(tmp_path):
    kit = tmp_path / "MedChain"
    (kit / "pki" / "st-marys-3f9a").mkdir(parents=True)
    (kit / "pki" / "st-marys-3f9a" / "tls.crt").write_text("cert")
    (kit / ".env").write_text("NODE_ID=st-marys-3f9a\nNODE_PORT=8000\nADVERTISE_ADDR=100.64.0.1:8000\nSHARED_SECRET_FILE=/pki/x\n")
    r, calls = run_installer(tmp_path, MEDCHAIN_PORT="18023")
    assert r.returncode == 0, r.stdout + r.stderr
    env = dict(line.split("=", 1) for line in (kit / ".env").read_text().splitlines())
    assert (env["NODE_PORT"], env["ADVERTISE_ADDR"]) == ("18023", "100.101.1.7:18023")
    assert env["SHARED_SECRET_FILE"] == "/pki/x", "everything else is kept"
    assert " run " not in f" {calls} ", "no new enrollment"
    # without MEDCHAIN_PORT a re-run keeps the port it has
    run_installer(tmp_path)
    assert dict(line.split("=", 1) for line in (kit / ".env").read_text().splitlines())["NODE_PORT"] == "18023"



def test_a_hospital_the_organiser_removed_is_told_so_instead_of_done(tmp_path):
    """Re-running the installer after the organiser removed this laptop: the hub refuses it, so no "Done"."""
    kit = tmp_path / "MedChain"
    (kit / "pki" / "st-marys-3f9a").mkdir(parents=True)
    (kit / "pki" / "st-marys-3f9a" / "tls.crt").write_text("cert")
    (kit / ".env").write_text("NODE_ID=st-marys-3f9a\nNODE_PORT=8000\n")
    r, calls = run_installer(tmp_path, REGISTER_EXIT="3")
    assert r.returncode == 1 and "Done" not in r.stdout
    assert "organiser removed this computer" in r.stderr and "new join code" in r.stderr
    assert "has been stopped" in r.stderr and "Nothing has been changed" not in r.stderr
    assert calls.splitlines()[-1].endswith("down"), "the refused node is stopped, not left retrying"
    # the node checks in with the hub only when it starts: a re-run recreates it before asking, so a node that
    # kept running since before the removal is not reported as registered
    lines = calls.splitlines()
    up = next(i for i, c in enumerate(lines) if c.startswith("docker compose") and " up -d" in c)
    check = next(i for i, c in enumerate(lines) if "node.registered" in c)
    assert "--force-recreate" in lines[up].split() and up < check
    # a Docker hiccup while stopping it still reports the refusal
    r, _ = run_installer(tmp_path, REGISTER_EXIT="3", DOWN_EXIT="1")
    assert r.returncode == 1 and "Done" not in r.stdout
    assert "organiser removed this computer" in r.stderr
    # one that is simply slow to reach the hub still finishes, saying it keeps trying
    r, _ = run_installer(tmp_path, REGISTER_EXIT="1", MEDCHAIN_REGISTER_TRIES="1")
    assert r.returncode == 0 and "keeps trying" in r.stdout and "Done" in r.stdout
