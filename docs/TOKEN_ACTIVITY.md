# Token activity

The profile card uses real daily account aggregates from the official Codex App
Server [`account/usage/read` method](https://learn.chatgpt.com/docs/app-server).
It shows relative activity as colors, without publishing exact token counts,
totals, account identifiers, credentials, or conversations.

This is the account activity returned by that service. It is not a claim to cover
every ChatGPT interface or OpenAI API request. API-key-only accounts are not
supported by this endpoint. Use an existing supported ChatGPT account login in
Codex; no OpenAI API key is needed by these scripts.

## What the colors mean

The card contains 53 calendar weeks, with Sunday at the top of each column.
Positive daily counts use four intensity levels relative to the largest reported
daily count in the visible window. Exact counts and thresholds are never embedded
in the SVG, including tooltips and accessibility labels.

- A dark filled square is an explicitly reported zero.
- A dashed outline means no data was reported for that date.
- Dates after today are blank.
- Dates supplied by the service are preserved. Its bucket timezone and historical
  completeness are not documented; the calendar cutoff and update date use UTC.

The Mac keeps aggregate counts only in process memory. Missing, null, invalid, or
empty daily data fails the update and retains the previous published card. A
failed update therefore leaves the card's previous update date visible.

## Update locally

Requirements: Python 3.9+, an installed official Codex CLI that supports
`account/usage/read`, and an existing supported Codex login. There are no Python
package dependencies. From this repository:

```sh
python3 scripts/update_token_activity.py
```

This reads actual account aggregates and replaces only the local
`assets/chatgpt-token-activity.svg`. It does not publish anything. Use `--output`
for a different preview location, or `--codex /absolute/path/to/codex` to select a
CLI. Discovery checks PATH, official app bundle locations, then installed VS Code
Codex extension executables; it never opens credential or conversation files.

The client sends only `initialize`, `initialized`, and `account/usage/read`.
Codex manages its existing authentication internally and may refresh it itself.
The collector does not request login, logout, authentication refresh, model
inference, or thread access. CLI diagnostic output and raw server error messages
are discarded.

## Publish the card

After the initial implementation and card are reviewed and merged into `main`,
use an existing GitHub CLI login with write access to this profile repository:

```sh
python3 scripts/update_token_activity.py --publish
```

The publisher checks that `wondukseo/wondukseo` still exists as a public,
unarchived repository whose default branch is `main`. It changes only the existing
`assets/chatgpt-token-activity.svg` via GitHub's Contents API. A matching file SHA
prevents overwriting concurrent edits; branch protection is respected. The commit
uses the owner's GitHub noreply email. Identical SVG content produces no commit.
No local changes are staged or uploaded, and no repository or visibility setting
is created or changed.

Only this public profile repository is an allowed publication target. Private
application repositories and their release workflows are unrelated to this card.

## Automatic updates on a Mac

First preview the installation plan:

```sh
python3 scripts/install_token_activity_agent.py
```

To install the reviewed local runner and enable updates:

```sh
python3 scripts/install_token_activity_agent.py --apply
```

The installer copies only the four collector/renderer/updater Python files into
`~/Library/Application Support/ChatGPT Token Activity/runner`. It creates a user
LaunchAgent, `local.wonduk.chatgpt-token-activity`, that runs at login/load and
every four hours while the Mac is awake and the user is logged in. It uses the
existing Codex and GitHub CLI sessions on that Mac. The CLI binaries can still
update independently; the runner code is a fixed local copy of this installation.

No account files, raw usage JSON, or secrets are copied. Local status logs are
private and contain no exact counts or server replies. Existing installations are
preserved rather than overwritten. A failed account read, missing login, revoked
GitHub access, branch restriction, or offline Mac leaves the previous public card
in place. GitHub image caching may delay display after a successful update.

Install on only one Mac at a time for the same card: each publisher replaces the
card from its signed-in account, rather than adding local device totals. Another
Mac with the same account can run the manual preview and publishing commands.

To stop automatic updates without deleting files or account data:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.wonduk.chatgpt-token-activity.plist"
```

Move that plist out of `~/Library/LaunchAgents` to keep it disabled at the next
login. To update the runner later, stop it and archive its existing app-specific
installation folder and plist before installing a freshly reviewed version.

## Verify changes

```sh
python3 -m unittest discover -s tests -v
git diff --check
```

Tests use synthetic data, fake subprocesses, and temporary directories. They do
not read an account, access GitHub, install a real LaunchAgent, or publish data.
