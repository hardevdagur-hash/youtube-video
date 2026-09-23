#!/bin/bash
set -euo pipefail

BACKUP_FILE="${1:-}"
BACKUP_DIR="/opt/youtube-export-platform/backups"
COMPOSE_FILE="/opt/youtube-export-platform/docker/docker-compose.yml"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="/opt/youtube-export-platform/logs/restore-${TIMESTAMP}.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "${LOG_FILE}"; }

list_backups() {
    log "Available database backups:"
    ls -lh "${BACKUP_DIR}/database/" 2>/dev/null | grep ".sql.gz" || log "No backups found"
    log ""
    log "Available Redis backups:"
    ls -lh "${BACKUP_DIR}/redis/" 2>/dev/null | grep ".rdb" || log "No Redis backups found"
}

restore_database() {
    local DB_BACKUP="$1"
    if [ ! -f "${DB_BACKUP}" ]; then
        log "ERROR: Backup file not found: ${DB_BACKUP}"
        exit 1
    fi
    
    log "Restoring database from: ${DB_BACKUP}"
    
    # Stop API and workers
    docker compose -f "${COMPOSE_FILE}" stop api worker beat
    
    # Drop and recreate database
    docker compose -f "${COMPOSE_FILE}" exec -T postgres \
        psql -U "${DB_USER:-ytplatform}" -d postgres -c \
        "DROP DATABASE IF EXISTS ${DB_NAME:-yt_platform};" 2>/dev/null || true
    
    docker compose -f "${COMPOSE_FILE}" exec -T postgres \
        psql -U "${DB_USER:-ytplatform}" -d postgres -c \
        "CREATE DATABASE ${DB_NAME:-yt_platform};"
    
    # Restore from backup
    gunzip -c "${DB_BACKUP}" | \
        docker compose -f "${COMPOSE_FILE}" exec -T postgres \
        psql -U "${DB_USER:-ytplatform}" "${DB_NAME:-yt_platform}"
    
    log "Database restored"
    
    # Start services
    docker compose -f "${COMPOSE_FILE}" start api worker beat
    
    log "Services restarted"
}

restore_redis() {
    local REDIS_BACKUP="$1"
    if [ ! -f "${REDIS_BACKUP}" ]; then
        log "ERROR: Redis backup not found: ${REDIS_BACKUP}"
        exit 1
    fi
    
    log "Restoring Redis from: ${REDIS_BACKUP}"
    
    docker compose -f "${COMPOSE_FILE}" stop redis
    docker compose -f "${COMPOSE_FILE}" cp "${REDIS_BACKUP}" redis:/data/dump.rdb
    docker compose -f "${COMPOSE_FILE}" start redis
    
    log "Redis restored"
}

main() {
    log "=== Restore started ==="
    
    if [ -z "${BACKUP_FILE}" ]; then
        list_backups
        log "Usage: $0 <backup-file>"
        log "       $0 database    (restore latest database backup)"
        log "       $0 redis       (restore latest redis backup)"
        exit 0
    fi
    
    case "${BACKUP_FILE}" in
        database)
            LATEST=$(ls -t "${BACKUP_DIR}/database"/db-*.sql.gz 2>/dev/null | head -1)
            if [ -n "${LATEST}" ]; then
                restore_database "${LATEST}"
            else
                log "No database backups found"
                exit 1
            fi
            ;;
        redis)
            LATEST=$(ls -t "${BACKUP_DIR}/redis"/redis-*.rdb 2>/dev/null | head -1)
            if [ -n "${LATEST}" ]; then
                restore_redis "${LATEST}"
            else
                log "No Redis backups found"
                exit 1
            fi
            ;;
        *)
            if [ -f "${BACKUP_FILE}" ]; then
                if [[ "${BACKUP_FILE}" == *.sql.gz ]]; then
                    restore_database "${BACKUP_FILE}"
                elif [[ "${BACKUP_FILE}" == *.rdb ]]; then
                    restore_redis "${BACKUP_FILE}"
                else
                    log "Unknown backup format: ${BACKUP_FILE}"
                    exit 1
                fi
            else
                log "Backup file not found: ${BACKUP_FILE}"
                exit 1
            fi
            ;;
    esac
    
    log "=== Restore completed ==="
}

main
