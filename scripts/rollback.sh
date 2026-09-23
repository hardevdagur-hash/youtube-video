#!/bin/bash
set -euo pipefail

ENVIRONMENT="${1:-production}"
VERSION="${2:-}"
COMPOSE_FILE="docker/docker-compose.yml"
PROJECT_DIR="/opt/youtube-export-platform"
BACKUP_DIR="${PROJECT_DIR}/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${PROJECT_DIR}/logs/rollback-${TIMESTAMP}.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "${LOG_FILE}"; }

rollback_to_version() {
    local TARGET_VERSION="$1"
    log "Rolling back to version: ${TARGET_VERSION}"
    
    export IMAGE_TAG="${TARGET_VERSION}"
    
    # Pull the target version
    docker compose -f "${COMPOSE_FILE}" pull 2>&1 | tee -a "${LOG_FILE}" || true
    
    # Restart with target version
    docker compose -f "${COMPOSE_FILE}" up -d --force-recreate 2>&1 | tee -a "${LOG_FILE}"
    
    log "Rollback to ${TARGET_VERSION} completed"
}

rollback_to_backup() {
    log "Rolling back to database backup..."
    
    local LATEST_BACKUP=$(ls -t "${BACKUP_DIR}"/db-*.sql 2>/dev/null | head -1)
    if [ -n "${LATEST_BACKUP}" ]; then
        log "Restoring database from: ${LATEST_BACKUP}"
        docker compose -f "${COMPOSE_FILE}" exec -T postgres \
            psql -U "${DB_USER:-ytplatform}" "${DB_NAME:-yt_platform}" < "${LATEST_BACKUP}"
        log "Database restored"
    else
        log "No backup found for database restore"
    fi
    
    # Restore environment
    local LATEST_ENV=$(ls -t "${BACKUP_DIR}"/env-*.backup 2>/dev/null | head -1)
    if [ -n "${LATEST_ENV}" ]; then
        cp "${LATEST_ENV}" "${PROJECT_DIR}/.env"
        log "Environment restored from backup"
    fi
}

main() {
    log "=== Rollback initiated: ${ENVIRONMENT} ==="
    
    if [ -n "${VERSION}" ]; then
        rollback_to_version "${VERSION}"
    else
        # Rollback to previous version
        if [ -f "${PROJECT_DIR}/.deployed" ]; then
            local PREV_VERSION=$(cat "${PROJECT_DIR}/.deployed" | cut -d: -f1)
            rollback_to_version "${PREV_VERSION}"
        fi
        rollback_to_backup
    fi
    
    # Health check
    sleep 15
    if curl -sf http://localhost:8000/api/v1/health > /dev/null 2>&1; then
        log "✓ Rollback successful - service healthy"
    else
        log "✗ Rollback health check failed - manual intervention required"
        exit 1
    fi
}

main
