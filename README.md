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
| `poll check` | Report options with at least the minimum vote count | Only with `--send-to-user USER` and matching options |
| `poll check-payments` | Compare selected poll options’ voters with message authors and mentions in a forum topic | None |
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

## Check poll vote counts

```bash
telegram-automations poll check \
  --poll-link "https://t.me/c/1234567890/42" \
  --minimum 15
```

`--poll-link` is required. `--minimum` is a positive integer, defaults to 15,
and is inclusive: an option with exactly 15 votes qualifies. All options,
including those starting with emoji, are considered in their original order.
Open, closed, and anonymous group polls are supported using aggregate counts;
no voter identities are retrieved. Hidden or incomplete results cause an error;
the command never votes automatically.

Example output:

```text
Poll: Which day works for you?
https://t.me/c/1234567890/42

Options with at least 15 votes:
• Friday — 18 votes
• Saturday — 15 votes
• 👋 Pass — 20 votes
```

Add `--send-to-user USER` to send the same plain-text report to an explicit recipient.
There is no default recipient:

```bash
telegram-automations poll check \
  --poll-link "https://t.me/c/1234567890/42" --send-to-user @recipient
```

Without `--send-to-user`, nothing is sent. With no qualifying options, the command prints
`No options with at least 15 votes.`, exits successfully, and sends nothing.
Every invocation with matching options sends again; there is no notification history.
After all parts are sent, the command prints `Report sent to @recipient.`
Long reports are split into Telegram-sized messages. A delivery error stops the
run with a nonzero exit code; earlier parts may already have been delivered.

For cron, sign in interactively first with the same configuration directory.
Without a terminal, an unauthorized session fails with a clear error instead of
prompting. The `scripts/run_check.sh` wrapper accepts exactly three arguments:

```bash
./scripts/run_check.sh "https://t.me/c/1234567890/42" 15 @recipient
```

It finds the project and its `.venv` relative to the script, uses the project as
`--config-dir`, and timestamps both stdout and stderr in
`${XDG_CACHE_HOME:-$HOME/.cache}/telegram-poll-check.log`. A shared lock in the
same directory prevents overlapping checks; a busy lock skips the run successfully.
Command failures retain their nonzero exit status. Log output is appended;
rotation is not configured.

Example hourly cron entry (replace the script path and poll URL):

```cron
0 * * * * /path/to/telegram-automations/scripts/run_check.sh "https://t.me/c/1234567890/42" 15 @recipient
```

This example does not install a cron job.

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

## Check payment messages against poll options

This read-only command compares the people who selected any of the chosen poll options with
message authors and explicitly mentioned users in a forum topic in the same group.

```bash
telegram-automations poll check-payments \
  --option "https://t.me/c/1234567890/42?option=MQ" \
  --option "https://t.me/c/1234567890/42?option=Mg" \
  --since-message "https://t.me/c/1234567890/30/31"
```

Repeat `--option` to combine options from the same poll. Each person is counted
once, even if they selected several options; duplicate option links are ignored.

Alternatively, select the answer with `--poll-link` and exact, case-sensitive
`--answer` text instead of `--option`. The `--since-message` link must identify
both the forum topic and its starting message. That message is excluded; only
later messages and replies within that topic count, up to the cutoff captured at startup.
Other topics and older messages are excluded.

Any ordinary message counts for its author, including a photo or document without
a caption. Explicit `@username` and Telegram user mentions in message text or an
attachment caption also count for the mentioned person. Usernames are resolved
without case sensitivity; people are matched by Telegram ID. Service messages,
forwarded-author headers, and reply headers do not count as payment by another
person. Attachment contents and payment amounts are not inspected.

The report prints three disjoint lists in this order:

1. Selected at least one chosen option, but no payment message or mention was found.
2. Did not select any chosen option, but payment was counted.
3. Selected at least one chosen option and payment was counted.

Immediately before the third list, **Sent messages without media (possibly cash)**
shows authors of at least one ordinary message without media, with profile and
source-message links. This supplementary list includes authors regardless of their
poll choice, and they remain in their main lists. Mentions alone do not qualify;
posting a photo or file as well does not remove an author from this list.
The list indicates possible cash payments, not confirmed payment methods.

For example, if `@a`, `@b`, `@c`, and `@d` selected the option, `@a` wrote
"for two @b", `@c` uploaded a photo, and `@f` posted a message, the lists contain
`@d`; `@f`; and `@a`, `@b`, `@c`, respectively. Selecting a different answer still
belongs in the second list when payment is counted.

Each person appears once in their list. People credited by the same first message
are grouped together, and all source-message links retain author and mention
attribution. Each username row includes a plain `https://t.me/username` profile URL
for convenient opening from a terminal. Users without usernames are shown by name
and ID. Unresolved mentions
and unidentified authors appear under **Needs review**; they are not guessed from
display names. Counts, timestamps, and the message range describe the snapshot;
an open poll is marked as changeable. A payment status means only that the
message-or-mention rule matched.

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
