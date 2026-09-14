"""Tests for the fork-local chat scope (telegram_mcp/chat_scope.py)."""

import pathlib

import pytest

from telegram_mcp import chat_scope

# --- doubles -------------------------------------------------------------
# Named after the Telethon classes because chat_scope keys off type(...).__name__.


class Channel:
    def __init__(self, id, username=None, usernames=None):
        self.id = id
        self.username = username
        self.usernames = usernames or []


class Chat:
    def __init__(self, id):
        self.id = id
        self.username = None


class User:
    def __init__(self, id, username=None, is_self=False):
        self.id = id
        self.username = username
        self.is_self = is_self


class Dialog:
    def __init__(self, entity):
        self.entity = entity


class Username:
    def __init__(self, username):
        self.username = username


@pytest.fixture
def restore_patch_points():
    """Undo chat_scope.install()'s global patches after the test.

    install() patches module attributes and a Telethon class method, none of
    which monkeypatch knows about; left in place they would filter dialogs for
    every later test in the session.
    """
    import sys

    from telethon import TelegramClient

    from telegram_mcp import runtime
    from telegram_mcp.tools import events as events_module

    saved_dialogs = TelegramClient.get_dialogs
    saved_resolve = runtime._resolve
    saved_handler = events_module._on_new_incoming
    saved_formatters = {
        module.__name__: module.log_and_format_error
        for module in list(sys.modules.values())
        if module is not None
        and getattr(module, "__name__", "").startswith("telegram_mcp")
        and hasattr(module, "log_and_format_error")
    }
    try:
        yield
    finally:
        TelegramClient.get_dialogs = saved_dialogs
        runtime._resolve = saved_resolve
        events_module._on_new_incoming = saved_handler
        for name, formatter in saved_formatters.items():
            setattr(sys.modules[name], "log_and_format_error", formatter)


# --- parsing -------------------------------------------------------------


def test_empty_allowlist_disables_scoping():
    scope = chat_scope.load_scope("")
    assert not scope.enabled
    assert scope.allows(Channel(999)) is True


@pytest.mark.parametrize(
    "entry",
    ["-1001234567890", "1234567890", "https://t.me/c/1234567890/42", "t.me/c/1234567890"],
)
def test_channel_is_matched_however_its_id_is_spelled(entry):
    scope = chat_scope.load_scope(entry)
    assert scope.allows(Channel(1234567890))
    assert not scope.allows(Channel(999))


def test_basic_group_marked_id():
    scope = chat_scope.load_scope("-4242")
    assert scope.allows(Chat(4242))
    assert not scope.allows(Chat(4243))


@pytest.mark.parametrize(
    "entry", ["@pulse_chat", "pulse_chat", "https://t.me/pulse_chat", "t.me/pulse_chat/99"]
)
def test_username_entries(entry):
    scope = chat_scope.load_scope(entry)
    assert scope.allows(Channel(7, username="Pulse_Chat"))
    assert not scope.allows(Channel(8, username="other"))


def test_secondary_usernames_count():
    scope = chat_scope.load_scope("@pulse_alt")
    assert scope.allows(Channel(7, username="main", usernames=[Username("pulse_alt")]))


def test_username_match_teaches_the_id():
    """A chat allowed by @username must also pass later id-only checks."""
    scope = chat_scope.load_scope("@pulse_chat")
    assert not scope.allows_id(-1000000000007)
    assert scope.allows(Channel(7, username="pulse_chat"))
    assert scope.allows_id(-1000000000007)


def test_me_token_allows_own_user_only():
    scope = chat_scope.load_scope("me")
    assert scope.allows(User(1, is_self=True))
    assert not scope.allows(User(2))


def test_entries_split_on_mixed_separators():
    scope = chat_scope.load_scope(" -100111 , @two;\n-100333 ")
    assert scope.allows(Channel(111))
    assert scope.allows(Channel(9, username="two"))
    assert scope.allows(Channel(333))


def test_private_invite_link_is_rejected_loudly():
    """Silently keeping an unmatched entry would look like a working allowlist."""
    with pytest.raises(SystemExit, match="private invite link"):
        chat_scope.load_scope("https://t.me/+AbCdEf123")


def test_invalid_global_tools_mode():
    with pytest.raises(SystemExit, match="TELEGRAM_SCOPE_GLOBAL_TOOLS"):
        chat_scope.global_tools_mode("sometimes")


@pytest.mark.parametrize("mode", chat_scope._GLOBAL_TOOL_MODES)
def test_valid_global_tools_modes(mode):
    assert chat_scope.global_tools_mode(mode) == mode


def test_global_tools_mode_reads_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_SCOPE_GLOBAL_TOOLS", "hide")
    assert chat_scope.global_tools_mode() == "hide"


def test_load_scope_reads_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "-100777")
    assert chat_scope.load_scope().allows(Channel(777))


# --- gate A: entity resolution -------------------------------------------


@pytest.fixture
def gated_runtime(monkeypatch):
    """runtime with a stubbed _resolve, wrapped by the entity gate."""
    from telegram_mcp import runtime

    calls = []

    async def fake_resolve(getter, identifier, client, label):
        calls.append(getter)
        if getter == "get_input_entity":
            return object()  # an InputPeer carries no id the gate can read
        return Channel(int(identifier))

    monkeypatch.setattr(runtime, "_resolve", fake_resolve)
    chat_scope._install_entity_gate(runtime, chat_scope.load_scope("-100111"))
    return runtime, calls


def test_entity_gate_passes_allowed_chat(gated_runtime):
    import asyncio

    runtime, _ = gated_runtime
    assert asyncio.run(runtime.resolve_entity(111)).id == 111


def test_entity_gate_blocks_other_chats(gated_runtime):
    import asyncio

    runtime, _ = gated_runtime
    with pytest.raises(chat_scope.ChatNotInScopeError, match="Out of scope"):
        asyncio.run(runtime.resolve_entity(222))


def test_input_entity_is_checked_via_a_second_lookup(gated_runtime):
    """get_input_entity returns an InputPeer with no readable id, so the gate
    re-resolves the identifier as a full entity rather than waving it through."""
    import asyncio

    runtime, calls = gated_runtime
    asyncio.run(runtime.resolve_input_entity(111))
    assert calls == ["get_input_entity", "get_entity"]

    with pytest.raises(chat_scope.ChatNotInScopeError):
        asyncio.run(runtime.resolve_input_entity(222))


# --- gate B: dialog listings ---------------------------------------------


@pytest.fixture
def gated_dialogs(monkeypatch):
    """TelegramClient.get_dialogs stubbed, then wrapped by the dialog gate."""
    from telethon import TelegramClient

    seen = {}

    async def fake_get_dialogs(self, limit=None, *args, **kwargs):
        seen["limit"] = limit
        # Out-of-scope chats first: Telegram orders by recent activity, not by
        # what we are allowed to see.
        return [Dialog(User(333)), Dialog(Channel(222)), Dialog(Channel(111))]

    monkeypatch.setattr(TelegramClient, "get_dialogs", fake_get_dialogs)
    chat_scope._install_dialog_gate(chat_scope.load_scope("-100111,-100222"))
    try:
        yield TelegramClient, seen
    finally:
        monkeypatch.undo()


def test_dialog_gate_filters_listings(gated_dialogs):
    import asyncio

    client_cls, _ = gated_dialogs
    dialogs = asyncio.run(client_cls.get_dialogs(object()))
    assert [d.entity.id for d in dialogs] == [222, 111]


@pytest.mark.parametrize("call", ["positional", "keyword"])
def test_limit_applies_after_filtering_not_before(gated_dialogs, call):
    """Otherwise get_dialogs(limit=1) answers "no chats" whenever the most
    recent dialog is one we hide, which is most of the time."""
    import asyncio

    client_cls, seen = gated_dialogs
    if call == "positional":
        dialogs = asyncio.run(client_cls.get_dialogs(object(), 1))
    else:
        dialogs = asyncio.run(client_cls.get_dialogs(object(), limit=1))

    assert seen["limit"] is None, "the underlying fetch must not be truncated first"
    assert [d.entity.id for d in dialogs] == [222]


def test_dialog_gate_passes_other_arguments_through(gated_dialogs):
    import asyncio

    client_cls, _ = gated_dialogs
    asyncio.run(client_cls.get_dialogs(object(), limit=5, archived=True))


# --- gate C: account-wide tools ------------------------------------------


class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeManager:
    def __init__(self, names):
        self.tools = [_FakeTool(n) for n in names]
        self.called = []

    def list_tools(self):
        return list(self.tools)

    def remove_tool(self, name):
        self.tools = [t for t in self.tools if t.name != name]

    async def call_tool(self, name, arguments, *args, **kwargs):
        self.called.append(name)
        return f"ran {name}"


class _FakeServer:
    def __init__(self, names):
        self._tool_manager = _FakeManager(names)


TOOL_NAMES = ["list_messages", "search_global", "list_contacts"]


def test_global_tools_are_refused_in_deny_mode():
    import asyncio

    server = _FakeServer(TOOL_NAMES)
    affected = chat_scope._install_tool_gate(server, "deny")
    assert affected == ["list_contacts", "search_global"]

    manager = server._tool_manager
    assert asyncio.run(manager.call_tool("list_messages", {})) == "ran list_messages"
    with pytest.raises(chat_scope.ChatNotInScopeError, match="Out of scope"):
        asyncio.run(manager.call_tool("search_global", {}))
    # Refused, not merely reported as failed: the tool never ran.
    assert manager.called == ["list_messages"]
    assert [t.name for t in manager.list_tools()] == TOOL_NAMES


def test_global_tools_are_unregistered_in_hide_mode():
    server = _FakeServer(TOOL_NAMES)
    chat_scope._install_tool_gate(server, "hide")
    assert [t.name for t in server._tool_manager.list_tools()] == ["list_messages"]


def test_allow_mode_keeps_global_tools_callable():
    import asyncio

    server = _FakeServer(TOOL_NAMES)
    chat_scope._install_tool_gate(server, "allow")
    assert asyncio.run(server._tool_manager.call_tool("search_global", {})) == "ran search_global"


def test_refusals_travel_as_the_exception_text():
    """FastMCP renders a raised exception as str(exc), so the payload must be it.

    Returning the text instead would be validated against the tool's
    outputSchema and rejected as "no structured output returned", leaving the
    model with a schema complaint rather than the reason.
    """
    assert (
        str(chat_scope._denied_tool("search_global"))
        == chat_scope._denied_tool("search_global").payload
    )
    assert str(chat_scope._denied_chat("telegram")) == chat_scope._denied_chat("telegram").payload


# --- gate E: the refusal survives the generic error formatter -------------


def test_scope_error_is_not_flattened_into_a_generic_code():
    class FakeRuntime:
        @staticmethod
        def log_and_format_error(function_name, error, *args, **kwargs):
            return "An error occurred (code: GEN-ERR-001)."

    runtime = FakeRuntime()
    chat_scope._install_error_passthrough(runtime)
    assert runtime.log_and_format_error("t", ValueError("x")).startswith("An error occurred")
    assert runtime.log_and_format_error("t", chat_scope.ChatNotInScopeError("nope")) == "nope"


def test_rebind_repoints_star_imported_copies():
    import sys
    import types

    original = object()
    replacement = object()
    module = types.ModuleType("telegram_mcp._rebind_probe")
    module.target = original
    sys.modules[module.__name__] = module
    try:
        assert chat_scope._rebind("target", original, replacement) == 1
        assert module.target is replacement
    finally:
        del sys.modules[module.__name__]


# --- merge canary ---------------------------------------------------------


REVIEWED_TOOLS_FILE = pathlib.Path(__file__).with_name("scope_reviewed_tools.txt")


def test_every_registered_tool_has_been_reviewed_for_scope():
    """Fail when upstream adds or renames a tool this fork has not classified.

    chat_scope narrows tools by peer resolution (gates A/B) or by name
    (GLOBAL_TOOLS). A tool that reaches past a single chat and is missing from
    GLOBAL_TOOLS would quietly escape the scope, and nothing else in the suite
    would notice. After `git merge upstream/main`, this test is the prompt to
    classify what arrived; regenerate the file only once you have.
    """
    import telegram_mcp.tools  # noqa: F401 - registers the tools

    from telegram_mcp.runtime import mcp

    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    reviewed = set(REVIEWED_TOOLS_FILE.read_text().split())

    added = sorted(registered - reviewed)
    removed = sorted(reviewed - registered)
    assert not added and not removed, (
        f"Unreviewed tool changes: added={added or 'none'}, removed={removed or 'none'}. "
        "Decide for each added tool whether it is confined to one chat (gates A/B "
        "handle it) or reaches the whole account (add it to chat_scope.GLOBAL_TOOLS), "
        "then run `make refresh-tool-baseline`."
    )


def test_global_tools_all_exist():
    """A stale name in GLOBAL_TOOLS silently protects nothing."""
    import telegram_mcp.tools  # noqa: F401

    from telegram_mcp.runtime import mcp

    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert not sorted(chat_scope.GLOBAL_TOOLS - registered)


# --- gate D: the incoming-message feed ------------------------------------


class _FakeClient:
    def __init__(self):
        self.handlers = []

    def remove_event_handler(self, callback, event=None):
        self.handlers = [(c, e) for c, e in self.handlers if c is not callback]

    def add_event_handler(self, callback, event=None):
        self.handlers.append((callback, event))


class _FakeEvent:
    def __init__(self, chat_id, chat):
        self.chat_id = chat_id
        self._chat = chat

    async def get_chat(self):
        return self._chat


def test_event_gate_drops_out_of_scope_messages(monkeypatch):
    import asyncio

    from telegram_mcp import runtime
    from telegram_mcp.tools import events as events_module

    seen = []

    async def original(event):
        seen.append(event.chat_id)

    monkeypatch.setattr(events_module, "_on_new_incoming", original)
    client = _FakeClient()
    client.add_event_handler(original, None)
    monkeypatch.setattr(runtime, "clients", {"default": client})

    chat_scope._install_event_gate(chat_scope.load_scope("-100111"))
    scoped = events_module._on_new_incoming
    assert [c for c, _ in client.handlers] == [scoped]

    asyncio.run(scoped(_FakeEvent(-1000000000111, Channel(111))))
    asyncio.run(scoped(_FakeEvent(-1000000000222, Channel(222))))
    assert seen == [-1000000000111]


def test_event_gate_falls_back_to_the_chat_for_username_allowlists(monkeypatch):
    """An allowlist of @usernames cannot match on the raw id alone."""
    import asyncio

    from telegram_mcp import runtime
    from telegram_mcp.tools import events as events_module

    seen = []

    async def original(event):
        seen.append(event.chat_id)

    monkeypatch.setattr(events_module, "_on_new_incoming", original)
    monkeypatch.setattr(runtime, "clients", {"default": _FakeClient()})

    chat_scope._install_event_gate(chat_scope.load_scope("@pulse_chat"))
    scoped = events_module._on_new_incoming
    asyncio.run(scoped(_FakeEvent(-1000000000111, Channel(111, username="pulse_chat"))))
    assert seen == [-1000000000111]


# --- install() ------------------------------------------------------------


def test_install_is_a_no_op_and_warns_when_the_allowlist_is_empty(monkeypatch, capsys):
    from telegram_mcp import runtime

    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    before = runtime._resolve

    scope = chat_scope.install(server=_FakeServer(TOOL_NAMES))

    assert not scope.enabled
    assert runtime._resolve is before, "nothing should be patched while scoping is off"
    assert "scoping is OFF" in capsys.readouterr().err


def test_install_reports_the_active_scope(monkeypatch, capsys, restore_patch_points):
    from telegram_mcp import runtime

    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "-100111")
    monkeypatch.setenv("TELEGRAM_SCOPE_GLOBAL_TOOLS", "hide")
    monkeypatch.setattr(runtime, "clients", {"default": _FakeClient()})

    server = _FakeServer(TOOL_NAMES)
    chat_scope.install(server=server)

    err = capsys.readouterr().err
    assert "Chat scope active" in err and "111" in err
    assert [t.name for t in server._tool_manager.list_tools()] == ["list_messages"]


# --- refusing without a lookup -------------------------------------------


@pytest.mark.parametrize("identifier", [-1001338630868, "-1001338630868", 42, "42"])
def test_numeric_identifiers_are_refused_without_asking_telegram(identifier):
    scope = chat_scope.load_scope("-100111")
    assert scope.denies_identifier(identifier)


@pytest.mark.parametrize("identifier", [-1000000000111, "-1000000000111", 111, "111"])
def test_allowed_numeric_identifiers_are_not_pre_refused(identifier):
    scope = chat_scope.load_scope("-100111")
    assert not scope.denies_identifier(identifier)


@pytest.mark.parametrize("identifier", ["@somechat", "somechat", "мамин чат", "me", True])
def test_non_numeric_identifiers_need_a_lookup(identifier):
    """Usernames and saved aliases only reveal their chat once resolved."""
    scope = chat_scope.load_scope("-100111")
    assert not scope.denies_identifier(identifier)


def test_pre_check_stands_down_while_usernames_are_configured():
    """An unresolved @username could still turn out to be this id."""
    scope = chat_scope.load_scope("-100111,@pulse_chat")
    assert not scope.denies_identifier(-1001338630868)


def test_pre_check_is_off_when_scoping_is_disabled():
    assert not chat_scope.load_scope("").denies_identifier(-1001338630868)


def test_entity_gate_refuses_before_resolving(gated_runtime):
    """The refusal must not depend on the chat being resolvable: a chat this
    account has never joined would otherwise fail resolution and reach the
    model as upstream's generic "An error occurred (code: ...)"."""
    import asyncio

    runtime, calls = gated_runtime
    with pytest.raises(chat_scope.ChatNotInScopeError):
        asyncio.run(runtime.resolve_entity(-1001338630868))
    assert calls == [], "no Telegram lookup should have been attempted"
