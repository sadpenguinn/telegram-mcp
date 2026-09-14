#!/usr/bin/env python3
"""List the chats of a Telegram folder, to fill TELEGRAM_ALLOWED_CHATS.

Fork-local tooling; see FORK.md. Deliberately does NOT import
telegram_mcp.runner, so telegram_mcp.chat_scope never installs itself and the
listing stays unfiltered -- otherwise you could not discover the chats you have
not allowed yet.

    uv run python scripts/discover_chats.py                # what folders exist
    uv run python scripts/discover_chats.py Пульс          # that folder's chats
    uv run python scripts/discover_chats.py Пульс --env    # ready-to-paste line
    uv run python scripts/discover_chats.py пульс --by-title  # match chat titles

A folder is the better source: keeping the allowlist in sync later is "add the
chat to the folder, re-run this", with no guessing about which titles happen to
contain the right word.
"""

import argparse
import asyncio
import sys

from telethon import functions
from telethon.tl.types import DialogFilter, DialogFilterChatlist, TextWithEntities

from telegram_mcp.runtime import clients, get_entity_type, get_marked_id

# Folder flags that pull in whole categories of chats rather than named peers.
# A folder using them has members that its peer lists do not spell out.
_CATEGORY_FLAGS = ("contacts", "non_contacts", "groups", "broadcasts", "bots")


def _folder_title(folder) -> str:
    title = folder.title
    return title.text if isinstance(title, TextWithEntities) else str(title)


def _peer_key(peer) -> tuple:
    """Identity of an InputPeer, for de-duplicating pinned vs included."""
    return tuple(getattr(peer, attr, None) for attr in ("user_id", "chat_id", "channel_id"))


async def _folders(client) -> list:
    result = await client(functions.messages.GetDialogFiltersRequest())
    return [f for f in result.filters if isinstance(f, (DialogFilter, DialogFilterChatlist))]


async def _rows_for_folder(client, folder) -> list[tuple[int, str, str, str]]:
    """Resolve a folder's members to (marked id, type, title, @username)."""
    excluded = {_peer_key(p) for p in getattr(folder, "exclude_peers", [])}

    seen = set()
    peers = []
    # Pinned peers are stored separately from include_peers, so a folder read
    # from include_peers alone silently loses whatever is pinned in it.
    for peer in list(getattr(folder, "pinned_peers", [])) + list(
        getattr(folder, "include_peers", [])
    ):
        key = _peer_key(peer)
        if key in seen or key in excluded:
            continue
        seen.add(key)
        peers.append(peer)

    rows = []
    for peer in peers:
        try:
            entity = await client.get_entity(peer)
        except Exception as exc:
            print(f"  ! could not resolve {peer}: {exc}", file=sys.stderr)
            continue
        username = getattr(entity, "username", None)
        name = (
            getattr(entity, "title", None)
            or (
                f"{getattr(entity, 'first_name', '') or ''} "
                f"{getattr(entity, 'last_name', '') or ''}"
            ).strip()
        )
        rows.append(
            (
                get_marked_id(entity),
                get_entity_type(entity),
                name or "(no title)",
                f"@{username}" if username else "",
            )
        )
    return rows


async def _rows_by_title(client, pattern: str, limit: int) -> list[tuple[int, str, str, str]]:
    rows = []
    for dialog in await client.get_dialogs(limit=limit):
        entity = dialog.entity
        username = getattr(entity, "username", None)
        handle = f"@{username}" if username else ""
        if pattern.lower() not in f"{dialog.name or ''} {handle}".lower():
            continue
        rows.append(
            (
                get_marked_id(entity),
                get_entity_type(entity),
                dialog.name or "(no title)",
                handle,
            )
        )
    return rows


def _print_rows(rows) -> None:
    for marked, kind, name, handle in rows:
        print(f"{marked:>16}  {kind:<12} {name}{('  ' + handle) if handle else ''}")


async def _run(pattern: str, as_env: bool, by_title: bool, limit: int) -> int:
    matches: list[tuple[int, str, str, str]] = []
    incomplete: list[str] = []

    for label, client in clients.items():
        await client.connect()
        if not await client.is_user_authorized():
            print(
                f"Account '{label}' is not authorized. Generate a session string first: "
                "uv run session_string_generator.py",
                file=sys.stderr,
            )
            return 1
        if len(clients) > 1 and not as_env:
            print(f"\n=== account: {label} ===")

        # Warm the entity cache: a StringSession starts empty and resolving a
        # folder's peers one by one would otherwise be a lookup per chat.
        await client.get_dialogs(limit=limit)

        if by_title:
            rows = await _rows_by_title(client, pattern, limit)
            matches += rows
            if not as_env:
                _print_rows(rows)
        elif not pattern:
            for folder in await _folders(client):
                title = _folder_title(folder)
                size = len(getattr(folder, "pinned_peers", [])) + len(
                    getattr(folder, "include_peers", [])
                )
                print(f"{getattr(folder, 'emoticon', '') or ' '} {title}  ({size} chats)")
        else:
            hit = False
            for folder in await _folders(client):
                title = _folder_title(folder)
                if pattern.lower() not in title.lower():
                    continue
                hit = True
                if any(getattr(folder, flag, False) for flag in _CATEGORY_FLAGS):
                    incomplete.append(title)
                rows = await _rows_for_folder(client, folder)
                matches += rows
                if not as_env:
                    print(f"\n--- folder: {title} ({len(rows)} chats) ---")
                    _print_rows(rows)
            if not hit and not as_env:
                print(
                    f"No folder matched '{pattern}'. Run without arguments to list folders.",
                    file=sys.stderr,
                )

        await client.disconnect()

    for title in incomplete:
        print(
            f"\nWarning: folder '{title}' also includes whole categories of chats "
            "(contacts/groups/channels/bots), which are not named in it. The list above "
            "covers only its explicitly added chats.",
            file=sys.stderr,
        )

    if not matches:
        return 0 if not pattern and not by_title else 1
    if as_env:
        # Sorted and de-duplicated: a chat pinned in two folders must not
        # produce a duplicate entry.
        print(
            "TELEGRAM_ALLOWED_CHATS=" + ",".join(str(i) for i in sorted({m[0] for m in matches}))
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "pattern",
        nargs="?",
        default="",
        help="case-insensitive folder name; omit to list the folders",
    )
    parser.add_argument(
        "--env", action="store_true", help="print a TELEGRAM_ALLOWED_CHATS line for the matches"
    )
    parser.add_argument(
        "--by-title",
        action="store_true",
        help="match chat titles instead of a folder name (fallback when there is no folder)",
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="how many dialogs to fetch per account"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.pattern, args.env, args.by_title, args.limit)))


if __name__ == "__main__":
    main()
