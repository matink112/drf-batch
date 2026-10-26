# Developer shortcuts for drf-batch. Everything runs through uv.
# `make` or `make help` lists the targets.

UV        ?= uv
RUN       := $(UV) run --frozen
DOCS_RUN  := NO_MKDOCS_2_WARNING=1 $(RUN) --group docs
DOCS_ADDR ?= 127.0.0.1:8000
DIST      ?= dist
CZ        := $(RUN) cz

# Release knobs: `make bump INCREMENT=MINOR`, `make bump PRERELEASE=rc`.
INCREMENT  ?=
PRERELEASE ?=
REV_RANGE  ?= HEAD
BUMP_ARGS  := $(if $(INCREMENT),--increment $(INCREMENT)) \
	$(if $(PRERELEASE),--prerelease $(PRERELEASE))

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test test-fast test-postgres \
	matrix quickstart check docs docs-serve build check-dist clean \
	hooks commit commit-check version changelog bump-dry bump release \
	push-release

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install the dev and docs environment (uv sync)
	$(UV) sync --frozen --group docs

lint: ## Ruff lint + format check
	$(RUN) ruff check
	$(RUN) ruff format --check

format: ## Auto-fix lint issues and format the code
	$(RUN) ruff check --fix
	$(RUN) ruff format

typecheck: ## mypy (strict, with django-stubs)
	$(RUN) mypy

test: ## Full test suite with the coverage gate
	$(RUN) pytest

test-fast: ## Tests without coverage, stop at the first failure
	$(RUN) pytest -q -x --no-cov

test-postgres: ## Tests against PostgreSQL (uses the PG* env vars)
	DRF_BATCH_TEST_DB=postgres $(RUN) pytest

matrix: ## Oldest and newest Django x DRF cells, like CI
	$(RUN) nox -s matrix

quickstart: ## Run the README quickstart against the built wheel
	$(RUN) python scripts/run_quickstart.py

check: lint typecheck test docs ## Everything CI checks (except matrix/postgres)

docs: ## Build the docs site strictly into site/
	$(DOCS_RUN) mkdocs build --strict

docs-serve: ## Serve the docs with live reload (DOCS_ADDR=host:port)
	$(DOCS_RUN) mkdocs serve --dev-addr $(DOCS_ADDR)

build: clean-dist ## Build the sdist and wheel into dist/
	$(UV) build --out-dir $(DIST)

check-dist: build ## Build, then validate the dists and list the sdist contents
	$(UV)x twine check $(DIST)/*
	tar tzf $(DIST)/*.tar.gz | grep -v '/tests/'

# --- Versioning (commitizen) -------------------------------------------------

hooks: ## Install the git hook that checks commit messages
	$(RUN) pre-commit install

commit: ## Write a Conventional Commit interactively
	$(CZ) commit

commit-check: ## Check commit messages (REV_RANGE=origin/main..HEAD)
	$(CZ) check --rev-range $(REV_RANGE)

version: ## Show the current version and the one the next bump would make
	@echo "current: $$($(CZ) version --project)"
	@echo "next:    $$($(CZ) bump --get-next --yes $(BUMP_ARGS) 2>/dev/null || echo 'none (no feat/fix commits since the last tag)')"

changelog: ## Preview the changelog entry for the next release
	$(CZ) changelog --dry-run --unreleased-version "v$$($(CZ) bump --get-next --yes $(BUMP_ARGS))"

bump-dry: ## Show what `make bump` would do, without changing anything
	$(CZ) bump --dry-run --yes $(BUMP_ARGS)

bump: ## Bump the version from the commits, update CHANGELOG, commit and tag
	@test -z "$$(git status --porcelain)" || { echo "working tree is not clean"; exit 1; }
	@test "$$(git branch --show-current)" = main || { echo "releases are cut from main"; exit 1; }
	$(CZ) bump --yes $(BUMP_ARGS)

release: check bump ## Run every check, then bump (push with `make push-release`)
	@echo "Tagged v$$($(CZ) version --project). Publish with: make push-release"

push-release: ## Push main and the release tag; the tag publishes to PyPI
	git push --follow-tags origin main

clean-dist:
	rm -rf $(DIST)

clean: clean-dist ## Remove build, docs and cache artifacts
	rm -rf site build .pytest_cache .mypy_cache .ruff_cache .hypothesis .coverage
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +
