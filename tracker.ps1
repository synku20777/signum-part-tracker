param (
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet("init", "scan", "doctor", "backup", "update")]
    [string]$Command
)

switch ($Command) {
    "init" {
        Write-Host "Creating data directory..."
        New-Item -ItemType Directory -Force -Path data | Out-Null
        Write-Host "Starting container..."
        docker compose up -d
        Write-Host "Waiting for container to start..."
        Start-Sleep -Seconds 5
        Write-Host "Running database migrations..."
        docker compose exec irmscher-tracker tracker db upgrade
        Write-Host "Initialization complete."
    }
    "scan" {
        Write-Host "Triggering scan..."
        docker compose exec irmscher-tracker tracker trigger-scan
    }
    "doctor" {
        Write-Host "Running doctor..."
        docker compose exec irmscher-tracker tracker doctor
    }
    "backup" {
        $DateStr = Get-Date -Format "yyyyMMdd_HHmmss"
        $BackupFile = "backup_$DateStr.sqlite"
        Write-Host "Backing up database to $BackupFile..."
        docker compose exec irmscher-tracker tracker backup "/app/data/$BackupFile"
        Write-Host "Backup created at data/$BackupFile"
    }
    "update" {
        Write-Host "Pulling latest changes and rebuilding..."
        docker compose pull
        docker compose build
        docker compose up -d
        Write-Host "Applying any pending database migrations..."
        Start-Sleep -Seconds 5
        docker compose exec irmscher-tracker tracker db upgrade
    }
}
