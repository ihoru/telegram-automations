from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_automations.cli import build_parser
from telegram_automations.commands import check_payments
from telegram_automations.common import AutomationError
from telegram_automations.polls.answer_filter import VoterRecord, VoteSnapshot
from telegram_automations.polls.payments import (
    PaymentEvidence,
    PaymentRecord,
    PaymentReport,
    PaymentUser,
    ReviewItem,
    TopicSnapshot,
)

CAPTURED_AT = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
START_DATE = datetime(2026, 9, 7, 18, tzinfo=timezone.utc)
START_LINK = "https://t.me/c/1234567890/30/30"


def user(user_id: int, username: str | None) -> PaymentUser:
    return PaymentUser(user_id, username, username, None)


def evidence(
    message_id: int, sender_id: int | None, kind: str, mention: str | None = None
) -> PaymentEvidence:
    return PaymentEvidence(
        message_id=message_id,
        message_url=f"https://t.me/c/1234567890/30/{message_id}",
        sender_id=sender_id,
        kind=kind,
        mention=mention,
    )


def fixture() -> tuple[PaymentReport, VoteSnapshot, TopicSnapshot, SimpleNamespace]:
    people = {letter: user(number, letter) for number, letter in enumerate("abcdf", 1)}
    report = PaymentReport(
        missing=(people["d"],),
        paid_without_option=(
            PaymentRecord(people["f"], (evidence(34, 5, "sender"),)),
        ),
        paid_with_option=(
            PaymentRecord(
                people["b"],
                (evidence(31, 1, "mention", "@b"), evidence(35, 3, "mention", "@B")),
            ),
            PaymentRecord(people["a"], (evidence(31, 1, "sender"),)),
            PaymentRecord(
                people["c"], (evidence(32, 3, "sender"), evidence(35, 3, "sender"))
            ),
        ),
        review=(),
    )
    votes = VoteSnapshot(
        voters=tuple(
            VoterRecord(person.id, person.username, None, None, frozenset({b"1"}))
            for person in people.values()
            if person.username != "f"
        ),
        captured_at=CAPTURED_AT,
    )
    topic = TopicSnapshot(
        chat=SimpleNamespace(id=1234567890),
        location=SimpleNamespace(
            chat_ref=-1001234567890, topic_id=30, message_id=30
        ),
        messages=tuple(SimpleNamespace(id=number) for number in (31, 32, 34, 35)),
        upper_message_id=35,
        captured_at=CAPTURED_AT,
        start_date=START_DATE,
    )
    answer = SimpleNamespace(text="Yes", option=b"1")
    return report, votes, topic, answer


class PaymentRenderingTests(unittest.TestCase):
    def render(
        self, report: PaymentReport, *, closed: bool = True
    ) -> str:
        _, votes, topic, answer = fixture()
        output = io.StringIO()
        with redirect_stdout(output):
            check_payments.print_payment_report(
                report,
                poll=SimpleNamespace(question="Bath?", closed=closed),
                target_answer=answer,
                votes=votes,
                topic=topic,
            )
        return output.getvalue()

    def test_three_lists_group_mentions_and_preserve_all_evidence(self) -> None:
        report, _, _, _ = fixture()
        output = self.render(report)
        missing, without, with_option = (
            "No payment message found (1):",
            "Did not select the option, payment counted (1):",
            "Selected the option, payment counted (3):",
        )
        self.assertLess(output.index(missing), output.index(without))
        self.assertLess(output.index(without), output.index(with_option))
        self.assertIn("@d (d; ID 4)", output.split(without)[0])
        self.assertIn("@d (d; ID 4) — https://t.me/d", output.split(without)[0])
        second_section = output.split(without)[1].split(with_option)[0]
        self.assertIn("@f (f; ID 5)", second_section)
        self.assertIn("@f (f; ID 5) — https://t.me/f", second_section)
        self.assertNotIn("@a", second_section)
        self.assertIn("@a (a; ID 1) — https://t.me/a, @b (b; ID 2) — https://t.me/b", output)
        self.assertEqual(output.count("@b (b; ID 2)"), 1)
        self.assertIn("31 — from @a; mentions @b as @b", output)
        self.assertIn("35 — from @c; mentions @b as @B", output)
        self.assertIn("32 — from @c\n", output)
        self.assertIn("35 — from @c\n", output)
        self.assertNotIn("counts sender", output)
        self.assertIn("Voters who selected the option: 4", output)
        self.assertIn("after 30 (excluded) through 35 (included)", output)
        self.assertIn(CAPTURED_AT.isoformat(), output)
        self.assertIn(START_DATE.isoformat(), output)
        self.assertIn("Payment rule:", output)
        self.assertNotIn("WARNING:", output)

    def test_cross_section_mentions_keep_the_source_author(self) -> None:
        report = PaymentReport(
            missing=(),
            paid_without_option=(
                PaymentRecord(user(2, "b"), (evidence(31, 1, "mention", "@b"),)),
            ),
            paid_with_option=(
                PaymentRecord(user(1, "a"), (evidence(31, 1, "sender"),)),
            ),
            review=(),
        )
        output = self.render(report)
        self.assertIn("31 — from @a; mentions @b as @b", output)
        self.assertIn("31 — from @a\n", output)
        self.assertEqual(output.count("@a (a; ID 1)"), 1)
        self.assertEqual(output.count("@b (b; ID 2)"), 1)

    def test_empty_lists_names_without_usernames_and_review(self) -> None:
        report = PaymentReport(
            missing=(PaymentUser(7, None, "First", "Last"),),
            paid_without_option=(),
            paid_with_option=(
                PaymentRecord(user(8, None), (evidence(31, None, "mention"),)),
            ),
            review=(ReviewItem(START_LINK, "Could not resolve @unknown"),),
        )
        output = self.render(report, closed=False)
        self.assertIn("First Last (ID 7)", output)
        self.assertIn("[no name] (ID 8)", output)
        self.assertIn("(0):\n  (none)", output)
        self.assertIn("from [unidentified author]", output)
        self.assertIn("Needs review (1):", output)
        self.assertIn(f"{START_LINK} — Could not resolve @unknown", output)
        self.assertIn("WARNING: The poll is still open", output)


class PaymentCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_selects_answer_and_captures_topic_before_votes(self) -> None:
        for selector in (
            ["--option", "https://t.me/c/1234567890/42?option=MQ"],
            ["--poll-link", "https://t.me/c/1234567890/42", "--answer", "Yes"],
        ):
            with self.subTest(selector=selector):
                args = build_parser().parse_args(
                    ["poll", "check-payments", *selector, "--since-message", START_LINK]
                )
                report, votes, topic, answer = fixture()
                runtime = SimpleNamespace(client=object())
                context = SimpleNamespace(
                    chat=topic.chat,
                    message=SimpleNamespace(id=42),
                    poll=SimpleNamespace(
                        question="Bath?", answers=[answer], closed=True
                    ),
                )
                calls = MagicMock()
                with (
                    patch.object(
                        check_payments,
                        "load_poll_context",
                        AsyncMock(return_value=context),
                    ) as load_poll,
                    patch.object(
                        check_payments,
                        "load_topic_snapshot",
                        AsyncMock(return_value=topic),
                    ) as load_topic,
                    patch.object(
                        check_payments,
                        "fetch_vote_snapshot",
                        AsyncMock(return_value=votes),
                    ) as fetch_votes,
                    patch.object(
                        check_payments,
                        "reconcile_payments",
                        AsyncMock(return_value=report),
                    ) as reconcile,
                    redirect_stdout(io.StringIO()),
                ):
                    for name, mock in (
                        ("poll", load_poll),
                        ("topic", load_topic),
                        ("votes", fetch_votes),
                        ("reconcile", reconcile),
                    ):
                        calls.attach_mock(mock, name)
                    self.assertEqual(await check_payments.run(args, runtime), 0)
                self.assertEqual(
                    [call[0] for call in calls.mock_calls],
                    ["poll", "topic", "votes", "reconcile"],
                )
                load_poll.assert_awaited_once_with(
                    runtime.client, -1001234567890, 42
                )
                load_topic.assert_awaited_once_with(
                    runtime.client, START_LINK, context.chat
                )
                fetch_votes.assert_awaited_once_with(runtime.client, context.chat, 42)
                reconcile.assert_awaited_once_with(runtime.client, votes, b"1", topic)

    async def test_invalid_selector_stops_before_loading_data(self) -> None:
        for selector in (
            [],
            ["--answer", "Yes"],
            ["--poll-link", "https://t.me/c/1234567890/42"],
            [
                "--option", "https://t.me/c/1234567890/42?option=MQ",
                "--answer", "Yes",
            ],
        ):
            with self.subTest(selector=selector):
                args = build_parser().parse_args(
                    ["poll", "check-payments", *selector, "--since-message", START_LINK]
                )
                with (
                    patch.object(check_payments, "load_poll_context") as load,
                    self.assertRaises(AutomationError),
                ):
                    await check_payments.run(args, SimpleNamespace(client=object()))
                load.assert_not_called()

    async def test_failed_data_read_does_not_print_partial_report(self) -> None:
        _, _, topic, answer = fixture()
        args = build_parser().parse_args(
            [
                "poll", "check-payments",
                "--option", "https://t.me/c/1234567890/42?option=MQ",
                "--since-message", START_LINK,
            ]
        )
        context = SimpleNamespace(
            chat=topic.chat,
            message=SimpleNamespace(id=42),
            poll=SimpleNamespace(answers=[answer]),
        )
        for failing_stage in ("load_topic_snapshot", "fetch_vote_snapshot"):
            with self.subTest(failing_stage=failing_stage):
                output = io.StringIO()
                with (
                    patch.object(
                        check_payments, "load_poll_context", AsyncMock(return_value=context)
                    ),
                    patch.object(
                        check_payments, "load_topic_snapshot", AsyncMock(return_value=topic)
                    ),
                    patch.object(check_payments, "fetch_vote_snapshot", AsyncMock()),
                    patch.object(
                        check_payments,
                        failing_stage,
                        AsyncMock(side_effect=AutomationError("Incomplete snapshot")),
                    ),
                    patch.object(check_payments, "reconcile_payments") as reconcile,
                    redirect_stdout(output),
                    self.assertRaisesRegex(AutomationError, "Incomplete snapshot"),
                ):
                    await check_payments.run(args, SimpleNamespace(client=object()))
                self.assertEqual(output.getvalue(), "")
                reconcile.assert_not_called()
