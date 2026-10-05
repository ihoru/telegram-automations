from __future__ import annotations

import argparse
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import types

from telegram_automations.commands import all_members
from telegram_automations.common import AutomationError


class MemberTextTests(unittest.TestCase):
    def test_prefers_username_and_falls_back_to_name_and_id(self) -> None:
        cases = (
            (SimpleNamespace(id=1, username="alice"), "@alice"),
            (
                SimpleNamespace(
                    id=2, username=None, first_name="Alice\nExample", last_name="Smith"
                ),
                "Alice Example Smith (ID: 2)",
            ),
            (
                SimpleNamespace(id=3, username=None, first_name=None, last_name=None),
                "ID: 3",
            ),
        )
        for user, expected in cases:
            with self.subTest(user=user.id):
                self.assertEqual(all_members.member_text(user), expected)


class MembersAllTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.chat = types.Chat(
            id=10,
            title="Test group",
            photo=types.ChatPhotoEmpty(),
            participants_count=5,
            date=None,
            version=1,
        )
        self.runtime = SimpleNamespace(client=object(), me=SimpleNamespace(id=99))

    async def test_prints_one_list_with_admins_but_without_bots_or_self(self) -> None:
        users = [
            SimpleNamespace(id=1, username="admin", bot=False),
            SimpleNamespace(
                id=2, username=None, first_name="Bob", last_name="Jones", bot=False
            ),
            SimpleNamespace(id=3, username="helperbot", bot=True),
            SimpleNamespace(id=99, username="me", bot=False),
            SimpleNamespace(id=4, username=None, first_name=None, last_name=None),
        ]
        output = StringIO()
        with (
            patch.object(
                all_members, "resolve_chat", new_callable=AsyncMock
            ) as resolve,
            patch.object(
                all_members, "fetch_all_participants", new_callable=AsyncMock
            ) as fetch,
            redirect_stdout(output),
        ):
            resolve.return_value = self.chat
            fetch.return_value = users
            result = await all_members.run(
                argparse.Namespace(chat="@sample_group", new_line=False), self.runtime
            )

        self.assertEqual(result, 0)
        resolve.assert_awaited_once_with(self.runtime.client, "@sample_group")
        fetch.assert_awaited_once_with(self.runtime.client, self.chat)
        self.assertEqual(output.getvalue(), "@admin Bob Jones (ID: 2) ID: 4\n")

    async def test_new_line_prints_each_member_on_separate_line(self) -> None:
        users = [
            SimpleNamespace(id=1, username="alice", bot=False),
            SimpleNamespace(id=2, username="bob", bot=False),
        ]
        output = StringIO()
        with (
            patch.object(
                all_members, "resolve_chat", new_callable=AsyncMock
            ) as resolve,
            patch.object(
                all_members, "fetch_all_participants", new_callable=AsyncMock
            ) as fetch,
            redirect_stdout(output),
        ):
            resolve.return_value = self.chat
            fetch.return_value = users
            await all_members.run(
                argparse.Namespace(chat="@sample_group", new_line=True), self.runtime
            )

        self.assertEqual(output.getvalue(), "@alice\n@bob\n")

    async def test_private_message_link_selects_group_without_reading_message(
        self,
    ) -> None:
        output = StringIO()
        with (
            patch.object(
                all_members, "resolve_chat", new_callable=AsyncMock
            ) as resolve,
            patch.object(
                all_members, "fetch_all_participants", new_callable=AsyncMock
            ) as fetch,
            redirect_stdout(output),
        ):
            resolve.return_value = self.chat
            fetch.return_value = [SimpleNamespace(id=1, username="alice", bot=False)]
            await all_members.run(
                argparse.Namespace(chat="https://t.me/c/2546560986/1", new_line=False),
                self.runtime,
            )

        resolve.assert_awaited_once_with(self.runtime.client, -1002546560986)
        fetch.assert_awaited_once_with(self.runtime.client, self.chat)
        self.assertEqual(output.getvalue(), "@alice\n")

    async def test_prints_long_list_without_splitting(self) -> None:
        users = [
            SimpleNamespace(id=index, username=f"person_{index}", bot=False)
            for index in range(1, 600)
        ]
        output = StringIO()
        with (
            patch.object(
                all_members, "resolve_chat", new_callable=AsyncMock
            ) as resolve,
            patch.object(
                all_members, "fetch_all_participants", new_callable=AsyncMock
            ) as fetch,
            redirect_stdout(output),
        ):
            resolve.return_value = self.chat
            fetch.return_value = users
            await all_members.run(
                argparse.Namespace(chat="-100123", new_line=False), self.runtime
            )

        self.assertGreater(len(output.getvalue()), 4096)
        self.assertEqual(
            output.getvalue(),
            " ".join(f"@person_{i}" for i in range(1, 600) if i != 99) + "\n",
        )
        resolve.assert_awaited_once_with(self.runtime.client, -100123)

    async def test_incomplete_list_does_not_print_partial_output(self) -> None:
        output = StringIO()
        with (
            patch.object(
                all_members, "resolve_chat", new_callable=AsyncMock
            ) as resolve,
            patch.object(
                all_members, "fetch_all_participants", new_callable=AsyncMock
            ) as fetch,
            redirect_stdout(output),
        ):
            resolve.return_value = self.chat
            fetch.side_effect = AutomationError("Incomplete member list")
            with self.assertRaisesRegex(AutomationError, "Incomplete member list"):
                await all_members.run(
                    argparse.Namespace(chat="@sample_group", new_line=False),
                    self.runtime,
                )
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
