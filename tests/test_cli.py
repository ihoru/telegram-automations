from __future__ import annotations

import unittest
from pathlib import Path

from telegram_automations.cli import build_parser
from telegram_automations.commands import (
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
