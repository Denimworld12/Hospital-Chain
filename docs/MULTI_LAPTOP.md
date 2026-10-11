# Running a MedChain hospital on your laptop

MedChain trains one shared heart-disease model across several "hospitals" without
any hospital sending its patient data anywhere. For this test, each hospital is a
laptop. Your laptop runs one hospital. It trains on the small data file in your kit,
and the only thing it sends out is model updates, to the one or two laptops next to
it in the chain.

You do not need to know Docker or programming. There are two ways in:

- **One command with a join code (start here).** Install two apps, then paste one
  command and answer two questions. No zip file, no files to edit, no secret to type.
- **A kit** (a zip from the organiser) and three commands, further down. It still works.

**What you need:** a laptop (Windows 10/11, Mac or Linux) with at least 8 GB of
memory, about 5 GB of free disk space, and permission to install apps. Keep it
plugged in.

## The quick way: one command with a join code

1. Install **Docker Desktop** and **Tailscale** and join the organiser's tailnet:
   step 1 of the kit way below (only that step). Open Docker Desktop and wait for
   **Engine running**.
2. The organiser gives you a **join code** (it looks like `K7QM-2XRT-9VBD-H4NC-PW3Z`,
   works **once** and expires after **30 minutes**) and the command to paste, with
   their hub's address already in it.
3. **Windows:** open **PowerShell** (Start menu, type "PowerShell") and paste:

   ```powershell
   $env:MEDCHAIN_HUB = "100.101.1.99"; irm https://raw.githubusercontent.com/Denimworld12/Hospital-Chain/v1.1.0/deploy/install/install.ps1 | iex
   ```

   **Mac or Linux:** open **Terminal** and paste:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/Denimworld12/Hospital-Chain/v1.1.0/deploy/install/install.sh | MEDCHAIN_HUB=100.101.1.99 bash
   ```

   (Use the hub address the organiser gave you instead of `100.101.1.99`.)
4. It checks that Docker is running, Tailscale is connected, the organiser's hub
   answers and there is enough disk space. If something is missing, it says in plain
   words what to do: do that and paste the same command again.
5. It asks for **your hospital's name** (type anything, e.g. `St Mary's Leeds`: this is
   what everyone sees on the dashboard; if another hospital already has it, it asks for
   another) and the **join code** (hidden as you type).
6. It downloads the MedChain program (about 2 GB the first time), makes your
   hospital's keys **on your laptop** (they never leave it), enrolls with the hub, and
   starts your hospital. It finds your laptop's Tailscale address by itself.

Your hospital's folder is `MedChain` in your home folder. It holds your keys: don't
share it. It ends by waiting up to a minute for the hub to place your hospital. If the organiser
removed your laptop from the network, it says so instead of "Done": delete the `MedChain`
folder, ask for a new join code and run the command again.
Running the command again is safe: it keeps your keys and enrollment,
refreshes your address, and restarts the hospital (do that after a reboot, too, so the
dashboard shows the right uptime). Then do step 4 of the kit way (keep the laptop
awake). If Windows asks whether **Docker Desktop Backend** may use the network, tick
**Private networks** and allow.

The organiser's dashboard shows your laptop's real name, operating system, uptime,
Tailscale name, how fast it reaches its neighbours and how many data rows it holds,
each signed with your hospital's key and marked **Signature verified**.

To leave the chain politely, then stop (PowerShell; on a Mac use `~/MedChain`):

```powershell
docker compose --project-directory "$HOME\MedChain" -f "$HOME\MedChain\deploy\node.yml" exec node python -m node.leave
docker compose --project-directory "$HOME\MedChain" -f "$HOME\MedChain\deploy\node.yml" down
```

## The kit way

**What the organiser gives you:**

- a zip file (your **kit**), for example `hospital-b.zip`. It contains your
  hospital's private keys, so don't share it.
- an invite to their Tailscale network (a "tailnet"), by email or as a link.
- the names to check in your `.env` file (step 2).

You will install two apps, unzip a folder, check one file, and type three commands.
Allow about 30 minutes the first time, most of it downloads.

## 1. Install Docker Desktop and Tailscale

**Docker Desktop** runs the hospital program in a sealed box (a "container"), so
you don't have to install Python or anything else.

1. Download it from <https://www.docker.com/products/docker-desktop/>. On a Mac,
   choose the "Apple silicon" download for M1/M2/M3/M4 Macs, "Intel chip" otherwise
   ( > About This Mac tells you which).
2. **Windows only:** Docker Desktop needs **WSL 2** (Windows' built-in Linux). Leave
   "Use WSL 2 instead of Hyper-V" ticked in the installer. If Docker Desktop later
   says WSL is missing or out of date, open **PowerShell as administrator**, run
   `wsl --install` (or `wsl --update`), and restart the laptop.
3. Open Docker Desktop and wait until the bottom-left corner says **Engine running**.
   You can skip signing in to Docker.

**Tailscale** connects the laptops to each other privately, wherever they are,
without touching your router.

1. Download it from <https://tailscale.com/download> and sign in.
2. Open the organiser's invite and accept it. Your laptop is now on their tailnet.
3. In the Tailscale menu (menu bar on a Mac, system tray on Windows), you should see
   the organiser's laptop listed. Your own laptop's **MagicDNS name** is shown under
   "This device", something like `alex-laptop.tail1234.ts.net`. If the organiser
   asks for it, send it to them.

## 2. Unzip your kit and check `.env`

Unzip the kit somewhere easy to find, for example your Documents folder. You get a
folder like `hospital-b` containing:

| In the folder | What it is |
| --- | --- |
| `.env` | your hospital's settings: the only file you may need to touch |
| `deploy/node.yml` | tells Docker how to run your hospital |
| `pki/` | your hospital's keys and certificates. Don't share them |
| `data/` | your hospital's data file. The node also saves its progress here |
| `config/node.yaml` | the training settings, the same for every hospital |
| `README.md` | a short version of this guide |

`.env` starts with a dot, so it is hidden:

- **Mac:** in Finder press <kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>.</kbd> to show hidden
  files. Right-click `.env` > Open With > TextEdit.
- **Windows:** right-click `.env` > Open with > Notepad.

The kit has already filled in everything. Check only these lines against what the
organiser told you, and correct them if they differ:

```ini
NODE_ID=hospital-b                                   # your hospital
ADVERTISE_ADDR=alex-laptop.tail1234.ts.net:8002      # YOUR laptop's MagicDNS name, then the port
DIRECTORY_URL=https://sam-laptop.tail1234.ts.net:8100   # the organiser's laptop
OBSERVER_URL=https://sam-laptop.tail1234.ts.net:8200    # the organiser's laptop
```

Keep the `:8002`-style port numbers exactly as the kit wrote them. Don't change
anything under "The network". Save the file. In TextEdit, if it asks, keep the name
`.env` and plain text. Don't let it add `.txt`.

## 3. Start your hospital and run the preflight

Open a terminal: **Terminal** on a Mac (Applications > Utilities), **PowerShell** on
Windows. Go into your kit folder: type `cd ` (with a space), drag the kit folder
onto the terminal window, and press Enter.

**Start the hospital** (the first time, this downloads the MedChain program, a
couple of gigabytes):

```sh
docker compose --project-directory . -f deploy/node.yml up -d
```

**Run the preflight**, which checks that your hospital can reach its neighbours:

```sh
docker compose --project-directory . -f deploy/node.yml exec node python scripts/preflight.py
```

The preflight asks your running hospital who its neighbours are, then tries to reach
each one from inside the container, the same way training will. That's why the
hospital has to be started first.

- `hospital-b: chain (not placed yet)` and `no neighbours yet`: the organiser hasn't
  started the other hospitals yet, or the directory hasn't placed you yet. Wait a
  minute and run it again.
- `hospital-b <-> hospital-a  unknown  no tailscale command on this PATH ...`: **good.** The
  neighbour answered. ("unknown" only means the preflight can't see from inside the
  container whether Tailscale goes direct or through a relay.)
- `hospital-b <-> hospital-a  unreachable  ConnectError: ...`: **not good.** The
  text after "unreachable" says why. Look it up in [Troubleshooting](#troubleshooting).

**Watch it work** (press <kbd>Ctrl</kbd>+<kbd>C</kbd> to stop watching; the
hospital keeps running):

```sh
docker compose --project-directory . -f deploy/node.yml logs -f
```

The organiser starts training for everyone and watches it on their dashboard.

## 4. Keep the laptop awake

A hospital that falls asleep drops out of the chain. Its neighbours carry on, but
training gets worse until it comes back.

- Keep the laptop **plugged in** and the **lid open**.
- **Mac:** System Settings > Battery > Options: turn on "Prevent automatic sleeping on
  power adapter when the display is off". Or leave a Terminal window running
  `caffeinate -dis` for the session.
- **Windows:** Settings > System > Power & battery > Screen and sleep: set "When
  plugged in, put my device to sleep after" to **Never**.
- Leave Docker Desktop and Tailscale running.

If it does sleep, wake it up. Docker restarts the hospital by itself and it rejoins
using the progress saved in `data/`. You don't need to do anything else.

## When you're done

To leave the chain politely (your neighbours connect to each other directly), then
stop:

```sh
docker compose --project-directory . -f deploy/node.yml exec node python -m node.leave
docker compose --project-directory . -f deploy/node.yml down
```

To start again later, run the `up -d` command from step 3 again.

## Hotspot fallback

If Tailscale can't be used (a network that blocks it, or no internet at all), all
the laptops can join **one phone's hotspot** instead. Only the addresses in `.env`
change; the kit, keys and commands stay the same. Your hospital is recognised by
its key, not its address.

1. Connect your laptop to the hotspot the organiser names.
2. Find your laptop's address on the hotspot:
   - **Mac:** System Settings > Wi-Fi > Details (next to the hotspot) > IP address,
     or run `ipconfig getifaddr en0` in Terminal.
   - **Windows:** run `ipconfig` in PowerShell and look for "IPv4 Address" under
     "Wireless LAN adapter Wi-Fi".
3. The organiser tells you the hub's hotspot address (their laptop's).
4. In `.env`, put a `#` in front of the three Tailscale address lines and remove the
   `#` from the three hotspot lines. Then replace the placeholders. With your
   address `172.20.10.4` and the hub's `172.20.10.2`, the lines go from:

   ```ini
   ADVERTISE_ADDR=alex-laptop.tail1234.ts.net:8002
   DIRECTORY_URL=https://sam-laptop.tail1234.ts.net:8100
   OBSERVER_URL=https://sam-laptop.tail1234.ts.net:8200

   #ADVERTISE_ADDR=THIS-LAPTOPS-HOTSPOT-IP:8002
   #DIRECTORY_URL=https://HUB-HOTSPOT-IP:8100
   #OBSERVER_URL=https://HUB-HOTSPOT-IP:8200
   ```

   to:

   ```ini
   #ADVERTISE_ADDR=alex-laptop.tail1234.ts.net:8002
   #DIRECTORY_URL=https://sam-laptop.tail1234.ts.net:8100
   #OBSERVER_URL=https://sam-laptop.tail1234.ts.net:8200

   ADVERTISE_ADDR=172.20.10.4:8002
   DIRECTORY_URL=https://172.20.10.2:8100
   OBSERVER_URL=https://172.20.10.2:8200
   ```

   Nothing else in `.env` changes. The port numbers stay as they were.
5. Run the `up -d` command from step 3 again. Docker restarts the hospital with the
   new addresses, and it tells the directory where it is now. Then run the preflight.

If the phone gives your laptop a different address later (after a reconnect, for
example), repeat steps 2, 4 and 5 with the new address. To go back to Tailscale,
swap the `#`s back.

## Troubleshooting

Each row is a failure that the multi-laptop audit found or that this setup is known
to hit. "Preflight says" is the text after `unreachable`; "logs say" is from the
`logs -f` command.

| What you see | Why it happens | What to do |
| --- | --- | --- |
| Preflight says `ConnectError ... Name or service not known` (or `nodename nor servname`, `Temporary failure in name resolution`) for a `*.ts.net` neighbour | **Container DNS (audit H15).** Docker Desktop doesn't always pass Tailscale's MagicDNS through to containers, so a `.ts.net` name that works in your browser doesn't resolve inside the hospital's container. | Use Tailscale **IP addresses** (`100.x.y.z`) instead of names. Each laptop's IP is in the Tailscale menu, or run `tailscale ip -4`. Put yours in `ADVERTISE_ADDR` and the hub's in `DIRECTORY_URL`/`OBSERVER_URL`, then run `up -d` again. Your neighbours' addresses are whatever *they* wrote in their `.env`, so tell the organiser; usually everyone switches to IPs. |
| Preflight says `ConnectTimeout` for a neighbour, the neighbour's own preflight says `ConnectTimeout` for you, and the organiser's dashboard shows your hospital never placed in the chain | **Firewall (audit H14).** Your neighbours and the organiser's directory have to connect *in* to your laptop on your port (`NODE_PORT`). A firewall that blocks incoming connections stops them, even though your outgoing connections work. | **Mac:** System Settings > Network > Firewall > Options: allow incoming connections for Docker (`com.docker.backend`). **Windows:** when the "Windows Defender Firewall has blocked some features" box appears for Docker Desktop Backend, tick **Private networks** and allow. Also mark the Wi-Fi network as **Private**. A company-managed laptop may not allow this: use another laptop. |
| On a phone hotspot, **every** neighbour is `ConnectTimeout`, though the internet works | **Hotspot client isolation (audit H16).** Some phones (many Android ones) don't let devices on their hotspot talk to each other ("AP isolation" / "client isolation"). Nothing on the laptops can fix that. | Turn off "AP isolation" / "client isolation" in the phone's hotspot settings if it has the option, or use a different phone (iPhone hotspots let devices talk to each other), or go back to Tailscale. |
| The dashboard shows your hospital as down after a while; the logs stop, then pick up again later | **Sleep (audit §3).** A sleeping laptop is a crashed hospital: its neighbours run "degraded" rounds without it. It rejoins on wake from the chain it saved in `data/`, even if the organiser's directory is down. | Step 4: plugged in, lid open, sleep off. After waking, run `docker compose --project-directory . -f deploy/node.yml ps`. If the hospital isn't "running" (Docker Desktop itself quit), open Docker Desktop and run the `up -d` command again. |
| Tailscale is connected but neighbours are `ConnectTimeout`, or `tailscale status` shows the other laptops offline; or rounds are often slow | **Another VPN.** A work or personal VPN (Cisco AnyConnect, GlobalProtect, NordVPN, ...) can take over the routes Tailscale uses (`100.x` addresses) or block it. Tailscale then relays traffic through its servers (DERP, audit H5) or not at all. | Disconnect the other VPN for the session. In the Tailscale menu, check the organiser's laptop shows as connected. A relayed connection still works, just slower. If rounds keep timing out, the organiser can raise `ROUND_TIMEOUT_S` for everyone. |
| Logs say `certificate is not yet valid` or `certificate has expired`; every connection fails | **Clock skew.** Certificates are valid from a day before the organiser (or the hub, for a join code) made them. A laptop clock that is more than a day behind theirs rejects them, and theirs rejects yours. (A wrong clock doesn't make the dashboard mark you down: it uses the hub's own clock.) | Turn on automatic time. **Mac:** System Settings > General > Date & Time > "Set time and date automatically". **Windows:** Settings > Time & language > Date & time > "Set time automatically", then **Sync now**. Then run the `up -d` command again. |
| Logs say `directory registration failed (...); retrying` | The organiser's hub isn't reachable yet: it's not started, it's on another network, or `DIRECTORY_URL` is wrong. | Check `DIRECTORY_URL` with the organiser. The hospital keeps retrying by itself. If it was already training, it carries on meanwhile. |
| Logs or preflight say `TLS key changed` / `tls_pin_mismatch` | Your kit's keys don't match what the directory has on record, usually because you have an older kit than the organiser's latest. | Ask the organiser for your current kit. Don't mix files from two kits. |
| `required variable NODE_ID is missing a value` | The command ran outside the kit folder, or without `--project-directory .`. | `cd` into the kit folder (step 3) and copy the command exactly. |
| `port is already allocated` / `address already in use` | Another program is using your hospital's port. | Quit it, or ask the organiser. Changing the port means changing `NODE_PORT` and the port at the end of `ADVERTISE_ADDR` together. |
| `denied` or `unauthorized` when Docker downloads `ghcr.io/denimworld12/medchain-node` | The MedChain image isn't public. | Ask the organiser: they either make the image public or give you a login (`docker login ghcr.io`). |

## For the organiser

**Join codes (the quick way).** Make your kits with only your own hospital in
`--peers` (step 1 below: the hub kit from `provision.py` is still how the hub gets
its keys; everyone else joins with a code), start the hub (step 2), open the dashboard and log in
as `admin` (set `MEDCHAIN_ADMIN_PASSWORD`, at least 10 characters, in `hub.env`
first: without it nobody can log in). On **JOIN CODES**, press **Create a join code**: it is shown once.
Give it to one friend with the install command the page shows (it has your hub's
address, `HUB_ADDRESS` in `hub.env`, in it). Each code works once and expires (30
minutes by default); **Revoke** cancels one. Nobody types or receives the network
secret by hand: it travels inside the enrollment. Watch **NODES** for each laptop's
signed facts. The install scripts need the `v1.1.0` image and tag published (below);
to try a test tag first, set `INSTALLER_VERSION` in `hub.env`.

**A hub made before join codes** (its kit has no `pki/ca.key` and its directory config
no `enrollment:` block) answers "This hub cannot take join codes" on the Join codes
page. Upgrade it once: re-run `provision.py` with the same `--peers`, `--directory` and
`--pki` as before plus `--hospital directory --force` (every key is kept, so no hospital
needs a new kit), set `IMAGE_VERSION=v1.1.0` in the new `kits/directory/hub.env`, add
`MEDCHAIN_ADMIN_PASSWORD`, and run `make hub` again.

**A run that cannot finish** (a hospital went dark mid-run, so its neighbour waits out
every round): press **Stop** next to Pause on the dashboard. The dashboard sends it to
every hospital that is still alive, so the ones beyond the dark one stop too; then remove the dark one (below) and start again.

**A laptop that is off or gone.** A hospital that never answers splits the chain: the
hospitals beyond it never hear "start". Once the run ends, the control strip says it was
**incomplete** and names who never took part. On **NODES**, **Remove** takes that
hospital out on its behalf: its neighbours bridge to each other, as if it had left (with
only two hospitals, yours waits alone until someone joins again). A
hospital that enrolled with a code is also forgotten (its keys stop working); on **JOIN
CODES**, **Forget** does the same, and frees the name of an install that failed or was
abandoned, so the friend can enroll again with a new code. A kit hospital you remove
rejoins if its node starts again.

**Another program on the hospital's port?** The friend sets `$env:MEDCHAIN_PORT` (or
`MEDCHAIN_PORT=...` on a Mac) to a free port and runs the same command again: no new join
code needed. **Postgres already running on the hub laptop?** Set `POSTGRES_PORT` in
`hub.env`.

You run the **hub** (the directory, the observer, Postgres and the dashboard) from
a checkout of this repository, or the release's source zip, unpacked. Run it with
Docker on your own laptop. Docker is all you need: no Python, no `make`.

1. **Make the kits.** List every hospital with the address the others will reach
   it at (including any you run yourself, like `hospital-d` here), and your
   laptop as the directory:

   ```sh
   python scripts/provision.py --zip \
     --peers hospital-a=alex-laptop.tail1234.ts.net:8001,hospital-b=kim-laptop.tail1234.ts.net:8002,hospital-c=lee-laptop.tail1234.ts.net:8003,hospital-d=sam-laptop.tail1234.ts.net:8004 \
     --directory sam-laptop.tail1234.ts.net:8100
   ```

   If everyone else will join with a join code, list only your own hospital:
   `--peers hospital-d=sam-laptop.tail1234.ts.net:8004`.

   **No Python?** Run the same command inside the published image, from the
   repository folder (it works in PowerShell and in a Mac or Linux terminal; on
   Linux add `--user "$(id -u):$(id -g)"` after `--rm` so the kits belong to you):

   ```sh
   docker run --rm -v "${PWD}:/src" -w /src ghcr.io/denimworld12/medchain-node:v1.1.0 python scripts/provision.py --zip --peers hospital-d=sam-laptop.tail1234.ts.net:8004 --directory sam-laptop.tail1234.ts.net:8100
   ```

   This writes `kits/hospital-a.zip` and so on, plus `kits/directory/`, your hub kit
   with a filled `hub.env`. It also prints the network's shared secret; keep it.
   Send each hospital its own zip privately. A kit holds that hospital's keys.
2. **Start the hub:** `make hub`. Without `make` (a stock Windows laptop has none),
   run what it runs, from the repository folder:

   ```sh
   docker compose --project-directory . --env-file kits/directory/hub.env -f deploy/hub.yml up -d --wait
   ```

   The dashboard is at <http://localhost:8080>. Postgres listens on this laptop
   only. The directory and the observer listen on `HUB_BIND_IP`: your Tailscale IP,
   if `--directory` was one, or every interface otherwise.
3. **Run a hospital yourself**, for example the mid-run joiner:
   `make node KIT=kits/hospital-d`. This is the same `deploy/node.yml` a friend runs;
   without `make`: `docker compose --project-directory kits/hospital-d -f kits/hospital-d/deploy/node.yml up -d`.
4. **Start training** from the dashboard, or from any hospital with the `curl`
   command in its kit's `README.md`.

**Hotspot:** in `kits/directory/hub.env`, swap the `HUB_BIND_IP` lines as the comments
say and run `make hub` again. Then give each friend your hotspot address. The
hospitals' links to each other and to the hub check keys, not addresses, so nobody
needs a new kit. The dashboard's Start/Pause buttons are the exception. They reach
each hospital at its registered address and check that address against the
hospital's certificate, which was issued for its Tailscale name. On a hotspot those
buttons can fail with a certificate error. Start the run with the `curl` command
from a kit's `README.md` instead. Or re-run `provision.py` for that hospital with
its hotspot IP: the key is kept, so the pin is unchanged.

**Without Docker** (when Docker's networking misbehaves on a laptop): from a checkout of
this repository, with Python 3.10 or newer and `make` (on Windows, inside WSL),
`make node-native KIT=/path/to/the/unzipped/kit`. It creates a virtual environment
`.venv-node`, installs `node/requirements.txt`, trains the risk-assessment model
into `models/` once, and runs the hospital directly with the kit's `.env`, using
the kit's `pki/`, `data/` and `config/` folders in place of the container's.

**The image** is `ghcr.io/denimworld12/medchain-node:<IMAGE_VERSION>`. It is built for
both Intel/AMD and ARM (Apple silicon) laptops by `.github/workflows/image.yml` when
a `v*` tag is pushed (`git tag v1.1.0 && git push origin v1.1.0`), or from
Actions > Node image > Run workflow. Friends can only download it without a login if
the package is public. To make it public: GitHub > your profile > Packages >
`medchain-node` > Package settings > Change visibility.
