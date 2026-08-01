#!/usr/bin/env bash
set -e

COMMAND=$1

case "$COMMAND" in
    init)
        echo "Creating data directory..."
        mkdir -p data
        echo "Starting container..."
        docker compose up -d
        echo "Waiting for container to start..."
        sleep 5
        echo "Running database migrations..."
        docker compose exec irmscher-tracker tracker db upgrade
        echo "Initialization complete."
        ;;
    scan)
        echo "Triggering scan..."
        docker compose exec irmscher-tracker tracker trigger-scan
        ;;
    doctor)
        echo "Running doctor..."
        docker compose exec irmscher-tracker tracker doctor
        ;;
    backup)
        BACKUP_FILE="backup_$(date +%Y%m%d_%H%M%S).sqlite"
        echo "Backing up database to $BACKUP_FILE..."
        docker compose exec irmscher-tracker tracker backup "/app/data/$BACKUP_FILE"
        echo "Backup created at data/$BACKUP_FILE"
        ;;
    update)
        echo "Pulling latest changes and rebuilding..."
        docker compose pull
        docker compose build
        docker compose up -d
        echo "Applying any pending database migrations..."
        sleep 5
        docker compose exec irmscher-tracker tracker db upgrade
        ;;
    *)
        echo "Usage: ./tracker.sh {init|scan|doctor|backup|update}"
        exit 1
        ;;
esac
