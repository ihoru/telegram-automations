from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Any

from telegram_automations.common import display_text, load_poll_context
from telegram_automations.polls.answer_filter import (
    VoteSnapshot,
    answer_text,
    fetch_vote_snapshot,
    poll_question,
    resolve_target_request,
    select_exact_answer,
    select_option_answer,
    voters_with_answer_count,
)
from telegram_automations.polls.payments import (
    PaymentEvidence,
    PaymentRecord,
    PaymentReport,
    PaymentUser,
    TopicSnapshot,
    load_topic_snapshot,
    reconcile_payments,
)
from telegram_automations.runtime import CommandRuntime, ErrorPolicy

ERROR_POLICY = ErrorPolicy()


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = (
        "Compare people who selected one poll answer with message authors and "
        "mentioned users in a forum topic. A message or mention counts as payment."
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
        "--since-message",
        required=True,
        help="t.me forum-topic message link; only later messages count (start excluded)",
    )


def _name(user: PaymentUser, *, full: bool = False) -> str:
    name = " ".join(
        display_text(part, limit=None)
        for part in (user.first_name, user.last_name)
        if part
    )
    if user.username:
        handle = f"@{display_text(user.username, limit=None)}"
        if full:
            detail = f"{name}; ID {user.id}" if name else f"ID {user.id}"
            return f"{handle} ({detail}) — https://t.me/{user.username}"
        return handle
    return f"{name or '[no name]'} (ID {user.id})"


def _print_paid_section(
    title: str,
    records: tuple[PaymentRecord, ...],
    users: dict[int, PaymentUser],
) -> None:
    print(f"{title} ({len(records)}):")
    if not records:
        print("  (none)")
        return

    groups: dict[int, list[PaymentRecord]] = defaultdict(list)
    for record in records:
        groups[min(item.message_id for item in record.evidence)].append(record)

    for first_message_id, group in sorted(groups.items()):
        # Keep the sender before the people they mentioned in the shared message.
        group.sort(
            key=lambda record: (
                not any(
                    item.message_id == first_message_id and item.kind == "sender"
                    for item in record.evidence
                ),
                record.user.id,
            )
        )
        print("  " + ", ".join(_name(record.user, full=True) for record in group))
        messages: dict[
            int, list[tuple[PaymentUser, PaymentEvidence]]
        ] = defaultdict(list)
        for record in group:
            for evidence in record.evidence:
                messages[evidence.message_id].append((record.user, evidence))
        for _, entries in sorted(messages.items()):
            evidence = entries[0][1]
            author = users.get(evidence.sender_id)
            if author is not None:
                source = _name(author)
            elif evidence.sender_id is not None:
                source = f"ID {evidence.sender_id}"
            else:
                source = "[unidentified author]"
            attributions: list[str] = []
            for user, item in entries:
                if item.kind == "sender":
                    if user.id == evidence.sender_id:
                        continue
                    attribution = f"counts sender {_name(user)}"
                else:
                    attribution = f"mentions {_name(user)}"
                    if item.mention:
                        attribution += f" as {display_text(item.mention, limit=None)}"
                if attribution not in attributions:
                    attributions.append(attribution)
            suffix = "; " + "; ".join(attributions) if attributions else ""
            print(f"    {evidence.message_url} — from {source}{suffix}")


def print_payment_report(
    report: PaymentReport,
    *,
    poll: Any,
    target_answer: Any,
    votes: VoteSnapshot,
    topic: TopicSnapshot,
) -> None:
    users = {
        record.user.id: record.user
        for record in (*report.paid_without_option, *report.paid_with_option)
    }
    users.update((user.id, user) for user in report.missing)

    print(f"No payment message found ({len(report.missing)}):")
    for user in report.missing:
        print(f"  {_name(user, full=True)}")
    if not report.missing:
        print("  (none)")
    print()
    _print_paid_section(
        "Did not select the option, payment counted", report.paid_without_option, users
    )
    print()
    _print_paid_section(
        "Selected the option, payment counted", report.paid_with_option, users
    )
    if report.review:
        print()
        print(f"Needs review ({len(report.review)}):")
        for item in report.review:
            print(f"  {item.message_url} — {display_text(item.detail, limit=None)}")

    print()
    print(f"Poll: {display_text(poll_question(poll), limit=None)}")
    print(f"Selected answer: {display_text(answer_text(target_answer), limit=None)}")
    closed = bool(getattr(poll, "closed", False))
    print(f"Poll state: {'closed' if closed else 'open'}")
    print(f"Unique voters retrieved: {votes.total_voters}")
    print(
        "Voters who selected the option: "
        f"{voters_with_answer_count(votes, target_answer.option)}"
    )
    print(f"Vote snapshot captured at: {votes.captured_at.isoformat()}")
    print(f"Message snapshot captured at: {topic.captured_at.isoformat()}")
    print(
        f"Topic: {topic.location.topic_id}; message ID range: "
        f"after {topic.location.message_id} (excluded) "
        f"through {topic.upper_message_id} (included)"
    )
    print(f"Starting message date: {topic.start_date.isoformat()}")
    print(f"Topic messages retrieved: {len(topic.messages)}")
    print(
        "Payment rule: an ordinary message or an explicit mention counts; "
        "amounts and attachment contents are not checked."
    )
    if not closed:
        print("WARNING: The poll is still open; votes may change after this snapshot.")


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
    topic = await load_topic_snapshot(runtime.client, args.since_message, context.chat)
    votes = await fetch_vote_snapshot(runtime.client, context.chat, context.message.id)
    report = await reconcile_payments(runtime.client, votes, target_answer.option, topic)
    print_payment_report(
        report,
        poll=context.poll,
        target_answer=target_answer,
        votes=votes,
        topic=topic,
    )
    return 0
