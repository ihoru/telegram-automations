from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from telegram_automations.cli import build_parser
from telegram_automations.commands import (
    all_members,
    check_payments,
    list_non_voters,
    list_without_answer,
    remove_non_voters,
)


class GroupedParserTests(unittest.TestCase):
    def test_routes_each_explicit_poll_command(self) -> None:
        cases = (
            ("list-without-answer", list_without_answer.run),
            ("list-non-voters", list_non_voters.run),
            ("remove-non-voters", remove_non_voters.run),
        )

        for name, handler in cases:
            with self.subTest(name=name):
                args = build_parser().parse_args(["poll", name])
                self.assertIs(args.command_handler, handler)

    def test_routes_members_all_and_requires_chat(self) -> None:
        args = build_parser().parse_args(["members", "all", "--chat", "-100123"])
        self.assertIs(args.command_handler, all_members.run)
        self.assertIs(args.error_policy, all_members.ERROR_POLICY)
        self.assertEqual(args.chat, "-100123")
        self.assertFalse(args.new_line)
        for flag in ("--new-line", "-nl"):
            with self.subTest(flag=flag):
                option_args = build_parser().parse_args(
                    ["members", "all", "--chat", "-100123", flag]
                )
                self.assertTrue(option_args.new_line)

        output = StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["members", "all"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--chat", output.getvalue())

    def test_config_dir_is_global_and_explicit(self) -> None:
        args = build_parser().parse_args(
            ["--config-dir", "/tmp/config", "poll", "remove-non-voters"]
        )
        self.assertEqual(args.config_dir, Path("/tmp/config"))

    def test_preserves_command_specific_defaults(self) -> None:
        remove_args = build_parser().parse_args(["poll", "remove-non-voters"])
        list_args = build_parser().parse_args(["poll", "list-non-voters"])

        self.assertEqual(remove_args.input, Path("non_voters.json"))
        self.assertEqual(remove_args.batch_size, 10)
        self.assertIsNone(remove_args.exclusions)
        self.assertEqual(list_args.output, Path("non_voters.json"))
        self.assertIsNone(list_args.exclusions)

    def test_routes_payment_command_with_both_answer_forms(self) -> None:
        cases = (
            ["--option", "https://t.me/sample_group/42?option=MQ"],
            ["--poll-link", "https://t.me/sample_group/42", "--answer", "Yes"],
        )
        for selector in cases:
            with self.subTest(selector=selector):
                args = build_parser().parse_args(
                    [
                        "poll",
                        "check-payments",
                        *selector,
                        "--since-message",
                        "https://t.me/sample_group/30/31",
                    ]
                )
                self.assertIs(args.command_handler, check_payments.run)
                self.assertEqual(
                    args.since_message, "https://t.me/sample_group/30/31"
                )
                self.assertIs(args.error_policy, check_payments.ERROR_POLICY)

    def test_payment_command_requires_starting_message(self) -> None:
        output = StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(
                [
                    "poll",
                    "check-payments",
                    "--option",
                    "https://t.me/sample_group/42?option=MQ",
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--since-message", output.getvalue())
