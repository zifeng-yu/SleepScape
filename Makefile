.PHONY: install run doctor check format browser-install browser-check clean clean-dev

install:
	uv sync --locked

run:
	uv run --locked sleepscape serve

doctor:
	uv run --locked sleepscape doctor

check:
	uv run --locked ruff check .
	uv run --locked ruff format --check .
	uv run --locked pytest

format:
	uv run --locked ruff format .

browser-install:
	npm ci --ignore-scripts --no-audit --no-fund
	PLAYWRIGHT_BROWSERS_PATH=.browser-cache npx playwright install chromium

browser-check:
	PLAYWRIGHT_BROWSERS_PATH=.browser-cache npm run test:browser

clean:
	python3 -B tools/clean_workspace.py

clean-dev:
	python3 -B tools/clean_workspace.py --include-dev-deps
