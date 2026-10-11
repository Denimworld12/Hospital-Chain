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
        case "$1" in
          run)
            kit=$(printf '%s\\n' "$@" | sed -n 's/:\\/kit$//p')
            printf 'NODE_ID=%s\\nNODE_PORT=8000\\n' "$ENROLLED_ID" >"$kit/.env"
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
    bin_dir.mkdir()
    for name, body in STUBS.items():
        (bin_dir / name).write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body) + "\n")
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    full = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "STUB_LOG": str(log),
            "MEDCHAIN_DIR": str(tmp_path / "MedChain"), "MEDCHAIN_HUB": "100.101.1.99",
            "ENROLLED_ID": "st-marys-3f9a", **env}
    r = subprocess.run(["bash", str(ROOT / "deploy/install/install.sh")], env=full, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
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
