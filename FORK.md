# Fork notes

This is a fork of [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp)
that confines the server to a fixed set of chats. It is meant to track upstream
closely, so the fork is deliberately tiny and additive.

## What this fork changes

| File | Kind | Why |
| --- | --- | --- |
| `telegram_mcp/chat_scope.py` | new | The entire restriction. |
| `tests/test_chat_scope.py` | new | Gate tests + the merge canary. |
| `tests/scope_reviewed_tools.txt` | new | Snapshot of the 128 upstream tools we have classified. |
| `scripts/discover_chats.py` | new | Reads a Telegram folder's chats to fill the allowlist. |
| `Makefile`, `FORK.md` | new | Fork workflow. |
| `telegram_mcp/runner.py` | **2 lines** | One import, one `_chat_scope.install()` in `main()`. |
| `.env.example` | +1 block | Documents the two new variables. |
| `pyproject.toml` | +1 line | Adds `chat_scope` to the coverage source list. |

Only the last three touch upstream files, and each is an insertion rather than a
rewrite, so `git merge upstream/main` almost never conflicts.

## Setup

1. `cp .env.example .env` and fill in `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`
   plus a session (`uv run session_string_generator.py`).
2. Put the chats you want into a Telegram folder, then read it back:
   ```
   make folders                              # what folders exist
   make discover-chats ARGS="Пульс"          # that folder's chats
   make discover-chats ARGS="Пульс --env"    # ready-to-paste line
   ```
   A folder beats matching chat titles because keeping the allowlist current
   later is "add the chat to the folder, re-run this". `--by-title` is still
   there as a fallback when there is no folder.

   The script never loads `chat_scope`, so it can still see chats you have not
   allowed — that is the point.
3. Put the result in `.env` as `TELEGRAM_ALLOWED_CHATS=...`.
4. Start the server. It prints the active scope on stderr; if the allowlist is
   empty it prints a warning saying scoping is **off**.

## Keeping up with upstream

```
make sync-upstream     # fetch + merge upstream/main + run the tests
make fork-diff         # everything this fork adds on top of upstream
```

`make sync-upstream` reports whether the merge touched any of the files the
scope hooks into. If it did, re-read the chokepoint list below.

### The chokepoints

`chat_scope.install()` patches five things. Each is named at the top of
`telegram_mcp/chat_scope.py`; if an upstream refactor renames one, the patch
stops applying:

| Chokepoint | Covers |
| --- | --- |
| `runtime._resolve` | Every tool that names a peer (~104 call sites). |
| `TelegramClient.get_dialogs` | Dialog listings, which bypass the above. |
| `ToolManager.call_tool` | The account-wide tools in `chat_scope.GLOBAL_TOOLS`. |
| `tools.events._on_new_incoming` | The incoming-message feed. |
| `runtime.log_and_format_error` | Keeps the refusal readable instead of `An error occurred (code: …)`. |

The gates are attribute patches, so a rename fails **open**, not closed — the
tests are what catch it. `test_chat_scope.py` exercises all five against stubs,
and `test_every_registered_tool_has_been_reviewed_for_scope` fails whenever
upstream adds or renames a tool. When it does, decide whether the new tool is
confined to one chat (gates A/B already handle it) or reaches the whole account
(add it to `chat_scope.GLOBAL_TOOLS`), then run `make refresh-tool-baseline`.

## Known limits

- **Scoping is per-chat, not per-topic.** A forum supergroup is allowed or
  denied whole; individual topics are not separated.
- **Members of an allowed chat are not themselves allowed.** Looking up a
  sender's full profile (`get_full_user`) is refused unless that user is in the
  allowlist. Message listings still carry sender names.
- **The Telegram session keeps full account authority.** This is a guardrail on
  what the model can ask for, not a reduction of the session's own rights.
  Anything that bypasses the five chokepoints — a raw MTProto call added
  upstream, for instance — is not covered.
- `scripts/discover_chats.py` is intentionally unscoped; do not wire it into the
  MCP server.
- A folder that includes whole *categories* (all contacts, all groups, …) rather
  than named chats cannot be enumerated from the folder alone; the script says so
  and lists only the chats explicitly added to it.
