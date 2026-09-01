from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from telethon import TelegramClient, types

from telegram_automations.common import (
    AutomationError,
    PollLocation,
    display_text,
    fetch_poll_votes,
    parse_poll_link,
    print_table,
)

PollFilterError = AutomationError


@dataclass(frozen=True)
class PollOptionLocation:
    location: PollLocation
    option: bytes


@dataclass(frozen=True)
class TargetRequest:
    location: PollLocation
    answer_text: str | None = None
    option: bytes | None = None


@dataclass(frozen=True)
class VoterRecord:
    id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    selected_options: frozenset[bytes]
    used_input_option: bool = False


@dataclass(frozen=True)
class VoteSnapshot:
    voters: tuple[VoterRecord, ...]
    captured_at: datetime

    @property
    def total_voters(self) -> int:
        return len(self.voters)


def parse_option_link(link: str) -> PollOptionLocation:
    location = parse_poll_link(link)
    parsed = urlparse(link.strip())
    values = parse_qs(parsed.query, keep_blank_values=True).get("option", [])
    if len(values) != 1 or not values[0]:
        raise PollFilterError(
            "The --option link must contain exactly one non-empty option parameter."
        )

    try:
        encoded_option = values[0].encode("ascii")
    except UnicodeEncodeError as error:
        raise PollFilterError(
            "The option parameter must be a base64url value."
        ) from error

    padding = b"=" * (-len(encoded_option) % 4)
    try:
        option = base64.b64decode(
            encoded_option + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise PollFilterError("The option parameter is not valid base64url.") from error
    if not option:
        raise PollFilterError("The decoded poll option must not be empty.")
    return PollOptionLocation(location=location, option=option)


def resolve_target_request(
    *, poll_link: str | None, answer: str | None, option_link: str | None
) -> TargetRequest:
    if option_link is not None:
        if poll_link is not None or answer is not None:
            raise PollFilterError(
                "Use either --option by itself or --poll-link together with --answer."
            )
        parsed_option = parse_option_link(option_link)
        return TargetRequest(
            location=parsed_option.location,
            option=parsed_option.option,
        )

    if poll_link is None or answer is None:
        raise PollFilterError(
            "Provide --option, or provide both --poll-link and --answer."
        )
    return TargetRequest(location=parse_poll_link(poll_link), answer_text=answer)


def text_value(value: Any) -> str:
    return str(getattr(value, "text", value))


def poll_question(poll: Any) -> str:
    return text_value(getattr(poll, "question", ""))


def answer_text(answer: Any) -> str:
    return text_value(getattr(answer, "text", ""))


def select_exact_answer(poll: Any, requested_text: str) -> Any:
    answers = list(getattr(poll, "answers", ()))
    matches = [answer for answer in answers if answer_text(answer) == requested_text]
    if len(matches) == 1:
        return matches[0]

    available = "\n".join(
        f"  {index}. {answer_text(answer)}"
        for index, answer in enumerate(answers, start=1)
    )
    if not available:
        available = "  (none)"
    if not matches:
        detail = "No poll answer exactly matches the supplied text."
    else:
        detail = "More than one poll answer exactly matches the supplied text."
    raise PollFilterError(f"{detail}\nAvailable answers:\n{available}")


def select_option_answer(poll: Any, requested_option: bytes) -> Any:
    matches = [
        answer
        for answer in getattr(poll, "answers", ())
        if bytes(answer.option) == bytes(requested_option)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise PollFilterError(
            "The option from the link does not exist in the referenced poll."
        )
    raise PollFilterError(
        "The poll contains the same option identifier more than once."
    )


def vote_options(vote: Any) -> frozenset[bytes]:
    if isinstance(vote, types.MessagePeerVote):
        return frozenset((bytes(vote.option),))
    if isinstance(vote, types.MessagePeerVoteMultiple):
        return frozenset(bytes(option) for option in vote.options)
    if isinstance(vote, types.MessagePeerVoteInputOption):
        return frozenset()
    raise PollFilterError(
        f"Telegram returned an unsupported vote format: {type(vote).__name__}."
    )


def _user_record(
    user: Any,
    selected_options: frozenset[bytes],
    *,
    used_input_option: bool,
) -> VoterRecord:
    return VoterRecord(
        id=int(user.id),
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        selected_options=selected_options,
        used_input_option=used_input_option,
    )


async def fetch_vote_snapshot(
    client: TelegramClient, chat: Any, message_id: int
) -> VoteSnapshot:
    selected_options_by_user: dict[int, frozenset[bytes]] = {}
    input_option_user_ids: set[int] = set()
    users_by_id: dict[int, Any] = {}
    result = await fetch_poll_votes(client, chat, message_id)

    for user in result.users:
        users_by_id[int(user.id)] = user

    for vote in result.votes:
        peer = getattr(vote, "peer", None)
        if not isinstance(peer, types.PeerUser):
            raise PollFilterError(
                "A vote submitted as a channel or another chat was found. "
                "It cannot be safely associated with a person."
            )
        user_id = int(peer.user_id)
        if user_id in selected_options_by_user:
            raise PollFilterError(
                "Telegram returned the same voter more than once. "
                "The snapshot may have changed; run the command again."
            )
        selected_options_by_user[user_id] = vote_options(vote)
        if isinstance(vote, types.MessagePeerVoteInputOption):
            input_option_user_ids.add(user_id)

    if len(selected_options_by_user) != result.count:
        raise PollFilterError(
            "Incomplete voter list: received "
            f"{len(selected_options_by_user)} of {result.count}."
        )

    missing_user_ids = sorted(set(selected_options_by_user) - set(users_by_id))
    if missing_user_ids:
        preview = ", ".join(str(user_id) for user_id in missing_user_ids[:10])
        suffix = "..." if len(missing_user_ids) > 10 else ""
        raise PollFilterError(
            f"Telegram did not provide profile data for voter IDs: {preview}{suffix}."
        )

    voters = tuple(
        _user_record(
            users_by_id[user_id],
            selected_options_by_user[user_id],
            used_input_option=user_id in input_option_user_ids,
        )
        for user_id in sorted(selected_options_by_user)
    )
    return VoteSnapshot(voters=voters, captured_at=datetime.now(timezone.utc))


def voters_without_answer(
    snapshot: VoteSnapshot, target_option: bytes
) -> tuple[VoterRecord, ...]:
    return tuple(
        voter
        for voter in snapshot.voters
        if bytes(target_option) not in voter.selected_options
    )


def voters_with_answer_count(snapshot: VoteSnapshot, target_option: bytes) -> int:
    option = bytes(target_option)
    return sum(option in voter.selected_options for voter in snapshot.voters)


def voter_answer_text(voter: VoterRecord, poll: Any) -> str:
    answers = list(getattr(poll, "answers", ()))
    known_options = {bytes(answer.option) for answer in answers}
    unknown_options = voter.selected_options - known_options
    if unknown_options:
        raise PollFilterError(
            f"Poll answer text is unavailable for voter ID {voter.id}. "
            "The poll may have changed; run the command again."
        )

    selected_texts = [
        answer_text(answer)
        for answer in answers
        if bytes(answer.option) in voter.selected_options
    ]
    if voter.used_input_option:
        selected_texts.append("[free-text answer; text unavailable]")
    return " | ".join(selected_texts) or "[no answer text available]"


def print_voter_table(
    voters: Sequence[VoterRecord], *, poll: Any | None = None
) -> None:
    headers = ["ID", "username", "first name", "last name"]
    if poll is not None:
        headers.append("voted for")

    rows: list[tuple[str, ...]] = []
    for voter in voters:
        row = [
            str(voter.id),
            display_text(voter.username),
            display_text(voter.first_name),
            display_text(voter.last_name),
        ]
        if poll is not None:
            row.append(display_text(voter_answer_text(voter, poll), limit=None))
        rows.append(tuple(row))
    print_table(headers, rows)
