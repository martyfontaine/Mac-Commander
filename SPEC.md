# Mac-Commander — build spec

v1.0 — 2026-07-27 — approved by Marty (Option A). Builder: Claude Code.
This file is the contract; where chat memory and this file disagree, this file wins.

## What this is

A minimal macOS GUI-automation MCP server. It fully replaces the third-party
"MacOS-MCP" server. It does NOT read/write files or run shell commands —
Desktop Commander (separate, upstream-maintained) owns that layer.
Scope discipline is a feature: five tools, no more.

## Why (bugs in the server it replaces — all reproduced 2026-07-27)

1. User strings interpolated into `osascript -e "..."` → any double quote or
   non-ASCII char (em dash) throws AppleScript error -2741. Root cause:
   generating code from strings.
2. Keyboard/mouse fire at whatever is frontmost; the Claude client steals focus
   between tool calls — a cmd+a/cmd+c landed on the Claude window and copied
   the chat. Root cause: focus assumed, never verified; sequences spanned
   multiple tool calls.
3. Coordinate-only clicking: resolution-dependent, brittle.
4. Full-desktop snapshots dump every dock item and window title — privacy leak
   plus context bloat.

## Non-negotiable design rules

- NEVER build AppleScript (or any code) via string interpolation. AppleScript
  runs as `osascript /dev/stdin <args>` — script on stdin, ALL dynamic values
  passed as argv into an `on run argv` handler. No exceptions, helpers included.
- Every input step verifies the target app is frontmost immediately before
  firing, and re-checks after. Focus lost mid-batch → abort remaining steps,
  return which step failed and who stole focus.
- Prefer AX actions (AXPress on a resolved element) over synthetic mouse
  events; fall back to CGEvent at the element's center only if AX fails.
- Typing uses CGEventKeyboardSetUnicodeString — full Unicode (é — " ' 🙂)
  must round-trip.
- Snapshots scoped to one app by default; whole desktop needs explicit all=true.
- Refuse input to blocklisted apps (default: 1Password, Passwords,
  System Settings) unless config overrides.
- Append-only audit log: every act/applescript/notify call appends one JSONL
  line (ts, tool, target, summary, result) to audit.jsonl. The server never
  reads or rewrites it.
- Size budget: ~600–900 lines total. A feature that busts the budget stays
  out. Small is the point.

## Stack & location

- Python 3.12+, FastMCP (`mcp` package), PyObjC frameworks:
  ApplicationServices (AX*), Quartz (CGEvent*), AppKit (NSWorkspace,
  NSRunningApplication). stdio transport.
- Lives in /Users/Marty/Claude/Mac-Commander/ with its own .venv and pinned
  requirements.txt.
- Git: no enclosing repo — `git init` in Mac-Commander/. Meaningful commits
  as you go; final commit at the end.
- Permissions: launched by Claude Desktop, the server inherits Claude's
  Accessibility + Screen Recording grants (both currently granted). Running
  verify from a terminal may need the terminal granted Accessibility — if AX
  calls fail there, say so instead of debugging blind.

## Tools (exactly five)

### see(app=None, all=False, vision=False, max_elements=150)

Accessibility snapshot scoped to `app` (name or bundle id). Returns frontmost
app, the app's windows, and interactive elements with stable refs ("e17"):
role, label, center xy, enabled. Cache AXUIElement handles per ref for the
session; invalidate on app relaunch. all=True → whole desktop (explicit
opt-in). vision=True → screencapture to a temp file, returned as image
content; if Screen Recording is missing, still return the tree plus a
one-line note naming the permission.

### act(target, steps, settle_ms=150)

Atomic batched input against one target app. Step kinds:
- click:  {ref | xy, button: left|right, clicks: 1|2}
- type:   {text, submit=false}
- key:    {combo}            # "cmd+a", "cmd+shift+4"
- scroll: {ref | xy, dx, dy}
- drag:   {from_ref|from_xy, to_ref|to_xy}
- wait:   {ms}               # cap 10000
Flow: resolve target → blocklist check → activate → poll frontmost until match
(3 s timeout) → per step: verify frontmost, execute, settle. Structured result
always: steps_completed, then ok or failed_step {index, reason, current
frontmost}.

### app(action, name, window=None)

launch | quit | switch | hide via NSWorkspace / NSRunningApplication.
window = {move:[x,y], size:[w,h]} optional. quit is graceful terminate only —
never force-kill.

### notify(message, title="Mac-Commander", subtitle=None, sound=None)

Via the stdin+argv osascript pattern.
Must pass: message `test — "quotes" 'single' é 🙂` sends successfully.

### applescript(script, args=[], timeout=30)

`osascript /dev/stdin` plus args. Pass the script through untouched.
Return stdout, stderr, exit code.

## config.json (auto-created beside server.py if absent)

{"input_blocklist": ["1Password", "Passwords", "System Settings"],
 "audit_log": "audit.jsonl"}

## Acceptance tests — run them, paste real output; never report success you did not observe

1. notify with `test — "quotes" é 🙂` → returns ok, banner shown.
2. applescript `on run argv … return item 1 of argv` with an arg containing
   `"` and `—` → round-trips byte-exact.
3. see(app="Finder") → elements returned; nothing from other apps.
4. End-to-end on TextEdit: save the user's clipboard first. applescript:
   activate + new document. One act() batch: click into doc, type
   `Ligne — "était" 'ok'`, key cmd+a, key cmd+c. Verify via applescript
   `the clipboard` equals the typed text. Then close the doc without saving,
   quit TextEdit, restore the saved clipboard.
5. act(target="System Settings", …) → refused, reason given, audit line written.
6. Focus-abort path: unit test with mocked frontmost flipping mid-batch →
   asserts abort and correct failed_step.
7. tail audit.jsonl → one line per call above.

## Deliverables & final report

server.py (small helpers only if genuinely needed), config.json,
requirements.txt, the verify script/tests, README.md containing the
claude_desktop_config.json snippet (command = /Users/Marty/Claude/Mac-Commander/.venv/bin/python,
args = [server.py]). Print the snippet — do NOT edit the live Claude config;
Marty wires it in himself. Final message = manifest of every file created or
changed + real test output + anything that does not work, stated plainly.
