"""Fork-local chat scoping: confine every MCP tool to an allowlist of chats.

WHY THIS FILE EXISTS
--------------------
Upstream telegram-mcp hands the model the full authority of a Telegram account:
128 tools, every dialog, every contact. This fork runs the server against a
personal account but only wants it to see a couple of work chats. Rather than
editing the ~40 upstream modules that touch a peer -- which would conflict on
every `git merge upstream/main` -- the whole restriction lives here and is
installed by monkey-patching a handful of chokepoints from one call in
runner.main(). Upstream can refactor its tools freely; as long as the five
chokepoints below keep their names, merges stay clean.

THE CHOKEPOINTS
---------------
A. runtime._resolve       -- every tool reaches a peer through resolve_entity()/
                             resolve_input_entity(), both of which look `_resolve`
                             up in runtime's globals at call time. Patching it
                             therefore covers all ~104 call sites at once,
                             including the `from runtime import *` re-exports.
B. TelegramClient.get_dialogs -- the dialog listings (get_chats, list_chats,
                             contact scans) never go through (A). Filtering the
                             returned list leaves Telethon's entity cache warm,
                             which (A)'s retry path depends on.
C. ToolManager.call_tool  -- tools that reach past any single chat (global
                             search, the contact book, folders, chat creation)
                             cannot be narrowed by (A) or (B); they are handled
                             by name here.
D. events._on_new_incoming -- the NewMessage handler is registered by object at
                             import time, so it is swapped for a scoped wrapper.
E. runtime.log_and_format_error -- tools funnel exceptions through it and it
                             flattens unknown ones into "An error occurred
                             (code: ...)". Without this patch the model would
                             see an opaque failure instead of "that chat is out
                             of scope".

CONFIGURATION
-------------
TELEGRAM_ALLOWED_CHATS       Comma/whitespace separated chat ids, @usernames,
                             t.me links, or `me` for Saved Messages. EMPTY OR
                             UNSET DISABLES SCOPING ENTIRELY (upstream
                             behaviour) -- fail-open is deliberate so a fresh
                             clone still works, and startup says so loudly.
TELEGRAM_SCOPE_GLOBAL_TOOLS  deny (default) | hide | allow -- what to do with
                             the account-wide tools of (C). `deny` keeps them
                             listed and returns a scope error; `hide`
                             unregisters them so they never reach the model.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

# Separators accepted in TELEGRAM_ALLOWED_CHATS.
_ENTRY_SEPARATORS = ",;\n\t "

# Prefixes stripped when an entry is pasted as a link.
_LINK_PREFIXES = ("https://", "http://", "t.me/", "telegram.me/", "telegram.dog/", "@")

# Entries meaning "my own Saved Messages".
_SELF_TOKENS = {"me", "self", "saved", "saved_messages"}

_GLOBAL_TOOL_MODES = ("deny", "hide", "allow")


class ChatNotInScopeError(Exception):
    """Raised when a tool tries to touch a chat outside the allowlist.

    Deliberately NOT a ValueError: several upstream tools wrap peer resolution
    in `except ValueError` and would rewrite the message into their own
    "couldn't find that chat" wording, which would read as a lookup failure
    rather than a policy decision.
    """

    def __init__(self, payload: str):
        super().__init__(payload)
        self.payload = payload


# ---------------------------------------------------------------------------
# Tools that reach past any single chat
# ---------------------------------------------------------------------------
# These never resolve a peer the caller named (or resolve many), so chokepoints
# (A) and (B) cannot narrow them. Grouped by why they are here, because the
# reason decides whether a future upstream tool belongs in the list.

# Discovering or joining chats outside the allowlist.
_GLOBAL_DISCOVERY = {
    "search_public_chats",
    "resolve_username",
    "subscribe_public_channel",
    "join_chat_by_link",
    "import_chat_invite",
    "create_group",
    "create_channel",
    "get_common_chats",
}

# Reading account-wide message state across every chat.
_GLOBAL_MESSAGE_STATE = {
    "search_global",
    "get_drafts",
}

# Exposing or mutating the contact book, which is account-wide by nature.
_GLOBAL_CONTACTS = {
    "list_contacts",
    "search_contacts",
    "get_contact_ids",
    "export_contacts",
    "get_blocked_users",
    "import_contacts",
}

# Chat folders enumerate and reorganise every dialog on the account.
_GLOBAL_FOLDERS = {
    "list_folders",
    "get_folder",
    "create_folder",
    "add_chat_to_folder",
    "remove_chat_from_folder",
    "delete_folder",
    "reorder_folders",
}

# Writes to the account's own profile/privacy. Not a leak of other chats, but
# outside the remit of "read and answer in these chats", so held to the same
# line; flip TELEGRAM_SCOPE_GLOBAL_TOOLS=allow if you want them back.
_GLOBAL_ACCOUNT_WRITES = {
    "update_profile",
    "set_profile_photo",
    "delete_profile_photo",
    "set_privacy_settings",
    "set_bot_commands",
}

GLOBAL_TOOLS = frozenset(
    _GLOBAL_DISCOVERY
    | _GLOBAL_MESSAGE_STATE
    | _GLOBAL_CONTACTS
    | _GLOBAL_FOLDERS
    | _GLOBAL_ACCOUNT_WRITES
)


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


@dataclass
class ChatScope:
    """Chats this server may touch, as parsed from TELEGRAM_ALLOWED_CHATS."""

    ids: set[int] = field(default_factory=set)
    usernames: set[str] = field(default_factory=set)
    allow_self: bool = False
    # The entries exactly as configured, kept only so the startup banner echoes
    # what was written rather than the expanded id keys matching works on.
    entries: list[str] = field(default_factory=list)
    # Ids learned at runtime by resolving a configured @username, so later
    # id-only checks (dialog filtering, event routing) match without a lookup.
    learned_ids: set[int] = field(default_factory=set)

    @property
    def enabled(self) -> bool:
        return bool(self.ids or self.usernames or self.allow_self)

    def describe(self) -> str:
        return ", ".join(self.entries) or "(empty)"

    def remember(self, entity: Any) -> None:
        """Record the id of an entity that matched by username."""
        for candidate in _id_candidates(entity):
            self.learned_ids.add(candidate)

    def allows_id(self, chat_id: Optional[int]) -> bool:
        if chat_id is None:
            return False
        known = self.ids | self.learned_ids
        return bool(_id_keys(int(chat_id)) & known)

    def denies_identifier(self, identifier: Any) -> bool:
        """True when `identifier` can be refused without asking Telegram.

        Only safe for a bare numeric id, and only while the allowlist names no
        @usernames: an unresolved username could still turn out to be this id,
        and refusing it would be wrong. Worth the narrow scope -- it is the
        common case, it saves a lookup per refused call, and it means a chat
        this account is not a member of is refused as out of scope instead of
        failing resolution and surfacing as a generic error.
        """
        if not self.enabled or self.usernames:
            return False
        if isinstance(identifier, bool):
            return False
        if isinstance(identifier, int):
            return not self.allows_id(identifier)
        text = str(identifier).strip()
        if text.startswith("-"):
            digits = text[1:]
        else:
            digits = text
        if not digits.isdigit():
            return False
        return not self.allows_id(int(text))

    def allows(self, entity: Any) -> bool:
        """True if `entity` (a Telethon entity, a Dialog, or a raw id) is in scope."""
        if not self.enabled:
            return True
        if isinstance(entity, int):
            return self.allows_id(entity)

        if self.allow_self and getattr(entity, "is_self", False):
            return True

        known = self.ids | self.learned_ids
        if known and any(c in known for c in _id_candidates(entity)):
            return True

        if self.usernames:
            for name in _usernames_of(entity):
                if name in self.usernames:
                    self.remember(entity)
                    return True
        return False


def _id_keys(value: int) -> set[int]:
    """Canonical lookup keys for one chat id, so either convention matches.

    Telethon marks channel ids as -100<id> and basic-group ids as -<id>, while
    the ids people copy out of a client are sometimes already marked and
    sometimes bare. Storing both the given form and its unmarked form means a
    config entry of -1001234567890 and one of 1234567890 name the same chat,
    instead of one of them silently matching nothing.
    """
    keys = {value}
    if value < 0:
        text = str(abs(value))
        keys.add(int(text[3:]) if text.startswith("100") and len(text) > 3 else abs(value))
    return keys


def _id_candidates(entity: Any) -> set[int]:
    """Ids by which `entity` may legitimately be named in the allowlist."""
    # A Dialog is not an entity but exposes the already-marked id directly.
    candidates: set[int] = set()
    marked = getattr(entity, "chat_id", None)
    if isinstance(marked, int) and not isinstance(marked, bool):
        candidates |= _id_keys(marked)

    raw = getattr(entity, "id", None)
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        return candidates

    candidates.add(raw)
    kind = type(entity).__name__
    if kind == "Channel":
        candidates.add(-1000000000000 - raw)
    elif kind == "Chat":
        candidates.add(-raw)
    return candidates


def _usernames_of(entity: Any) -> set[str]:
    names = set()
    primary = getattr(entity, "username", None)
    if primary:
        names.add(str(primary).lower())
    # Channels and users may carry several usernames since layer 154.
    for extra in getattr(entity, "usernames", None) or []:
        value = getattr(extra, "username", None)
        if value:
            names.add(str(value).lower())
    return names


def _split_entries(raw: str) -> list[str]:
    for sep in _ENTRY_SEPARATORS[1:]:
        raw = raw.replace(sep, ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_entry(entry: str, scope: ChatScope) -> None:
    lowered = entry.lower()
    if lowered in _SELF_TOKENS:
        scope.allow_self = True
        return

    try:
        scope.ids.update(_id_keys(int(entry)))
        return
    except ValueError:
        pass

    cleaned = lowered
    for prefix in _LINK_PREFIXES:
        while cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    cleaned = cleaned.split("?", 1)[0].strip("/")

    # https://t.me/c/1234567890/42 -- an internal link to a private supergroup.
    if cleaned.startswith("c/"):
        parts = cleaned.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            scope.ids.update(_id_keys(-1000000000000 - int(parts[1])))
            return

    # A private invite link carries an opaque hash, not an identity we can
    # check against a resolved chat. Refusing beats admitting an entry that
    # would never match and would look like a silently-ignored allowlist.
    if cleaned.startswith("+") or cleaned.startswith("joinchat/"):
        raise SystemExit(
            f"Invalid TELEGRAM_ALLOWED_CHATS entry '{entry}': a private invite link "
            "cannot identify a chat. Join the chat first, then list its numeric id "
            "(see `make discover-chats`)."
        )

    cleaned = cleaned.split("/", 1)[0]
    if not cleaned:
        raise SystemExit(f"Invalid TELEGRAM_ALLOWED_CHATS entry '{entry}'.")
    scope.usernames.add(cleaned)


def load_scope(value: Optional[str] = None) -> ChatScope:
    """Parse the allowlist from TELEGRAM_ALLOWED_CHATS (or an explicit value)."""
    raw = os.getenv("TELEGRAM_ALLOWED_CHATS", "") if value is None else value
    scope = ChatScope()
    for entry in _split_entries(raw or ""):
        _parse_entry(entry, scope)
        scope.entries.append(entry)
    return scope


def global_tools_mode(value: Optional[str] = None) -> str:
    raw = os.getenv("TELEGRAM_SCOPE_GLOBAL_TOOLS", "deny") if value is None else value
    mode = (raw or "deny").strip().lower()
    if mode not in _GLOBAL_TOOL_MODES:
        accepted = ", ".join(_GLOBAL_TOOL_MODES)
        raise SystemExit(
            f"Invalid TELEGRAM_SCOPE_GLOBAL_TOOLS '{raw}'. Expected one of: {accepted}."
        )
    return mode


# ---------------------------------------------------------------------------
# Agent-facing refusals
# ---------------------------------------------------------------------------


def _denied_chat(reference: Any) -> ChatNotInScopeError:
    """Refusal for a peer outside the allowlist.

    Names only the reference the caller already supplied. Echoing the chat's
    real title or the rest of the allowlist would hand back exactly the
    information the scope exists to withhold.
    """
    return ChatNotInScopeError(
        f"Out of scope: this server is restricted to a fixed set of chats and "
        f"'{reference}' is not one of them. Do not retry with a different spelling of "
        "the same chat -- ask the operator to add it to TELEGRAM_ALLOWED_CHATS if it "
        "genuinely belongs in scope."
    )


def _denied_tool(tool_name: str) -> ChatNotInScopeError:
    return ChatNotInScopeError(
        f"Out of scope: '{tool_name}' reaches beyond the chats this server is "
        "restricted to (it searches, lists or modifies the account as a whole), so it "
        "is disabled here. Use a chat-scoped tool instead."
    )


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _rebind(name: str, old: Any, new: Any) -> int:
    """Repoint every `telegram_mcp.*` module that star-imported `old` at `new`.

    `from telegram_mcp.runtime import *` copies the function object into each
    tool module's globals at import time, so patching runtime alone would miss
    them.
    """
    count = 0
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("telegram_mcp"):
            continue
        if getattr(module, name, None) is old:
            setattr(module, name, new)
            count += 1
    return count


def _install_error_passthrough(runtime: Any) -> None:
    """(E) Let a scope refusal survive the generic error formatter."""
    original = runtime.log_and_format_error
    if getattr(original, "_scope_wrapped", False):
        return

    def log_and_format_error(function_name, error, *args, **kwargs):
        if isinstance(error, ChatNotInScopeError):
            return error.payload
        return original(function_name, error, *args, **kwargs)

    log_and_format_error._scope_wrapped = True
    runtime.log_and_format_error = log_and_format_error
    _rebind("log_and_format_error", original, log_and_format_error)


def _install_entity_gate(runtime: Any, scope: ChatScope) -> None:
    """(A) Refuse any peer outside the allowlist, for every tool at once."""
    original = runtime._resolve
    if getattr(original, "_scope_wrapped", False):
        return

    async def _resolve(getter, identifier, client, label):
        if scope.denies_identifier(identifier):
            raise _denied_chat(identifier)
        entity = await original(getter, identifier, client, label)
        # get_input_entity returns an InputPeer, which carries channel_id/chat_id/
        # user_id rather than a plain id; resolve it to a real entity to check.
        checked = entity
        if not scope.allows(checked):
            if getter != "get_entity":
                client = client or runtime.get_client()
                checked = await original("get_entity", identifier, client, label)
            if not scope.allows(checked):
                raise _denied_chat(identifier)
        return entity

    _resolve._scope_wrapped = True
    runtime._resolve = _resolve


def _install_dialog_gate(scope: ChatScope) -> None:
    """(B) Hide out-of-scope chats from every dialog listing."""
    from telethon import TelegramClient

    original = TelegramClient.get_dialogs
    if getattr(original, "_scope_wrapped", False):
        return

    async def get_dialogs(self, *args, **kwargs):
        # `limit` counts dialogs Telegram returns, which are ordered by recent
        # activity and mostly out of scope. Applying it before the filter makes
        # get_dialogs(limit=5) answer "no chats" whenever the five most recent
        # dialogs happen to be ones we hide -- so ask for everything and impose
        # the caller's limit on what survives. The universe is bounded by the
        # account's dialog list, which startup already fetches in full to warm
        # the entity cache.
        requested = kwargs.pop("limit", args[0] if args else None)
        rest = args[1:] if args else ()
        dialogs = await original(self, None, *rest, **kwargs)

        # Telethon has already cached every entity it just fetched, so dropping
        # them from the returned list keeps resolution of in-scope chats working.
        try:
            allowed = [d for d in dialogs if scope.allows(getattr(d, "entity", d))]
        except TypeError:
            return dialogs
        if isinstance(requested, int) and requested >= 0:
            return allowed[:requested]
        return allowed

    get_dialogs._scope_wrapped = True
    TelegramClient.get_dialogs = get_dialogs


def _install_tool_gate(server: Any, mode: str) -> list[str]:
    """(C) Handle the account-wide tools that (A) and (B) cannot narrow."""
    manager = server._tool_manager
    registered = {tool.name for tool in manager.list_tools()}
    targets = sorted(GLOBAL_TOOLS & registered)

    if mode == "hide":
        for name in targets:
            manager.remove_tool(name)
        return targets

    original = manager.call_tool
    if getattr(original, "_scope_wrapped", False):
        return targets

    async def call_tool(name, arguments, *args, **kwargs):
        if mode == "deny" and name in GLOBAL_TOOLS:
            # Raise rather than return the text: FastMCP calls this with
            # convert_result=True and validates the result against the tool's
            # outputSchema, so a bare string here fails as "no structured output
            # returned" and the model never sees why it was refused. An
            # exception takes the documented error path instead, which carries
            # str(exc) and skips output validation.
            raise _denied_tool(name)
        return await original(name, arguments, *args, **kwargs)

    call_tool._scope_wrapped = True
    manager.call_tool = call_tool
    return targets


def _install_event_gate(scope: ChatScope) -> None:
    """(D) Keep out-of-scope chats out of the incoming-message feed."""
    from telethon import events as telethon_events

    from telegram_mcp.runtime import clients
    from telegram_mcp.tools import events as events_module

    original = events_module._on_new_incoming
    if getattr(original, "_scope_wrapped", False):
        return

    async def _on_new_incoming(event):
        if not scope.allows_id(getattr(event, "chat_id", None)):
            chat = await event.get_chat()
            if not scope.allows(chat):
                return
        return await original(event)

    _on_new_incoming._scope_wrapped = True
    events_module._on_new_incoming = _on_new_incoming
    for client in clients.values():
        # The handler was registered by object at import time, so swap it.
        client.remove_event_handler(original)
        client.add_event_handler(_on_new_incoming, telethon_events.NewMessage(incoming=True))


def install(server: Any = None, scope: Optional[ChatScope] = None) -> ChatScope:
    """Apply chat scoping to the already-registered MCP server. Idempotent."""
    from telegram_mcp import runtime

    scope = load_scope() if scope is None else scope
    mode = global_tools_mode()

    if not scope.enabled:
        print(
            "WARNING: TELEGRAM_ALLOWED_CHATS is unset -- chat scoping is OFF and every "
            "tool can reach every chat on this account. Set it to the chats this server "
            "is meant to see (see `make discover-chats`).",
            file=sys.stderr,
        )
        return scope

    _install_error_passthrough(runtime)
    _install_entity_gate(runtime, scope)
    _install_dialog_gate(scope)
    _install_event_gate(scope)
    affected = _install_tool_gate(server or runtime.mcp, mode)

    verb = {"deny": "refused", "hide": "unregistered", "allow": "left enabled"}[mode]
    print(
        f"Chat scope active: {scope.describe()}. "
        f"{len(affected)} account-wide tool(s) {verb} (TELEGRAM_SCOPE_GLOBAL_TOOLS={mode}).",
        file=sys.stderr,
    )
    return scope
