from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon import types

from telegram_automations.polls.answer_filter import (
    PollFilterError,
    PollLocation,
    VoterRecord,
    VoteSnapshot,
    parse_option_link,
    parse_poll_link,
    print_voter_table,
    resolve_target_request,
    select_exact_answer,
    select_option_answer,
    vote_options,
    voter_answer_text,
    voters_with_answer_count,
    voters_without_answer,
)


def poll_answer(text: str, option: bytes) -> types.PollAnswer:
    return types.PollAnswer(
        text=types.TextWithEntities(text=text, entities=[]),
        option=option,
    )


class PollLocationTests(unittest.TestCase):
    def test_parse_poll_link_supports_public_private_and_forum_links(self) -> None:
        cases = {
            "public": (
                "https://t.me/sample_group/42",
                PollLocation(chat_ref="@sample_group", message_id=42),
            ),
            "public_preview": (
                "https://telegram.me/s/sample_group/42",
                PollLocation(chat_ref="@sample_group", message_id=42),
            ),
            "private": (
                "https://t.me/c/1234567890/42?single",
                PollLocation(chat_ref=-1001234567890, message_id=42),
            ),
            "public_forum_topic": (
                "https://t.me/sample_group/77/42",
                PollLocation(chat_ref="@sample_group", message_id=42),
            ),
            "private_forum_topic": (
                "https://t.me/c/1234567890/77/42",
                PollLocation(chat_ref=-1001234567890, message_id=42),
            ),
        }

        for name, (link, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_poll_link(link), expected)

    def test_parse_poll_link_rejects_malformed_links(self) -> None:
        invalid_links = (
            "https://example.com/sample_group/42",
            "https://t.me/sample_group/not-a-message-id",
            "https://t.me/sample_group/0",
            "https://t.me/c/not-a-chat/42",
            "https://t.me/+invite/42",
        )
        for link in invalid_links:
            with self.subTest(link=link), self.assertRaises(PollFilterError):
                parse_poll_link(link)

    def test_parse_option_link_decodes_telegram_base64url_option(self) -> None:
        parsed = parse_option_link("https://t.me/c/2546560986/6968?option=MA")

        self.assertEqual(
            parsed.location,
            PollLocation(chat_ref=-1002546560986, message_id=6968),
        )
        self.assertEqual(parsed.option, b"0")

    def test_parse_option_link_rejects_missing_duplicate_or_invalid_option(
        self,
    ) -> None:
        invalid_links = (
            "https://t.me/c/2546560986/6968",
            "https://t.me/c/2546560986/6968?option=",
            "https://t.me/c/2546560986/6968?option=MA&option=MQ",
            "https://t.me/c/2546560986/6968?option=***",
            "https://t.me/c/2546560986/6968?option=%D1%82%D0%B5%D1%81%D1%82",
        )
        for link in invalid_links:
            with self.subTest(link=link), self.assertRaises(PollFilterError):
                parse_option_link(link)


class TargetRequestTests(unittest.TestCase):
    def test_target_request_accepts_both_supported_cli_forms(self) -> None:
        by_text = resolve_target_request(
            poll_link="https://t.me/sample_group/42",
            answer="Yes",
            option_link=None,
        )
        by_option = resolve_target_request(
            poll_link=None,
            answer=None,
            option_link="https://t.me/c/2546560986/6968?option=MA",
        )

        self.assertEqual(by_text.answer_text, "Yes")
        self.assertIsNone(by_text.option)
        self.assertEqual(by_option.option, b"0")
        self.assertIsNone(by_option.answer_text)

    def test_target_request_rejects_partial_or_mixed_cli_forms(self) -> None:
        cases = (
            {"poll_link": None, "answer": None, "option_link": None},
            {
                "poll_link": "https://t.me/sample_group/42",
                "answer": None,
                "option_link": None,
            },
            {"poll_link": None, "answer": "Yes", "option_link": None},
            {
                "poll_link": "https://t.me/sample_group/42",
                "answer": "Yes",
                "option_link": "https://t.me/c/2546560986/6968?option=MA",
            },
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(PollFilterError):
                resolve_target_request(**arguments)


class AnswerSelectionTests(unittest.TestCase):
    def test_select_exact_answer_is_case_sensitive(self) -> None:
        expected = poll_answer("Yes", b"yes")
        poll = SimpleNamespace(answers=[expected, poll_answer("yes", b"lowercase")])

        self.assertIs(select_exact_answer(poll, "Yes"), expected)

    def test_select_exact_answer_reports_missing_and_ambiguous_answers(self) -> None:
        cases = {
            "missing": ([poll_answer("Yes", b"one")], "No"),
            "ambiguous": (
                [
                    poll_answer("Yes", b"one"),
                    poll_answer("Yes", b"two"),
                ],
                "Yes",
            ),
        }
        for name, (answers, requested_text) in cases.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(PollFilterError, "Available answers"),
            ):
                select_exact_answer(SimpleNamespace(answers=answers), requested_text)

    def test_select_option_answer_uses_decoded_option_bytes(self) -> None:
        expected = poll_answer("No", b"0")
        poll = SimpleNamespace(answers=[poll_answer("Yes", b"1"), expected])

        self.assertIs(select_option_answer(poll, b"0"), expected)
        with self.assertRaisesRegex(PollFilterError, "does not exist"):
            select_option_answer(poll, b"missing")


class VoteSelectionTests(unittest.TestCase):
    def test_vote_options_supports_regular_multiple_and_free_text_votes(self) -> None:
        regular = types.MessagePeerVote(
            peer=types.PeerUser(1), option=b"yes", date=None
        )
        multiple = types.MessagePeerVoteMultiple(
            peer=types.PeerUser(2), options=[b"yes", b"maybe"], date=None
        )
        free_text = types.MessagePeerVoteInputOption(peer=types.PeerUser(3), date=None)

        self.assertEqual(vote_options(regular), frozenset({b"yes"}))
        self.assertEqual(vote_options(multiple), frozenset({b"yes", b"maybe"}))
        self.assertEqual(vote_options(free_text), frozenset())

    def test_filter_keeps_only_voters_without_target_option(self) -> None:
        voters = (
            VoterRecord(3, "three", "Three", None, frozenset()),
            VoterRecord(1, "one", "One", None, frozenset({b"yes"})),
            VoterRecord(2, "two", "Two", None, frozenset({b"no"})),
            VoterRecord(
                4,
                "four",
                "Four",
                None,
                frozenset({b"yes", b"maybe"}),
            ),
        )
        snapshot = VoteSnapshot(
            voters=voters,
            captured_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [voter.id for voter in voters_without_answer(snapshot, b"yes")],
            [3, 2],
        )
        self.assertEqual(voters_with_answer_count(snapshot, b"yes"), 2)


class TableTests(unittest.TestCase):
    def test_table_uses_requested_column_order(self) -> None:
        voters = (
            VoterRecord(
                id=7,
                username="second_column",
                first_name="Third",
                last_name="Fourth",
                selected_options=frozenset(),
            ),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            print_voter_table(voters)

        lines = output.getvalue().splitlines()
        self.assertEqual(
            lines[0].split(), ["ID", "username", "first", "name", "last", "name"]
        )
        self.assertLess(lines[2].index("second_column"), lines[2].index("Third"))
        self.assertLess(lines[2].index("Third"), lines[2].index("Fourth"))

    def test_voted_for_column_uses_poll_order_and_marks_free_text(self) -> None:
        poll = SimpleNamespace(
            answers=[
                poll_answer("First option", b"one"),
                poll_answer("Second option", b"two"),
            ]
        )
        voters = (
            VoterRecord(
                id=7,
                username="multiple",
                first_name=None,
                last_name=None,
                selected_options=frozenset({b"two", b"one"}),
            ),
            VoterRecord(
                id=8,
                username="free_text",
                first_name=None,
                last_name=None,
                selected_options=frozenset(),
                used_input_option=True,
            ),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            print_voter_table(voters, poll=poll)

        lines = output.getvalue().splitlines()
        self.assertTrue(lines[0].rstrip().endswith("voted for"))
        self.assertIn("First option | Second option", lines[2])
        self.assertIn("[free-text answer; text unavailable]", lines[3])
        self.assertEqual(
            voter_answer_text(voters[0], poll),
            "First option | Second option",
        )

    def test_voted_for_rejects_option_missing_from_poll(self) -> None:
        voter = VoterRecord(
            id=7,
            username=None,
            first_name=None,
            last_name=None,
            selected_options=frozenset({b"unknown"}),
        )

        with self.assertRaisesRegex(PollFilterError, "poll may have changed"):
            voter_answer_text(voter, SimpleNamespace(answers=[]))


if __name__ == "__main__":
    unittest.main()
