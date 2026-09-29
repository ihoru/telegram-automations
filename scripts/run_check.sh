#!/usr/bin/env bash
# Run a poll check from cron, with a shared session lock and timestamped logs.
set -euo pipefail
umask 077

if [[ $# -ne 3 || -z ${1:-} || ! ${2:-} =~ ^[1-9][0-9]*$ || -z ${3:-} ]]; then
    printf 'Usage: %s POLL_URL MINIMUM USERNAME\nMINIMUM must be a positive integer; USERNAME accepts an optional @.\n' "$0" >&2
    exit 2
fi

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cache_dir=${XDG_CACHE_HOME:-${HOME:?HOME must be set}/.cache}
mkdir -p -- "$cache_dir"
log_file="$cache_dir/telegram-poll-check.log"
lock_file="$cache_dir/telegram-poll-check.lock"

# Lock all checks sharing this session, including checks for different polls.
exec 9>"$lock_file"
if /usr/bin/flock -n -E 75 9; then
    :
else
    status=$?
    if [[ $status -eq 75 ]]; then
        exit 0
    fi
    exit "$status"
fi

# Merge stderr before the pipe so errors receive timestamps as well.
# pipefail preserves a failed check's exit status; Python output is unbuffered.
{
    executable="$project_dir/.venv/bin/telegram-automations"
    if [[ ! -x $executable ]]; then
        printf 'Error: executable not found: %s\nInstall the project in its .venv first.\n' "$executable" >&2
        exit 2
    fi
    PYTHONUNBUFFERED=1 "$executable" --config-dir "$project_dir" poll check \
        --poll-link "$1" --minimum "$2" --send-to-user "$3"
} 2>&1 | while IFS= read -r line || [[ -n $line ]]; do
    printf '[%(%F %T %z)T] %s\n' -1 "$line"
done >>"$log_file" 2>&1
