from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram_automations.common import (
    AppConfig,
    AutomationError,
    load_config,
)
from telegram_automations.runtime import (
    ErrorPolicy,
    execute_command,
    run,
)


class ConfigTests(unittest.TestCase):
    def test_loads_dotenv_and_resolves_relative_session_from_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / ".env").write_text(
                "TELEGRAM_API_ID=123\n"
                "TELEGRAM_API_HASH=test-hash\n"
                "TELEGRAM_SESSION=sessions/main\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(config_dir)

        self.assertEqual(config.api_id, 123)
        self.assertEqual(config.api_hash, "test-hash")
        self.assertEqual(config.session_path, config_dir / "sessions/main")


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_one_client_and_always_disconnects(self) -> None:
        client = SimpleNamespace(
            start=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(id=7, bot=False)),
            disconnect=AsyncMock(),
        )
        handler = AsyncMock(return_value=0)
        args = argparse.Namespace(
            config_dir=Path("."),
            command_handler=handler,
        )
        config = AppConfig(123, "hash", Path("session"))

        with (
            patch("telegram_automations.runtime.load_config", return_value=config),
            patch("telegram_automations.runtime.create_client", return_value=client),
            patch("telegram_automations.runtime.tighten_session_permissions") as tighten,
        ):
            result = await execute_command(args)

        self.assertEqual(result, 0)
        client.start.assert_awaited_once()
        client.get_me.assert_awaited_once_with()
        client.disconnect.assert_awaited_once_with()
        self.assertEqual(tighten.call_count, 2)
        runtime = handler.await_args.args[1]
        self.assertIs(runtime.client, client)
        self.assertEqual(runtime.config, config)


class RuntimeErrorTests(unittest.TestCase):
    def test_expected_error_uses_exit_code_two(self) -> None:
        async def fail(_args: argparse.Namespace) -> int:
            raise AutomationError("expected failure")

        args = argparse.Namespace(error_policy=ErrorPolicy())
        stderr = io.StringIO()
        with (
            patch("telegram_automations.runtime.execute_command", new=fail),
            patch("sys.stderr", stderr),
        ):
            result = run(args)

        self.assertEqual(result, 2)
        self.assertEqual(stderr.getvalue(), "Error: expected failure\n")
