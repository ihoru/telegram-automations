# Telegram Automations

[![CI](https://github.com/ihoru/telegram-automations/actions/workflows/ci.yml/badge.svg)](https://github.com/ihoru/telegram-automations/actions/workflows/ci.yml)

A safety-first Python and [Telethon](https://docs.telethon.dev/) CLI for auditable
Telegram poll analysis and group cleanup.

The toolkit consolidates several one-off automations behind one account configuration,
shared Telegram adapters, and independently testable decision policy. Destructive
commands default to a live-rechecked dry run and require both `--execute` and typed
confirmation before removing anyone.

## Commands

| Command | Purpose | Telegram writes |
| --- | --- | --- |
| `poll list-without-answer` | List poll participants who did not select one exact answer | None |
| `poll list-non-voters` | Export current group members who did not vote in a closed poll | Local JSON only |
| `poll remove-non-voters` | Recheck an export and optionally remove eligible members | Only with `--execute` and typed confirmation |

Every command is available through either entry point:

```bash
telegram-automations --help
python -m telegram_automations --help
```

## Safety model

- Read-only analysis refuses incomplete voter or member snapshots.
- Exports exclude administrators, recent joiners, unverifiable members, and explicit
  protected accounts.
- Removal reloads the poll and membership before the run, then rechecks each candidate
  immediately before acting.
- Limits, randomized delays, audit records, and stop-on-rate-limit behavior bound the
  impact of a bad or interrupted run.

> [!WARNING]
> These commands act through a Telegram user account. Telegram may restrict or
> ban accounts that perform automated or high-volume actions. Test with a
> separate account and group, review every dry run, use small batches, and never
> try to bypass a Telegram rate limit.

## Requirements

- Python 3.12 or newer.
- A Telegram user account; bot tokens are not supported.
- Telegram `api_id` and `api_hash` credentials from
  [my.telegram.org](https://my.telegram.org).
- Access to the target group and poll.
- Administrator permission to remove members when using the removal command.

Only basic groups and supergroups are supported. Broadcast channels are not.
Poll voter identities are available only for non-anonymous polls, and the
authorized account must vote before Telegram will return the voter list.

## Installation

```bash
git clone git@github.com:ihoru/telegram-automations.git
cd telegram-automations
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

For linting and tests:

```bash
python -m pip install -e ".[dev]"
```

## Configuration

Create the local configuration and protect it:

```bash
cp .env.example .env
chmod 600 .env
```

Fill in the credentials:

```dotenv
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION=telegram_automations
```

On the first run, Telethon asks for the account phone number, login code, and
2FA password when enabled. Authorization is stored in the local session file.

By default, the CLI reads `.env`, the relative session path, and
`exclusions.txt` from the current working directory. Use a different directory
with the global option before `poll`:

```bash
telegram-automations --config-dir /path/to/private/config poll list-non-voters \
  --poll-link "https://t.me/c/1234567890/42"
```

Output paths such as `non_voters.json` and `removal_results.jsonl` remain
relative to the current working directory unless an explicit path is supplied.

## Poll participants without an answer

This read-only command lists people who participated in a poll but did not
select one exact answer.

Select the answer by exact, case-sensitive text:

```bash
telegram-automations poll list-without-answer \
  --poll-link "https://t.me/c/1234567890/42" \
  --answer "Exact answer text"
```

Or use Telegram's link for a specific poll option:

```bash
telegram-automations poll list-without-answer \
  --option "https://t.me/c/1234567890/42?option=MA"
```

Show the choices made by each listed voter:

```bash
telegram-automations poll list-without-answer \
  --option "https://t.me/c/1234567890/42?option=MA" \
  --voted-for
```

Open polls are allowed, but the result is only a point-in-time snapshot. The
command aborts if the voter count changes while pages are being retrieved.
Multiple-choice and free-text vote records are supported; Telegram does not
expose the submitted free-text value.

## Export poll non-voters

This command finds current group members who did not participate in a closed,
non-anonymous poll. It prints the candidates and saves an auditable JSON file.

```bash
telegram-automations poll list-non-voters \
  --poll-link "https://t.me/c/1234567890/42" \
  --output non_voters.json
```

The group and message can also be supplied separately:

```bash
telegram-automations poll list-non-voters \
  --chat "@group_username" \
  --message-id 42
```

The export excludes the group owner, administrators, the current account,
members who joined after the poll, members with an unverifiable join date, and
entries protected by `exclusions.txt`. Bots and deleted accounts remain
candidates because they cannot vote.

To protect additional people, copy the example file and put one username or
numeric user ID on each line:

```bash
cp exclusions.example.txt exclusions.txt
chmod 600 exclusions.txt
```

Numeric IDs are safer because usernames can change or be transferred.

The command refuses incomplete member or voter lists and aborts if membership
or vote counts change during export.

## Review and remove non-voters

The removal command accepts the schema-version-1 JSON produced by this toolkit
or the archived `telegram-poll-cleanup` repository.

Always run the default dry run first. It performs all live rechecks but does
not remove anyone:

```bash
telegram-automations poll remove-non-voters
```

Use a different export or exclusions file when needed:

```bash
telegram-automations poll remove-non-voters \
  --input /path/to/non_voters.json \
  --exclusions /path/to/exclusions.txt
```

After reviewing the final live candidate list, explicitly enable actions:

```bash
telegram-automations poll remove-non-voters --limit 1 --execute
```

Before the first removal, the command requires typed confirmation in the form
`REMOVE N`, where `N` is the displayed candidate count.

Defaults:

```text
limit:       10 members per run
min delay:   15 seconds
max delay:   30 seconds
input:       non_voters.json
log:         removal_results.jsonl
```

`--batch-size` remains an alias for `--limit`.

Immediately before acting, the command reloads the poll and group membership.
Before each person, it checks again for a changed vote, rejoin, administrator
promotion, existing restriction, or new exclusion.

For a supergroup, removal uses one 10-minute temporary ban calculated from
Telegram server time. The user can rejoin after it expires. A `kick_started`
record is written before the request; after an uncertain interruption, further
removals are blocked until the recorded safety period has expired. The command
does not send a second automatic unban request.

For a basic group, the command uses Telegram's kick operation without a
permanent ban. Any `FloodWaitError` or `PeerFloodError` stops execution
immediately.

## Adding a command

New commands live under `src/telegram_automations/commands`. A command module
only needs to:

1. configure its command-specific `argparse` parser;
2. implement an async handler accepting `args` and `CommandRuntime`;
3. register its name, help text, handler, and error policy in the CLI registry.

`CommandRuntime` already provides the resolved configuration directory,
validated user account, started Telethon client, and session configuration.
The shared runtime owns `asyncio`, authentication, disconnection, permission
tightening, and top-level error-to-exit-code handling.

Keep Telegram API adapters and decision policy out of the command module so
they remain independently testable.

## Development

```bash
ruff check .
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m pip wheel --no-deps . --wheel-dir dist
```

The test suite uses fakes and does not connect to Telegram or remove members.
GitHub Actions runs the same checks and smoke-tests every help entry point.

## Data security

The following local files can contain credentials, authenticated sessions, or
personal data and are excluded from Git:

- `.env`
- `*.session*`
- `exclusions.txt`
- `non_voters*.json`
- `removal_results*.jsonl`

Do not publish them, put them in cloud-synced folders, or send them in chat.
Use mode `0600` where supported. If a session may have been exposed, terminate
it in Telegram under **Settings -> Devices**.

## License

Released under the [MIT License](LICENSE).
