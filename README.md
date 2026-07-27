# Mac-Commander

A minimal macOS GUI-automation MCP server. Five tools, ~830 lines, one file.

It replaces the third-party **MacOS-MCP** server. It does **not** read or write
files and does not run shell commands for the caller — Desktop Commander owns
that layer. The scope discipline is the feature.

Built to spec: [`SPEC.md`](SPEC.md) v1.0.

## Wiring it into Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`
inside the existing `"mcpServers"` object, then quit and reopen Claude Desktop:

```json
{
  "mcpServers": {
    "mac-commander": {
      "command": "/Users/Marty/Claude/Mac-Commander/.venv/bin/python",
      "args": ["/Users/Marty/Claude/Mac-Commander/server.py"]
    }
  }
}
```

Remove the old `MacOS-MCP` entry at the same time — running both means two
things fighting over the same keyboard.

### Permissions

Launched by Claude Desktop, the server inherits Claude's grants. It needs:

- **Accessibility** — required. Without it `see()` returns an empty tree and says so.
- **Screen Recording** — only for `see(vision=true)`. Without it you still get
  the element tree plus a note naming the missing permission.

Running `verify.py` from a terminal instead uses *that terminal's* grants, which
are usually narrower. `AXIsProcessTrusted()` is printed at the top of the run.

## The five tools

### `see(app=None, all=False, vision=False, max_elements=150)`

Accessibility snapshot of one app — the frontmost one unless you name another.
Returns the frontmost app, the target's windows, and its interactive elements
with stable refs (`"e17"`) that `act()` accepts.

Scoped to a single app on purpose: a whole-desktop dump leaks every window
title and dock item into the transcript. `all=true` is the explicit opt-in.
`vision=true` adds a screenshot — of the target app's window when it can be
identified, not the whole screen.

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

### `applescript(script, args=[], timeout=30)`

Runs your script untouched, with `args` delivered as argv. Write it with an
`on run argv` handler and read values from there. Returns stdout, stderr and
exit code.

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

`config.json` is created beside `server.py` on first run:

```json
{
  "input_blocklist": ["1Password", "Passwords", "System Settings"],
  "audit_log": "audit.jsonl"
}
```

`act()` refuses any target whose name or bundle id contains a blocked term,
matched case-insensitively and before the app is even resolved — so a
blocklisted app that is not running is still refused, not reported missing.
It fails closed.

## Audit log

Every `see` / `act` / `app` / `notify` / `applescript` call appends one JSONL
line to `audit.jsonl`. The server only ever appends — it never reads or
rewrites the file.

```json
{"ts": "2026-07-27T18:14:49.564Z", "tool": "act", "target": "TextEdit",
 "summary": "click(e151), type(20 chars), key(cmd+a), key(cmd+c)", "result": "ok: 4 steps"}
```

Typed text is counted, never recorded — it might be a password.

## Tests

```bash
.venv/bin/python verify.py                            # all seven, live
.venv/bin/python -m pytest test_focus_abort.py -v      # test 6 alone, no real input
```

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
