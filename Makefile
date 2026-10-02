.DEFAULT_GOAL := help
.PHONY: help install update uninstall check

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
