# matrixcli

A Matrix client that runs in your terminal.

matrixcli is a full-screen terminal client built with Textual and matrix-nio. The home screen gives you Spaces, recent and favourite rooms, invites and DMs in one place, with unread counts and mentions visible without opening each room.

It supports encrypted rooms, threads, reactions, edits, message history, files, local search and image previews. Navigation is deliberately Vim-ish: `hjkl`, counts such as `10j`, `g`/`G`, a command line, a jumplist and a few other familiar motions are used throughout.

Python 3.10–3.14 is supported.

## Installation

Clone the repository and run the launcher:

```sh
cd matrixcli
bin/matrix
```

The launcher creates the Poetry environment and installs the project on the first run. After that it starts matrixcli directly from the virtualenv. If `poetry.lock` changes after a pull, it runs the install again before starting.

You need:

- Python 3.10, 3.11, 3.12, 3.13 or 3.14
- Poetry

If several Python versions are installed and Poetry picks the wrong one:

```sh
poetry env use python3.12
poetry install
```

You can also skip the launcher and use Poetry directly:

```sh
poetry install
poetry run matrix
```

Or install into a normal virtualenv without Poetry:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/matrix
```

The last method installs from `pyproject.toml` rather than using the exact versions in `poetry.lock`.

The launcher follows symlinks, so it can also be put somewhere on your `PATH`:

```sh
ln -s /path/to/matrixcli/bin/matrix ~/.local/bin/matrix
```

After that, just run:

```sh
matrix
```

## First run

There is no config file to prepare beforehand.

On the first launch matrixcli asks for:

- your Matrix user ID, for example `@you:example.org`
- your password
- your homeserver

The homeserver may be left empty. matrixcli will then try Matrix `.well-known` discovery using the domain from your user ID.

Once login succeeds it writes:

```text
~/.config/matrixcli/config.ini
```

and keeps the access token and local encryption-store secret for future launches.

On systems with a working keyring, secrets are stored in the macOS Keychain or the Linux Secret Service/KWallet backend.

On machines without a usable keyring — common over SSH, in containers and on servers — the session information falls back to:

```text
~/.local/share/matrixcli/secrets.json
```

That file is created with mode `0600`. The Matrix password itself is not written to that fallback file; without a keyring it is only kept for the login that needs it.

## Using matrixcli

Start it with:

```sh
bin/matrix
```

The home screen is split into three columns.

The left side contains Spaces and their rooms. Rooms which are not part of a space are shown separately.

The middle contains pending invites, recently opened rooms and favourites.

The right side contains DMs, ordered by recent activity.

Unread rooms are marked directly in the lists, and mentions get their own highlight. The dashboard stays live while matrixcli is connected.

### Home screen

The keys used most often are:

| Key | Action |
| --- | --- |
| `j` / `k` | Move down / up |
| `h` / `l` | Previous / next column |
| `Tab` / `Shift+Tab` | Previous / next column |
| `Enter` | Open the selected room, DM or invite |
| `/` | Search |
| `f` | Favourite / unfavourite a room |
| `c` | Enable or disable disk caching for the selected space |
| `S` | Download full history for all rooms |
| `+` / `-` | Resize Recent or Favourites |
| `q` | Quit |

Arrow keys work as well, but the navigation model is built around `hjkl`.

`j` and `k` treat everything in a column as one continuous list. For example, moving down from the bottom of Spaces continues into the room list underneath it.

Counts work in the usual Vim style:

```text
5j
12k
```

### Search

Press `/` from the dashboard to search people, rooms and locally downloaded messages.

Search is accent-insensitive, so a plain spelling can still find names containing accents.

When the cursor is inside Recent or Favourites, `/` searches that section rather than the entire account. This is also useful for getting at rooms which do not currently fit in the visible section.

Message search is local. A message can only appear once matrixcli has downloaded it.

If you want complete local search across your account, press `S` on the home screen once and let the history sync finish.

### Inside a room

Basic movement:

| Key | Action |
| --- | --- |
| `j` / `k` | Next / previous message |
| `g` | Oldest message |
| `G` | Newest message |
| `u` | First unread message |
| `Ctrl+D` / `Ctrl+U` | Half-page down / up |
| `Ctrl+F` / `Ctrl+B` | Page down / up |
| `{` / `}` | Previous / next block of messages by sender |
| `/` | Search this room |
| `q` | Return to the dashboard |
| `Esc` | Go back one screen |

`g` can take you all the way into downloaded history. `G` returns to the live end of the room.

Counts also work here:

```text
10j
25k
100G
```

`100G` jumps to message 100, counted from the start of the room.

The command line can do the same thing:

```text
:1
:100
:10000
```

A number beyond the end simply lands on the newest message.

Long jumps are recorded in a jumplist. Use `Ctrl+O` to go back and `Ctrl+I` or `Tab` to go forward again.

### Writing messages

Press:

```text
n
```

to write a new message, or:

```text
r
```

to reply to the selected message.

The editor opens underneath the timeline.

`Enter` sends. `Shift+Enter` inserts a newline. `Alt+Enter` is available for terminals which cannot distinguish Shift+Enter from Enter.

`Esc` closes the editor but keeps the draft for the rest of the session.

For your own messages:

```text
e    edit
d    delete
```

Deletion asks for confirmation.

### Reactions

Press `a` on a message.

Numbers `1` through `9` select from the quick reaction row. `/` opens emoji search, using Unicode names.

Selecting a reaction you already sent removes it.

### Threads

Threads can either stay collapsed in the normal room timeline or be shown inline.

```text
l    enter/unfold a thread
h    leave/fold a thread
t    toggle threaded view
T    open the selected thread full-screen
```

When a thread is opened full-screen the composer is ready immediately and sends into that thread.

### Files, links and images

`Enter` acts on the selected message when it contains something actionable.

For a file, it opens a download destination picker. Encrypted Matrix attachments are decrypted during download.

For a web link, it opens the link using the system browser.

When there is more than one possible action, matrixcli shows an action menu instead.

Press `Space` on an image to preview it directly in the terminal.

The default preview uses coloured half-block characters. Press `~` while the preview is open to switch between that and ASCII rendering. The choice is remembered.

On 256-colour terminals the block preview is dithered. On basic 16-colour terminals matrixcli uses the ASCII renderer.

`j` and `k` inside the preview move through other images in the room.

### Edits and deleted messages

Edited messages are displayed once using the current text rather than appearing as a stream of corrections.

When a message has older versions available, press:

```text
Shift+Enter
```

to inspect them. `Alt+Enter` does the same thing on terminals where Shift+Enter cannot be detected separately.

A deleted message is shown as deleted rather than silently disappearing. If the running session saw the original before its deletion, matrixcli can show that copy while the process is still running. Once it is gone from the server and the session is restarted, only the deletion remains.

## A few useful tricks

### Jump straight to unread

Inside a room:

```text
u
```

goes to the first unread message. New messages are separated from older history by a `new` divider.

### Get complete offline search

Message search only knows about history matrixcli has downloaded.

From the dashboard, press:

```text
S
```

to work through every room and populate the local archives.

This happens one room at a time rather than hammering the homeserver with every history request at once.

### Disable message caching for a space

Select a space and press:

```text
c
```

This toggles persistent message caching for the rooms in that space.

It is useful for spaces where you do not want decrypted message history kept locally.

To disable the persistent message cache globally, set:

```ini
[cache]
messages = false
```

matrixcli will then keep message history in memory only.

### Change names to Matrix IDs

Inside a room:

```text
~
```

toggles the sender column between display names and raw Matrix IDs.

### Compact busy rooms

Press:

```text
c
```

inside a room to toggle compact mode. This removes the extra spacing between speaker blocks.

### Commands

Press `:` when an editor is not focused.

Useful commands include:

```text
:q
:q!
:settings
:set
:verify
:help
:?
```

`:q` closes the current page. `:q!` exits immediately from anywhere.

`:settings` lets you change account/display settings including your display name, terminal-title unread count and timestamp timezone.

The timezone accepts an IANA name such as:

```text
Europe/Amsterdam
```

Leave it empty to use the operating system's timezone.

## Using matrixcli from scripts

The TUI is not required just to find out whether something is waiting.

Run:

```sh
bin/matrix --check
```

This performs an incremental sync, prints the unread state and exits.

A normal interactive run must have completed at least once before using `--check`, because the initial account state is built by the application during that first run.

### Output formats

Plain output is the default:

```sh
bin/matrix --check
```

Just print the total count:

```sh
bin/matrix --check --format count
```

JSON:

```sh
bin/matrix --check --format json
```

Or use the exit status only:

```sh
bin/matrix --check --format quiet
```

`stdout` is reserved for the requested result. Warnings and errors go to `stderr`, which makes the command safe to use in scripts.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | There are unread messages or invites |
| `1` | Nothing new |
| `2` | The check could not be completed reliably |

For example:

```sh
if bin/matrix --check --format quiet; then
    notify-send "Matrix has new messages"
fi
```

A connection failure can still have last-known counts available. In that case matrixcli reports them as stale but exits with code `2`, so a script does not mistake cached information for a successful live check.

### JSON output

The JSON form contains the aggregate unread state as well as room-level information.

The main fields are:

```text
unread
highlights
invites
total
new
rooms
invited
source
ok
```

`rooms` contains the room ID, title, unread count, highlight count and whether the room is a DM.

`source` tells you where the reading came from:

```text
sync     fresh sync with the homeserver
cache    the running matrixcli instance's saved snapshot
stale    last known state because a fresh sync failed
```

When matrixcli itself is already open, `--check` does not try to open its encryption store a second time. It reads the live application's saved unread snapshot instead.

## Custom config files

The default configuration is:

```text
~/.config/matrixcli/config.ini
```

Use another one with:

```sh
bin/matrix --config /path/to/config.ini
```

or:

```sh
bin/matrix -c /path/to/config.ini
```

The `MATRIXCLI_CONFIG` environment variable is also supported.

A normal generated configuration looks roughly like this:

```ini
[matrix]
homeserver = https://matrix.org
user_id = @you:matrix.org
device_name = matrixcli
room =

[keychain]
service = matrix-cli

[storage]
store_path =
state_path =

[cache]
messages = true
```

`room` is optional. Set it to a room ID or canonical alias to open that room instead of the dashboard at startup.

`store_path` and `state_path` default to matrixcli's directory under `~/.local/share/`.

When using separate configs for different accounts, give them separate storage paths as well. Matrix encryption stores are device/account state and should not be shared between accounts.

### Sending to unverified devices

matrixcli normally sends encrypted messages even when one of the recipient devices has not been verified. This avoids a new or rotated device blocking a send.

For stricter behaviour:

```ini
[matrix]
allow_unverified = false
```

## Device verification

Open:

```text
:verify
```

to see the Matrix sessions on your account and their verification state.

There are two useful flows.

Press `k` to unlock your account's cross-signing keys using the Matrix Security Key or Security Phrase and sign this matrixcli session.

Select another session and press `v` to start an emoji/SAS verification with it.

Select the current session and press `v` to wait for a verification request started by another Matrix client.

During the emoji comparison:

```text
y    emojis match
n    reject
Esc  cancel
```

There is also a non-TUI responder:

```sh
bin/matrix --verify
```

This is useful on a server or over SSH when another Matrix client is starting the verification.

Cross-signing must already exist on the account. For a completely new Matrix account, use Element once to initialise cross-signing and create a Security Key. After that matrixcli can use the existing identity.

## Encrypted history

matrixcli uses matrix-nio's end-to-end encryption support.

A newly created Matrix session does not automatically possess every Megolm key for messages sent before that device had access to the room. If old encrypted messages cannot be decrypted, import the room keys from another client which has them.

Export the E2EE room keys from Element and run:

```sh
bin/matrix --import-keys element-keys.txt
```

You will be asked for the export passphrase.

The other direction works too:

```sh
bin/matrix --export-keys ~/matrixcli-keys.txt
```

matrixcli generates an encryption passphrase for the export and prints it once. Save it somewhere separate from the file; matrixcli does not store it.

The exported file contains keys capable of decrypting your Matrix history. Treat it accordingly.

matrix-nio does not provide matrixcli with server-side key-backup support, so importing/exporting room keys is currently the way to move older history between installations.

## Where files are stored

| Data | Default location |
| --- | --- |
| Configuration | `~/.config/matrixcli/config.ini` |
| Encryption store | `~/.local/share/matrixcli/store/` |
| UI/account state | `~/.local/share/matrixcli/state.json` |
| Secret fallback without a keyring | `~/.local/share/matrixcli/secrets.json` |
| Message/archive/media cache | below the encryption store |

Persistent message and media caches contain decrypted application data and are encrypted at rest.

Set:

```ini
[cache]
messages = false
```

if you do not want matrixcli to retain message history between runs.

## One instance at a time

Only one interactive matrixcli process may use a store at once.

The encryption database, local archives and account state are shared files, so starting two writers against them would risk corrupting or reverting state. A second normal launch therefore exits rather than opening the same store.

There is no stale-lock-file cleanup to worry about: the operating system releases the lock when the process exits.

`--check` is the exception. If matrixcli is running, it reads the application's saved unread snapshot instead of opening another live instance.

## Development

Source code lives in:

```text
src/matrixcli/
```

Install the development dependencies with Poetry and run the tests with:

```sh
poetry install
poetry run pytest
```

The executable entry point is:

```text
matrixcli.app:main
```

The `bin/matrix` script is a launcher around that entry point; it handles the local environment and keeps it in sync with `poetry.lock`.