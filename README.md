# matrixcli

A terminal [Matrix](https://matrix.org) client with a **three-column home
dashboard**, end-to-end encryption, and credentials kept in the **macOS
Keychain**. Built on [matrix-nio](https://github.com/matrix-nio/matrix-nio) and
[Textual](https://textual.textualize.io/); works on Python 3.10 through 3.14.

The home screen shows three columns:

- **Spaces**: your spaces; selecting one lists its rooms below, sorted
  unread-first.
- **Recent + Favourites**: the rooms you last opened, then rooms and DMs
  tagged as favourite (toggle with `f`). Each section shows five rows by
  default and can be resized with `+` / `-` (see below). Pending invites
  appear above these when there are any.
- **DMs**: one entry per person, most recently active first, with unread
  counts and online indicators.

Unread counts show in yellow; rooms where you were mentioned add a red `(N!)`
badge. Columns taller than the terminal scroll.

Plus a search overlay (`/`) over all people and rooms, and a per-room view to
read history and send messages, which updates live as messages arrive. The
terminal window's own titlebar follows along, naming the room (or thread)
being read and prefixing the total unread count as `(N)` (switchable in
`:settings`).

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

Store your password in the system keyring once (you will be prompted for it):

```sh
security add-generic-password -s "matrix-cli" -a "@you:matrix.org" -w   # macOS
keyring set matrix-cli @you:matrix.org                                  # Linux
```

On Linux the `keyring` CLI comes with the installed dependencies
(`poetry run keyring ...` also works) and needs a Secret Service keyring
such as gnome-keyring, or KWallet, installed and unlocked; without one the
app exits at startup with a pointer to this section.

On first launch the app logs in with that password and caches an **access token +
device id** back into the keyring (service `matrix-cli-token`), so later launches
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

- `/`: search people, rooms, and every locally held message in every room
  (accent-insensitive: `agnes` finds `Ágnes`); `↓`/`↑` move through the
  results while you keep typing, `Enter` opens the highlighted hit. Message
  hits are listed after the room and people hits, newest first, each naming
  its room; opening one opens that room and jumps straight to the message.
  With the cursor in Recent or Favourites, the search is instead scoped to
  that whole section: it opens already listing all of the section's rooms
  (beyond the rows the dashboard has room for), and message hits are
  limited to messages in those rooms. The popup's border always names the
  active scope (as does the in-room message search, with the room's name)
- `j` / `k` or `↓` / `↑`: move the selection down / up; `j` and `k` treat a
  column as one continuous list, so they roll on from Spaces into its Rooms,
  and from Invites through Recent into Favourites. A typed count repeats
  the motion (`5j`), as in a room
- `l` / `h` (or `tab` / `shift+tab`): move to the next / previous column,
  landing back on the list you last used there
- `Enter`: open the selected room or DM (on a space: list its rooms; on an
  invite: accept it)
- `f`: toggle favourite on the selected room or DM (the label reads
  `Favourite` or `Unfavourite` to match the selected row, and disappears on
  rows that cannot be tagged)
- `+` / `-`: grow / shrink the section the cursor is in, for Recent and
  Favourites (vim's `Ctrl-W +`/`-` window resize boiled down to one key;
  `=` works as an unshifted `+`). Neither section goes below five rows, and
  growth stops where it would squeeze the other section or run off the
  screen; shrinking the terminal takes rows back automatically, and the
  sizes are remembered across sessions
- `S`: sync everything: start the full-history download for every room at
  once, so the local caches (and with them the global message search and
  offline reading) end up covering all rooms without opening each one by
  hand. It is the same background walk opening a room starts, run for all
  rooms, one room at a time so the homeserver is not hammered; a
  notification says how many rooms still needed downloading. Hidden when
  `[cache] messages` is off, since nothing would be kept
- `?`: about box
- `q`: quit, from the home screen only; on any other page `q` returns
  straight to the home screen instead (`Ctrl+Q` and the `Ctrl+P` command
  palette are disabled)
- `:`: vim-style command line (works on any page when no editor is
  focused): `:q!` quits immediately from anywhere, `:q` closes the current
  page like the `q` key, a bare number inside a room jumps to that message
  (see the room keys below), `:settings` (or `:set`) opens the settings
  screen, `Esc` cancels
- `:settings`: account and app settings. Your display name (saved to the
  homeserver), the email addresses on the account (read-only; changing them
  needs a validation mail, so that stays in Element), whether the terminal
  titlebar shows the total unread count, and a timezone override for every
  displayed timestamp (an IANA name like `Europe/Amsterdam`; empty uses the
  system zone). The last two persist in `state.json`; `Enter` or `Ctrl+S`
  saves, `Esc` cancels

Pending invitations appear in an `Invites` section (marked `✉`) above
Favourites whenever there are any; `Enter` accepts.

In a room:

- `j` / `k` or `↓` / `↑`: move the selection down / up (the selected message
  is marked with an accent bar in the left margin); moving up past the top
  fetches older history, all the way back to the room's first message. A dim
  `── Tue 12 Aug 2026 ──` divider marks every change of day, and reactions
  show as a dim `👍 3` line under the message they apply to. Messages that
  mention you get a red timestamp and a red `@` marker. A typed count
  repeats the motion, as in vim: `10j` moves ten messages down (the pending
  digits show in the footer's right corner, and any other key cancels them)
- `g` / `G`: the room's first / newest message. `g` detaches from the live
  tail and browses the downloaded archive from the very beginning (loaded a
  chunk at a time as you move); `G` jumps to the newest message and
  reattaches to the live tail. With a count both become vim's goto-line:
  `10G` (or `10g`) goes to message 10, counted from the room's first
- `:` + a number: the same jump from the command line: `:1` is the oldest
  message, `:10000` the ten-thousandth, and a number past the end lands on
  the newest, like `G`
- `Ctrl+D` / `Ctrl+U`: half a window down / up; `Ctrl+F` / `Ctrl+B` a whole
  window. Measured against what is actually on screen, so tall messages
  count for their height
- `{` / `}`: previous / next sender block, vim's paragraph motion for chat:
  `{` jumps to the first message of the current speaker's run, then run by
  run upward; `}` to the first message of the next speaker's run
- `Ctrl+O` / `Ctrl+I` (or `Tab`): walk back / forward through the jumplist,
  as in vim. Every long jump (`g`, `G`, `:N`, `u`, a search hit) records
  where you were; `Ctrl+O` returns there, however deep in the archive that
  was, and `Ctrl+I` re-runs the jump
- `u`: jump to the first unread message; messages that arrived after you last
  opened the room sit below a red `── new ──` divider
- `Enter`: act on what the selected message says. The bottom bar names what it
  will do, and says nothing when there is nothing to do:
  - It is an uploaded file, shown as `📎 name (size)` (`Download`): a popup
    picks the destination: last-used folder, `~/Desktop`, `~/Downloads`, or
    the current directory. Encrypted attachments are decrypted on download.
  - It holds a link, underlined and, in terminals that support it,
    mouse-clickable (`Open link`): open it with the system's default browser.
    Only `http(s)` links are picked up.

  A message with several of those (more than one link, a file with a link in
  its caption, or reactions on top of either) shows a popup to pick which
  (`Message actions`), with `Who reacted` among the choices where it applies.
  Every popup
  takes `j`/`k` or `↓`/`↑` to choose, `Enter` to confirm, and `Esc` to cancel.
- `Space`: peek at the selected message; a second `Space` closes what the
  first one opened. On a message wearing a reaction badge it shows **who
  reacted**: a line per emoji naming everyone who sent it, in the same
  colors the timeline gives those people. On an image upload it previews
  the image right in the terminal instead, scaled to the window and
  rescaled live when the window resizes (a gimmick, but a useful one); a
  reacted image keeps `Space` for the preview, with its reactions in
  `Enter`'s actions menu. Two styles, flipped with `~` inside the preview and remembered
  across sessions: truecolor half-blocks (the default), and classic ASCII art
  built from a configurable character ramp (`[preview] ascii_ramp` in
  `config.ini`; the default is
  [Paul Bourke's 70-level ramp](https://paulbourke.net/dataformats/asciiart/),
  mapped brightest-pixel-to-densest-glyph for dark terminals, so reverse the
  string on a light one). `j`/`k` walk straight to the room's next/previous
  image without leaving the preview, and closing lands the timeline selection
  on the image last shown. Unencrypted images fetch a server-side thumbnail;
  encrypted ones download and decrypt the full file. Fetched previews are
  cached (AES-encrypted at rest, capped at 64 MB, oldest pruned first) so
  reopening one is instant and works offline; the cache follows the same
  `[cache] messages` switch and per-space opt-outs as message history. The
  bottom bar shows the current style (`Style: ascii` / `Style: blocks`);
  `Esc` closes. The preview degrades with the terminal: 24-bit color gets
  exact blocks, 256-color terminals get Floyd-Steinberg-dithered blocks
  (the title says so), and 16-color terminals get the ASCII ramp only,
  with the style toggle hidden.
- `Shift+Enter`: look behind the selected message, when it carries a trailing
  `*` saying the line on screen is not the whole story (`Show history`). This
  is a separate key from `Enter` so neither has to guess which you meant on,
  say, an edited message that also holds a link. `Alt+Enter` does the same,
  for terminals where Shift+Enter is indistinguishable from Enter.
  - **Edited**: shown once, with its newest text, rather than as two
    near-identical messages. The popup lists every version oldest first with
    the time it was sent, fetched from the server, so versions older than the
    loaded history are included too.
  - **Deleted**: shown as `this message has been deleted`. If the message
    arrived before it was deleted, the popup shows the text as we received it,
    marked with the deletion time. The server no longer holds that text, so it
    is only available in the session that saw it: after a restart the
    tombstone is all that is left, and there is nothing to open.
- `r` / `n`: reply to the selected message / compose a new one. Both open a
  five-line editor docked below the timeline (the history moves up to make
  room); its header line names what you are replying to. A draft longer than
  five lines scrolls inside the panel, and a message arriving mid-typing
  redraws the history without touching what you have written. Escape stashes
  the draft rather than destroying it: `r`/`n` in the same room (or thread)
  hands it back, for as long as the app runs.
- `e`: edit the selected message, when it is your own (the composer opens
  with its current text and sends the correction as a Matrix edit)
- `d`: delete the selected message, when it is your own; a confirmation
  popup gates it (Enter confirms, Esc backs out)
- `a`: react to the selected message: digits `1`-`9` send from a quick row
  instantly, `/` searches every emoji by Unicode name (`giraff` finds 🦒).
  Picking one you already sent takes it back; the quick row ticks those
- `/`: search everything this client holds for the room (accent-insensitive,
  newest hit first): the full downloaded history, not just what is on
  screen, thread replies included. The query matches the message text, the
  sender's display name, or their matrix id, so `agnes` finds what Ágnes
  said as well as messages that mention her. `Enter` jumps the selection to
  the picked message, unfolding its thread or detaching into deep history
  (as `g` does; `G` returns to the live tail) when the hit is not in view.
  The search is local: while a room's background download is still running,
  the count line under the input says so, and older not-yet-fetched
  messages cannot match yet.
- `l` / `h`: unfold / fold the selected message's thread in place. `l` on a
  message with a `⤷ N replies` badge opens its replies inline (indented,
  fetched in full from the server); `l` again steps into the first reply.
  `h` on a reply jumps back to the root; `h` on the root folds the thread.
  Both appear in the bottom bar only when the selected message has a thread
  to act on.
- `t`: toggle between normal view (threads collapsed behind a dim
  `⤷ N replies` badge, except those unfolded with `l`) and threaded view
  (every reply indented as a whole under its root, with a vertical bar down
  the replies' left edge marking the thread); the bottom bar shows which
  view you are in (`View: normal` / `View: threaded`). In threaded view,
  `h`/`l` jump out of / into a thread.
- `T`: open the selected message's thread full-screen (or start a new one)
- `Enter` (in the composer): send its contents (`Shift+Enter` inserts a newline;
  `Alt+Enter` does too, for terminals where Shift+Enter is indistinguishable
  from Enter)
- `c`: toggle compact mode (no blank line between speakers); the label reads
  `Compact: off` / `Compact: on`
- `~`: toggle the sender column between display names and raw
  `@user:server` ids (vim's toggle key); the label reads
  `Show: names` / `Show: ids`
- `Esc`: cancel the editor, or go back one page
- `q`: straight back to the home screen, however deep you are (also from a
  thread); quitting is `q` on the home screen or `:q!` anywhere

In a thread (opened with `T`): the composer is ready immediately and sends
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

It works the other way too: `--export-keys` writes this device's room keys to
an encrypted file in the same format, so Element (or another matrixcli install)
can read them back. There is no server-side key backup, so this file is the
only copy that survives a lost store. The passphrase is generated rather than
asked for, and printed once when the export finishes:

```sh
bin/matrix --export-keys ~/matrixcli-keys.txt
```

Write the passphrase down before the terminal scrolls away; it is stored
nowhere, and the file cannot be read without it.

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
- Only one instance runs at a time: a second launch exits immediately with a
  message naming the first one's pid, because two instances would corrupt
  the shared encryption store and caches. The lock is released by the OS
  when the process ends, however it ends, so a crashed or killed instance
  never blocks the next launch.

## Development

Source lives in `src/matrixcli/`. Run the tests with:

```sh
poetry run pytest
```
