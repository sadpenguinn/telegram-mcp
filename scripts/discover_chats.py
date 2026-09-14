#!/usr/bin/env python3
"""List this account's dialogs so you can pick what belongs in TELEGRAM_ALLOWED_CHATS.

Fork-local tooling; see FORK.md. Deliberately does NOT import telegram_mcp.runner,
so telegram_mcp.chat_scope never installs itself and the listing stays unfiltered
-- otherwise you could not discover the chats you have not allowed yet.

    uv run python scripts/discover_chats.py             # every dialog
    uv run python scripts/discover_chats.py пульс       # only matching titles
    uv run python scripts/discover_chats.py пульс --env # ready-to-paste .env line
"""

import argparse
import asyncio
import sys

from telegram_mcp.runtime import clients, get_marked_id, get_entity_type


def _row(dialog) -> tuple[int, str, str, str]:
    entity = dialog.entity
    try:
        marked = get_marked_id(entity)
    except Exception:
        marked = dialog.id
    username = getattr(entity, "username", None)
    return (
        marked,
        get_entity_type(entity),
        dialog.name or "(no title)",
        f"@{username}" if username else "",
    )


async def _run(pattern: str, as_env: bool, limit: int) -> int:
    matches: list[tuple[int, str, str, str]] = []
    for label, client in clients.items():
        await client.connect()
        if not await client.is_user_authorized():
            print(
                f"Account '{label}' is not authorized. Generate a session string first: "
                "uv run session_string_generator.py",
                file=sys.stderr,
            )
            return 1
        if len(clients) > 1:
            print(f"\n=== account: {label} ===")
        for dialog in await client.get_dialogs(limit=limit):
            row = _row(dialog)
            if pattern and pattern.lower() not in f"{row[2]} {row[3]}".lower():
                continue
            matches.append(row)
            if not as_env:
                print(f"{row[0]:>16}  {row[1]:<12} {row[2]}{('  ' + row[3]) if row[3] else ''}")
        await client.disconnect()

    if as_env:
        print("TELEGRAM_ALLOWED_CHATS=" + ",".join(str(m[0]) for m in matches))
    elif not matches:
        print("No dialogs matched.", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pattern", nargs="?", default="", help="case-insensitive title/@username filter"
    )
    parser.add_argument(
        "--env", action="store_true", help="print a TELEGRAM_ALLOWED_CHATS line for the matches"
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="how many dialogs to fetch per account"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.pattern, args.env, args.limit)))


if __name__ == "__main__":
    main()
