from __future__ import annotations

import unittest
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from telethon import errors, types

from telegram_automations.common import AutomationError
from telegram_automations.polls.answer_filter import VoterRecord, VoteSnapshot
from telegram_automations.polls.payments import (
    TopicMessageLocation,
    TopicSnapshot,
    parse_topic_message_link,
    reconcile_payments,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
CHAT_ID = 1234567890
TOPIC_ID = 6865
START_ID = 6866


def user(user_id: int, username: str | None = None) -> types.User:
    return types.User(
        id=user_id,
        first_name=f"Person {user_id}",
        username=username,
    )


def message(
    message_id: int,
    sender: types.User | None,
    text: str = "",
    **kwargs: Any,
) -> types.Message:
    kwargs.setdefault(
        "reply_to", types.MessageReplyHeader(reply_to_msg_id=TOPIC_ID, forum_topic=True)
    )
    result = types.Message(
        id=message_id,
        peer_id=types.PeerChannel(CHAT_ID),
        from_id=types.PeerUser(sender.id) if sender is not None else None,
        date=NOW,
        message=text,
        **kwargs,
    )
    result._sender = sender
    return result


def votes(
    selected: Sequence[types.User], others: Sequence[types.User] = ()
) -> VoteSnapshot:
    return VoteSnapshot(
        voters=tuple(
            VoterRecord(
                id=person.id,
                username=person.username,
                first_name=person.first_name,
                last_name=person.last_name,
                selected_options=frozenset({b"yes" if person in selected else b"no"}),
            )
            for person in (*selected, *others)
        ),
        captured_at=NOW,
    )


def topic(messages: Sequence[types.Message]) -> TopicSnapshot:
    return TopicSnapshot(
        chat=types.Channel(
            id=CHAT_ID,
            title="Bath group",
            photo=types.ChatPhotoEmpty(),
            date=NOW,
            forum=True,
            megagroup=True,
        ),
        location=TopicMessageLocation(-1001234567890, TOPIC_ID, START_ID - 1),
        messages=tuple(messages),
        upper_message_id=max((item.id for item in messages), default=START_ID),
        captured_at=NOW,
        start_date=NOW,
    )


class EntityClient:
    def __init__(self, users: Sequence[types.User]) -> None:
        self.users = {person.id: person for person in users}
        self.requests: list[Any] = []

    async def get_entity(self, reference: Any) -> Any:
        self.requests.append(reference)
        if isinstance(reference, types.User):
            return reference
        if isinstance(reference, str):
            username = reference.lstrip("@").lower()
            for person in self.users.values():
                if person.username and person.username.lower() == username:
                    return person
            raise ValueError(f"Unknown username: {reference}")
        user_id = getattr(reference, "user_id", reference)
        if user_id in self.users:
            return self.users[user_id]
        raise ValueError("Unknown user")


class TopicMessageLinkTests(unittest.TestCase):
    def test_parser_preserves_chat_topic_and_message(self) -> None:
        cases = (
            (
                "https://t.me/c/1234567890/6865/6866",
                TopicMessageLocation(-1001234567890, 6865, 6866),
            ),
            (
                "https://telegram.me/bath_group/77/81?single",
                TopicMessageLocation("@bath_group", 77, 81),
            ),
        )
        for link, expected in cases:
            with self.subTest(link=link):
                self.assertEqual(parse_topic_message_link(link), expected)

    def test_parser_rejects_missing_topic_and_invalid_identifiers(self) -> None:
        links = (
            "https://example.com/c/1234567890/6865/6866",
            "https://t.me/c/1234567890/6866",
            "https://t.me/bath_group/6866",
            "https://t.me/c/invalid/6865/6866",
            "https://t.me/c/1234567890/0/6866",
            "https://t.me/c/1234567890/6865/0",
            "https://t.me/c/1234567890/6865/not-a-number",
            "https://t.me/c/1234567890/6865/6866/6867",
            "https://t.me/+invite/6865/6866",
        )
        for link in links:
            with self.subTest(link=link), self.assertRaises(AutomationError):
                parse_topic_message_link(link)


class ReconcilePaymentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_message_credits_neither_author_nor_mentions(self) -> None:
        a, b, c = user(1, "alice"), user(2, "bob"), user(3, "carol")
        snapshot = topic(
            [
                message(START_ID, a, "Instructions for @bob and @unknown"),
                message(START_ID + 1, c, media=types.MessageMediaPhoto()),
            ]
        )
        snapshot = replace(
            snapshot, location=replace(snapshot.location, message_id=START_ID)
        )

        report = await reconcile_payments(
            EntityClient([a, b, c]), votes([a, b, c]), b"yes", snapshot
        )

        self.assertEqual({person.id for person in report.missing}, {a.id, b.id})
        self.assertEqual([record.user.id for record in report.paid_with_option], [c.id])
        self.assertEqual(report.paid_without_option, ())
        self.assertEqual(report.review, ())

    async def test_user_example_partitions_voters_and_credits_payment_for_other(
        self,
    ) -> None:
        a, b, c, d, f = [user(index, name) for index, name in enumerate("abcdf", 1)]
        snapshot = topic(
            [
                message(START_ID, a, "за двоих @b"),
                message(START_ID + 1, c, media=types.MessageMediaPhoto()),
                message(START_ID + 2, f, "paid"),
            ]
        )

        report = await reconcile_payments(
            EntityClient([a, b, c, d, f]), votes([a, b, c, d]), b"yes", snapshot
        )

        self.assertEqual({person.id for person in report.missing}, {d.id})
        self.assertEqual(
            {record.user.id for record in report.paid_without_option}, {f.id}
        )
        self.assertEqual(
            {record.user.id for record in report.paid_with_option}, {a.id, b.id, c.id}
        )
        b_record = next(
            record for record in report.paid_with_option if record.user.id == b.id
        )
        self.assertEqual(len(b_record.evidence), 1)
        self.assertEqual(b_record.evidence[0].kind, "mention")
        self.assertEqual(b_record.evidence[0].sender_id, a.id)
        self.assertEqual(b_record.evidence[0].message_id, START_ID)
        self.assertEqual(
            b_record.evidence[0].message_url,
            "https://t.me/c/1234567890/6865/6866",
        )
        self.assertEqual(report.review, ())

    async def test_caption_mentions_and_repeated_messages_keep_unique_people(
        self,
    ) -> None:
        a, b = user(1, "alice"), user(2, "bob")
        client = EntityClient([a, b])
        snapshot = topic(
            [
                message(
                    START_ID,
                    a,
                    "За @BOB, @bob и @alice",
                    media=types.MessageMediaDocument(),
                ),
                message(START_ID + 1, a, "Ещё @Bob"),
                message(START_ID + 2, b, media=types.MessageMediaDocument()),
            ]
        )

        report = await reconcile_payments(client, votes([a, b]), b"yes", snapshot)

        self.assertEqual(len(report.paid_with_option), 2)
        self.assertEqual(report.missing, ())
        self.assertEqual(report.paid_without_option, ())
        records = {record.user.id: record for record in report.paid_with_option}
        self.assertEqual(
            {evidence.message_id for evidence in records[b.id].evidence},
            {START_ID, START_ID + 1, START_ID + 2},
        )
        b_mentions = [
            evidence
            for evidence in records[b.id].evidence
            if evidence.kind == "mention"
        ]
        self.assertEqual(len(b_mentions), 2)
        resolutions = [
            reference
            for reference in client.requests
            if isinstance(reference, str) and reference.lstrip("@").lower() == "bob"
        ]
        self.assertLessEqual(len(resolutions), 1)

    async def test_named_mentions_after_emoji_resolve_people_without_username(
        self,
    ) -> None:
        a, b, c = user(1, "alice"), user(2), user(3, "carol")
        text = "🔥 Борис @carol"
        snapshot = topic(
            [
                message(
                    START_ID,
                    a,
                    text,
                    entities=[
                        types.MessageEntityMentionName(
                            offset=3, length=5, user_id=b.id
                        ),
                        types.MessageEntityMention(offset=9, length=6),
                    ],
                ),
            ]
        )

        report = await reconcile_payments(
            EntityClient([a, b, c]), votes([a, b, c]), b"yes", snapshot
        )

        self.assertEqual(report.missing, ())
        self.assertEqual(
            {record.user.id for record in report.paid_with_option}, {a.id, b.id, c.id}
        )
        named = next(
            record for record in report.paid_with_option if record.user.id == b.id
        )
        self.assertIsNone(named.user.username)
        self.assertEqual(named.evidence[0].sender_id, a.id)
        self.assertEqual(report.review, ())

    async def test_mentions_are_partitioned_independently_from_sender(self) -> None:
        a, b, f, g = [user(index, name) for index, name in enumerate("abfg", 1)]
        snapshot = topic(
            [
                message(START_ID, a, "@f"),
                message(START_ID + 1, g, "@b"),
            ]
        )

        report = await reconcile_payments(
            EntityClient([a, b, f, g]), votes([a, b], [f]), b"yes", snapshot
        )

        self.assertEqual(report.missing, ())
        self.assertEqual(
            {record.user.id for record in report.paid_without_option}, {f.id, g.id}
        )
        self.assertEqual(
            {record.user.id for record in report.paid_with_option}, {a.id, b.id}
        )

    async def test_named_mention_id_takes_precedence_over_its_displayed_handle(
        self,
    ) -> None:
        a, b, c = user(1, "alice"), user(2, "bob"), user(3, "carol")
        receipt = message(
            START_ID,
            a,
            "🔥 @carol",
            entities=[types.MessageEntityMentionName(offset=3, length=6, user_id=b.id)],
        )

        report = await reconcile_payments(
            EntityClient([a, b, c]), votes([a, b, c]), b"yes", topic([receipt])
        )

        self.assertEqual([person.id for person in report.missing], [c.id])
        self.assertEqual(
            {record.user.id for record in report.paid_with_option}, {a.id, b.id}
        )

    async def test_unavailable_profile_retains_known_telegram_id_and_warns(
        self,
    ) -> None:
        a = user(1, "alice")
        receipt = message(
            START_ID,
            a,
            "За Бориса",
            entities=[types.MessageEntityMentionName(offset=3, length=6, user_id=77)],
        )

        report = await reconcile_payments(
            EntityClient([a]), votes([a]), b"yes", topic([receipt])
        )

        self.assertEqual(
            [record.user.id for record in report.paid_without_option], [77]
        )
        self.assertIsNone(report.paid_without_option[0].user.username)
        self.assertEqual(len(report.review), 1)
        self.assertIn("77", report.review[0].detail)

    async def test_unresolved_mentions_remain_review_items_with_evidence(self) -> None:
        a, b = user(1, "alice"), user(2, "bob")
        snapshot = topic([message(START_ID, a, "@unknown @UNKNOWN")])

        report = await reconcile_payments(
            EntityClient([a, b]), votes([a, b]), b"yes", snapshot
        )

        self.assertEqual({person.id for person in report.missing}, {b.id})
        self.assertEqual(len(report.review), 1)
        self.assertIn("unknown", report.review[0].detail.lower())
        self.assertEqual(
            report.review[0].message_url, "https://t.me/c/1234567890/6865/6866"
        )

    async def test_non_user_author_is_reviewed_but_valid_mention_counts(self) -> None:
        a = user(1, "alice")
        anonymous = message(START_ID, None, "За @alice")
        anonymous.from_id = types.PeerChannel(CHAT_ID)

        report = await reconcile_payments(
            EntityClient([a]), votes([a]), b"yes", topic([anonymous])
        )

        self.assertEqual(report.missing, ())
        self.assertEqual([record.user.id for record in report.paid_with_option], [a.id])
        self.assertEqual(report.paid_without_option, ())
        self.assertEqual(len(report.review), 1)
        self.assertEqual(report.paid_with_option[0].evidence[0].kind, "mention")

    async def test_forward_and_reply_header_authors_do_not_count(self) -> None:
        a, b, c = user(1, "alice"), user(2, "bob"), user(3, "carol")
        receipt = message(
            START_ID,
            a,
            "Receipt",
            fwd_from=types.MessageFwdHeader(date=NOW, from_id=types.PeerUser(b.id)),
            reply_to=types.MessageReplyHeader(
                reply_to_msg_id=START_ID - 1,
                reply_to_top_id=TOPIC_ID,
                forum_topic=True,
                reply_from=types.MessageFwdHeader(
                    date=NOW, from_id=types.PeerUser(c.id)
                ),
                quote_text="@carol",
                quote_entities=[types.MessageEntityMention(offset=0, length=6)],
            ),
        )

        report = await reconcile_payments(
            EntityClient([a, b, c]), votes([a, b, c]), b"yes", topic([receipt])
        )

        self.assertEqual({person.id for person in report.missing}, {b.id, c.id})
        self.assertEqual([record.user.id for record in report.paid_with_option], [a.id])

    async def test_service_message_does_not_credit_its_author(self) -> None:
        a = user(1, "alice")
        service = types.MessageService(
            id=START_ID,
            peer_id=types.PeerChannel(CHAT_ID),
            from_id=types.PeerUser(a.id),
            date=NOW,
            action=types.MessageActionPinMessage(),
        )

        report = await reconcile_payments(
            EntityClient([a]), votes([a]), b"yes", topic([service])
        )

        self.assertEqual([person.id for person in report.missing], [a.id])
        self.assertEqual(report.paid_with_option, ())
        self.assertEqual(report.review, ())

    async def test_rate_limit_is_not_reported_as_an_unresolved_username(self) -> None:
        a = user(1, "alice")

        class LimitedClient(EntityClient):
            async def get_entity(self, reference: Any) -> Any:
                if isinstance(reference, str):
                    raise errors.FloodWaitError(request=None, capture=30)
                return await super().get_entity(reference)

        with self.assertRaises(errors.FloodWaitError):
            await reconcile_payments(
                LimitedClient([a]),
                votes([a]),
                b"yes",
                topic([message(START_ID, a, "@unknown")]),
            )


if __name__ == "__main__":
    unittest.main()
