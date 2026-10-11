#!/usr/bin/env bash
# MedChain hospital installer for macOS and Linux: one command sets up this computer as one hospital.
#
#   curl -fsSL https://raw.githubusercontent.com/Denimworld12/Hospital-Chain/v1.1.0/deploy/install/install.sh \
#     | MEDCHAIN_HUB=100.101.1.99 bash
#
# The same steps as install.ps1 (Windows): it assumes Docker and Tailscale are installed and this
# computer is on the organiser's tailnet; checks that, the hub and disk space, and says how to fix
# what is missing; asks for the hospital's name and the join code; finds this computer's
# Tailscale address itself; enrolls inside the published MedChain image (no Python, no
# repository); and starts the hospital. Its keys are made here and stay here.
#
# Safe to run again: an enrolled computer is not enrolled twice; it refreshes its address and
# facts, pulls the image and (re)starts the hospital. It never prints a secret.
#
# Optional settings (environment): MEDCHAIN_HUB, MEDCHAIN_DIR (default ~/MedChain), MEDCHAIN_PORT
# (8000), MEDCHAIN_IMAGE (ghcr.io/denimworld12/medchain-node), MEDCHAIN_IMAGE_VERSION (this installer's).
# MEDCHAIN_DISPLAY_NAME and MEDCHAIN_JOIN_CODE, if set, are used instead of asking (unattended runs).

set -u

INSTALLER_VERSION=1.1.0
IMAGE=${MEDCHAIN_IMAGE:-ghcr.io/denimworld12/medchain-node}
VERSION=${MEDCHAIN_IMAGE_VERSION:-v$INSTALLER_VERSION}
DIR=${MEDCHAIN_DIR:-$HOME/MedChain}
PORT=${MEDCHAIN_PORT:-8000}
DIRECTORY_PORT=8100
NEED_GB=5
REGISTER_TRIES=${MEDCHAIN_REGISTER_TRIES:-20}  # x 3 s: how long to wait for the hub to place the hospital

ok() { printf '  [ok] %s\n' "$1"; }
CREATED_DIR=""
problem() {
  # a folder this run created and left empty goes again, so "nothing has been changed" stays true
  [ -n "$CREATED_DIR" ] && rmdir "$DIR" 2>/dev/null
  printf '\n  [!] %s\n      What to do: %s\n\n  %s\n' "$1" "$2" \
    "${3:-Fix that, then run the same command again. Nothing has been changed on this computer.}" >&2
  exit 1
}
# node/enroll.py's normalize_code: 20 letters and digits from its alphabet, any dashes or spaces dropped
valid_code() {
  local raw
  raw=$(printf '%s' "$1" | tr -cd 'A-Za-z0-9' | tr 'a-z' 'A-Z')
  [ "${#raw}" -eq 20 ] && [ -z "$(printf '%s' "$raw" | tr -d 'ABCDEFGHJKMNPQRSTVWXYZ23456789')" ]
}
# read from the terminal even when this script itself arrives on stdin (curl ... | bash)
ask() { local reply; IFS= read -r -p "$1" reply </dev/tty || reply=""; printf '%s' "$reply"; }
ask_hidden() { local reply; IFS= read -r -s -p "$1" reply </dev/tty || reply=""; printf '\n' >/dev/tty; printf '%s' "$reply"; }

printf '\nMedChain hospital installer %s\n-------------------------------------------\n' "$INSTALLER_VERSION"

# ---- 1. Docker, running ------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 ||
  problem "Docker is not installed (no 'docker' command)." \
          "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ (or Docker Engine on Linux) and start it."
docker info >/dev/null 2>&1 ||
  problem "Docker is installed but its engine is not running (or you may not use it)." \
          "Open Docker Desktop and wait for 'Engine running'. On Linux: 'sudo systemctl start docker', and add yourself to the docker group."
docker compose version >/dev/null 2>&1 ||
  problem "Docker Compose is missing." "Update Docker Desktop, or install the docker-compose-plugin package on Linux."
ok "Docker is running"

# ---- 2. Tailscale, logged in -------------------------------------------------------------------
TS=$(command -v tailscale || true)
[ -z "$TS" ] && [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] && TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
[ -n "$TS" ] || problem "Tailscale is not installed." "Install it from https://tailscale.com/download , sign in, and accept the organiser's invite."
TS_IP=$("$TS" ip -4 2>/dev/null | head -n 1)
[ -n "$TS_IP" ] ||
  problem "Tailscale is not connected." "Open Tailscale (menu bar on a Mac), log in or connect, and make sure you accepted the organiser's invite."
# this computer's MagicDNS name: the first DNSName in the status (Self comes before the peers)
TS_NAME=$("$TS" status --json 2>/dev/null | sed -n 's/.*"DNSName": *"\([^"]*\)".*/\1/p' | head -n 1)
TS_NAME=${TS_NAME%.}
DEVICE=${TS_NAME:-$(hostname)}
ok "Tailscale is connected: this computer is $TS_IP ($DEVICE)"

# ---- 3. The hub, reachable over the tailnet ----------------------------------------------------
HUB=${MEDCHAIN_HUB:-}
[ -n "$HUB" ] || HUB=$(ask "The organiser's hub address (their Tailscale IP, e.g. 100.101.1.99): ")
HUB=$(printf '%s' "$HUB" | tr -d '[:space:]')
[ -n "$HUB" ] || problem "No hub address given." "Ask the organiser for their hub address (a 100.x.y.z Tailscale IP)."
case "$HUB" in *:*) HUB_HOST=${HUB%:*}; HUB_PORT=${HUB##*:} ;; *) HUB_HOST=$HUB; HUB_PORT=$DIRECTORY_PORT ;; esac
curl -sk --max-time 6 "https://$HUB_HOST:$HUB_PORT/health" 2>/dev/null | grep -q '"directory"' ||
  problem "The organiser's hub at $HUB_HOST:$HUB_PORT does not answer." \
          "Check the address with the organiser, that their hub is running, and that their computer shows as connected in your Tailscale menu. A work VPN can block Tailscale: disconnect it."
ok "The hub at $HUB_HOST:$HUB_PORT answers"

# ---- 4. Disk space -----------------------------------------------------------------------------
[ -d "$DIR" ] || CREATED_DIR=1
mkdir -p "$DIR" || problem "Cannot create $DIR." "Set MEDCHAIN_DIR to a folder you can write to."
FREE_KB=$(df -Pk "$DIR" | awk 'NR==2 {print $4}')
[ "${FREE_KB:-0}" -ge $((NEED_GB * 1024 * 1024)) ] ||
  problem "Only $((FREE_KB / 1024 / 1024)) GB free where $DIR is; the hospital needs about $NEED_GB GB." \
          "Free some space, or set MEDCHAIN_DIR to a folder on another disk."
ok "$((FREE_KB / 1024 / 1024)) GB free"

# ---- 5. Enrolled already? If not: the hospital's name and the join code, checked before the download ----
ENV_FILE="$DIR/.env"
NODE_ID=$(sed -n 's/^NODE_ID=//p' "$ENV_FILE" 2>/dev/null | head -n 1)
RERUN=""
if [ -n "$NODE_ID" ] && [ -f "$DIR/pki/$NODE_ID/tls.crt" ]; then
  RERUN=1
  ok "This computer is already enrolled ($NODE_ID); keeping its keys"
else
  printf '\nEnrolling this computer. The organiser gives you a join code (it works once, for 30 minutes).\n'
  NAME=${MEDCHAIN_DISPLAY_NAME:-}
  CODE=${MEDCHAIN_JOIN_CODE:-}
  if [ -n "$NAME" ] && { [ "${#NAME}" -lt 2 ] || [ "${#NAME}" -gt 60 ]; }; then
    problem "The hospital name in MEDCHAIN_DISPLAY_NAME must be 2 to 60 characters." \
            "Set MEDCHAIN_DISPLAY_NAME to the hospital's name, then run the same command again."
  fi
  while [ "${#NAME}" -lt 2 ] || [ "${#NAME}" -gt 60 ]; do
    ( : </dev/tty ) 2>/dev/null || problem "No hospital name given." "Set MEDCHAIN_DISPLAY_NAME, or run it in a terminal and type the name."
    NAME=$(ask "Your hospital's name, as the dashboard should show it (2 to 60 characters): ")
  done
  [ -n "$CODE" ] || CODE=$(ask_hidden "The join code (like ABCD-EFGH-JKMN-PQRS-TVWX; hidden as you type): ")
  [ -n "$CODE" ] || problem "No join code given." "Set MEDCHAIN_JOIN_CODE to the code the organiser gave you (or type it when asked), then run the same command again."
  valid_code "$CODE" ||
    problem "That is not a join code: a join code is 20 letters and digits in groups of four, like ABCD-EFGH-JKMN-PQRS-TVWX." \
            "Check it with the organiser (or ask for a new one), then run the same command again."
fi

# ---- 6. The MedChain image ---------------------------------------------------------------------
printf '\nDownloading the MedChain program (%s:%s, about 2 GB the first time)...\n' "$IMAGE" "$VERSION"
if ! docker pull "$IMAGE:$VERSION"; then
  docker image inspect "$IMAGE:$VERSION" >/dev/null 2>&1 ||
    problem "Could not download $IMAGE:$VERSION." \
            "Check the internet connection. If it says 'denied' or 'unauthorized', ask the organiser to make the medchain-node package public."
  printf '  (could not check for a newer copy; using the one already on this computer)\n'
fi

# ---- 7. This computer's facts, for the dashboard's proof of origin -----------------------------
if [ "$(uname)" = Darwin ]; then
  OS="$(sw_vers -productName) $(sw_vers -productVersion)"
  BOOTED=$(sysctl -n kern.boottime | sed -n 's/^{ sec = \([0-9]*\),.*/\1/p')  # "{ sec = 1712345678, usec = ... }"
else
  OS=$(sed -n 's/^PRETTY_NAME="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' /etc/os-release 2>/dev/null)
  OS="${OS:-Linux} ($(uname -r))"
  BOOTED=$(awk '/^btime/ {print $2}' /proc/stat 2>/dev/null)
fi
export MEDCHAIN_HOST_NAME MEDCHAIN_HOST_OS MEDCHAIN_HOST_BOOTED_AT MEDCHAIN_TAILSCALE_DEVICE
MEDCHAIN_HOST_NAME=$(hostname)
MEDCHAIN_HOST_OS=$OS
MEDCHAIN_HOST_BOOTED_AT=$BOOTED
MEDCHAIN_TAILSCALE_DEVICE=$DEVICE

# ---- 8. Enroll (once), or refresh an enrolled computer's settings -------------------------------
USER_FLAG=()
RECREATE=()
[ "$(uname)" = Linux ] && USER_FLAG=(--user "$(id -u):$(id -g)")  # the kit's files belong to you, not root
if [ -n "$RERUN" ]; then
  # the node checks in with the hub only when it starts: recreate it, so step 9 sees the hub's answer of now
  RECREATE=(--force-recreate)
  # refresh what can change: this computer's Tailscale address, its facts, and its port when MEDCHAIN_PORT asks
  # for another one (peers pin the key, not the address: no new join code is needed)
  NODE_PORT=${MEDCHAIN_PORT:-$(sed -n 's/^NODE_PORT=//p' "$ENV_FILE" | head -n 1)}
  PORT=$NODE_PORT
  { grep -Ev '^(NODE_PORT|ADVERTISE_ADDR|MEDCHAIN_HOST_NAME|MEDCHAIN_HOST_OS|MEDCHAIN_HOST_BOOTED_AT|MEDCHAIN_TAILSCALE_DEVICE)=' "$ENV_FILE"
    printf 'NODE_PORT=%s\nADVERTISE_ADDR=%s:%s\nMEDCHAIN_HOST_NAME=%s\nMEDCHAIN_HOST_OS=%s\nMEDCHAIN_HOST_BOOTED_AT=%s\nMEDCHAIN_TAILSCALE_DEVICE=%s\n' \
      "$NODE_PORT" "$TS_IP" "$NODE_PORT" "$MEDCHAIN_HOST_NAME" "$MEDCHAIN_HOST_OS" "$MEDCHAIN_HOST_BOOTED_AT" "$MEDCHAIN_TAILSCALE_DEVICE"
  } >"$ENV_FILE.new" && mv "$ENV_FILE.new" "$ENV_FILE"
else
  enrolled=""
  for _ in 1 2 3; do
    while [ "${#NAME}" -lt 2 ] || [ "${#NAME}" -gt 60 ]; do
      NAME=$(ask "Your hospital's name, as the dashboard should show it (2 to 60 characters): ")
    done
    [ -n "$CODE" ] || CODE=$(ask_hidden "The join code (like ABCD-EFGH-JKMN-PQRS-TVWX; hidden as you type): ")
    MEDCHAIN_JOIN_CODE=$CODE
    MEDCHAIN_DISPLAY_NAME=$NAME
    export MEDCHAIN_JOIN_CODE MEDCHAIN_DISPLAY_NAME
    ALSO=()
    [ -n "$TS_NAME" ] && ALSO=(--also "$TS_NAME")
    docker run --rm ${USER_FLAG[@]+"${USER_FLAG[@]}"} -v "$DIR:/kit" -e MEDCHAIN_JOIN_CODE -e MEDCHAIN_DISPLAY_NAME \
      -e MEDCHAIN_HOST_NAME -e MEDCHAIN_HOST_OS -e MEDCHAIN_HOST_BOOTED_AT -e MEDCHAIN_TAILSCALE_DEVICE \
      "$IMAGE:$VERSION" python -m node.enroll --hub "$HUB_HOST:$HUB_PORT" --advertise "$TS_IP" \
      ${ALSO[@]+"${ALSO[@]}"} --port "$PORT" --out /kit --image-version "$VERSION" --image "$IMAGE"
    code=$?
    unset MEDCHAIN_JOIN_CODE
    if [ $code -eq 0 ]; then enrolled=1; break; fi
    if [ $code -eq 3 ]; then
      ( : </dev/tty ) 2>/dev/null || problem "That name is taken." \
        "Set MEDCHAIN_DISPLAY_NAME to a name no other hospital uses, then run the same command again."
      printf 'That name is taken: choose another one (the same join code still works).\n'
      NAME=""
      continue
    fi
    problem "Enrollment failed (the reason is just above)." \
            "If the code expired, was used or was mistyped, ask the organiser for a new join code, then run the same command again."
  done
  [ -n "$enrolled" ] || problem "No free name after three tries." "Pick a name no other hospital uses, then run the same command again."
  NODE_ID=$(sed -n 's/^NODE_ID=//p' "$ENV_FILE" | head -n 1)
fi

# ---- 9. Start (or restart) the hospital --------------------------------------------------------
printf '\n'
docker compose --project-directory "$DIR" -f "$DIR/deploy/node.yml" up -d ${RECREATE[@]+"${RECREATE[@]}"} ||
  problem "Docker could not start the hospital." \
          "If it says 'port is already allocated', another program uses port $PORT: stop it, or set MEDCHAIN_PORT to another port and run the same command again (no new join code needed). Otherwise restart Docker and run this again."
UP="docker compose --project-directory \"$DIR\" -f \"$DIR/deploy/node.yml\""

# ---- 10. Did the hub let it in? -------------------------------------------------------------------
printf '\nWaiting for the hub to place this hospital (up to %ss)...\n' "$((REGISTER_TRIES * 3))"
registered=""
for _ in $(seq 1 "$REGISTER_TRIES"); do
  line=$(docker compose --project-directory "$DIR" -f "$DIR/deploy/node.yml" exec -T node python -m node.registered 2>/dev/null)
  code=$?
  [ $code -eq 0 ] && { registered=1; break; }
  [ $code -eq 3 ] && {
    docker compose --project-directory "$DIR" -f "$DIR/deploy/node.yml" down >/dev/null 2>&1
    problem "The organiser removed this computer from the network: the hub refuses its keys (${line#the hub refuses this hospital: })." \
      "Delete the folder $DIR, ask the organiser for a new join code, then run the same command again." \
      "This computer's hospital has been stopped; nothing else was changed."
  }
  sleep 3
done
[ -n "$registered" ] || printf '  (it has not reached the hub yet; it keeps trying by itself. If this lasts, check the hub address with the organiser.)\n'
printf '\nDone. This computer is running hospital %s.\n' "$NODE_ID"
printf '  - Keep it plugged in and awake, with Docker and Tailscale running.\n'
printf "  - The organiser sees this computer's name and live numbers on the dashboard's Nodes page within a minute.\n"
printf "  - Your hospital's files and keys are in %s (don't share that folder).\n\n" "$DIR"
printf '  Watch it:  %s logs -f\n  Leave:     %s exec node python -m node.leave\n  Stop:      %s down\n' "$UP" "$UP" "$UP"
