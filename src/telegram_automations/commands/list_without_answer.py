from __future__ import annotations

import argparse

from telegram_automations.common import load_poll_context
from telegram_automations.polls.answer_filter import (
    answer_text,
    fetch_vote_snapshot,
    poll_question,
    print_voter_table,
    resolve_target_request,
    select_exact_answer,
    select_option_answer,
    voters_with_answer_count,
    voters_without_answer,
)
from telegram_automations.runtime import CommandRuntime, ErrorPolicy

ERROR_POLICY = ErrorPolicy()


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "List people who participated in a non-anonymous Telegram poll "
        "but did not select one exact answer."
    )
    parser.add_argument(
        "--poll-link",
        help="t.me link to the poll message; use together with --answer",
    )
    parser.add_argument(
        "--answer",
        help="exact, case-sensitive answer text; use with --poll-link",
    )
    parser.add_argument(
        "--option",
        help=(
            "t.me poll-option link containing ?option=...; replaces both "
            "--poll-link and --answer"
        ),
    )
    parser.add_argument(
        "--voted-for",
        action="store_true",
        help="add a final column with the answer text selected by each voter",
    )
async def run(args: argparse.Namespace, runtime: CommandRuntime) -> int:
    target_request = resolve_target_request(
        poll_link=args.poll_link,
        answer=args.answer,
        option_link=args.option,
    )
    context = await load_poll_context(
        runtime.client,
        target_request.location.chat_ref,
        target_request.location.message_id,
    )
    if target_request.answer_text is not None:
        target_answer = select_exact_answer(context.poll, target_request.answer_text)
    else:
        assert target_request.option is not None
        target_answer = select_option_answer(context.poll, target_request.option)
    snapshot = await fetch_vote_snapshot(
        runtime.client, context.chat, context.message.id
    )
    matching_voters = voters_without_answer(snapshot, target_answer.option)
    selected_count = voters_with_answer_count(snapshot, target_answer.option)

    if not bool(getattr(context.poll, "closed", False)):
        print(
            "WARNING: The poll is still open. This is a changing snapshot "
            f"captured at {snapshot.captured_at.isoformat()}."
        )
        print()

    print_voter_table(
        matching_voters,
        poll=context.poll if args.voted_for else None,
    )
    print()
    print(f"Poll: {poll_question(context.poll)}")
    print(f"Excluded answer: {answer_text(target_answer)}")
    print(
        "Poll state: "
        f"{'closed' if bool(getattr(context.poll, 'closed', False)) else 'open'}"
    )
    print(f"Unique voters retrieved: {snapshot.total_voters}")
    print(f"Voters who selected the excluded answer: {selected_count}")
    print(f"Voters who did not select the excluded answer: {len(matching_voters)}")
    return 0
