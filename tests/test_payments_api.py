from __future__ import annotations

import unittest
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from typing import Any

from telethon import errors, types

from telegram_automations.common import AutomationError
from telegram_automations.polls.payments import load_topic_snapshot

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
CHAT_ID = 1234567890
TOPIC_ID = 6865
START_ID = 6866
LINK = "https://t.me/c/1234567890/6865/6866"


def chat() -> types.Channel:
    return types.Channel(
        id=CHAT_ID,
        title="Bath group",
        photo=types.ChatPhotoEmpty(),
        date=NOW,
        megagroup=True,
        forum=True,
    )


def message(message_id: int, *, topic_id: int = TOPIC_ID) -> types.Message:
    return types.Message(
        id=message_id,
        peer_id=types.PeerChannel(CHAT_ID),
        from_id=types.PeerUser(1),
        date=NOW,
        message="receipt",
        reply_to=types.MessageReplyHeader(
            reply_to_msg_id=topic_id,
            forum_topic=True,
        ),
    )


def root_message() -> types.MessageService:
    return types.MessageService(
        id=TOPIC_ID,
        peer_id=types.PeerChannel(CHAT_ID),
        from_id=types.PeerUser(1),
        date=NOW,
        action=types.MessageActionTopicCreate(title="Receipts", icon_color=0),
    )


class TopicClient:
    def __init__(
        self,
        history: Sequence[types.Message],
        *,
        start: types.Message | None = None,
        latest: types.Message | None = None,
    ) -> None:
        self.chat = chat()
        self.root = root_message()
        self.history = list(history)
        self.messages = {item.id: item for item in self.history}
        self.messages[TOPIC_ID] = self.root
        if start is not None:
            self.messages[start.id] = start
        self.latest = latest
        if latest is None and history:
            self.latest = max(history, key=lambda item: item.id)
        self.reads: list[dict[str, Any]] = []
        self.iterations: list[dict[str, Any]] = []

    async def get_entity(self, reference: Any) -> types.Channel:
        return self.chat

    async def get_messages(self, peer: Any, **kwargs: Any) -> Any:
        self.reads.append(kwargs)
        if "ids" in kwargs:
            ids = kwargs["ids"]
            if isinstance(ids, int):
                return self.messages.get(ids)
            return [self.messages.get(message_id) for message_id in ids]
        return [self.latest] if self.latest is not None else []

    async def iter_messages(self, peer: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self.iterations.append(kwargs)
        for item in self.history:
            yield item


class TopicSnapshotAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_exclusive_start_inclusive_end_nested_replies_and_other_topics(self) -> None:
        start = message(START_ID)
        nested_reply = message(START_ID + 1)
        nested_reply.reply_to = types.MessageReplyHeader(
            reply_to_msg_id=START_ID,
            reply_to_top_id=TOPIC_ID,
            forum_topic=True,
        )
        upper = message(START_ID + 3)
        service = types.MessageService(
            id=START_ID + 2,
            peer_id=types.PeerChannel(CHAT_ID),
            date=NOW,
            action=types.MessageActionPinMessage(),
            reply_to=types.MessageReplyHeader(
                reply_to_msg_id=TOPIC_ID, forum_topic=True
            ),
        )
        client = TopicClient(
            [
                message(START_ID + 100),
                upper,
                message(START_ID + 2, topic_id=6000),
                service,
                nested_reply,
                start,
                message(START_ID - 1),
            ],
            latest=upper,
        )

        snapshot = await load_topic_snapshot(client, LINK, client.chat)

        self.assertEqual(
            [item.id for item in snapshot.messages],
            [START_ID + 1, START_ID + 2, START_ID + 3],
        )
        self.assertEqual(snapshot.upper_message_id, upper.id)
        self.assertEqual(snapshot.start_date, NOW)
        self.assertIsNotNone(snapshot.captured_at.tzinfo)
        self.assertEqual(client.reads[0]["ids"], [TOPIC_ID, START_ID])
        self.assertEqual(client.reads[1], {"limit": 1, "reply_to": TOPIC_ID})
        self.assertEqual(
            client.iterations,
            [
                {
                    "reply_to": TOPIC_ID,
                    "min_id": START_ID,
                    "max_id": upper.id + 1,
                    "limit": None,
                }
            ],
        )

    async def test_iterator_consumes_more_than_one_telegram_page(self) -> None:
        history = [
            message(message_id) for message_id in range(START_ID, START_ID + 205)
        ]
        client = TopicClient(list(reversed(history)))

        snapshot = await load_topic_snapshot(client, LINK, client.chat)

        self.assertEqual(len(snapshot.messages), 204)
        self.assertEqual(
            [item.id for item in snapshot.messages], [item.id for item in history[1:]]
        )
        self.assertEqual(client.iterations[0]["limit"], None)

    async def test_topic_root_can_be_the_excluded_start(self) -> None:
        client = TopicClient([message(START_ID)])

        snapshot = await load_topic_snapshot(
            client, "https://t.me/c/1234567890/6865/6865", client.chat
        )

        self.assertEqual([item.id for item in snapshot.messages], [START_ID])

    async def test_rejects_missing_or_wrong_start_and_invalid_topic_root(self) -> None:
        cases = {
            "missing start": TopicClient([message(START_ID + 1)]),
            "other topic": TopicClient([message(START_ID, topic_id=6000)]),
            "missing topic": TopicClient([message(START_ID)]),
            "not a topic root": TopicClient([message(START_ID)]),
        }
        del cases["missing topic"].messages[TOPIC_ID]
        cases["not a topic root"].messages[TOPIC_ID] = message(TOPIC_ID)

        for name, client in cases.items():
            with self.subTest(name=name), self.assertRaises(AutomationError):
                await load_topic_snapshot(client, LINK, client.chat)

    async def test_only_anchor_or_empty_topic_produces_empty_message_snapshot(self) -> None:
        for client, link in (
            (TopicClient([message(START_ID)]), LINK),
            (TopicClient([]), "https://t.me/c/1234567890/6865/6865"),
        ):
            with self.subTest(link=link):
                snapshot = await load_topic_snapshot(client, link, client.chat)
                self.assertEqual(snapshot.messages, ())

    async def test_excluded_anchor_need_not_be_returned_by_history_iterator(self) -> None:
        start, upper = message(START_ID), message(START_ID + 1)
        client = TopicClient([upper], start=start)

        snapshot = await load_topic_snapshot(client, LINK, client.chat)

        self.assertEqual([item.id for item in snapshot.messages], [upper.id])

    async def test_rejects_different_poll_group_and_non_forum_groups(self) -> None:
        client = TopicClient([message(START_ID)])
        other_chat = chat()
        other_chat.id += 1
        with self.assertRaises(AutomationError):
            await load_topic_snapshot(client, LINK, other_chat)

        client.chat.forum = False
        with self.assertRaises(AutomationError):
            await load_topic_snapshot(client, LINK, client.chat)

    async def test_rejects_duplicate_and_incomplete_snapshot_boundaries(self) -> None:
        start, upper = message(START_ID), message(START_ID + 1)
        cases = {
            "duplicate": TopicClient([upper, upper, start]),
            "missing upper from iterator": TopicClient([start], latest=upper),
        }

        for name, client in cases.items():
            with self.subTest(name=name), self.assertRaises(AutomationError):
                await load_topic_snapshot(client, LINK, client.chat)

    async def test_flood_wait_during_history_load_stops_snapshot(self) -> None:
        class LimitedClient(TopicClient):
            async def iter_messages(
                self, peer: Any, **kwargs: Any
            ) -> AsyncIterator[Any]:
                yield self.history[0]
                raise errors.FloodWaitError(request=None, capture=30)

        client = LimitedClient([message(START_ID + 1), message(START_ID)])
        with self.assertRaises(errors.FloodWaitError):
            await load_topic_snapshot(client, LINK, client.chat)


if __name__ == "__main__":
    unittest.main()
