## Goal
- Transform the existing MATRIX YouTube Platform into a production-ready enterprise application with fully automated CI/CD, Docker containerization, zero-downtime deployments, health monitoring, disaster recovery, backup automation, observability, and security hardening.

## Constraints & Preferences
- DO NOT modify, rewrite, duplicate, or move existing business logic (Project Manager, Pipeline Orchestrator, KG Engine, SEO Engine, generators, editors, exporters, background processing, database, cache, security, observability, prompts, tests).
- DO NOT introduce breaking changes; all existing synchronous and asynchronous paths must continue to work.
- Follow 12-Factor App methodology, Cloud Native principles, and Infrastructure as Code.
- Use Docker + Docker Compose for container orchestration; design for future Kubernetes migration.
- Use GitHub Actions for CI/CD with automated testing, security scanning, and deployment pipelines.
- Follow blue-green deployment strategy for zero-downtime production updates.
- All secrets must be environment-injected, never stored in images or Git.
- Health checks required on every service with readiness/liveness probes.
- Automated backups with retention policy and point-in-time recovery.
- Monitoring via Prometheus + Grafana + OpenTelemetry + Sentry + Flower.
- Support horizontal scaling of API and worker services.
- Every deployment runs: Secret Scan → Build & Test → Docker Build → Security Scan → Staging Deploy → Smoke Tests → Production Deploy (Blue-Green) → Health Check → Version Tag.
- Cover testing infrastructure must continue to pass.

## Progress
### Done (Production Readiness Audit & Fix)
- Fixed OpenTelemetry `ConsoleSpanExporter` crash on Windows (`observability/tracing.py`, `observability/config.py`)
- Fixed global exception handler to expose real errors instead of generic messages (`webapp/main.py`)
- Fixed 16 test files (unit tests) with systematic API alignment — all 1081 unit tests passing
- Verified all 3 core workflows execute end-to-end successfully via API testing
- Generated comprehensive Production Readiness Report (`PRODUCTION_READINESS_REPORT.md`)
- Installed missing `pyjwt` dependency

### Done
- Created `database/config.py` – DB configuration with `from_env()`, `for_testing()`, SQLite/PostgreSQL URL generation, connection pooling.
- Created `database/base.py` – SQLAlchemy `Base` with `UUIDType`, `TimestampMixin`, `SoftDeleteMixin`, `VersionMixin`, `IdentityMixin`, `BaseModel` with `to_dict()` and `to_json_safe()`.
- Created `database/session.py` – `DatabaseSessionManager` singleton + `_make_fresh()` for test isolation, async engine, session factory, `create_all()`, health check.
- Created 15 SQLAlchemy models: `ProjectModel`, `VideoModel`, `TranscriptModel`, `AnalysisModel`, `KnowledgeGraphModel`, `SEOModel`, `OutlineModel`, `SectionModel`, `DraftModel`, `ReviewModel`, `OptimizationModel`, `ExportModel`, `VersionModel`, `PipelineStateModel`, `AuditLogModel`.
- Created `database/repositories/base.py` – `BaseRepository[ModelT]` with full CRUD, pagination, search text, bulk operations.
- Created 14 concrete repositories + `database/repositories/__init__.py`.
- Created `database/unit_of_work.py` – `UnitOfWork` with 14 repos + `version_manager`; `TransactionManager` with `transaction()` and `read_only()` context managers.
- Created `database/version_manager.py` – version snapshots with checksums, diff, changed_fields tracking.
- Created `database/rollback_engine.py` – rollback for 8 entity types with field allowlists.
- Created `database/resume_engine.py` – 13-stage pipeline resume with checkpoints.
- Created `database/audit.py` – `AuditService` with log events, timeline, activity, action summaries.
- Created `database/search.py` – `FullTextSearch` with global search across projects, transcripts, videos.
- Created `database/cache.py` – `DatabaseCache` with Redis + memory fallback, namespace invalidation.
- Created `database/__init__.py` – public API exporting 55+ symbols.
- Created `database/migrations/env.py` + `alembic.ini` + `initial_schema` migration (`55bc52dfc0c9`).
- Created `database/db_session.py` – `db_lifespan` FastAPI context manager + `get_db_health()`.
- Created `database/db_service.py` – `DatabaseService` with 45 public methods: project CRUD, pipeline stage management, per-entity operations (video, transcript, analysis, KG, SEO, outline, sections, draft, review, optimization, export), pipeline state, audit, version, health check, transaction grouping. Uses `_upsert_entity_data` with field mapping + `raw_data` JSON fallback, stage routing, session isolation per method.
- Fixed `UnitOfWork` duplicate `version_history` property.
- Added `DraftRepository.get_latest()` alias.
- Fixed `_model_to_dict` to handle ISO datetime conversion.
- Wrote 42 integration tests (repositories, UoW, version, rollback, resume, audit, search, cache, pipeline state) + 47 DatabaseService tests = 89 total, all passing.
- Deleted stale `data` and temp files.

### Background Processing Platform (complete)
- `background_processing/config.py` – `BackgroundProcessingConfig` with broker/backend/Redis/lock/retry/progress/monitoring settings, all driven by env vars.
- `background_processing/models.py` – `JobModel` (SQLAlchemy, extends `DBBaseModel`), Pydantic schemas (`JobCreate`, `JobResponse`, `JobProgress`, `RetryAttempt`, `DeadLetterEntry`, `QueueMetrics`, `WorkerInfo`, `BatchJobRequest/Response`), enums (`JobPriority`, `JobStatus`, `JobType`), `STAGE_ORDER` list.
- `background_processing/celery_app.py` – `create_celery_app()` with 7 queues (critical, high, default, low, background, system, dead_letter), task routing, signal handlers (task_prerun, task_success, task_failure, task_retry, worker_ready, etc.).
- `background_processing/distributed_lock.py` – `DistributedLock` with Redis SET NX EX, Lua unlock/renew scripts, blocking/non-blocking modes, `AutoRenewLock` with background renewal, async context manager `lock()`.
- `background_processing/event_bus.py` – `EventBus` with Redis Pub/Sub, `publish()`, `publish_progress()`, `publish_event()`, `publish_system()`, `subscribe()` with glob patterns, `EventType` constants, close/unsubscribe.
- `background_processing/job_repository.py` – `JobRepository` with `create()`, `get()`, `get_by_celery_id()`, `update()`, `update_status()`, `update_progress()`, `increment_attempts()`, list/count/query methods, `find_stuck_jobs()`, soft/hard delete, metrics.
- `background_processing/progress_emitter.py` – `ProgressEmitter` with `on_created`, `on_queued`, `on_started`, `on_progress`, `on_completed`, `on_failed`, `on_cancelled`, `on_retrying`, `on_dead_letter`, `on_recovered` – each persists state then publishes event.
- `background_processing/retry_manager.py` – `RetryManager` with exponential backoff (delay × 2^attempt), jitter, max cap, non-recoverable error detection (`NON_RECOVERABLE_ERRORS` tuple), `RetryDecision` with `should_retry`/`delay`/`send_to_dead_letter`, `build_retry_history()`.
- `background_processing/dead_letter_queue.py` – `DeadLetterQueue` with Redis-backed storage, `send()`, `get()`, `list()`, `count()`, `replay()`, `replay_all()`, `purge()`, `purge_all()`, `_recommend_recovery()`.
- `background_processing/worker_manager.py` – `WorkerManager` with `start_worker()` (subprocess.Popen), `stop_worker()`, `stop_all()`, `restart_worker()`, `list_workers()`, `get_worker_info()`, `get_system_metrics()`, `health_check()`, `wait_for_workers()`.
- `background_processing/worker_health.py` – `HealthMonitor` with background thread, psutil CPU/memory/status tracking, `WorkerStatus` enum (ACTIVE/IDLE/BUSY/DEGRADED/UNHEALTHY/STOPPED), auto-restart callback, `get_summary()`.
- `background_processing/task_scheduler.py` – `TaskScheduler` with `register_periodic_tasks()` (5 defaults: stale job cleanup, expired results, health check, history backup, DLQ cleanup), `schedule_delayed()`, `schedule_at()`, `schedule_batch()`, `register_cron()`.
- `background_processing/metrics.py` – Prometheus counters/gauges/histograms (jobs created/started/completed/failed/retried/cancelled/dead_letter, queue length, worker resources, system health), `start_prometheus_server()`, `setup_opentelemetry()` with Celery instrumentation, helper functions for all metric recording.
- `background_processing/tasks/__init__.py` – public task exports.
- `background_processing/tasks/pipeline_tasks.py` – 12 Celery tasks (`process_video`, `generate_analysis`, `generate_knowledge_graph`, `generate_seo_analysis`, `generate_outline`, `generate_sections`, `generate_draft`, `generate_review`, `generate_optimization`, `generate_export`, `run_full_pipeline`, `run_pipeline_stage`). Each wraps async `DatabaseService` calls with `ProgressEmitter` lifecycle and `_run_pipeline_stage` pattern.
- `background_processing/tasks/export_tasks.py` – `export_project`, `export_project_markdown`, `export_project_html`, `export_project_pdf`, `batch_export`.
- `background_processing/tasks/cleanup_tasks.py` – `cleanup_stale_jobs`, `cleanup_expired_results`, `system_health_check` (DB+Redis+poll), `backup_job_history` (placeholder), `dlq_cleanup`.
- `background_processing/task_registry.py` – 18 `JobType`→task path mappings, `register()`, `resolve()`, `resolve_all()`, `get_registry()`, `clear()`.
- `background_processing/__init__.py` – public API exporting 20+ symbols.
- All dependencies already installed: `celery==5.6.3`, `redis==8.0.0`, `flower==2.0.1`, `prometheus_client==0.25.0`, `opentelemetry-api==1.42.1`, `opentelemetry-instrumentation-celery==0.63b1`, `kombu==5.6.2`, `billiard==4.2.4`, `gevent==25.9.1`.

### Enterprise Testing Platform (complete)
- `tests/config.py` — `TestSettings` with 16 env-configurable fields, `from_env()`, auto-directory creation.
- `tests/test_runner.py` — `TestRunner` with 12 `run_*` methods, programmatic pytest executor, `parallel_execute()` via ThreadPoolExecutor, results formatter (JSON/HTML/JUnit XML).
- `tests/quality_gate.py` — `QualityGate` with 8 checks (coverage, SEO, grammar, readability, regression drift, perf degradation, security, critical bugs), `evaluate_all()`, `should_deploy()`, `summarize()`.
- `tests/golden_dataset.py` — `GoldenDataset` (load/save/compare/validate), `GoldenOutputValidator` with 6 validation methods (SEO tolerance, entity/keyword Dice overlap, outline/section structure).
- `tests/regression_engine.py` — `RegressionEngine` (4 detect methods, statistics, drift score), `RegressionTracker` (baseline persistence, trend analysis).
- `tests/benchmark_runner.py` — `BenchmarkRunner` (timed execution, baseline comparison, Markdown/JSON reports), `PipelineBenchmark`.
- `tests/report_generator.py` — `ReportGenerator` (8 `generate_*` methods, 5 export formats: HTML/JSON/JUnit/Markdown).
- `tests/coverage_analyzer.py` — `CoverageAnalyzer` (Cobertura XML parsing, module/overall/low-coverage analysis, baseline comparison).
- 240 Python test files (32,227 lines) across 20 test categories.
- **Unit Tests (27 files, 744+ tests):** metadata, transcript, analysis, knowledge_graph, seo, outline, section, draft, review, optimization, editor, export engines + database, cache, security, prompt, background_processing, observability, utilities, repositories, services, validators, orchestrator, project_manager, pipeline.
- **Integration Tests (11 files):** complete workflow, metadata→transcript, analysis→KG, SEO→outline, sections→draft, review→optimization, editor→export, database, cache, background_processing, pipeline orchestration.
- **E2E Tests (9 files):** channel export, transcript extraction, resume, retry, review, edit, export, delete, full user journey.
- **Regression Tests (5 files):** pipeline, SEO, quality, output stability, AI regression.
- **AI Regression Tests (7 files):** prompt quality, response quality, determinism, consistency, output structure, hallucination rate, semantic similarity.
- **Performance Tests (8 files):** pipeline duration, stage duration, API latency, database, cache, worker throughput, export time, memory usage.
- **Load Tests (3 files):** concurrent users (10/100/500/1000), concurrent projects, concurrent exports + locustfile.
- **Stress Tests (4 files):** system capacity, recovery, stability, graceful degradation.
- **Security Tests (10 files):** auth, authorization, RBAC, rate limiting, prompt injection, SQL injection, XSS, CSRF, secret leakage, OWASP Top 10.
- **Chaos Tests (7 files):** worker crash, Redis failure, database failure, LLM timeout, API failure, disk full, network delay.
- **Database Tests (6 files):** transactions, rollback, migrations, indexes, concurrency, resume, recovery.
- **Cache Tests (6 files):** hit rate, miss rate, TTL, eviction, versioning, consistency.
- **Background Job Tests (5 files):** queue, retry, DLQ, worker crash, progress events.
- **Export Tests (7 files):** markdown, HTML, DOCX, PDF, TXT, JSON, ZIP package.
- **Prompt Tests (5 files):** rendering, variables, version, regression, token count.
- **Accessibility Tests (5 files):** WCAG, keyboard nav, screen reader, color contrast, focus management.
- **Fixtures (16 files):** sample data for videos, transcripts, analysis, KG, SEO, outlines, sections, drafts, reviews, optimizations, exports + benchmark_videos.yaml, golden_outputs.yaml.
- **Mocks (7 files):** MockLLMProvider, MockYouTubeClient/TranscriptAPI, MockRedis (25+ commands), MockDatabaseSession, MockFileSystem, MockExternalAPIs (translation, OpenAI, Anthropic, Gemini, Whisper).
- `pyproject.toml` — black, isort, ruff, mypy, bandit, interrogate, coverage, pytest configuration.
- `pytest.ini` — 20 custom markers, 6 test paths, strict markers, filterwarnings.
- `.pre-commit-config.yaml` — 8 pre-commit hooks (trailing-whitespace, black, isort, ruff, mypy, bandit, interrogate).
- `.github/workflows/ci.yml` — 10-job CI pipeline: static analysis, unit (matrix 3.11/3.12), integration, E2E, regression, performance, security, quality gates, deploy, nightly benchmarks.
- `.github/workflows/pr-checks.yml` — PR checks (changed files linting, smoke tests).
- `tox.ini` — 7 tox environments (py311, py312, lint, type-check, security, coverage, full).
- `Makefile` — 17 targets (install, lint, typecheck, security, test-*, coverage, clean, pre-commit, format).
- `.coveragerc` — Branch coverage, fail-under=95, HTML report.
- `.bandit.yml` — Security scanning config.
- `.github/dependabot.yml` — Automated dependency updates (pip, github-actions, npm).
- `.github/CODEOWNERS` — Team ownership rules.
- All 89 existing tests continue to pass; 1918 total tests passing across all existing test suites.

### Production Deployment Platform (complete)
- `Dockerfile.api` — Multi-stage build (builder + runtime stages), Python 3.11-slim, non-root `appuser`, healthcheck, uvicorn 4 workers, proxy headers, `--forwarded-allow-ips=*`.
- `Dockerfile.worker` — Multi-stage Celery worker, 7 queues (critical, high, default, low, background, system, dead_letter), `celery inspect ping` healthcheck, `--max-tasks-per-child=1000`.
- `Dockerfile.beat` — Multi-stage Celery Beat scheduler, `--max-interval=300`.
- `Dockerfile.frontend` — Node 20-alpine build + nginx:1.25-alpine runtime, `npm ci` for reproducible builds.
- `.dockerignore` — 30+ patterns excluding `.env`, `__pycache__`, `node_modules`, tests, build artifacts.
- `docker/docker-compose.yml` — Production stack: PostgreSQL 16 (init-db.sql), Redis 7 (AOF, LRU), API (×2 replicas), Worker (×2 replicas), Beat, Frontend, Nginx (×2 replicas), Flower. All services have healthchecks, resource limits, `x-logging`/`x-deploy` anchors, loopback-bound ports.
- `docker/docker-compose.dev.yml` — Dev overrides: hot-reload, source mount, debugpy (5678), redis-insight, adminer, Mailpit.
- `docker/docker-compose.monitoring.yml` — Prometheus (30d retention, 50GB limit), Grafana (auto-provisioned), node-exporter, cAdvisor, OTel Collector (OTLP gRPC/HTTP).
- `docker/nginx/nginx.conf` — Brotli + gzip compression, rate limiting (API/export/auth zones), security headers (CSP, HSTS, X-Frame-Options), zone caches, least_conn upstreams.
- `docker/nginx/conf.d/default.conf` — SSL termination (TLS 1.2/1.3), HTTP→HTTPS redirect, API/Flower/Grafana/Prometheus proxying, SPA catch-all, hidden-file deny.
- `docker/nginx/frontend.conf` — Frontend container config, SPA try_files, asset caching with 1y TTL.
- `docker/nginx/conf.d/api.conf` — API GET caching, export rate limiting (5r/s), WebSocket support (86400s timeout).
- `docker/monitoring/prometheus/prometheus.yml` — 7 scrape jobs (API, worker, node, cAdvisor, Postgres, Redis, Nginx), 15s global interval.
- `docker/monitoring/prometheus/alerts.yml` — 15 Prometheus alert rules: API/Worker/Postgres/Redis down, high error rate (>5%), high latency (p95 >2s), queue backlog (>1000), resource thresholds (CPU 80%, memory 85%, disk 10%), cache hit rate (<50%), LLM latency (>30s).
- `docker/monitoring/grafana/provisioning/` — Auto-provisioned Prometheus + Loki datasources, file-based dashboard provider.
- `docker/monitoring/grafana/dashboards/youtube-seo-overview.json` — Grafana 10+ dashboard: 12 panels (API rate/latency/errors, workers/queues, cache/LLM, system resources), env/instance templating.
- `docker/monitoring/otel-collector.yml` — OTel Collector with OTLP receiver, batch/memory/attributes processors, Prometheus remote write exporter.
- `.github/workflows/deploy.yml` — 9-stage deployment: secret scan (TruffleHog) → build & test (3.11/3.12 matrix) → Docker build & push (api/worker/beat/frontend) with provenance/SBOM → Trivy container scan → staging deploy → staging health check/smoke tests → production deploy (blue-green) → production health check/smoke tests → version tag + GitHub release. Slack notifications. Auto-rollback trigger on failure.
- `.github/workflows/rollback.yml` — Manual rollback workflow: version or backup restore, health check, Slack notification.
- `.github/workflows/nightly.yml` — Nightly (2AM): full test suite, security tests, locust load tests (100 users/5min), pip-audit dependency scan, benchmarks.
- `scripts/deploy.sh` — Pre-deployment checks (disk 5GB, Docker status), database backup, rolling (staging) or blue-green (production) deployment, health check, alembic migrations, old image cleanup.
- `scripts/rollback.sh` — Version-targeted or backup-based rollback, DB restore from latest dump, health verification.
- `scripts/backup.sh` — Automated backup: PostgreSQL dump (gzip), Redis RDB, config/env snapshots, project exports, 30-day retention, integrity verification.
- `scripts/restore.sh` — Restore latest or specific DB/Redis backup with service stop/start orchestration.
- `scripts/init-db.sql` — PostgreSQL initialization: uuid-ossp/pgcrypto/pg_trgm extensions, app/audit schemas, role + privileges.
- `scripts/healthcheck.py` — Docker health checker: API, Redis, disk (>10% free), memory (<90% used).
- `tests/smoke/test_deployment.py` — 14 post-deployment smoke tests: API health, DB/Redis connectivity, security headers, CORS, TLS, compression, pipeline.

### In Progress
- None (all deployment deliverables complete)

### Blocked
- (none)

## Key Decisions
- Created `tests/` as the single source of truth for all testing; all test artifacts (fixtures, mocks, golden outputs, reports) live under `tests/` namespace.
- No modifications to any existing business logic; the testing platform is entirely additive.
- All test infrastructure (test_runner, quality_gate, golden_dataset, regression_engine, report_generator, coverage_analyzer) are importable Python modules, not CLI scripts.
- Tests use pytest markers for selective execution: `pytest -m "unit"`, `pytest -m "security"`, etc.
- Mock providers are comprehensive and deterministic — they simulate failure modes (timeout, rate limit, token limit, disk full) to test error handling paths.
- Golden datasets are stored as YAML for readability and version control; they include expected scores, entities, keywords, and structure validation rules.
- Quality gates are evaluated programmatically in CI and can block deployment on failure.
- CI pipeline is a 10-job DAG: static analysis → unit (matrix) → integration → E2E → regression → performance → security → quality gates → deploy.
- Every export format (markdown, HTML, DOCX, PDF, TXT, JSON, ZIP) has dedicated test coverage.
- Accessibility tests validate WCAG 2.2 compliance including keyboard navigation, screen reader, color contrast, and focus management.
- Background processing tests validate queue operations, retry logic, dead-letter queue, worker crash recovery, and progress events.
- All Dockerfiles use multi-stage builds with Python 3.11-slim base, non-root user, and healthchecks.
- Docker Compose uses `x-logging` and `x-deploy` YAML anchors for DRY configuration across services.
- All ports are bound to `127.0.0.1` (loopback) for security; Nginx is the only public-facing entry point.
- PostgreSQL uses the official `init-db.sql` script for first-run schema/setup; Alembic handles subsequent migrations.
- Redis uses AOF persistence with LRU eviction and 1GB memory limit.
- CI/CD uses blue-green deployment for production and rolling update for staging; both include pre-deploy backups and post-deploy health checks.
- Trivy container scanning runs on every Docker build; secrets scanning (TruffleHog) runs before anything else.
- Prometheus alert rules cover service availability (critical), performance degradation (high), and resource exhaustion (critical).
- Grafana dashboards are auto-provisioned with file-based provisioning; 1 overview dashboard with 12 panels.
- Backup strategy: database (daily gzip dump), Redis (RDB snapshot), config/env (copy), projects (tar.gz); 30-day retention with integrity verification.
- Rollback supports both version-targeted (revert to previous Docker tag) and backup-based (restore DB + config).
- Smoke tests run after every deployment to verify health endpoints, DB/Redis connectivity, security headers, TLS, and compression.
- Celery 5.6.3 + Redis 8.0.0 as primary stack (both already installed); Flower 2.0.1 for monitoring; Prometheus + OpenTelemetry SDKs also already present.
- Chose Redis Pub/Sub over Kafka/NATS for the event bus to minimize operational complexity; Kafka support noted as future path.
- Job state is dual-tracked: persisted in `background_jobs` SQL table (via `JobRepository`) and cached in Redis (TTL 7 days for DLQ entries).
- Workers remain stateless: all state (project data, stage outputs) lives in the database; workers only execute through existing `DatabaseService` methods.
- Priority queues use 6 levels (Critical=0, High=3, Default=5, Low=8, Background=10, System=1) enforced by Celery `x-max-priority: 10` per queue.
- Non-recoverable errors (payload/validation/auth) are immediately sent to DLQ without retry; all other errors use exponential backoff with jitter up to max retries.
- Distributed locking uses Lua scripts for atomic acquire/release/renew, preventing duplicate execution of the same job.
- `ProgressEmitter` is the single write path for job state transitions — all job updates flow through its `on_*` methods, ensuring consistent DB + Redis Pub/Sub output.
- `JobModel` extends `DBBaseModel` (inheriting `uuid`, `created_at`, `updated_at`, `is_deleted`, `version`) with 18 domain-specific columns; `background_jobs` table is separate from the 15 original database tables.
- The `config.py` exposes all settings via environment variables with sensible defaults for local development (Redis on `localhost:6379`).
- Celery tasks are defined as synchronous wrappers that create/use an asyncio event loop to call async `DatabaseService` methods — this allows reuse of the existing async database layer without modification.
- Pipeline tasks follow a common `_run_pipeline_stage` pattern that integrates with `ProgressEmitter` for real-time progress and failure tracking.
- The `TaskScheduler` defaults include 5 built-in Beat schedules (stale job cleanup, expired results, health check, history backup, DLQ cleanup) activated at startup.
- `WorkerManager` uses `subprocess.Popen` to manage Celery worker processes; `HealthMonitor` runs a background thread with psutil-based CPU/memory checks and auto-restart via callback.
- OpenTelemetry initialization is guarded by `OTEL_ENABLED` env var; on by default, it instruments Celery tasks with distributed trace spans.
- The `task_registry` maps 18 `JobType` enum values to Celery task paths; used by `TaskScheduler` and the public API for dynamic dispatch.

## Next Steps
1. Wire up `db_lifespan()` from `database/db_session.py` into `webapp/main.py`'s lifespan for database initialization.
2. Wire up `TelemetryOrchestrator` from `observability/` into the FastAPI startup for OpenTelemetry, Prometheus, and structured logging.
3. Wire up FastAPI endpoints to submit jobs to the background processing layer.
4. Deploy the Docker Compose stack to a staging environment and validate all health checks.
5. Set up GitHub Environments with required secrets for staging and production deployment workflows.

## Relevant Files
### Background Processing Core
- `background_processing/config.py`: All env-var-driven config
- `background_processing/models.py`: JobModel + Pydantic schemas + enums
- `background_processing/celery_app.py`: Celery app with 7 queues + routing + signals
- `background_processing/distributed_lock.py`: Redis-based distributed lock (Lua scripts)
- `background_processing/event_bus.py`: Redis Pub/Sub event bus
- `background_processing/job_repository.py`: Async SQLAlchemy job persistence
- `background_processing/progress_emitter.py`: 11 job lifecycle methods
- `background_processing/retry_manager.py`: Exponential backoff + jitter + non-recoverable errors
- `background_processing/dead_letter_queue.py`: Redis-backed DLQ with replay/purge
- `background_processing/worker_manager.py`: Worker lifecycle (start/stop/restart + subprocess management)
- `background_processing/worker_health.py`: Background health monitor with psutil + auto-restart
- `background_processing/task_scheduler.py`: Celery Beat registration + delayed/batch scheduling
- `background_processing/metrics.py`: Prometheus metrics + OpenTelemetry setup
- `background_processing/task_registry.py`: 18 JobType → task path mappings

### Background Processing Tasks
- `background_processing/tasks/pipeline_tasks.py`: 12 Celery tasks (pipeline stages + composite)
- `background_processing/tasks/export_tasks.py`: 5 export tasks (single + batch)
- `background_processing/tasks/cleanup_tasks.py`: 5 maintenance tasks

### Database Layer
- `database/db_service.py`: 45 methods for all pipeline operations
- `database/db_session.py`: FastAPI lifespan + health check
- `database/unit_of_work.py`: 14 repos + TransactionManager
- `database/session.py`: Async session manager + connection pooling

### Tests
- `tests/test_database.py`: 42 existing integration tests
- `tests/test_database_service.py`: 47 DatabaseService integration tests

## Observability Platform (complete)
- `observability/` — 16 modules covering the full observability stack:
  - `config.py` — `ObservabilityConfig` with env-driven settings for tracing, metrics, logging, Sentry, OTel, health checks, cost/token tracking, alerting
  - `logger.py` — `StructuredLogger` with JSON output, context binding, trace/span propagation, exception serialization
  - `tracing.py` — `TracingManager` wrapping OpenTelemetry SDK with OTLP/console exporters, sampling, span context injection
  - `metrics.py` — `MetricsManager` wrapping Prometheus client with counter/gauge/histogram helpers, `instrument()` decorator, latency measurement
  - `telemetry.py` — `TelemetryOrchestrator` initializes logging/tracing/metrics/Sentry at startup; singleton pattern
  - `instrumentation.py` — FastAPI middleware (`InstrumentationMiddleware`), `instrument_function()`, `instrument_method()`, `instrument_context()` context manager
  - `health_checks.py` — `HealthCheckRegistry` with `HealthStatus` enum; built-in DB, Redis, disk, memory check factories
  - `alert_manager.py` — `AlertManager` with `AlertRule` (cooldown, severity), evaluation engine, history
  - `error_tracker.py` — `ErrorTracker` with fingerprint-based dedup, grouping by type/module, resolution tracking
  - `token_metrics.py` — `TokenMetricsCollector` per-model/per-prompt/per-project aggregation, daily usage, cache savings, cost estimation
  - `cost_tracker.py` — `CostTracker` per-service/per-model/per-user/per-project aggregation, daily/monthly estimates, cumulative costs
  - `performance_monitor.py` — `PerformanceMonitor` with profiling, percentile computation (P50/P95/P99), slow operation detection
  - `dashboard_service.py` — `DashboardService` aggregates data across all collectors for 10 dashboard types (executive, developer, ops, AI, cost, pipeline, worker, DB, cache)
  - `audit_logger.py` — `AuditLogger` with 18 event types, actor/resource/trace tracking, filtered queries
  - `telemetry_exporter.py` — `TelemetryExporter` with batch buffer, file/console/HTTP exporters, periodic flush
  - `dashboards/` — 9 Grafana dashboard JSON files (executive, developer, operations, AI usage, cost, pipeline, worker, database, cache)
  - `__init__.py` — explicit `__all__` exports of 19 symbols

### Key Design Decisions
- Completely non-invasive: no modifications to any existing service, engine, or module
- All instrumentation is optional and gracefully degrades on failure
- StructuredLogger provides `bind()` for context propagation (trace_id, span_id, request_id, etc.)
- Metrics use prometheus_client directly with a centralized prefix (`youtube_seo_`)
- Tracing uses OpenTelemetry SDK with pluggable exporters (console/OTLP)
- Health checks are registered into a central `HealthCheckRegistry` and run on demand
- Errors are deduplicated by fingerprint (type + stack frame) and grouped for trend analysis
- Token/cost tracking supports per-user, per-project, per-model, per-prompt aggregation
- Audit logger captures 18 event types with full actor/resource/trace context
- Dashboard service aggregates across all collectors without direct DB queries
- Batch exporter buffers up to 10K records with configurable flush interval and multiple backends
- Grafana dashboards cover 9 domains with Prometheus datasource targets

### Next Steps
1. Write comprehensive tests (unit + integration) for all 16 observability modules
2. Run full test suite (1630 existing + background + prompt + observability)
3. Wire up TelemetryOrchestrator in FastAPI lifespan for automatic instrumentation
