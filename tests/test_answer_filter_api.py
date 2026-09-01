from __future__ import annotations

import unittest
from collections.abc import Sequence
from typing import Any

from telethon import functions, types

from telegram_automations.polls.answer_filter import (
    PollFilterError,
    fetch_vote_snapshot,
)


def user(user_id: int) -> types.User:
    return types.User(
        id=user_id,
        first_name=f"First {user_id}",
        last_name=f"Last {user_id}",
        username=f"user{user_id}",
    )


def votes_page(
    count: int,
    votes: Sequence[Any],
    *,
    next_offset: str | None = None,
    users: Sequence[Any] | None = None,
) -> types.messages.VotesList:
    if users is None:
        user_ids = [
            int(vote.peer.user_id)
            for vote in votes
            if isinstance(vote.peer, types.PeerUser)
        ]
        users = [user(user_id) for user_id in user_ids]
    return types.messages.VotesList(
        count=count,
        votes=list(votes),
        chats=[],
        users=list(users),
        next_offset=next_offset,
    )


class RequestClient:
    def __init__(self, responses: Sequence[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return self.responses.pop(0)


class PollVotesAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_snapshot_paginates_and_preserves_all_vote_types(self) -> None:
        responses = [
            votes_page(
                3,
                [
                    types.MessagePeerVote(
                        peer=types.PeerUser(2), option=b"no", date=None
                    ),
                    types.MessagePeerVoteMultiple(
                        peer=types.PeerUser(1),
                        options=[b"yes", b"maybe"],
                        date=None,
                    ),
                ],
                next_offset="page-2",
            ),
            votes_page(
                3,
                [types.MessagePeerVoteInputOption(peer=types.PeerUser(3), date=None)],
            ),
        ]
        client = RequestClient(responses)

        snapshot = await fetch_vote_snapshot(
            client, types.InputPeerChat(chat_id=10), message_id=42
        )

        self.assertEqual([voter.id for voter in snapshot.voters], [1, 2, 3])
        self.assertEqual(snapshot.voters[0].selected_options, {b"yes", b"maybe"})
        self.assertEqual(snapshot.voters[2].selected_options, set())
        self.assertFalse(snapshot.voters[0].used_input_option)
        self.assertTrue(snapshot.voters[2].used_input_option)
        self.assertEqual([request.id for request in client.requests], [42, 42])
        self.assertEqual(
            [request.offset for request in client.requests], [None, "page-2"]
        )
        self.assertTrue(all(request.option is None for request in client.requests))
        self.assertTrue(all(request.limit == 100 for request in client.requests))

    async def test_fetch_snapshot_rejects_unstable_or_incomplete_results(self) -> None:
        duplicate_vote = types.MessagePeerVote(
            peer=types.PeerUser(1), option=b"yes", date=None
        )
        cases = {
            "incomplete": [votes_page(2, [duplicate_vote])],
            "changing count": [
                votes_page(2, [duplicate_vote], next_offset="page-2"),
                votes_page(
                    3,
                    [
                        types.MessagePeerVote(
                            peer=types.PeerUser(2), option=b"no", date=None
                        )
                    ],
                ),
            ],
            "duplicate voter": [
                votes_page(2, [duplicate_vote], next_offset="page-2"),
                votes_page(2, [duplicate_vote]),
            ],
            "repeated offset": [
                votes_page(2, [duplicate_vote], next_offset="again"),
                votes_page(2, [], next_offset="again"),
            ],
            "missing user": [votes_page(1, [duplicate_vote], users=[])],
            "channel vote": [
                votes_page(
                    1,
                    [
                        types.MessagePeerVote(
                            peer=types.PeerChannel(20), option=b"yes", date=None
                        )
                    ],
                    users=[],
                )
            ],
        }

        for name, responses in cases.items():
            with self.subTest(name=name), self.assertRaises(PollFilterError):
                await fetch_vote_snapshot(
                    RequestClient(responses),
                    types.InputPeerChat(chat_id=10),
                    message_id=42,
                )

    async def test_request_uses_unfiltered_poll_votes(self) -> None:
        client = RequestClient([votes_page(0, [])])

        await fetch_vote_snapshot(
            client, types.InputPeerChat(chat_id=10), message_id=77
        )

        request = client.requests[0]
        self.assertIsInstance(request, functions.messages.GetPollVotesRequest)
        self.assertEqual(request.id, 77)
        self.assertIsNone(request.option)


if __name__ == "__main__":
    unittest.main()
