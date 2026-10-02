.DEFAULT_GOAL := help
.PHONY: help install update uninstall check version release test lint

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Build from this checkout and install mh/muxherd as a standalone uv tool (not linked to the repo)
	uv tool install --reinstall .
	@$(MAKE) --no-print-directory check

update: ## git pull, then reinstall
	git pull --ff-only
	@$(MAKE) --no-print-directory install

uninstall: ## Remove the installed tool
	uv tool uninstall muxherd

check: ## Show which mh is on PATH and its version
	@command -v mh && mh --version

test: ## Run the test suite (uses a private tmux server, never your real one)
	uv run pytest

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

version: ## Show the version this checkout would build as
	@git describe --tags --dirty --always

release: ## Tag and publish a release: make release VERSION=x.y.z
	@echo "$(VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "usage: make release VERSION=x.y.z (semver)"; exit 1; }
	@git diff --quiet && git diff --cached --quiet || { echo "uncommitted changes; commit or stash first"; exit 1; }
	@! git rev-parse -q --verify "refs/tags/v$(VERSION)" >/dev/null || { echo "tag v$(VERSION) already exists"; exit 1; }
	git push
	git tag -a "v$(VERSION)" -m "muxherd $(VERSION)"
	git push origin "v$(VERSION)"
	gh release create "v$(VERSION)" --generate-notes --title "muxherd $(VERSION)"
