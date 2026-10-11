# MedChain hospital installer for Windows: one command sets up this laptop as one hospital.
#
#   $env:MEDCHAIN_HUB = "100.101.1.99"   # the organiser's hub (their Tailscale IP); asked for if unset
#   irm https://raw.githubusercontent.com/Denimworld12/Hospital-Chain/v1.1.0/deploy/install/install.ps1 | iex
#
# It assumes Docker Desktop and Tailscale are installed and this laptop is on the organiser's
# tailnet. It checks that (and the hub, and disk space) and says how to fix what is missing; asks
# for the hospital's name and the join code the organiser gives you; finds this laptop's
# Tailscale address itself; enrolls (inside the published MedChain image: no Python, no
# repository); then starts the hospital. Your keys are made on this laptop and stay here.
#
# Safe to run again: an enrolled laptop is not enrolled twice; it refreshes this laptop's
# address and facts, pulls the image and (re)starts the hospital. It never prints a secret.
#
# Optional settings (environment): MEDCHAIN_DIR (default $HOME\MedChain), MEDCHAIN_PORT (8000),
# MEDCHAIN_IMAGE (ghcr.io/denimworld12/medchain-node), MEDCHAIN_IMAGE_VERSION (this installer's).
# MEDCHAIN_DISPLAY_NAME and MEDCHAIN_JOIN_CODE, if set, are used instead of asking (unattended runs).
# Works in Windows PowerShell 5.1 and PowerShell 7.

function Install-MedChain {
    $InstallerVersion = "1.1.0"
    $Image = if ($env:MEDCHAIN_IMAGE) { $env:MEDCHAIN_IMAGE } else { "ghcr.io/denimworld12/medchain-node" }
    $Version = if ($env:MEDCHAIN_IMAGE_VERSION) { $env:MEDCHAIN_IMAGE_VERSION } else { "v$InstallerVersion" }
    $Dir = if ($env:MEDCHAIN_DIR) { $env:MEDCHAIN_DIR } else { Join-Path $HOME "MedChain" }
    $Port = if ($env:MEDCHAIN_PORT) { [int]$env:MEDCHAIN_PORT } else { 8000 }
    $DirectoryPort = 8100
    $NeedGB = 5

    function Say($text) { Write-Host $text }
    function Ok($text) { Write-Host "  [ok] $text" -ForegroundColor Green }
    function Problem($what, $fix) {
        # a folder this run created and left empty goes again, so "nothing has been changed" stays true
        if ($createdDir -and (Test-Path $Dir) -and -not (Get-ChildItem -Force $Dir)) { Remove-Item $Dir }
        Write-Host ""
        Write-Host "  [!] $what" -ForegroundColor Red
        Write-Host "      What to do: $fix" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Fix that, then run the same command again. Nothing has been changed on this laptop." -ForegroundColor Yellow
    }

    Say ""
    Say "MedChain hospital installer $InstallerVersion"
    Say "-------------------------------------------"

    # ---- 1. Docker Desktop, running ----------------------------------------------------------
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Problem "Docker is not installed (no 'docker' command)." "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ , start it, then open a NEW PowerShell window."
        return
    }
    docker info --format "{{.ServerVersion}}" *> $null
    if ($LASTEXITCODE -ne 0) {
        Problem "Docker Desktop is installed but its engine is not running." "Open Docker Desktop from the Start menu and wait until the bottom-left corner says 'Engine running'. If it mentions WSL, open PowerShell as administrator, run 'wsl --update', and restart the laptop."
        return
    }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Problem "Docker Compose is missing." "Update Docker Desktop to a current version (it includes Compose)."
        return
    }
    Ok "Docker Desktop is running"

    # ---- 2. Tailscale, logged in ---------------------------------------------------------------
    $ts = Get-Command tailscale -ErrorAction SilentlyContinue
    $tsExe = if ($ts) { $ts.Source } else { [IO.Path]::Combine([string]$env:ProgramFiles, "Tailscale", "tailscale.exe") }
    if (-not (Test-Path $tsExe)) {
        Problem "Tailscale is not installed." "Install it from https://tailscale.com/download , sign in, and accept the organiser's invite."
        return
    }
    $statusJson = & $tsExe status --json 2>$null | Out-String
    $status = $null
    try { $status = $statusJson | ConvertFrom-Json } catch { }
    if (-not $status -or $status.BackendState -ne "Running") {
        $state = if ($status) { $status.BackendState } else { "not answering" }
        Problem "Tailscale is not connected (state: $state)." "Click the Tailscale icon in the system tray (bottom right, near the clock), choose 'Log in' or 'Connect', and make sure you accepted the organiser's invite."
        return
    }
    $tsIp = @($status.Self.TailscaleIPs | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' })[0]
    $tsName = ([string]$status.Self.DNSName).TrimEnd(".")
    if (-not $tsIp) {
        Problem "Tailscale is connected but this laptop has no Tailscale IPv4 address." "Open the Tailscale admin console or ask the organiser; then run this again."
        return
    }
    $device = if ($tsName) { $tsName } else { [string]$status.Self.HostName }
    Ok "Tailscale is connected: this laptop is $tsIp ($device)"

    # ---- 3. The hub, reachable over the tailnet ------------------------------------------------
    $hub = $env:MEDCHAIN_HUB
    if (-not $hub) { $hub = (Read-Host "The organiser's hub address (their Tailscale IP, e.g. 100.101.1.99)").Trim() }
    if (-not $hub) {
        Problem "No hub address given." "Ask the organiser for their hub address (a 100.x.y.z Tailscale IP)."
        return
    }
    $hubHost = $hub; $hubPort = $DirectoryPort
    if ($hub -match '^(.+):(\d+)$') { $hubHost = $Matches[1]; $hubPort = [int]$Matches[2] }
    $reachable = $false
    $tcp = New-Object System.Net.Sockets.TcpClient
    try {
        $wait = $tcp.BeginConnect($hubHost, $hubPort, $null, $null)
        if ($wait.AsyncWaitHandle.WaitOne(5000) -and $tcp.Connected) { $reachable = $true }
    } catch { } finally { $tcp.Close() }
    if (-not $reachable) {
        Problem "The organiser's hub at ${hubHost}:${hubPort} does not answer." "Check the address with the organiser, check that their hub is running ('make hub'), and that their laptop shows as connected in your Tailscale menu. A work VPN can block Tailscale: disconnect it."
        return
    }
    Ok "The hub at ${hubHost}:${hubPort} answers"

    # ---- 4. Disk space ----------------------------------------------------------------------------
    $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($Dir))
    $drive = New-Object System.IO.DriveInfo($root)
    $freeGB = [math]::Round($drive.AvailableFreeSpace / 1GB, 1)
    if ($freeGB -lt $NeedGB) {
        Problem "Only $freeGB GB free on $root; the hospital needs about $NeedGB GB." "Free some space (empty the Recycle Bin, remove large downloads), or set `$env:MEDCHAIN_DIR to a folder on another drive."
        return
    }
    Ok "$freeGB GB free on $root"

    # ---- 5. The MedChain image --------------------------------------------------------------------
    Say ""
    Say "Downloading the MedChain program ($Image`:$Version, about 2 GB the first time)..."
    docker pull "${Image}:${Version}"
    if ($LASTEXITCODE -ne 0) {
        docker image inspect "${Image}:${Version}" *> $null
        if ($LASTEXITCODE -ne 0) {
            Problem "Could not download ${Image}:${Version}." "Check the internet connection. If it says 'denied' or 'unauthorized', ask the organiser to make the medchain-node package public (or to give you a 'docker login ghcr.io')."
            return
        }
        Say "  (could not check for a newer copy; using the one already on this laptop)"
    }

    # ---- 6. This computer's facts, for the dashboard's proof of origin --------------------------
    $os = Get-CimInstance Win32_OperatingSystem
    $env:MEDCHAIN_HOST_NAME = if ($env:COMPUTERNAME) { $env:COMPUTERNAME } else { [Environment]::MachineName }
    $env:MEDCHAIN_HOST_OS = "$($os.Caption) $($os.Version)"
    $env:MEDCHAIN_HOST_BOOTED_AT = [string][int64](([DateTimeOffset]$os.LastBootUpTime).ToUnixTimeSeconds())
    $env:MEDCHAIN_TAILSCALE_DEVICE = $device

    # ---- 7. Enroll (once) ---------------------------------------------------------------------------
    $createdDir = -not (Test-Path $Dir)
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $envFile = Join-Path $Dir ".env"
    $enrolled = $false
    if (Test-Path $envFile) {
        $idLine = Select-String -Path $envFile -Pattern '^NODE_ID=(.+)$' | Select-Object -First 1
        if ($idLine -and (Test-Path ([IO.Path]::Combine($Dir, "pki", $idLine.Matches[0].Groups[1].Value, "tls.crt")))) {
            $enrolled = $true
        }
    }
    $rerun = $enrolled
    if ($enrolled) {
        Ok "This laptop is already enrolled ($($idLine.Matches[0].Groups[1].Value)); keeping its keys"
        # refresh what can change: this laptop's Tailscale address, its facts, and its port when MEDCHAIN_PORT asks
        # for another one (peers pin the key, not the address: no new join code is needed)
        $keep = Get-Content $envFile | Where-Object { $_ -notmatch '^(NODE_PORT|ADVERTISE_ADDR|MEDCHAIN_HOST_NAME|MEDCHAIN_HOST_OS|MEDCHAIN_HOST_BOOTED_AT|MEDCHAIN_TAILSCALE_DEVICE)=' }
        $nodePort = if ($env:MEDCHAIN_PORT) { $Port } else {
            (Select-String -Path $envFile -Pattern '^NODE_PORT=(\d+)$' | Select-Object -First 1).Matches[0].Groups[1].Value }
        $Port = $nodePort
        $lines = @($keep) + @("NODE_PORT=$nodePort", "ADVERTISE_ADDR=${tsIp}:$nodePort", "MEDCHAIN_HOST_NAME=$env:MEDCHAIN_HOST_NAME",
            "MEDCHAIN_HOST_OS=$env:MEDCHAIN_HOST_OS", "MEDCHAIN_HOST_BOOTED_AT=$env:MEDCHAIN_HOST_BOOTED_AT",
            "MEDCHAIN_TAILSCALE_DEVICE=$env:MEDCHAIN_TAILSCALE_DEVICE")
        [System.IO.File]::WriteAllLines($envFile, $lines)  # no BOM: Docker Compose reads it
    } else {
        Say ""
        Say "Enrolling this laptop. The organiser gives you a join code (it works once, for 30 minutes)."
        # unattended (the name given in MEDCHAIN_DISPLAY_NAME): never fall back to asking, as install.sh
        $unattended = [bool]$env:MEDCHAIN_DISPLAY_NAME
        $name = if ($env:MEDCHAIN_DISPLAY_NAME) { [string]$env:MEDCHAIN_DISPLAY_NAME } else { "" }
        $joinCode = if ($env:MEDCHAIN_JOIN_CODE) { $env:MEDCHAIN_JOIN_CODE } else { "" }
        for ($try = 1; $try -le 3; $try++) {
            while ($name.Trim().Length -lt 2 -or $name.Trim().Length -gt 60) {
                $typed = Read-Host "Your hospital's name, as the dashboard should show it (2 to 60 characters)"
                if ($null -eq $typed) {  # no one to ask (no input at all)
                    Problem "No hospital name given." "Run it in a PowerShell window and type the name, or set `$env:MEDCHAIN_DISPLAY_NAME."
                    return
                }
                $name = [string]$typed
            }
            if (-not $joinCode -and $unattended) {  # unattended: never prompt (on 5.1 -AsSecureString would wait forever)
                Problem "No join code given." "Set `$env:MEDCHAIN_JOIN_CODE to the code the organiser gave you, then run the same command again."
                return
            }
            if (-not $joinCode) {
                $secure = Read-Host "The join code (like ABCD-EFGH-JKMN-PQRS-TVWX; hidden as you type)" -AsSecureString
                if ($null -eq $secure -or $secure.Length -eq 0) {
                    Problem "No join code given." "Ask the organiser for a join code and type it when asked, or set `$env:MEDCHAIN_JOIN_CODE."
                    return
                }
                $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
                try { $joinCode = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
                finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
            }
            $env:MEDCHAIN_JOIN_CODE = $joinCode
            $env:MEDCHAIN_DISPLAY_NAME = $name.Trim()
            $enroll = @("run", "--rm", "-v", "${Dir}:/kit", "-e", "MEDCHAIN_JOIN_CODE", "-e", "MEDCHAIN_DISPLAY_NAME",
                "-e", "MEDCHAIN_HOST_NAME", "-e", "MEDCHAIN_HOST_OS", "-e", "MEDCHAIN_HOST_BOOTED_AT",
                "-e", "MEDCHAIN_TAILSCALE_DEVICE", "${Image}:${Version}", "python", "-m", "node.enroll",
                "--hub", "${hubHost}:${hubPort}", "--advertise", $tsIp, "--port", "$Port", "--out", "/kit",
                "--image-version", $Version, "--image", $Image)
            if ($tsName) { $enroll += @("--also", $tsName) }
            docker @enroll
            $code = $LASTEXITCODE
            Remove-Item Env:MEDCHAIN_JOIN_CODE -ErrorAction SilentlyContinue
            if ($code -eq 0) { $enrolled = $true; $joinCode = ""; break }
            if ($code -eq 3) {
                if ($unattended) {
                    Problem "That name is taken." "Set `$env:MEDCHAIN_DISPLAY_NAME to a name no other hospital uses, then run the same command again (the same join code still works)."
                    return
                }
                Say "That name is taken: choose another one (the same join code still works)."; $name = ""; continue
            }
            Problem "Enrollment failed (the reason is just above)." "If the code expired, was used or was mistyped, ask the organiser for a new join code, then run the same command again."
            return
        }
        if (-not $enrolled) {
            Problem "No free name after three tries." "Pick a name no other hospital uses, then run the same command again."
            return
        }
    }

    # ---- 8. Start (or restart) the hospital --------------------------------------------------------
    Say ""
    $compose = [IO.Path]::Combine($Dir, "deploy", "node.yml")
    # the node checks in with the hub only when it starts: on a re-run recreate it, so step 9 sees the hub's answer of now
    $recreate = if ($rerun) { @("--force-recreate") } else { @() }
    docker compose --project-directory $Dir -f $compose up -d @recreate
    if ($LASTEXITCODE -ne 0) {
        Problem "Docker could not start the hospital." "If it says 'port is already allocated', another program uses port ${Port}: close it, or set `$env:MEDCHAIN_PORT to another port and run the same command again (no new join code needed). Otherwise restart Docker Desktop and run this again."
        return
    }
    $id = (Select-String -Path $envFile -Pattern '^NODE_ID=(.+)$' | Select-Object -First 1).Matches[0].Groups[1].Value

    # ---- 9. Did the hub let it in? -------------------------------------------------------------------
    $tries = if ($env:MEDCHAIN_REGISTER_TRIES) { [int]$env:MEDCHAIN_REGISTER_TRIES } else { 20 }
    Say ""
    Say "Waiting for the hub to place this hospital (up to $($tries * 3)s)..."
    $registered = $false
    for ($i = 1; $i -le $tries; $i++) {
        $line = docker compose --project-directory $Dir -f $compose exec -T node python -m node.registered 2>$null
        $code = $LASTEXITCODE
        if ($code -eq 0) { $registered = $true; break }
        if ($code -eq 3) {
            docker compose --project-directory $Dir -f $compose down *> $null  # it would only retry, refused, forever
            Problem "The organiser removed this laptop from the network: the hub refuses its keys." "Delete the folder $Dir, ask the organiser for a new join code, then run the same command again."
            return
        }
        Start-Sleep -Seconds 3
    }
    if (-not $registered) { Say "  (it has not reached the hub yet; it keeps trying by itself. If this lasts, check the hub address with the organiser.)" }
    Say ""
    Write-Host "Done. This laptop is running hospital $id." -ForegroundColor Green
    Say "  - If Windows asks whether Docker Desktop Backend may use the network, tick 'Private networks' and Allow."
    Say "  - Keep the laptop plugged in, awake, and Docker Desktop and Tailscale running."
    Say "  - The organiser sees this laptop's name and live numbers on the dashboard's Nodes page within a minute."
    Say "  - Your hospital's files and keys are in $Dir (don't share that folder)."
    Say ""
    $up = "docker compose --project-directory `"$Dir`" -f `"$compose`""
    Say "  Watch it:  $up logs -f"
    Say "  Leave:     $up exec node python -m node.leave"
    Say "  Stop:      $up down"
}

Install-MedChain
