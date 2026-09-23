#!/bin/bash
set -euo pipefail

ENVIRONMENT="${1:-staging}"
VERSION="${2:-latest}"
COMPOSE_FILE="docker/docker-compose.yml"
PROJECT_DIR="/opt/youtube-export-platform"
BACKUP_DIR="${PROJECT_DIR}/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DEPLOY_LOG="${PROJECT_DIR}/logs/deploy-${TIMESTAMP}.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "${DEPLOY_LOG}"
}

error_exit() {
    log "ERROR: $1"
    exit 1
}

# Pre-deployment checks
pre_checks() {
    log "Running pre-deployment checks..."
    
    # Check disk space
    AVAILABLE=$(df / | awk 'NR==2 {print $4}')
    if [ "${AVAILABLE}" -lt 5242880 ]; then
        error_exit "Insufficient disk space: ${AVAILABLE}KB available, need 5GB"
    fi
    
    # Check Docker
    docker info > /dev/null 2>&1 || error_exit "Docker not running"
    
    # Check Docker Compose
    docker compose version > /dev/null 2>&1 || error_exit "Docker Compose not available"
    
    # Check if deployed already
    if [ -f "${PROJECT_DIR}/.deployed" ]; then
        log "Previous deployment exists: $(cat ${PROJECT_DIR}/.deployed)"
    fi
    
    log "Pre-deployment checks passed"
}

# Backup current state
backup_current() {
    log "Creating pre-deployment backup..."
    
    mkdir -p "${BACKUP_DIR}"
    
    # Backup database
    docker compose -f "${COMPOSE_FILE}" exec -T postgres \
        pg_dump -U "${DB_USER:-ytplatform}" "${DB_NAME:-yt_platform}" > \
        "${BACKUP_DIR}/db-${TIMESTAMP}.sql" || log "WARNING: Database backup failed"
    
    # Backup .env
    cp "${PROJECT_DIR}/.env" "${BACKUP_DIR}/env-${TIMESTAMP}.backup" || true
    
    # Backup Docker Compose
    cp "${COMPOSE_FILE}" "${BACKUP_DIR}/docker-compose-${TIMESTAMP}.backup" || true
    
    # Keep only last 7 backups
    ls -t "${BACKUP_DIR}"/db-*.sql 2>/dev/null | tail -n +8 | xargs -r rm
    ls -t "${BACKUP_DIR}"/env-*.backup 2>/dev/null | tail -n +8 | xargs -r rm
    
    log "Backup completed: ${BACKUP_DIR}"
}

# Deploy new version
deploy_version() {
    log "Deploying version: ${VERSION} to ${ENVIRONMENT}"
    
    # Pull latest images
    export IMAGE_TAG="${VERSION}"
    docker compose -f "${COMPOSE_FILE}" pull || \
        error_exit "Failed to pull images"
    
    # Blue-green deployment
    if [ "${ENVIRONMENT}" = "production" ]; then
        deploy_blue_green
    else
        deploy_rolling
    fi
    
    log "Deployment completed"
}

# Rolling deployment
deploy_rolling() {
    log "Performing rolling update..."
    
    # Start new containers with rolling update
    docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans --no-deps api worker 2>&1 | tee -a "${DEPLOY_LOG}"
    
    # Wait for stability
    log "Waiting for services to stabilize..."
    sleep 15
    
    # Health check
    if ! health_check; then
        log "Rolling update failed - initiating rollback"
        rollback
        error_exit "Rolling update failed, rolled back"
    fi
}

# Blue-green deployment
deploy_blue_green() {
    log "Performing blue-green deployment..."
    
    local COLOR="blue"
    if docker compose -f "${COMPOSE_FILE}" ps api-green 2>/dev/null | grep -q "Up"; then
        COLOR="blue"
        NEW_COLOR="green"
    else
        COLOR="green"
        NEW_COLOR="blue"
    fi
    
    log "Current: ${COLOR}, Deploying: ${NEW_COLOR}"
    
    # Start new stack
    COLOR="${NEW_COLOR}" docker compose -f "${COMPOSE_FILE}" up -d api-${NEW_COLOR} worker-${NEW_COLOR} 2>&1 | tee -a "${DEPLOY_LOG}"
    
    # Wait for new stack
    log "Waiting for new stack to be ready..."
    sleep 30
    
    # Health check new stack
    if ! health_check; then
        log "New stack health check failed - rolling back"
        docker compose -f "${COMPOSE_FILE}" stop api-${NEW_COLOR} worker-${NEW_COLOR}
        error_exit "Blue-green deployment failed"
    fi
    
    # Switch traffic
    log "Switching traffic to ${NEW_COLOR}"
    docker compose -f "${COMPOSE_FILE}" up -d nginx 2>&1 | tee -a "${DEPLOY_LOG}"
    
    # Stop old stack
    log "Stopping old stack (${COLOR})"
    docker compose -f "${COMPOSE_FILE}" stop api-${COLOR} worker-${COLOR}
    
    log "Blue-green deployment completed"
}

# Health check
health_check() {
    log "Running health checks..."
    
    local endpoints=(
        "http://localhost:8000/api/v1/health"
    )
    
    for endpoint in "${endpoints[@]}"; do
        for i in {1..10}; do
            if curl -sf "${endpoint}" > /dev/null 2>&1; then
                log "✓ ${endpoint}"
                break
            fi
            if [ "${i}" -eq 10 ]; then
                log "✗ ${endpoint} failed after 10 attempts"
                return 1
            fi
            sleep 3
        done
    done
    
    return 0
}

# Post-deployment
post_deploy() {
    log "Running post-deployment tasks..."
    
    # Run database migrations
    log "Running database migrations..."
    docker compose -f "${COMPOSE_FILE}" exec -T api alembic upgrade head || \
        log "WARNING: Migration may have failed"
    
    # Clean up old images
    docker image prune -f --filter "until=24h" || true
    
    # Record deployment
    echo "${VERSION}:${TIMESTAMP}" > "${PROJECT_DIR}/.deployed"
    
    log "Post-deployment tasks completed"
}

# Rollback
rollback() {
    log "Initiating rollback..."
    
    local PREV_VERSION=""
    if [ -f "${PROJECT_DIR}/.deployed" ]; then
        PREV_VERSION=$(cat "${PROJECT_DIR}/.deployed" | cut -d: -f1)
    fi
    
    if [ -n "${PREV_VERSION}" ]; then
        export IMAGE_TAG="${PREV_VERSION}"
        docker compose -f "${COMPOSE_FILE}" up -d --force-recreate
        log "Rolled back to version: ${PREV_VERSION}"
    else
        log "No previous version found for rollback"
        return 1
    fi
}

# Main
main() {
    log "=== Deployment started: ${ENVIRONMENT} / ${VERSION} ==="
    
    pre_checks
    backup_current
    deploy_version
    
    if health_check; then
        post_deploy
        log "✓ Deployment successful"
    else
        log "✗ Health check failed after deployment"
        rollback
        exit 1
    fi
}

main
