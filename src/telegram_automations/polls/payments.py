from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

from telethon import TelegramClient, errors, helpers, types, utils

from telegram_automations.common import (
    AutomationError,
    ensure_supported_group,
    parse_poll_link,
    resolve_chat,
)
from telegram_automations.polls.answer_filter import VoteSnapshot

USERNAME_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{1,32})(?!\w)")
IDENTITY_ERRORS = (
    ValueError,
    errors.UsernameInvalidError,
    errors.UsernameNotOccupiedError,
    errors.UserIdInvalidError,
    errors.PeerIdInvalidError,
)


@dataclass(frozen=True)
class TopicMessageLocation:
    chat_ref: int | str
    topic_id: int
    message_id: int


@dataclass(frozen=True)
class TopicSnapshot:
    chat: Any
    location: TopicMessageLocation
    messages: tuple[Any, ...]
    upper_message_id: int
    captured_at: datetime
    start_date: datetime


@dataclass(frozen=True)
class PaymentUser:
    id: int
    username: str | None
    first_name: str | None
    last_name: str | None


@dataclass(frozen=True)
class PaymentEvidence:
    message_id: int
    message_url: str
    sender_id: int | None
    kind: str
    mention: str | None = None


@dataclass(frozen=True)
class PaymentRecord:
    user: PaymentUser
    evidence: tuple[PaymentEvidence, ...]


@dataclass(frozen=True)
class ReviewItem:
    message_url: str
    detail: str


@dataclass(frozen=True)
class PaymentReport:
    missing: tuple[PaymentUser, ...]
    paid_without_option: tuple[PaymentRecord, ...]
    paid_with_option: tuple[PaymentRecord, ...]
    review: tuple[ReviewItem, ...]


@dataclass(frozen=True)
class UserMention:
    text: str
    user_id: int | None = None


def parse_topic_message_link(link: str) -> TopicMessageLocation:
    location = parse_poll_link(link)
    parts = [unquote(part) for part in urlparse(link.strip()).path.split("/") if part]
    private = parts[0] == "c"
    if len(parts) != (4 if private else 3):
        raise AutomationError(
            "Expected a forum message link containing both topic and message IDs: "
            "https://t.me/c/GROUP/TOPIC/MESSAGE or https://t.me/GROUP/TOPIC/MESSAGE."
        )
    if not parts[-2].isdigit() or int(parts[-2]) <= 0:
        raise AutomationError("The topic ID must be a positive integer.")
    topic_id = int(parts[-2])
    if location.message_id < topic_id:
        raise AutomationError("The start message cannot precede the topic root.")
    return TopicMessageLocation(location.chat_ref, topic_id, location.message_id)


def message_topic_id(message: Any) -> int | None:
    if isinstance(getattr(message, "action", None), types.MessageActionTopicCreate):
        return int(message.id)
    reply = getattr(message, "reply_to", None)
    if not bool(getattr(reply, "forum_topic", False)):
        return None
    return getattr(reply, "reply_to_top_id", None) or getattr(
        reply, "reply_to_msg_id", None
    )


def topic_message_url(topic: TopicSnapshot, message_id: int) -> str:
    # Use the numeric group ID so evidence survives a public username change.
    return f"https://t.me/c/{topic.chat.id}/{topic.location.topic_id}/{message_id}"


async def load_topic_snapshot(
    client: TelegramClient, link: str, poll_chat: Any
) -> TopicSnapshot:
    location = parse_topic_message_link(link)
    chat = await resolve_chat(client, location.chat_ref)
    ensure_supported_group(chat)
    if utils.get_peer_id(chat) != utils.get_peer_id(poll_chat):
        raise AutomationError("The poll and the message topic must be in the same group.")
    if not bool(getattr(chat, "forum", False)):
        raise AutomationError("The message link must point to a forum group.")

    root, start = await client.get_messages(
        chat, ids=[location.topic_id, location.message_id]
    )
    if not isinstance(getattr(root, "action", None), types.MessageActionTopicCreate):
        raise AutomationError("The specified forum topic was not found.")
    if start is None or isinstance(start, types.MessageEmpty):
        raise AutomationError("The start message was not found.")
    if message_topic_id(start) != location.topic_id:
        raise AutomationError("The start message does not belong to the specified topic.")

    latest = await client.get_messages(chat, limit=1, reply_to=location.topic_id)
    upper_id = latest[0].id if latest else location.topic_id
    captured_at = datetime.now(timezone.utc)
    if upper_id < location.message_id:
        raise AutomationError("Incomplete topic history: the start message is missing.")

    messages: dict[int, Any] = {}
    async for message in client.iter_messages(
        chat,
        reply_to=location.topic_id,
        min_id=location.message_id,
        max_id=upper_id + 1,
        limit=None,
    ):
        if not location.message_id < message.id <= upper_id:
            continue
        if message_topic_id(message) != location.topic_id:
            continue
        if message.id in messages:
            raise AutomationError(
                "Telegram returned a duplicate topic message. Run the command again."
            )
        messages[message.id] = message
    if upper_id > location.message_id and upper_id not in messages:
        raise AutomationError(
            "Incomplete topic history: a boundary message disappeared. "
            "Run the command again."
        )
    return TopicSnapshot(
        chat=chat,
        location=location,
        messages=tuple(messages[key] for key in sorted(messages)),
        upper_message_id=upper_id,
        captured_at=captured_at,
        start_date=start.date,
    )


def extract_mentions(message: Any) -> tuple[UserMention, ...]:
    text = getattr(message, "message", None) or ""
    # Telegram entity offsets are UTF-16 units, not Python character offsets.
    surrogate_text = helpers.add_surrogate(text)
    masked_text = list(surrogate_text)
    mentions: dict[tuple[str, int | str], UserMention] = {}
    for entity in getattr(message, "entities", None) or ():
        if isinstance(entity, types.MessageEntityMentionName):
            end = entity.offset + entity.length
            label = helpers.del_surrogate(surrogate_text[entity.offset:end])
            mentions[("id", entity.user_id)] = UserMention(label, entity.user_id)
            masked_text[entity.offset:end] = " " * entity.length
    for match in USERNAME_MENTION.finditer("".join(masked_text)):
        label = match.group(0)
        mentions[("username", label.lower())] = UserMention(label)
    return tuple(mentions.values())


def payment_user(user: Any) -> PaymentUser:
    return PaymentUser(
        id=int(user.id),
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
    )


class _IdentityResolver:
    def __init__(self, client: TelegramClient, users: Sequence[Any]) -> None:
        self.client = client
        self.users: dict[int, PaymentUser] = {}
        self.usernames: dict[str, set[int]] = {}
        self.failed_usernames: set[str] = set()
        self.missing_profiles: set[int] = set()
        for user in users:
            self.remember(user)

    def remember(self, user: Any) -> PaymentUser:
        record = payment_user(user)
        self.users[record.id] = record
        aliases = [record.username] + [
            alias.username
            for alias in getattr(user, "usernames", None) or ()
            if alias.active
        ]
        for alias in aliases:
            if alias:
                self.usernames.setdefault(alias.lower(), set()).add(record.id)
        return record

    async def by_id(self, user_id: int) -> PaymentUser:
        if user_id in self.users:
            return self.users[user_id]
        try:
            user = await self.client.get_entity(types.PeerUser(user_id))
        except IDENTITY_ERRORS:
            user = None
        if isinstance(user, types.User) and user.id == user_id:
            return self.remember(user)
        self.missing_profiles.add(user_id)
        record = PaymentUser(user_id, None, None, None)
        self.users[user_id] = record
        return record

    async def by_username(self, username: str) -> PaymentUser | None:
        key = username.lstrip("@").lower()
        if key in self.usernames:
            ids = self.usernames[key]
            return self.users[next(iter(ids))] if len(ids) == 1 else None
        if key in self.failed_usernames:
            return None
        try:
            user = await self.client.get_entity(f"@{key}")
        except IDENTITY_ERRORS:
            user = None
        if not isinstance(user, types.User):
            self.failed_usernames.add(key)
            return None
        record = self.remember(user)
        self.usernames.setdefault(key, set()).add(record.id)
        return record


def partition_payments(
    selected_ids: set[int],
    users: dict[int, PaymentUser],
    evidence: dict[int, list[PaymentEvidence]],
    review: Sequence[ReviewItem],
) -> PaymentReport:
    def records(ids: set[int]) -> tuple[PaymentRecord, ...]:
        return tuple(
            PaymentRecord(users[user_id], tuple(evidence[user_id]))
            for user_id in sorted(ids)
        )

    paid_ids = set(evidence)
    return PaymentReport(
        missing=tuple(users[user_id] for user_id in sorted(selected_ids - paid_ids)),
        paid_without_option=records(paid_ids - selected_ids),
        paid_with_option=records(paid_ids & selected_ids),
        review=tuple(dict.fromkeys(review)),
    )


async def reconcile_payments(
    client: TelegramClient,
    votes: VoteSnapshot,
    target_option: bytes,
    topic: TopicSnapshot,
) -> PaymentReport:
    messages = [
        message
        for message in sorted(topic.messages, key=lambda message: message.id)
        if isinstance(message, types.Message)
        and topic.location.message_id < message.id <= topic.upper_message_id
        and message_topic_id(message) == topic.location.topic_id
    ]
    known_users = list(votes.voters)
    known_users.extend(
        message.sender
        for message in messages
        if isinstance(message.sender, types.User)
    )
    resolver = _IdentityResolver(client, known_users)
    selected_ids = {
        voter.id for voter in votes.voters if target_option in voter.selected_options
    }
    evidence: dict[int, list[PaymentEvidence]] = {}
    review: list[ReviewItem] = []
    for message in messages:
        url = topic_message_url(topic, message.id)
        from_peer = message.from_id
        sender_id = from_peer.user_id if isinstance(from_peer, types.PeerUser) else None
        if sender_id is None:
            review.append(ReviewItem(url, "The sender cannot be identified as a user."))
        else:
            await resolver.by_id(sender_id)
            evidence.setdefault(sender_id, []).append(
                PaymentEvidence(message.id, url, sender_id, "sender")
            )

        for mention in extract_mentions(message):
            if mention.user_id is not None:
                user = await resolver.by_id(mention.user_id)
            else:
                user = await resolver.by_username(mention.text)
            if user is None:
                review.append(ReviewItem(url, f"Could not resolve mention {mention.text}."))
                continue
            item = PaymentEvidence(message.id, url, sender_id, "mention", mention.text)
            user_evidence = evidence.setdefault(user.id, [])
            if item not in user_evidence:
                user_evidence.append(item)

    for user_id in sorted(resolver.missing_profiles):
        for item in evidence.get(user_id, ()):
            review.append(
                ReviewItem(
                    item.message_url,
                    f"Profile unavailable for user ID {user_id}; credited by Telegram ID.",
                )
            )
    return partition_payments(selected_ids, resolver.users, evidence, review)
