from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import functions, types

from telegram_automations.cli import build_parser
from telegram_automations.commands.check import run
from telegram_automations.common import AutomationError, PollContext, load_poll_context
from telegram_automations.polls.check import (
    fetch_options,
    format_report,
    select_options,
    send_report,
    split_message,
)


def poll(*, closed=False, public=False):
    return types.Poll(
        id=7,
        hash=77,
        question=types.TextWithEntities("Which day?", []),
        answers=[
            types.PollAnswer(types.TextWithEntities(text, []), option)
            for text, option in [
                ("Friday", b"a"),
                ("Saturday", b"b"),
                ("👋 Pass", b"c"),
            ]
        ],
        closed=closed,
        public_voters=public,
    )


def results(counts=(14, 15, 16)):
    return types.PollResults(
        results=[
            types.PollAnswerVoters(option=option, voters=count)
            for option, count in reversed(
                list(zip((b"a", b"b", b"c"), counts, strict=True))
            )
        ]
    )


def client_for(p=None, r=None):
    p = p if p is not None else poll()
    r = r if r is not None else results()
    client = AsyncMock(
        return_value=SimpleNamespace(
            updates=[
                types.UpdateMessagePoll(p.id, r),
            ]
        )
    )
    client.get_entity.return_value = types.Chat(
        id=1,
        title="Group",
        photo=types.ChatPhotoEmpty(),
        participants_count=20,
        date=None,
        version=1,
    )
    client.get_messages.return_value = SimpleNamespace(
        id=42,
        media=types.MessageMediaPoll(p, r),
    )
    return client


class ParserTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        args = build_parser().parse_args(["poll", "check", "--poll-link", "URL"])
        self.assertEqual((args.minimum, args.send_to_user), (15, None))
        args = build_parser().parse_args(
            [
                "poll",
                "check",
                "--poll-link",
                "URL",
                "--minimum",
                "20",
                "--send-to-user",
                "@other",
            ]
        )
        self.assertEqual((args.minimum, args.send_to_user), (20, "@other"))

    def test_invalid_arguments(self):
        for extra in (
            [],
            ["--poll-link", "URL", "--minimum", "0"],
            ["--poll-link", "URL", "--minimum", "-1"],
            ["--poll-link", "URL", "--minimum", "1.5"],
            ["--poll-link", "URL", "--send-to-user"],
            ["--poll-link", "URL", "--send", "@other"],
            ["--poll-link", "URL", "--user", "@other"],
        ):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    build_parser().parse_args(["poll", "check", *extra])
                self.assertEqual(raised.exception.code, 2)


class PolicyTests(unittest.TestCase):
    def test_inclusive_threshold_order_and_emoji(self):
        selected = select_options(poll(), results(), 15)
        self.assertEqual(
            [(o.text, o.voters) for o in selected], [("Saturday", 15), ("👋 Pass", 16)]
        )
        self.assertEqual(len(select_options(poll(), results(), 16)), 1)
        self.assertEqual(select_options(poll(), results(), 17), ())
        self.assertEqual(select_options(poll(), results((0, 0, 0)), 15), ())

    def test_incomplete_and_invalid_results(self):
        invalid = [types.PollResults(), results(), results(), results(), results()]
        invalid[1].min = True
        invalid[2].results.pop()
        invalid[3].results[0].voters = None
        invalid[4].results[0].voters = -1
        duplicate = results()
        duplicate.results.append(duplicate.results[0])
        invalid.append(duplicate)
        unknown = results()
        unknown.results[0].option = b"unknown"
        invalid.append(unknown)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(AutomationError):
                select_options(poll(), value, 15)

    def test_missing_counts_explain_next_step(self):
        with self.assertRaisesRegex(AutomationError, "same Telegram account"):
            select_options(poll(), types.PollResults(), 15)
        hidden = poll()
        hidden.hide_results_until_close = True
        with self.assertRaisesRegex(AutomationError, "hidden until the poll closes"):
            select_options(hidden, types.PollResults(), 15)
        with self.assertRaises(AutomationError) as raised:
            select_options(poll(closed=True), types.PollResults(), 15)
        self.assertNotIn("vote first", str(raised.exception))

    def test_report(self):
        self.assertEqual(
            format_report(
                poll(),
                "https://t.me/group/42",
                select_options(poll(), results(), 15),
                15,
            ),
            "Poll: Which day?\nhttps://t.me/group/42\n\n"
            "Options with at least 15 votes:\n"
            "• Saturday — 15 votes\n• 👋 Pass — 16 votes",
        )

    def test_split_preserves_text_and_utf16_limit(self):
        for text in ["x" * 10000, "😀" * 5000, ("line\n" * 2000), "small"]:
            with self.subTest(length=len(text)):
                chunks = split_message(text)
                self.assertEqual("".join(chunks), text)
                self.assertTrue(
                    all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks)
                )


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_closed_anonymous_and_public_polls(self):
        for closed in (False, True):
            for public in (False, True):
                p = poll(closed=closed, public=public)
                client = client_for(p)
                context = await load_poll_context(
                    client,
                    "@group",
                    42,
                    require_public_voters=False,
                )
                _, options = await fetch_options(client, context, 15)
                self.assertEqual(len(options), 2)
                request = client.await_args.args[0]
                self.assertIsInstance(request, functions.messages.GetPollResultsRequest)
                self.assertEqual((request.msg_id, request.poll_hash), (42, 77))
                if not public:
                    with self.assertRaises(AutomationError):
                        await load_poll_context(client, "@group", 42)

    async def test_missing_poll_update(self):
        client = client_for()
        context = PollContext(
            client.get_entity.return_value, client.get_messages.return_value
        )
        client.return_value = SimpleNamespace(updates=[])
        with self.assertRaises(AutomationError):
            await fetch_options(client, context, 15)

    async def test_uses_updated_poll_and_short_update(self):
        client = client_for()
        context = PollContext(
            client.get_entity.return_value, client.get_messages.return_value
        )
        updated = poll()
        updated.answers[1].text.text = "New title"
        client.return_value = SimpleNamespace(
            update=types.UpdateMessagePoll(
                7,
                results(),
                poll=updated,
            )
        )
        _, options = await fetch_options(client, context, 15)
        self.assertEqual(options[0].text, "New title")

    async def test_send_plain_text_chunks_and_recipient_validation(self):
        client = AsyncMock()
        recipient = types.User(id=9)
        client.get_entity.return_value = recipient
        report = "😀" * 5000
        await send_report(client, "@other", report)
        client.get_entity.assert_awaited_once_with("@other")
        self.assertEqual(
            "".join(call.args[1] for call in client.send_message.await_args_list),
            report,
        )
        for call in client.send_message.await_args_list:
            self.assertEqual(call.args[0], recipient)
            self.assertEqual(call.kwargs, {"parse_mode": None, "link_preview": False})
        for bad in (types.User(id=9, deleted=True), SimpleNamespace(id=1)):
            client.reset_mock()
            client.get_entity.return_value = bad
            with self.assertRaises(AutomationError):
                await send_report(client, "@bad", "report")
            client.send_message.assert_not_awaited()

    async def test_send_failure_propagates(self):
        client = AsyncMock()
        client.get_entity.return_value = types.User(id=9)
        client.send_message.side_effect = OSError("network")
        with self.assertRaises(OSError):
            await send_report(client, "@other", "report")
        client.send_message.assert_awaited_once()

    async def test_command_read_only_empty_and_repeated_send(self):
        for send, counts, expected in (
            (False, (14, 15, 16), 0),
            (True, (1, 2, 3), 0),
            (True, (14, 15, 16), 2),
        ):
            client = client_for(r=results(counts))
            group = client.get_entity.return_value
            recipient = types.User(id=9)
            client.get_entity.side_effect = (
                lambda ref, recipient=recipient, group=group: (
                    recipient if ref == "@other" else group
                )
            )
            args = build_parser().parse_args(
                [
                    "poll",
                    "check",
                    "--poll-link",
                    "https://t.me/group/42",
                    *(["--send-to-user", "@other"] if send else []),
                ]
            )
            for _ in range(2):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(await run(args, SimpleNamespace(client=client)), 0)
                if expected:
                    self.assertTrue(
                        output.getvalue().endswith("Report sent to @other.\n")
                    )
                    self.assertEqual(
                        client.send_message.await_args.args[1],
                        output.getvalue()
                        .removesuffix("Report sent to @other.\n")
                        .rstrip("\n"),
                    )
            self.assertEqual(client.send_message.await_count, expected)
            if not expected:
                self.assertNotIn(
                    "@other",
                    [call.args[0] for call in client.get_entity.await_args_list],
                )

    async def test_command_does_not_report_success_on_send_failure(self):
        client = client_for()
        group = client.get_entity.return_value
        client.get_entity.side_effect = lambda ref: (
            types.User(id=9) if ref == "@other" else group
        )
        client.send_message.side_effect = OSError("network")
        args = build_parser().parse_args(
            [
                "poll",
                "check",
                "--poll-link",
                "https://t.me/group/42",
                "--send-to-user",
                "@other",
            ]
        )
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(OSError):
            await run(args, SimpleNamespace(client=client))
        self.assertNotIn("Report sent", output.getvalue())
