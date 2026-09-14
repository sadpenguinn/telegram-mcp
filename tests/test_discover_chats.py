"""Tests for the fork-local discovery script (scripts/discover_chats.py)."""

import asyncio
import importlib.util
import pathlib

import pytest
from telethon.tl.types import Channel as TLChannel, TextWithEntities

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "discover_chats.py"


def _load():
    spec = importlib.util.spec_from_file_location("discover_chats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


discover = _load()


class Peer:
    """Stands in for an InputPeerChannel/Chat/User."""

    def __init__(self, channel_id=None, chat_id=None, user_id=None):
        self.channel_id = channel_id
        self.chat_id = chat_id
        self.user_id = user_id


def Channel(id, title, username=None):
    """A real Telethon Channel: get_marked_id() keys off isinstance, so a
    stand-in class would be marked as a bare positive id and the test would
    assert the wrong thing."""
    return TLChannel(id=id, title=title, photo=None, date=None, username=username)


class Folder:
    def __init__(self, title, pinned=(), include=(), exclude=(), **flags):
        self.title = title
        self.pinned_peers = list(pinned)
        self.include_peers = list(include)
        self.exclude_peers = list(exclude)
        for key, value in flags.items():
            setattr(self, key, value)


class FakeClient:
    def __init__(self, entities):
        self.entities = entities
        self.lookups = []

    async def get_entity(self, peer):
        self.lookups.append(peer)
        key = peer.channel_id or peer.chat_id or peer.user_id
        if key not in self.entities:
            raise ValueError("no such entity")
        return self.entities[key]


def _rows(folder, entities):
    return asyncio.run(discover._rows_for_folder(FakeClient(entities), folder))


def test_pinned_chats_are_included():
    """pinned_peers is a separate field; reading include_peers alone loses them."""
    folder = Folder("Пульс", pinned=[Peer(channel_id=1)], include=[Peer(channel_id=2)])
    entities = {1: Channel(1, "Pinned"), 2: Channel(2, "Plain")}
    assert [r[0] for r in _rows(folder, entities)] == [-1000000000001, -1000000000002]


def test_a_chat_pinned_and_included_appears_once():
    folder = Folder("Пульс", pinned=[Peer(channel_id=1)], include=[Peer(channel_id=1)])
    assert len(_rows(folder, {1: Channel(1, "Once")})) == 1


def test_excluded_chats_are_dropped():
    folder = Folder(
        "Пульс",
        include=[Peer(channel_id=1), Peer(channel_id=2)],
        exclude=[Peer(channel_id=2)],
    )
    entities = {1: Channel(1, "Keep"), 2: Channel(2, "Drop")}
    assert [r[2] for r in _rows(folder, entities)] == ["Keep"]


def test_unresolvable_peer_is_skipped_not_fatal(capsys):
    folder = Folder("Пульс", include=[Peer(channel_id=1), Peer(channel_id=99)])
    rows = _rows(folder, {1: Channel(1, "Keep")})
    assert [r[2] for r in rows] == ["Keep"]
    assert "could not resolve" in capsys.readouterr().err


def test_username_is_reported():
    folder = Folder("Пульс", include=[Peer(channel_id=1)])
    rows = _rows(folder, {1: Channel(1, "Pulse", username="pulse_chat")})
    assert rows[0][3] == "@pulse_chat"


@pytest.mark.parametrize(
    "title, expected",
    [("Пульс", "Пульс"), (TextWithEntities(text="Пульс", entities=[]), "Пульс")],
)
def test_folder_title_handles_both_telegram_shapes(title, expected):
    assert discover._folder_title(Folder(title)) == expected


def test_peer_key_separates_peer_types():
    """A channel and a user that share a numeric id are different chats."""
    assert discover._peer_key(Peer(channel_id=5)) != discover._peer_key(Peer(user_id=5))
