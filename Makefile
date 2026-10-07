.PHONY: help install lint lint-fix test test-security frontend-check check dev-api dev-web deploy backup clean

help:
	@echo 'Development'
	@echo '  make install         Install backend (dev) and frontend dependencies'
	@echo '  make dev-api         Run the API with auto-reload on 127.0.0.1:8000 (development only)'
	@echo '  make dev-web         Run the Vite dev server on 127.0.0.1:5173 (proxies /api)'
	@echo '  make check           Lint + backend tests + frontend typecheck/tests/build'
	@echo '  make lint            Read-only lint (never modifies files)'
	@echo '  make lint-fix        Apply ruff fixes (explicit, review the diff afterwards)'
	@echo 'Production (on the server)'
	@echo '  make deploy          scripts/deploy.sh'
	@echo '  make backup          scripts/backup.sh'

install:
	pip install -r requirements-dev.txt
	npm --prefix frontend ci

lint:
	ruff check . --no-fix

lint-fix:
	ruff check . --fix

test:
	pytest -o log_cli=false -q

test-security:
	pytest -o log_cli=false -q -m security

frontend-check:
	cd frontend && npx tsc -b && npm test && npm run build

check: lint test frontend-check

dev-api:
	uvicorn webapp.main:app --host 127.0.0.1 --port 8000 --reload

dev-web:
	npm --prefix frontend run dev

deploy:
	scripts/deploy.sh

backup:
	scripts/backup.sh

clean:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	rm -rf frontend/dist
