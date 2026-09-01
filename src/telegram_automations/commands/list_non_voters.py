from __future__ import annotations

import argparse
from pathlib import Path

from telegram_automations.common import load_poll_context
from telegram_automations.polls.cleanup import (
    build_export_document,
    fetch_all_participants,
    fetch_voter_ids,
    load_exclusions,
    participant_to_record,
    print_candidate_table,
    resolve_poll_location,
    select_candidates,
    write_private_json,
)
from telegram_automations.runtime import CommandRuntime, ErrorPolicy

ERROR_POLICY = ErrorPolicy(
    flood_wait=(
        "Telegram requested a delay. Do not retry for at least {seconds} seconds. "
        "The list was not generated."
    )
)


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Generate a list of current group members who did not vote in a "
        "closed, non-anonymous Telegram poll."
    )
    parser.add_argument("--poll-link", help="t.me link to the poll message")
    parser.add_argument(
        "--chat",
        help="Group username (@group) or numeric ID; use with --message-id",
    )
    parser.add_argument(
        "--message-id",
        type=int,
        help="Poll message ID; use with --chat",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("non_voters.json"),
        help="Output file (default: non_voters.json)",
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
        help="Protected username/ID file (default: CONFIG_DIR/exclusions.txt)",
    )


async def run(args: argparse.Namespace, runtime: CommandRuntime) -> int:
    location = resolve_poll_location(
        poll_link=args.poll_link,
        chat=args.chat,
        message_id=args.message_id,
    )
    exclusions_path = args.exclusions or runtime.config_dir / "exclusions.txt"
    exclusions = load_exclusions(exclusions_path)

    context = await load_poll_context(
        runtime.client,
        location.chat_ref,
        location.message_id,
        require_closed=True,
    )
    voter_ids = await fetch_voter_ids(
        runtime.client, context.chat, context.message.id
    )
    users = await fetch_all_participants(runtime.client, context.chat)
    participants = [participant_to_record(user) for user in users]
    candidates, reasons = select_candidates(
        participants,
        voter_ids=voter_ids,
        own_user_id=int(runtime.me.id),
        poll_published_at=context.message.date,
        exclusions=exclusions,
    )

    document = build_export_document(
        own_user_id=int(runtime.me.id),
        context=context,
        candidates=candidates,
    )
    write_private_json(args.output, document)

    print_candidate_table(document["candidates"])
    print()
    print(f"Current members retrieved: {len(participants)}")
    print(f"Unique voters found: {len(voter_ids)}")
    print(f"Removal candidates: {len(candidates)}")
    print(
        "Excluded: "
        f"exclusions file={reasons['excluded']}, "
        f"voted={reasons['voted']}, "
        f"owner/administrators={reasons['admin']}, "
        f"current account={reasons['self']}, "
        f"joined later={reasons['joined_after_poll']}, "
        f"unknown join date={reasons['unknown_join_date']}"
    )
    print(f"List saved to: {args.output.expanduser().resolve()}")
    return 0
