# Fork-local helpers (see FORK.md). Upstream ships no Makefile, so this file
# never conflicts on `git merge upstream/main`.

UPSTREAM_BRANCH ?= main

.PHONY: help
help:
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t22

.PHONY: sync-upstream
sync-upstream: ## Merge the latest upstream/main into this branch, then re-check the scope
	@git diff --quiet || { echo "Working tree is dirty; commit or stash first."; exit 1; }
	git fetch upstream
	git merge --no-edit upstream/$(UPSTREAM_BRANCH)
	$(MAKE) test
	@echo
	@echo "Merged. Review FORK.md's chokepoint list if any of these upstream files changed:"
	@git diff --name-only ORIG_HEAD..HEAD -- telegram_mcp/runtime.py telegram_mcp/runner.py telegram_mcp/tools/events.py || true

.PHONY: fork-diff
fork-diff: ## Show every change this fork makes on top of upstream
	@git fetch -q upstream
	@git diff --stat upstream/$(UPSTREAM_BRANCH)...HEAD

.PHONY: test
test: ## Run the test suite (includes the scope gates and the merge canary)
	uv run pytest -q

.PHONY: folders
folders: ## List this account's Telegram folders
	uv run python scripts/discover_chats.py

.PHONY: discover-chats
discover-chats: ## Chats of a folder, to fill TELEGRAM_ALLOWED_CHATS (ARGS="Пульс --env")
	uv run python scripts/discover_chats.py $(ARGS)

.PHONY: refresh-tool-baseline
refresh-tool-baseline: ## Accept the current tool list AFTER classifying new tools for scope
	uv run --env-file .env.example python -c "import telegram_mcp.tools; from telegram_mcp.runtime import mcp; \
	  names=sorted(t.name for t in mcp._tool_manager.list_tools()); \
	  open('tests/scope_reviewed_tools.txt','w').write('\n'.join(names)+'\n'); \
	  print(len(names),'tools recorded')"

.PHONY: fmt
fmt: ## Format with the project's black config
	uv run black .
