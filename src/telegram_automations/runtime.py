from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors

from telegram_automations.common import (
    AppConfig,
    AutomationError,
    create_client,
    friendly_rpc_error,
    load_config,
    tighten_session_permissions,
)


@dataclass(frozen=True)
class CommandRuntime:
    config_dir: Path
    config: AppConfig
    client: TelegramClient
    me: Any


CommandHandler = Callable[[argparse.Namespace, CommandRuntime], Awaitable[int]]


@dataclass(frozen=True)
class ErrorPolicy:
    flood_wait: str = (
        "Telegram requested a delay. Do not retry for at least {seconds} seconds."
    )
    peer_flood: str = (
        "Telegram restricted the account (PeerFlood). Stop and check @SpamBot."
    )
    system_error: str = "System or network error: {error}"


async def execute_command(args: argparse.Namespace) -> int:
    config_dir = args.config_dir.expanduser().resolve()
    config = load_config(config_dir)
    client = create_client(config)
    try:
        await client.start(
            phone=lambda: input(
                "Please enter your phone number (bot tokens are not supported): "
            )
        )
        tighten_session_permissions(config.session_path)
        me = await client.get_me()
        if me is None:
            raise AutomationError("Could not identify the authorized account.")
        if bool(getattr(me, "bot", False)):
            raise AutomationError(
                "A user account is required; bot tokens are not supported."
            )

        runtime = CommandRuntime(
            config_dir=config_dir,
            config=config,
            client=client,
            me=me,
        )
        handler: CommandHandler = args.command_handler
        return await handler(args, runtime)
    finally:
        await client.disconnect()
        tighten_session_permissions(config.session_path)


def run(args: argparse.Namespace) -> int:
    policy: ErrorPolicy = args.error_policy
    try:
        return asyncio.run(execute_command(args))
    except AutomationError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except errors.FloodWaitError as error:
        print(policy.flood_wait.format(seconds=error.seconds), file=sys.stderr)
        return 3
    except errors.PeerFloodError:
        print(policy.peer_flood, file=sys.stderr)
        return 3
    except errors.RPCError as error:
        print(f"Error: {friendly_rpc_error(error)}", file=sys.stderr)
        return 2
    except OSError as error:
        print(policy.system_error.format(error=error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Operation cancelled.", file=sys.stderr)
        return 130


def main(
    build_parser: Callable[[], argparse.ArgumentParser],
    argv: Sequence[str] | None = None,
) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    return run(args)
