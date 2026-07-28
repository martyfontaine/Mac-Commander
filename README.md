# Mac-Commander

A minimal macOS GUI-automation MCP server. Five tools, one file.

It replaces the third-party **MacOS-MCP** server. Four of its five tools —
`see`, `act`, `app`, `notify` — do **not** read or write files and do not run
shell commands; Desktop Commander owns that layer. The scope discipline is the
feature.

`applescript` runs scripts **you** wrote, chosen by name. AppleScript is
unrestricted once running — it reaches the shell through `do shell script`,
reads and writes files, and can drive any app — so the code is yours to author
and the model only picks one and supplies argv. Model-authored script text is
refused unless you explicitly enable it. See
[The AppleScript boundary](#the-applescript-boundary).

Built to spec: [`SPEC.md`](SPEC.md) v1.0.

## Wiring it into Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`
inside the existing `"mcpServers"` object, then quit and reopen Claude Desktop:

Remove the old `MacOS-MCP` entry at the same time — running both means two
things fighting over the same keyboard.

### Permissions

Launched by Claude Desktop, the server inherits Claude's grants. It needs:

- **Accessibility** — required. Without it `see()` returns an empty tree and says so.
- **Screen Recording** — only for `see(vision=true)`. Without it you still get
  the element tree plus a note naming the missing permission.

Running `verify.py` from a terminal instead uses *that terminal's* grants, which
are usually narrower — the grant attaches to the host app (Terminal, iTerm),
not to the `python` binary. Both `AXIsProcessTrusted()` and
`CGPreflightScreenCaptureAccess()` are printed at the top of the run. Test 8
exercises the capture path and reports UNVERIFIED, not PASS, when Screen
Recording is missing.

## The five tools

### `see(app=None, all=False, vision=False, max_elements=150)`

Accessibility snapshot of one app — the frontmost one unless you name another.
Returns the frontmost app, the target's windows, and its interactive elements
with stable refs (`"e17"`) that `act()` accepts.

Scoped to a single app on purpose: a whole-desktop dump leaks every window
title and dock item into the transcript. `all=true` is the explicit opt-in.
`vision=true` adds a screenshot of the target app's window. If that window
cannot be identified — the app is hidden, minimised or windowless — you get no
image and a note saying why. It never silently substitutes a full-screen
capture; only `all=true` widens the scope. A screenshot of a blocklisted app is
refused outright, though its element tree is still readable.

Refs are cached for the session and dropped when their app is relaunched under
a new pid.

### `act(target, steps, settle_ms=150)`

A batch of input steps against one app, focus-guarded on both sides of every
step. This is the tool the rebuild exists for.

```
click  {ref|xy, button: "left"|"right", clicks: 1|2}
type   {text, submit: false}
key    {combo}                       "cmd+a", "cmd+shift+4"
scroll {ref|xy, dx, dy}
drag   {from_ref|from_xy, to_ref|to_xy}
wait   {ms}                          capped at 10000
```

Flow: resolve target → blocklist check → activate → poll frontmost (3 s) →
then per step: verify frontmost, execute, settle, verify frontmost again.

The result is always structured:

```json
{"ok": true,  "steps_completed": 4, "target": {"name": "TextEdit", "pid": 13670}}
{"ok": false, "steps_completed": 2,
 "failed_step": {"index": 2,
                 "reason": "focus lost before step: expected TextEdit, Claude is frontmost",
                 "frontmost": {"name": "Claude", "pid": 99}}}
```

`steps_completed` is how far it got; `failed_step.index` is where it went wrong.
A post-check failure at index *i* means step *i* did run but something took
focus during or right after it, so its effect may have landed elsewhere — the
batch stops rather than guessing.

Clicks prefer `AXPress` on the resolved element and only fall back to a
synthetic mouse event at the element's centre when AX refuses. Typing goes
through `CGEventKeyboardSetUnicodeString`, so `é — " ' 🙂` all round-trip.

### `app(action, name, window=None)`

`launch` | `quit` | `switch` | `hide`, by app name or bundle id.
`window` optionally places the front window: `{"move": [x, y], "size": [w, h]}`.

`quit` is a graceful terminate and waits for the process to actually go; it
never force-kills. If the app is still up after 5 s it says so — usually a save
dialog is open.

### `notify(message, title="Mac-Commander", subtitle=None, sound=None)`

A notification banner. Any Unicode is safe.

### `applescript(name=None, script=None, args=[], timeout=30)`

Runs one of your scripts from `scripts/`, chosen by name, with `args`
delivered as argv. Returns stdout, stderr and exit code.

```
applescript()                                    # the catalogue
applescript(name="clipboard-read")
applescript(name="clipboard-write", args=["hi"])
```

Adding a script is dropping a `.applescript` file in `scripts/` — no restart,
the catalogue is read per call. Write it with an `on run argv` handler and read
values from there.

Passing `script=` instead is refused unless `config.json` sets
`"allow_raw_applescript": true`.

## Why AppleScript is never interpolated

The server it replaces built AppleScript by pasting user strings into
`osascript -e "..."`. Any double quote or em dash produced AppleScript error
-2741. So: the script text is always a constant, and every dynamic value
travels as an argv entry.

```python
osa('on run argv\n\treturn item 1 of argv\nend run\n', ['test — "quotes" é 🙂'])
```

**One deviation from the spec.** It calls for `osascript /dev/stdin <args>`.
On macOS 26 osascript cannot read a device file — it seeks the program, and
`/dev/stdin`, `/dev/fd/0` and process substitution all fail with
`osascript: /dev/stdin: I/O error (bummers)`, even when stdin is a seekable
regular file. `osascript -` is the same contract that actually works: program
on stdin, values on argv, nothing interpolated. That is what `osa()` runs.

## Refusing input to sensitive apps

`config.json` sits beside `server.py` (created on first run if absent):

```json
{
  "input_blocklist": [
    "1Password", "com.1password.",
    "Passwords", "com.apple.Passwords",
    "System Settings", "com.apple.systempreferences",
    "Keychain Access", "com.apple.keychainaccess"
  ],
  "audit_log": "audit.jsonl"
}
```

`act()` refuses any target whose name or bundle id contains a blocked term,
matched case-insensitively and before the app is even resolved — so a
blocklisted app that is not running is still refused, not reported missing.
It fails closed.

Both display names and bundle ids are listed because display names are
localized: on a French Mac System Settings is "Réglages Système", and its
bundle id (`com.apple.systempreferences`) shares no substring with its English
name, so the name alone would guard nothing there.

**What the blocklist does not cover.** It gates input through `act()` and
screenshots through `see(vision=true)`. It does not gate the element tree —
reading a locked vault is harmless and sometimes useful — and a script in
`scripts/` can drive any app, because you wrote it.

## The AppleScript boundary

AppleScript cannot be sandboxed: it reaches the shell, the filesystem and every
scriptable app, and any check on script *text* is defeated by building the
string at runtime. So the boundary is not what a script may do — it is **who
writes it**.

The threat model's adversary is a prompt-injected model, not you. So:

- **You** author scripts in `scripts/`. They run unrestricted. Adding one is
  the same decision as running it yourself.
- **The model** picks one by name and supplies `args`. It cannot author code.

That is the argv discipline this server already applied to data, extended to
code: values never get interpolated into a script, and now neither does the
script get authored by the caller.

`"allow_raw_applescript": true` in `config.json` restores the old behaviour and
hands the model arbitrary code execution as you. It exists because it is
sometimes what you want during development; it is off by default, and every
refused attempt is logged with the script's fingerprint.

## Audit log

Every `see` / `act` / `app` / `notify` / `applescript` call appends one JSONL
line to `audit.jsonl`. The server only ever appends — it never reads or
rewrites the file.

```json
{"ts": "2026-07-27T18:14:49.564Z", "tool": "act", "target": "TextEdit",
 "summary": "click(e151), type(20 chars), key(cmd+a), key(cmd+c)", "result": "ok: 4 steps"}
```

Typed text is counted, never recorded — it might be a password. AppleScript is
identified by a sha256 prefix, its size, and its first body line, so two calls
can be told apart and a known script matched later without transcribing what it
contains:

```json
{"ts": "...", "tool": "applescript",
 "target": "sha256:1c16b4b068a21152 43c/3L return item 1 of argv",
 "summary": "1 args, 33 arg chars", "result": "ok"}
```

`notify` is the one tool whose message body is written to the log (first 120
characters), since a notification is shown on screen anyway.

## Tests

```bash
.venv/bin/python verify.py                                        # all seven, live
.venv/bin/python -m pytest test_focus_abort.py test_audit_fixes.py -v   # hermetic, no real input
```

`test_audit_fixes.py` pins the security fixes from the 2026-07-27 audit — the
blocklist holding on non-English locales, launch-path rejection, xy bounds,
capture scoping and audit-log fidelity. See [`AUDIT.md`](AUDIT.md).

`verify.py` drives the real machine. It saves the clipboard before the TextEdit
test and restores it after, and it closes the front TextEdit document only
after confirming the text is exactly what it typed — a document it did not
create is never touched.

## Install from scratch

```bash
cd /Users/Marty/Claude/Mac-Commander
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python verify.py
```
