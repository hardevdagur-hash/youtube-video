.PHONY: help install lint typecheck security test test-unit test-integration test-e2e test-regression test-performance test-security test-all coverage clean pre-commit start start-dev

help:
	@echo 'Commands:'
	@echo '  make install       - Install all dependencies'
	@echo '  make lint          - Run black, ruff, isort checks'
	@echo '  make typecheck     - Run mypy'
	@echo '  make security      - Run bandit security scan'
	@echo '  make test          - Run quick tests (unit + integration)'
	@echo '  make test-unit     - Run unit tests with coverage'
	@echo '  make test-integration - Run integration tests'
	@echo '  make test-e2e      - Run end-to-end tests'
	@echo '  make test-regression - Run regression tests'
	@echo '  make test-performance - Run performance tests'
	@echo '  make test-security - Run security tests'
	@echo '  make test-all      - Run full test suite'
	@echo '  make coverage      - Run tests with coverage report'
	@echo '  make clean         - Clean build artifacts'
	@echo '  make start         - Start backend server (uvicorn)'
	@echo '  make start-dev     - Start backend + frontend for local development'
	@echo '  make pre-commit    - Run pre-commit hooks on all files'

install:
	pip install -U pip
	pip install -r requirements-dev.txt
	pip install pytest-asyncio pytest-cov pytest-timeout httpx psutil locust playwright pre-commit tox
	pre-commit install

lint:
	black --check --line-length=100 .
	ruff check --no-fix --line-length=100 .
	isort --check-only --profile=black --line-length=100 .

typecheck:
	mypy --ignore-missing-imports .

security:
	bandit --skip=B101 --exclude=tests,venv -r .

test:
	pytest tests/unit/ tests/integration/ -v --timeout=120 -m "unit or integration" -x

test-unit:
	pytest tests/unit/ -v --timeout=60 -m "unit" --cov=. --cov-report=term --cov-report=html

test-integration:
	pytest tests/integration/ -v --timeout=120 -m "integration"

test-e2e:
	pytest tests/e2e/ -v --timeout=300 --run-e2e

test-regression:
	pytest tests/regression/ tests/ai_regression/ -v --timeout=120 -m "regression or ai_regression"

test-performance:
	pytest tests/performance/ -v --timeout=300 --run-performance

test-security:
	pytest tests/security/ -v --timeout=120 --run-security

test-all:
	pytest tests/ -v --timeout=300 --ignore=tests/load --ignore=tests/stress --ignore=tests/chaos

coverage:
	coverage run -m pytest tests/unit/ tests/integration/ -v --timeout=120 -m "unit or integration"
	coverage report --fail-under=95
	coverage html
	@echo "Coverage report: htmlcov/index.html"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .coverage -delete 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	find . -type f -name '.coverage' -delete 2>/dev/null || true
	rm -rf tests/reports/* 2>/dev/null || true

pre-commit:
	pre-commit run --all-files

format:
	black --line-length=100 .
	isort --profile=black --line-length=100 .
	ruff check --fix --line-length=100 .

start:
	uvicorn webapp.main:app --host 127.0.0.1 --port 8000 --reload

start-dev:
	@echo "Starting backend server..."
	@uvicorn webapp.main:app --host 127.0.0.1 --port 8000 --reload &
	@sleep 3
	@echo "Starting frontend dev server..."
	@cd frontend && npx vite --host
	@trap 'kill %1' EXIT
