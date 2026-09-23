#!/bin/bash
set -euo pipefail

BACKUP_DIR="/opt/youtube-export-platform/backups"
RETENTION_DAYS=30
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="/opt/youtube-export-platform/logs/backup-${TIMESTAMP}.log"
COMPOSE_FILE="/opt/youtube-export-platform/docker/docker-compose.yml"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "${LOG_FILE}"; }

backup_database() {
    log "Backing up PostgreSQL database..."
    mkdir -p "${BACKUP_DIR}/database"
    
    docker compose -f "${COMPOSE_FILE}" exec -T postgres \
        pg_dump -U "${DB_USER:-ytplatform}" "${DB_NAME:-yt_platform}" \
        --clean --if-exists --no-owner --no-privileges \
        | gzip > "${BACKUP_DIR}/database/db-${TIMESTAMP}.sql.gz"
    
    log "Database backup: ${BACKUP_DIR}/database/db-${TIMESTAMP}.sql.gz ($(du -h "${BACKUP_DIR}/database/db-${TIMESTAMP}.sql.gz" | cut -f1))"
}

backup_redis() {
    log "Backing up Redis data..."
    mkdir -p "${BACKUP_DIR}/redis"
    
    docker compose -f "${COMPOSE_FILE}" exec -T redis \
        redis-cli SAVE > /dev/null 2>&1
    
    docker compose -f "${COMPOSE_FILE}" cp \
        redis:/data/dump.rdb "${BACKUP_DIR}/redis/redis-${TIMESTAMP}.rdb"
    
    log "Redis backup: ${BACKUP_DIR}/redis/redis-${TIMESTAMP}.rdb"
}

backup_config() {
    log "Backing up configuration..."
    mkdir -p "${BACKUP_DIR}/config"
    
    cp /opt/youtube-export-platform/.env "${BACKUP_DIR}/config/env-${TIMESTAMP}.backup"
    cp "${COMPOSE_FILE}" "${BACKUP_DIR}/config/docker-compose-${TIMESTAMP}.backup"
    
    log "Configuration backup completed"
}

backup_projects() {
    log "Backing up project exports..."
    mkdir -p "${BACKUP_DIR}/projects"
    
    if [ -d "/opt/youtube-export-platform/exports" ]; then
        tar czf "${BACKUP_DIR}/projects/exports-${TIMESTAMP}.tar.gz" \
            -C /opt/youtube-export-platform exports/ 2>/dev/null || true
    fi
    
    if [ -d "/opt/youtube-export-platform/output" ]; then
        tar czf "${BACKUP_DIR}/projects/output-${TIMESTAMP}.tar.gz" \
            -C /opt/youtube-export-platform output/ 2>/dev/null || true
    fi
    
    log "Projects backup completed"
}

cleanup_old_backups() {
    log "Cleaning up backups older than ${RETENTION_DAYS} days..."
    
    find "${BACKUP_DIR}/database" -name "*.sql.gz" -type f -mtime +${RETENTION_DAYS} -delete
    find "${BACKUP_DIR}/redis" -name "*.rdb" -type f -mtime +${RETENTION_DAYS} -delete
    find "${BACKUP_DIR}/config" -name "*.backup" -type f -mtime +${RETENTION_DAYS} -delete
    find "${BACKUP_DIR}/projects" -name "*.tar.gz" -type f -mtime +${RETENTION_DAYS} -delete
    
    log "Old backups cleaned"
}

verify_backups() {
    log "Verifying backup integrity..."
    
    # Check database backup
    if [ -f "${BACKUP_DIR}/database/db-${TIMESTAMP}.sql.gz" ]; then
        gunzip -t "${BACKUP_DIR}/database/db-${TIMESTAMP}.sql.gz" && \
            log "✓ Database backup verified" || \
            log "✗ Database backup corrupted"
    fi
    
    # Check Redis backup
    if [ -f "${BACKUP_DIR}/redis/redis-${TIMESTAMP}.rdb" ]; then
        file "${BACKUP_DIR}/redis/redis-${TIMESTAMP}.rdb" | grep -q "Redis" && \
            log "✓ Redis backup verified" || \
            log "✗ Redis backup may be invalid"
    fi
}

report_status() {
    log "=== Backup Summary ==="
    log "Date: ${TIMESTAMP}"
    log "Database: $(ls -lh "${BACKUP_DIR}/database/db-${TIMESTAMP}.sql.gz" 2>/dev/null | awk '{print $5}')"
    log "Redis: $(ls -lh "${BACKUP_DIR}/redis/redis-${TIMESTAMP}.rdb" 2>/dev/null | awk '{print $5}')"
    log "Total backups: $(find "${BACKUP_DIR}" -type f | wc -l)"
    log "Backup size: $(du -sh "${BACKUP_DIR}" | cut -f1)"
}

main() {
    log "=== Backup started ==="
    
    backup_database
    backup_redis
    backup_config
    backup_projects
    cleanup_old_backups
    verify_backups
    report_status
    
    log "=== Backup completed successfully ==="
}

main
