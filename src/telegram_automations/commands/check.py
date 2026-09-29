from __future__ import annotations

import argparse

from telegram_automations.common import display_text, load_poll_context, parse_poll_link
from telegram_automations.polls.check import fetch_options, format_report, send_report
from telegram_automations.runtime import CommandRuntime, ErrorPolicy

ERROR_POLICY = ErrorPolicy()


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.allow_abbrev = False
    parser.description = "Report poll options that have reached a minimum vote count."
    parser.add_argument(
        "--poll-link", required=True, help="t.me link to the poll message"
    )
    parser.add_argument(
        "--minimum",
        type=positive_integer,
        default=15,
        help="inclusive minimum vote count (default: 15)",
    )
    parser.add_argument(
        "--send-to-user",
        metavar="USERNAME",
        help=(
            "send the report to this username (with or without @) "
            "only when at least one option qualifies"
        ),
    )


async def run(args: argparse.Namespace, runtime: CommandRuntime) -> int:
    location = parse_poll_link(args.poll_link)
    context = await load_poll_context(
        runtime.client,
        location.chat_ref,
        location.message_id,
        require_public_voters=False,
    )
    poll, options = await fetch_options(runtime.client, context, args.minimum)
    if not options:
        print(f"No options with at least {args.minimum} votes.")
        return 0
    report = format_report(poll, args.poll_link, options, args.minimum)
    print(report)
    if args.send_to_user is not None:
        await send_report(runtime.client, args.send_to_user, report)
        print(f"Report sent to {display_text(args.send_to_user, limit=None)}.")
    return 0
