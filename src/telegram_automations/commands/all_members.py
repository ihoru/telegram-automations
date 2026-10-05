from __future__ import annotations

import argparse
from typing import Any

from telegram_automations.common import (
    AutomationError,
    display_text,
    ensure_supported_group,
    parse_poll_link,
    resolve_chat,
)
from telegram_automations.polls.cleanup import fetch_all_participants
from telegram_automations.runtime import CommandRuntime, ErrorPolicy

ERROR_POLICY = ErrorPolicy()


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Print current group members as text ready to paste into a Telegram message. "
        "Bots and the signed-in account are excluded."
    )
    parser.add_argument(
        "--chat",
        required=True,
        help="Group username (@group), numeric ID, or t.me group message link",
    )
    parser.add_argument(
        "-nl",
        "--new-line",
        action="store_true",
        help="Print each member on a separate line (default: space-separated)",
    )


def member_text(user: Any) -> str:
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"

    name = " ".join(
        part
        for value in (
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        )
        if (part := display_text(value, limit=None))
    )
    user_id = int(user.id)
    return f"{name} (ID: {user_id})" if name else f"ID: {user_id}"


async def run(args: argparse.Namespace, runtime: CommandRuntime) -> int:
    chat_ref = args.chat.strip()
    if not chat_ref:
        raise AutomationError("--chat must be a group username, ID, or message link.")
    if chat_ref.startswith(("https://", "http://")):
        chat_ref = parse_poll_link(chat_ref).chat_ref
    else:
        try:
            chat_ref = int(chat_ref)
        except ValueError:
            chat_ref = chat_ref if chat_ref.startswith("@") else f"@{chat_ref}"

    chat = await resolve_chat(runtime.client, chat_ref)
    ensure_supported_group(chat)
    users = await fetch_all_participants(runtime.client, chat)
    own_user_id = int(runtime.me.id)
    lines = [
        member_text(user)
        for user in users
        if int(user.id) != own_user_id and not bool(getattr(user, "bot", False))
    ]
    if lines:
        print(("\n" if args.new_line else " ").join(lines))
    return 0
