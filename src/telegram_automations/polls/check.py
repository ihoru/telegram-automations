from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telethon import functions, types

from telegram_automations.common import AutomationError, display_text
from telegram_automations.polls.answer_filter import answer_text, poll_question


@dataclass(frozen=True)
class OptionCount:
    text: str
    voters: int


def select_options(poll: Any, results: Any, minimum: int) -> tuple[OptionCount, ...]:
    """Require a complete aggregate snapshot; never guess missing counts."""
    if getattr(results, "results", None) is None:
        message = "Telegram did not return vote counts for this poll."
        if not getattr(poll, "closed", False):
            if getattr(poll, "hide_results_until_close", False):
                message += " Results are hidden until the poll closes."
            else:
                message += (
                    " You may need to vote first using the same Telegram account "
                    "as this script, then run the command again."
                )
        message += " The script does not vote automatically."
        raise AutomationError(message)
    if getattr(results, "min", False):
        raise AutomationError("Telegram returned incomplete poll results. Try again later.")
    counts: dict[bytes, int] = {}
    for result in results.results:
        count = result.voters
        option = bytes(result.option)
        if type(count) is not int or count < 0 or option in counts:
            raise AutomationError("Poll results contain invalid or duplicate counts.")
        counts[option] = count
    options = [bytes(answer.option) for answer in poll.answers]
    if len(set(options)) != len(options) or set(counts) != set(options):
        raise AutomationError(
            "Poll results are incomplete or do not match the answers."
        )
    return tuple(
        OptionCount(answer_text(answer), counts[bytes(answer.option)])
        for answer in poll.answers
        if counts[bytes(answer.option)] >= minimum
    )


async def fetch_options(
    client: Any, context: Any, minimum: int
) -> tuple[Any, tuple[OptionCount, ...]]:
    updates = await client(
        functions.messages.GetPollResultsRequest(
            peer=context.chat,
            msg_id=context.message.id,
            poll_hash=context.poll.hash,
        )
    )
    entries = getattr(updates, "updates", None)
    if entries is None:
        entries = [getattr(updates, "update", None)]
    matches = [
        update
        for update in entries
        if isinstance(update, types.UpdateMessagePoll)
        and update.poll_id == context.poll.id
    ]
    if len(matches) != 1:
        raise AutomationError(
            "Telegram did not return a complete result for this poll."
        )
    update = matches[0]
    poll = update.poll or context.poll
    return poll, select_options(poll, update.results, minimum)


def format_report(
    poll: Any, link: str, options: tuple[OptionCount, ...], minimum: int
) -> str:
    lines = [
        f"Poll: {display_text(poll_question(poll), limit=None)}",
        link.strip(),
        "",
        f"Options with at least {minimum} votes:",
    ]
    lines.extend(
        f"• {display_text(option.text, limit=None)} — {option.voters} votes"
        for option in options
    )
    return "\n".join(lines)


def split_message(text: str, limit: int = 4096) -> tuple[str, ...]:
    """Split on lines where possible, respecting Telegram's UTF-16 limit."""
    chunks: list[str] = []
    remaining = text
    while remaining:
        units = 0
        end = 0
        for character in remaining:
            size = 2 if ord(character) > 0xFFFF else 1
            if units + size > limit:
                break
            units += size
            end += 1
        if end == 0:
            raise ValueError("Message limit is too small.")
        if end < len(remaining):
            newline = remaining.rfind("\n", 0, end)
            if newline >= 0:
                end = newline + 1
        chunks.append(remaining[:end])
        remaining = remaining[end:]
    return tuple(chunks)


async def send_report(client: Any, user: str, report: str) -> None:
    try:
        recipient = await client.get_entity(user)
    except (TypeError, ValueError) as error:
        raise AutomationError("Could not resolve the recipient user.") from error
    if not isinstance(recipient, types.User) or recipient.deleted:
        raise AutomationError("The recipient must be an available Telegram user.")
    for chunk in split_message(report):
        await client.send_message(recipient, chunk, parse_mode=None, link_preview=False)
