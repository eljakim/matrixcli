# matrixcli

A terminal [Matrix](https://matrix.org) client with a **three-column home
dashboard**, end-to-end encryption, and credentials kept in the **macOS
Keychain**. Built on [matrix-nio](https://github.com/matrix-nio/matrix-nio) and
[Textual](https://textual.textualize.io/); works on Python 3.10 through 3.14.

The home screen shows three columns:

- **Spaces**: your spaces; selecting one lists its rooms below, sorted
  unread-first.
- **Recent + Favourites**: the five rooms you last opened, then rooms and
  DMs tagged as favourite (toggle with `f`). Pending invites appear above
  these when there are any.
- **DMs**: one entry per person, most recently active first, with unread
  counts and online indicators.

Plus a search overlay (`/`) over all people and rooms, and a per-room view to
read history and send messages, which updates live as messages arrive.

## Install

End-to-end encryption comes from matrix-nio's vodozemac backend (prebuilt
Rust wheels), so installation is plain Poetry with no system crypto
libraries:

```sh
cd matrix
poetry install
```

Poetry picks any Python between 3.10 and 3.14 (`poetry env use python3.12`
to pin one explicitly).

Verify the crypto stack:

```sh
poetry run python -c "import vodozemac; print(vodozemac.Account().ed25519_key and 'e2e ok')"
```

## Configure

First run writes a template to `~/.config/matrixcli/config.ini` (or put a
`config.ini` in the current directory). Edit it:

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
```

`room` is optional: set it to a room id or canonical alias to open that room
automatically on launch.

Store your password in the Keychain once (you will be prompted for it):

```sh
security add-generic-password -s "matrix-cli" -a "@you:matrix.org" -w
```

On first launch the app logs in with that password and caches an **access token +
device id** back into the Keychain (service `matrix-cli-token`), so later launches
never touch your password. To force a fresh login, delete that token entry.

## Run

```sh
bin/matrix
```

The launcher works from any directory (symlink it into `/opt/local/bin` if you
like) and forwards arguments, resolving file paths against your current
directory. `poetry run matrix` from the project directory does the same thing.

### Keys

On the home screen:

- `/`: search people and rooms (accent-insensitive: `agnes` finds `Ágnes`);
  `↓`/`↑` move through the results while you keep typing, `Enter` opens the
  highlighted hit
- `tab` / `shift+tab`: move between columns
- `Enter`: open the selected room or DM (on a space: list its rooms; on an
  invite: accept it)
- `f`: toggle favourite on the selected room or DM
- `?`: about box
- `q`: quit (`Ctrl+Q` and the `Ctrl+P` command palette are disabled)

Pending invitations appear in an `Invites` section (marked `✉`) above
Favourites whenever there are any; `Enter` accepts.

In a room:

- `j` / `k` or `↓` / `↑`: move the selection down / up (the selected message
  is marked with an accent bar in the left margin); moving up past the top
  fetches older history, all the way back to the room's first message
- `u`: jump to the first unread message; messages that arrived after you last
  opened the room sit below a red `── new ──` divider
- `Enter`: act on the selected message.
  - It holds a link (underlined and, in terminals that support it,
    mouse-clickable): open it with the system's default browser. A message
    with several links shows a popup to pick one. Only `http(s)` links are
    picked up.
  - It is an uploaded file (shown as `📎 name (size)`): download it; a popup
    picks the destination: last-used folder, `~/Desktop`, `~/Downloads`, or
    the current directory. Encrypted attachments are decrypted on download.

  Both popups take `j`/`k` or `↓`/`↑` to choose, `Enter` to confirm, and
  `Esc` to cancel.
- `r`: reply to the selected message (inline editor below it)
- `R`: compose a new message (editor at the end)
- `l` / `h`: unfold / fold the selected message's thread in place. `l` on a
  message with a `⤷ N replies` badge opens its replies inline (indented,
  fetched in full from the server); `l` again steps into the first reply.
  `h` on a reply jumps back to the root; `h` on the root folds the thread.
- `t`: toggle between normal view (threads collapsed behind a dim
  `⤷ N replies` badge, except those unfolded with `l`) and threaded view
  (every reply indented as a whole under its root, with a vertical bar down
  the replies' left edge marking the thread); the bottom bar shows which
  view you are in (`View: normal` / `View: threaded`). In threaded view,
  `h`/`l` jump out of / into a thread.
- `T`: open the selected message's thread full-screen (or start a new one)
- `Enter`: send the editor's contents (`Shift+Enter` inserts a newline;
  `Alt+Enter` does too, for terminals where Shift+Enter is indistinguishable
  from Enter)
- `c`: toggle compact mode (no blank line between speakers)
- `Esc`: cancel the editor, or go back to the home screen

In a thread (opened with `t`): the composer is ready immediately and sends
into the thread; `r`, `j`/`k`, and `Esc` work as in a room.

### Device verification and history decryption

To make other clients trust this session, verify it via emoji (SAS) with the
CLI as the responder:

```sh
bin/matrix --verify
```

then start the verification from Element (Settings -> Sessions -> this device
-> Verify session -> Compare unique emoji).

Encrypted history older than this device needs the room keys. Export them from
a client that has them (Element: Settings -> Security & Privacy -> Export E2E
room keys) and import the file:

```sh
bin/matrix --import-keys element-keys.txt
```

Delete the export file afterwards; it contains the keys to your message
history.

## Where things live

| What | Where |
|------|-------|
| Settings | `~/.config/matrixcli/config.ini` |
| Password | macOS Keychain, service `matrix-cli` |
| Cached token | macOS Keychain, service `matrix-cli-token` |
| Encryption store (Olm keys) | `~/.local/share/matrixcli/store/` |
| Recency state (UI ranking) | `~/.local/share/matrixcli/state.json` |

## Notes & limitations

- Encrypted rooms only decrypt on devices that have the keys. A brand-new
  device (first login) can read messages sent *after* it joined; for older
  history, use `--import-keys` (matrix-nio has no server-side key backup
  support, so the keys must come from an export).
- Outgoing messages are sent with `ignore_unverified_devices=True` so you are
  not blocked by unverified sessions. Use `--verify` to make your other clients
  trust this one.
- Room-opening recency (used for sorting) is tracked locally; it starts empty
  until you open some rooms.

## Development

Source lives in `src/matrixcli/`. Run the tests with:

```sh
poetry run pytest
```
