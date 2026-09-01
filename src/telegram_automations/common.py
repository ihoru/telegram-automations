from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions, types, utils

DEFAULT_SESSION_NAME = "telegram_automations"


class AutomationError(RuntimeError):
    """An expected error that can be shown to the operator."""


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    session_path: Path


@dataclass(frozen=True)
class PollLocation:
    chat_ref: int | str
    message_id: int


@dataclass(frozen=True)
class PollContext:
    chat: Any
    message: Any

    @property
    def poll(self) -> Any:
        return self.message.media.poll


@dataclass(frozen=True)
class PollVotesResult:
    count: int
    votes: tuple[Any, ...]
    users: tuple[Any, ...]


def load_config(config_dir: Path) -> AppConfig:
    config_dir = config_dir.expanduser().resolve()
    load_dotenv(config_dir / ".env", override=False)

    raw_api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    raw_session = os.getenv("TELEGRAM_SESSION", DEFAULT_SESSION_NAME).strip()

    if not raw_api_id or not api_hash:
        raise AutomationError(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in the .env file."
        )

    try:
        api_id = int(raw_api_id)
    except ValueError as error:
        raise AutomationError("TELEGRAM_API_ID must be an integer.") from error
    if api_id <= 0:
        raise AutomationError("TELEGRAM_API_ID must be a positive integer.")

    session_value = raw_session or DEFAULT_SESSION_NAME
    session_path = Path(session_value).expanduser()
    if not session_path.is_absolute():
        session_path = config_dir / session_path

    return AppConfig(api_id=api_id, api_hash=api_hash, session_path=session_path)


def create_client(config: AppConfig) -> TelegramClient:
    return TelegramClient(
        str(config.session_path),
        config.api_id,
        config.api_hash,
        flood_sleep_threshold=0,
        receive_updates=False,
    )


def session_file_path(session_path: Path) -> Path:
    if session_path.suffix == ".session":
        return session_path
    return Path(f"{session_path}.session")


def tighten_session_permissions(session_path: Path) -> None:
    primary_path = session_file_path(session_path)
    for candidate in (
        primary_path,
        Path(f"{primary_path}-journal"),
        Path(f"{primary_path}-wal"),
        Path(f"{primary_path}-shm"),
    ):
        if candidate.exists():
            candidate.chmod(0o600)


def parse_poll_link(link: str) -> PollLocation:
    parsed = urlparse(link.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {
        "t.me",
        "www.t.me",
        "telegram.me",
        "www.telegram.me",
    }:
        raise AutomationError(
            "Expected a poll message link in the form https://t.me/...."
        )

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if len(parts) < 2:
        raise AutomationError("The link does not contain a message ID.")

    try:
        message_id = int(parts[-1])
    except ValueError as error:
        raise AutomationError(
            "The final part of the link must be a message ID."
        ) from error
    if message_id <= 0:
        raise AutomationError("The message ID must be a positive integer.")

    if parts[0] == "c":
        if len(parts) < 3 or not parts[1].isdigit():
            raise AutomationError("Invalid private group message link.")
        chat_ref: int | str = int(f"-100{parts[1]}")
    else:
        username = parts[0].lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,}", username):
            raise AutomationError(
                "The link does not contain a valid group username."
            )
        chat_ref = f"@{username}"

    return PollLocation(chat_ref=chat_ref, message_id=message_id)


async def resolve_chat(client: TelegramClient, chat_ref: int | str) -> Any:
    try:
        return await client.get_entity(chat_ref)
    except (TypeError, ValueError) as original_error:
        if isinstance(chat_ref, int):
            async for dialog in client.iter_dialogs():
                if utils.get_peer_id(dialog.entity) == chat_ref:
                    return dialog.entity
        raise AutomationError(
            "Could not find the group. Make sure the account is a member and "
            "the link or ID is correct."
        ) from original_error


def ensure_supported_group(chat: Any) -> None:
    if isinstance(chat, types.Chat):
        if getattr(chat, "deactivated", False):
            raise AutomationError("The group has been deactivated.")
        return
    if (
        isinstance(chat, types.Channel)
        and bool(getattr(chat, "megagroup", False))
        and not bool(getattr(chat, "broadcast", False))
    ):
        return
    raise AutomationError(
        "Only basic groups and supergroups are supported; channels are not."
    )


async def load_poll_context(
    client: TelegramClient,
    chat_ref: int | str,
    message_id: int,
    *,
    require_closed: bool = False,
) -> PollContext:
    chat = await resolve_chat(client, chat_ref)
    ensure_supported_group(chat)

    message = await client.get_messages(chat, ids=message_id)
    if message is None:
        raise AutomationError("No message was found with the specified ID.")
    if not isinstance(getattr(message, "media", None), types.MessageMediaPoll):
        raise AutomationError("The specified message does not contain a poll.")

    poll = message.media.poll
    if not bool(getattr(poll, "public_voters", False)):
        raise AutomationError(
            "The poll is anonymous, so Telegram does not expose voter identities."
        )
    if require_closed and not bool(getattr(poll, "closed", False)):
        raise AutomationError(
            "The poll is still open. Close it before generating the list."
        )
    return PollContext(chat=chat, message=message)


async def fetch_poll_votes(
    client: TelegramClient, chat: Any, message_id: int
) -> PollVotesResult:
    votes: list[Any] = []
    users: list[Any] = []
    offset: str | None = None
    seen_offsets: set[str] = set()
    expected_count: int | None = None

    while True:
        result = await client(
            functions.messages.GetPollVotesRequest(
                peer=chat,
                id=message_id,
                limit=100,
                option=None,
                offset=offset,
            )
        )
        page_count = int(result.count)
        if expected_count is None:
            expected_count = page_count
        elif page_count != expected_count:
            raise AutomationError(
                "The voter count changed while the snapshot was being read. "
                "Run the command again."
            )

        votes.extend(result.votes)
        users.extend(getattr(result, "users", ()))

        next_offset = getattr(result, "next_offset", None) or ""
        if not next_offset:
            break
        if next_offset in seen_offsets:
            raise AutomationError("Telegram returned a repeated vote offset.")
        seen_offsets.add(next_offset)
        offset = next_offset

    return PollVotesResult(
        count=expected_count or 0,
        votes=tuple(votes),
        users=tuple(users),
    )


def display_text(value: Any, *, limit: int | None = 32) -> str:
    if value is None:
        return ""
    normalized = " ".join(str(value).replace("\t", " ").splitlines())
    if limit is not None and len(normalized) > limit:
        return f"{normalized[: limit - 1]}…"
    return normalized


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    print(render(headers))
    print(render(tuple("-" * width for width in widths)))
    for row in rows:
        print(render(row))


def friendly_rpc_error(error: errors.RPCError) -> str:
    messages: Mapping[str, str] = {
        "PollVoteRequiredError": (
            "Telegram did not allow voter retrieval: this account had to vote "
            "in the poll first. The command does not vote automatically."
        ),
        "BroadcastForbiddenError": (
            "Telegram does not allow voter retrieval for a broadcast channel."
        ),
        "MessageIdInvalidError": "Telegram did not find a message with that ID.",
        "ChatAdminRequiredError": (
            "The account does not have the administrator rights required for "
            "this action."
        ),
        "ChannelPrivateError": (
            "The group is unavailable to the account, or the account is no "
            "longer a member."
        ),
        "ChatWriteForbiddenError": (
            "The account cannot perform administrative actions in the group."
        ),
    }
    error_name = type(error).__name__
    return messages.get(error_name, f"Telegram error {error_name}: {error}")
