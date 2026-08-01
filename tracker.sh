#!/usr/bin/env bash
set -euo pipefail

SERVICE=irmscher-tracker

ensure_env() {
    if [[ ! -f .env ]]; then
        cp .env.example .env
    fi
}

ensure_token() {
    local current token temporary
    current="$(sed -n 's/^TRACKER_API_TOKEN=//p' .env | tail -n 1)"
    if [[ ${#current} -ge 32 ]]; then
        return
    fi
    token="$(docker compose run --rm --no-deps -T --entrypoint python "$SERVICE" -c 'import secrets; print(secrets.token_hex(32))')"
    temporary="$(mktemp .env.XXXXXX)"
    awk -v token="$token" '
        BEGIN { found=0 }
        /^TRACKER_API_TOKEN=/ { print "TRACKER_API_TOKEN=" token; found=1; next }
        { print }
        END { if (!found) print "TRACKER_API_TOKEN=" token }
    ' .env > "$temporary"
    mv "$temporary" .env
}

wait_for_health() {
    local status
    for _ in {1..60}; do
        status="$(docker inspect --format '{{.State.Health.Status}}' "$SERVICE" 2>/dev/null || true)"
        if [[ "$status" == healthy ]]; then
            return
        fi
        if [[ "$status" == unhealthy ]]; then
            docker compose logs "$SERVICE"
            exit 1
        fi
        sleep 1
    done
    docker compose logs "$SERVICE"
    echo "Timed out waiting for tracker health" >&2
    exit 1
}

require_backup_name() {
    if [[ -z ${1:-} || "$1" == */* || "$1" == *\\* ]]; then
        echo "Backup must be a filename inside data/" >&2
        exit 2
    fi
}

command="${1:-}"
case "$command" in
    init)
        ensure_env
        mkdir -p data
        docker compose build
        ensure_token
        docker compose up -d
        wait_for_health
        ;;
    start)
        docker compose up -d
        wait_for_health
        ;;
    stop)
        docker compose stop
        ;;
    restart)
        docker compose restart
        wait_for_health
        ;;
    status)
        docker compose ps
        ;;
    logs)
        docker compose logs -f "$SERVICE"
        ;;
    scan-source)
        source="${2:-ebay}"
        [[ "$source" == ebay || "$source" == sscom ]] || { echo "Source must be ebay or sscom" >&2; exit 2; }
        docker compose exec "$SERVICE" tracker trigger-scan "$source" --wait
        ;;
    doctor)
        docker compose exec "$SERVICE" tracker doctor
        ;;
    backup)
        filename="${2:-backup_$(date +%Y%m%d_%H%M%S).sqlite}"
        require_backup_name "$filename"
        docker compose exec "$SERVICE" tracker backup "/app/data/$filename"
        echo "Backup created at data/$filename"
        ;;
    restore)
        filename="${2:-}"
        require_backup_name "$filename"
        [[ -f "data/$filename" ]] || { echo "data/$filename not found" >&2; exit 2; }
        before="pre_restore_$(date +%Y%m%d_%H%M%S).sqlite"
        docker compose exec "$SERVICE" tracker backup "/app/data/$before"
        docker compose stop "$SERVICE"
        if docker compose run --rm --no-deps -T --entrypoint tracker "$SERVICE" restore "/app/data/$filename"; then
            docker compose start "$SERVICE"
            wait_for_health
        else
            docker compose start "$SERVICE"
            echo "Restore failed; pre-restore backup is data/$before" >&2
            exit 1
        fi
        ;;
    update)
        ensure_env
        docker compose build --pull
        ensure_token
        docker compose up -d --remove-orphans
        wait_for_health
        ;;
    *)
        echo "Usage: $0 {init|start|stop|restart|status|logs|scan-source|backup|restore|doctor|update}" >&2
        exit 2
        ;;
esac
