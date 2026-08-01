param (
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("init", "start", "stop", "restart", "status", "logs", "scan-source", "backup", "restore", "doctor", "update")]
    [string]$Command,
    [Parameter(Position = 1)]
    [string]$Value
)

$ErrorActionPreference = "Stop"
$Service = "irmscher-tracker"

function Ensure-Environment {
    if (-not (Test-Path -LiteralPath ".env")) {
        Copy-Item -LiteralPath ".env.example" -Destination ".env"
    }
}

function Ensure-Token {
    $lines = Get-Content -LiteralPath ".env"
    $tokenLine = $lines | Where-Object { $_ -match '^TRACKER_API_TOKEN=' } | Select-Object -Last 1
    $current = if ($tokenLine) { $tokenLine.Substring("TRACKER_API_TOKEN=".Length) } else { "" }
    if ($current.Length -ge 32) { return }

    $token = (docker compose run --rm --no-deps -T --entrypoint python $Service -c "import secrets; print(secrets.token_hex(32))").Trim()
    $found = $false
    $updated = foreach ($line in $lines) {
        if ($line -match '^TRACKER_API_TOKEN=') {
            $found = $true
            "TRACKER_API_TOKEN=$token"
        } else {
            $line
        }
    }
    if (-not $found) { $updated += "TRACKER_API_TOKEN=$token" }
    Set-Content -LiteralPath ".env" -Value $updated -Encoding utf8
}

function Wait-TrackerHealth {
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $status = docker inspect --format '{{.State.Health.Status}}' $Service 2>$null
        if ($status -eq "healthy") { return }
        if ($status -eq "unhealthy") {
            docker compose logs $Service
            throw "Tracker is unhealthy"
        }
        Start-Sleep -Seconds 1
    }
    docker compose logs $Service
    throw "Timed out waiting for tracker health"
}

function Assert-BackupName([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name) -or $Name -ne [IO.Path]::GetFileName($Name)) {
        throw "Backup must be a filename inside data/"
    }
}

switch ($Command) {
    "init" {
        Ensure-Environment
        New-Item -ItemType Directory -Force -Path "data" | Out-Null
        docker compose build
        Ensure-Token
        docker compose up -d
        Wait-TrackerHealth
    }
    "start" { docker compose up -d; Wait-TrackerHealth }
    "stop" { docker compose stop }
    "restart" { docker compose restart; Wait-TrackerHealth }
    "status" { docker compose ps }
    "logs" { docker compose logs -f $Service }
    "scan-source" {
        $source = if ($Value) { $Value } else { "ebay" }
        if ($source -notin @("ebay", "sscom")) { throw "Source must be ebay or sscom" }
        docker compose exec $Service tracker trigger-scan $source --wait
    }
    "doctor" { docker compose exec $Service tracker doctor }
    "backup" {
        $filename = if ($Value) { $Value } else { "backup_$(Get-Date -Format 'yyyyMMdd_HHmmss').sqlite" }
        Assert-BackupName $filename
        docker compose exec $Service tracker backup "/app/data/$filename"
        Write-Host "Backup created at data/$filename"
    }
    "restore" {
        Assert-BackupName $Value
        if (-not (Test-Path -LiteralPath (Join-Path "data" $Value))) { throw "data/$Value not found" }
        $before = "pre_restore_$(Get-Date -Format 'yyyyMMdd_HHmmss').sqlite"
        docker compose exec $Service tracker backup "/app/data/$before"
        docker compose stop $Service
        try {
            docker compose run --rm --no-deps -T --entrypoint tracker $Service restore "/app/data/$Value"
            docker compose start $Service
            Wait-TrackerHealth
        } catch {
            docker compose start $Service
            throw "Restore failed; pre-restore backup is data/$before"
        }
    }
    "update" {
        Ensure-Environment
        docker compose build --pull
        Ensure-Token
        docker compose up -d --remove-orphans
        Wait-TrackerHealth
    }
}
