from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from telegram_automations import __version__
from telegram_automations.commands import (
    list_non_voters,
    list_without_answer,
    remove_non_voters,
)
from telegram_automations.runtime import main as runtime_main

POLL_COMMANDS = (
    (
        "list-without-answer",
        "List poll participants who did not select one exact answer.",
        list_without_answer,
    ),
    (
        "list-non-voters",
        "Export current group members who did not vote in a closed poll.",
        list_non_voters,
    ),
    (
        "remove-non-voters",
        "Recheck an export and optionally remove eligible non-voters.",
        remove_non_voters,
    ),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-automations",
        description="Reusable Telegram scripts and automations.",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path.cwd(),
        help="Directory containing .env, the session, and exclusions.txt (default: cwd)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    groups = parser.add_subparsers(dest="command_group", required=True)
    poll_parser = groups.add_parser(
        "poll",
        help="Analyze Telegram polls and act on reviewed results.",
    )
    commands = poll_parser.add_subparsers(dest="poll_command", required=True)
    for name, help_text, module in POLL_COMMANDS:
        command_parser = commands.add_parser(name, help=help_text)
        module.configure_parser(command_parser)
        command_parser.set_defaults(
            command_handler=module.run,
            error_policy=module.ERROR_POLICY,
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return runtime_main(build_parser, argv)
