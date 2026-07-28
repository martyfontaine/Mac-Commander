# Audit — 2026-07-27
Base commit: 5e0b433 · Branch: audit/2026-07-27 · Third party: Codex CLI (codex exec, read-only, blind on the base commit)

## Threat model

Local-only tool. stdio transport, no listener, no network surface; the only
caller is the MCP client that spawns it (Claude Desktop). It runs with the
user's full Accessibility and Screen Recording grants, so it can read any
window's contents and synthesize any keystroke or click.

The adversary in scope is not a remote attacker — it is **prompt injection
reaching the driving LLM** through screen contents, web pages, documents or
email the model reads, plus a buggy or over-eager model. At stake: keystrokes
landing in the wrong app, credentials read out of or typed into a password
manager, screen contents leaking into transcripts or temp files, and code
execution as the user.

The blocklist, the focus guard and the one-app scoping **are** the security
controls. Anything that bypasses one of them is a real finding here, not a
hypothetical. Severity below is calibrated to that, not to a generic checklist.

## Regression check

First audit — no prior findings to re-verify. Baseline recorded for next time:
no secrets in the working tree or in any of the 6 commits of history;
`.venv/bin/pip freeze` matches `requirements.txt` exactly with no drift;
`audit.jsonl` is gitignored and has never been committed.

## Findings

| ID | file:line | Sev | What | Why it matters here | Status | Verified |
|----|-----------|-----|------|---------------------|--------|----------|
| A-001 | server.py:543 (instructions), :843 (tool); README.md:5 | Critical | The server's own MCP instructions and README stated it "does not read or write files and does not run shell commands", while `applescript()` runs arbitrary AppleScript — reaching the shell via `do shell script`, reading and writing files, and bound by neither the blocklist nor the focus guard. | The instructions string is text the **model** reads to decide what is safe to call. A false capability claim makes it treat an unbounded tool as a sandboxed one. Both auditors independently ranked this first; Codex rated it Critical. | FIXED@9362b86 (claim) · FIXED@a4d9893 (contained) | Executed `do shell script "echo …; id -un"` through `osa()` — exit 0, returned the username. Now refused by default: `verify.py` test 2b and 8 hermetic tests, including one asserting a refused script never reaches osascript. |
| A-002 | server.py:613-630 | High | `see(app=X, vision=True)` silently captured the entire screen whenever no layer-0 window could be found for X; nothing in the payload said the scope had widened. | Routine trigger (app hidden, minimised or windowless), and README promised "of the target app's window …, not the whole screen". Hands back every other visible window — the exact whole-desktop leak scoping exists to prevent. | FIXED@9362b86 | `test_scoped_capture_refuses_rather_than_grabbing_the_whole_screen` fails the test if `screencapture` is invoked unscoped for a named app. |
| A-003 | server.py:617 | Medium | Every `see(vision=True)` wrote a PNG of the screen into a fresh `mkdtemp` directory that nothing ever deleted. | Screen contents at rest, accumulating for the life of the machine. `Image(path=)` reads lazily after return, so the file could not simply be unlinked — bytes are now read up front and passed as `Image(data=)`. | FIXED@9362b86 | `test_capture_removes_its_temp_directory` asserts the directory is gone before return. |
| A-004 | server.py:858 | Medium | Every `applescript` audit line recorded the literal string `on run argv` — the documented mandatory first line — plus an argument count. Nothing about what ran. | The append-only log is the *only* detective control over the one tool that can do arbitrary damage. 50 existing log lines confirmed: every applescript row identical. A clipboard read and an exfiltration script were indistinguishable. | FIXED@9362b86 | `test_script_fingerprint_distinguishes_scripts`; visible live in `verify.py` output as `sha256:1c16b4b068a21152 43c/3L return item 1 of argv`. |
| A-005 | server.py:549-610 | Medium | `see()` consulted no blocklist, so `see(app="1Password", vision=true)` would return the password manager's element tree **and** write a bitmap of its window. `_label()` falls back to `AXValue`, so a revealed field's contents enter the transcript. | The threat model's named stake is credentials being read out of a password manager. Reading is the higher-value attack; the control only covered typing. | FIXED@9362b86 · completed@2026-07-28 (screenshot) / resolved (element tree, left readable) | The 2026-07-28 review found the refusal only covered a **single-app** scope: `see(all=True, vision=True)` set `single = None`, skipped the check and took a full-screen grab, so a visible password manager still reached the transcript — and `_screenshot`'s own error text recommended that exact call. Now every target is checked (an unscoped grab is refused when a blocklisted app has a window on screen) and the steering text is gone. Pinned by `test_unscoped_capture_refuses_when_a_blocklisted_app_is_on_screen` and `test_capture_error_does_not_steer_the_caller_to_the_unscoped_path`, both of which fail against the 9362b86 code. |
| A-006 | server.py:36, config.json | Medium | The blocklist matched substrings of the **localized display name**. "System Settings" therefore guarded nothing outside English: its bundle id is `com.apple.systempreferences`, which shares no substring with its English name. | One of the three shipped entries silently protected nothing on a French, German or Japanese Mac — no refusal, no warning, an `ok` audit line. | FIXED@9362b86 | Bundle id confirmed via `defaults read`. Parametrised test covers English/French/German/Japanese names; `test_benign_apps_are_not_blocked` guards against over-blocking. |
| A-007 | server.py:453-456 | Medium | A raw `xy` in an `act()` step was never bounds-checked against the target's windows. | Mouse events post at `kCGHIDEventTap` and land wherever the pointer is, not in the process the guard verified. A click aimed at a blocklisted app's window landed there while a permitted app held focus — a blocklist bypass through `act()` itself, detected only by the after-check, once the click had fired. | FIXED@9362b86 · completed@2026-07-28 | `test_xy_outside_the_target_windows_is_refused` / `test_xy_inside_the_target_window_is_allowed`. The 2026-07-28 review found the check **failed open**: `if not rects: return` left it inert for any app exposing no AX windows (TextEdit with no document, a menu-bar extra) — precisely the target an injected caller would pick. Now fails closed, pinned by `test_xy_is_refused_when_the_target_exposes_no_windows`. |
| A-008 | server.py:449 | Medium | When a ref's element no longer existed, `_resolve_point` silently substituted the coordinates cached at snapshot time. | The UI having moved is exactly when the stale point is most likely to be over something else — a click delivered to whatever now occupies those pixels. | FIXED@9362b86 | `test_dead_ref_does_not_fall_back_to_cached_coordinates`. |
| A-009 | server.py:641 (docstring) | Medium | The `act()` docstring promised "input can never land in the wrong window". Focus is proven at the two step boundaries only; a chunked type or a 15-event drag fires over hundreds of ms between those checks. | The model calibrates its trust from this text. The mechanism detects a mid-step steal after the fact — the README was honest ("may have landed elsewhere"), the model-facing docstring was not. | FIXED@9362b86 | Docstring now states the real guarantee. Measured: 2000 chars ≈ 125 chunks ≈ 750ms; drag ≈ 285ms. |
| A-010 | server.py:585-588 | Medium | `max_elements` was enforced per app rather than across the snapshot, and the truncation note fired whenever the *sum* crossed the cap even if nothing was truncated. | `see(all=True)` could return `max_elements` × number-of-apps — the context bloat the cap exists to prevent — while the note misreported both ways. | FIXED@9362b86 | Budget now shared via `budget["count"]`; note driven by an explicit `truncated` flag. |
| A-011 | server.py:710-722 | Medium | `_app_url()` built `Path(folder) / f"{name}.app"` from the caller-supplied name. `Path("/Applications") / "/tmp/Evil.app"` discards the left operand; `../` escapes the same way. | Launches any bundle on disk rather than the five app folders. Marty runs Desktop Commander alongside this server, which *can* write files — so "write a bundle to /tmp, then launch it by path" is a live cross-tool chain, not a hypothetical. Raised from the finder's Low for that reason. | FIXED@9362b86 | Demonstrated: `/tmp/Evil` → `/private/tmp/Evil.app`, `../../../../tmp/Evil` → `/private/tmp/Evil.app`. The 2026-07-28 review found the five rejection cases **vacuous**: none of those paths exists, so the base implementation returns None for all five and the test passed against unguarded code. The fix itself is real — base resolves `../../System/Applications/Calculator` to a launchable bundle, the guard rejects it — and that discriminating case is now `test_app_launch_rejects_traversals_that_would_otherwise_resolve`, which fails without the guard. `test_app_url_still_resolves_a_real_bundle_id` guards the legitimate path. |
| A-012 | verify.py | Medium | `app()`, the capture path, the blocklist's bundle-id behaviour and launch-path handling had **no automated coverage**. `verify.py` needs a live Mac with Accessibility, so `test_focus_abort.py` (5 tests, focus only) was the entire CI-runnable suite. | A security control with no hermetic test regresses silently. | FIXED@9362b86 | `test_audit_fixes.py` added: 28 tests, each pinned to a finding. Suite now 33 hermetic tests. |
| A-013 | server.py:771-790 | Low | `app(action="launch")` returned `ok=True` when its 15s wait expired without the app finishing launching, as long as some app matched the name. | Tells the caller it is safe to drive an app that is not ready. | FIXED@9362b86 | Returns an explicit not-finished-launching error. |
| A-014 | server.py:733-740 | Low | `_place_window` coerced unvalidated caller data with `float()` and indexing, so a malformed `window` dict raised out of the tool — losing the audit line and returning an unstructured error. | Every other failure in this server is structured; this one escaped to FastMCP. | FIXED@9362b86 | Wrapped; the error is reported in `result["window"]`. |
| A-015 | server.py:758-761 | Low | `app()` returned on an unrecognised action without writing an audit line. | README states every `see`/`act`/`app`/`notify`/`applescript` call appends one line. It did not. | FIXED@9362b86 | `test_unknown_app_action_is_audited`. |
| A-016 | server.py:79-102 | Low | The caller-supplied `timeout` had a floor but no ceiling. | The server is single-threaded; `applescript(timeout=10**9)` wedges all five tools indefinitely. | FIXED@9362b86 · amended@2026-07-28 | Capped at 300s; `test_osa_timeout_is_bounded`. The 2026-07-28 review found the timeout *message* still reported the caller's uncapped value; now reports what was actually waited (`test_osa_timeout_message_reports_the_capped_value`). |
| A-017 | server.py:133-135 | Low | `_REFS` was pruned only when an app reappeared under a different pid, so repeated `see()` on a live app grew it for the life of the process, each entry pinning an `AXUIElement`. | A long-lived server session accumulates unboundedly. Read-verified from the code, not live-demonstrated. | FIXED@9362b86 | Bounded at 20 000 with oldest-first eviction; `test_ref_cache_is_bounded`. |
| A-018 | server.py:88 | Low | `osa()` executed the bare name `osascript`, resolved through the PATH inherited from whatever launched the server. | An earlier writable PATH entry substitutes the interpreter that runs every AppleScript. Requires PATH control, so Low. | FIXED@9362b86 | Absolute `/usr/bin/osascript`; `test_osa_uses_an_absolute_binary_path`. |
| A-019 | server.py:603-606 | Low | Any capture failure was reported to the caller as a missing Screen Recording permission. | Sends the user to fix a permission that is already granted, hiding the real cause. | FIXED@9362b86 | `_screenshot` returns the actual error; `test_capture_reports_missing_permission_distinctly`. |
| A-020 | server.py:41-51 | Low | `_load_config` wrote `config.json` at import with no exception handling. | A read-only install directory would stop the server from starting at all. | FIXED@9362b86 | Wrapped; falls back to defaults with a stderr note. |
| A-021 | server.py:373 | ~~Low~~ **WITHDRAWN** | ~~`press_combo`'s comment claimed it treated `"cmd++"` as the `=` key.~~ **This finding was wrong — the auditor misread the comment.** The base comment reads `key = "="  # "cmd++" is not a thing; treat a bare + as the = key`, which states the opposite of what the row claimed and is accurate: a *trailing* `+` maps to `=`, and `cmd++` is explicitly called out as not a thing. | Nothing was wrong with the original comment. Recorded rather than deleted, because a withdrawn finding is part of the audit's record. | WITHDRAWN@2026-07-28 | Base comment re-read at `git show 5e0b433:server.py:373`. The 9362b86 reword is harmless and stands; the behaviour never changed. |
| A-022 | requirements.txt | Low | All 39 pins are version-only: no lockfile, no hashes, no `--require-hashes`. | The process holds Accessibility and Screen Recording; a compromised package inherits both. | PROPOSED | Not fixed — adding hashes changes the documented install flow. |
| A-023 | server.py:839 | Info | `notify()` writes the first 120 characters of the message body verbatim to the audit log. | Not a defect (a notification is displayed on screen anyway), but it is the one tool whose content reaches the log. Now documented in README. | FIXED@9362b86 (documented) | README "Audit log" section. |
| A-024 | server.py:41-56 | Info | `config.json` is read once at import and never re-read; editing the blocklist requires restarting the server. | Worth knowing when changing the blocklist — the change is not live. | DEFERRED | Behaviour confirmed by reading; no fix applied. |
| A-025 | repo-wide | Info | No secrets in the working tree or in any of the 6 commits. No scanner is installed on this machine — gitleaks, trufflehog, pip-audit and osv-scanner are all absent — so this is a manual `git log -p` sweep plus targeted pattern searches, not a tool result. | Recording the method so the next audit knows what this baseline is worth. | n/a | `git log -p --all` filtered for key/token/password/PEM/AWS/Slack/GitHub patterns: no hits. |
| A-026 | config.json | Info | `config.json` is committed, so `_load_config`'s auto-create branch is unreachable in a fresh clone — and a committed config **overrides** `DEFAULT_CONFIG` wholesale. | Found by testing the A-006 fix: editing `DEFAULT_CONFIG` alone changed nothing, because the shipped file replaced the list. Anyone hardening the defaults must edit both. | FIXED@9362b86 | Both updated; `blocklist_hit` re-verified against the loaded config. |
| A-027 | server.py:430-434 | Medium | `press_combo` posted key-down **and** key-up both carrying the modifier flags and never released them, leaving the modifier asserted in the session's flag state. Found 2026-07-27 in follow-up work, after the audit run — it is the cause of the test 4 failure recorded below. | The next `CGEventKeyboardSetUnicodeString` is read as a shortcut and silently dropped: a `cmd+c` ending one `act()` batch eats the typing in the next. Keycode events are unaffected, so it presents as a dead HID path rather than a stuck flag. It also leaves a modifier asserted on the user's machine until a physical key press resets it. | FIXED@4bd37c3 · amended@2026-07-28 | 4 hermetic tests in `test_audit_fixes.py` that **fail against the previous implementation**, plus a 5th pinning the unchanged plain-key path (it passes on base by design — the release never fires without modifiers, so it is a guard against over-firing, not fix evidence); `verify.py` run twice back-to-back, 11/11 then 11/11, with no residual flags after. |

**Severity counts:** 1 Critical · 1 High · 11 Medium · 10 Low · 4 Info — 27 rows.
**Status counts:** 23 FIXED · 1 WITHDRAWN (A-021) · 1 PROPOSED (A-022) ·
1 DEFERRED (A-024) · 1 with no status, being an observation not a finding
(A-025) = 27. Hermetic suite: 60 tests.
A-027 was found after the audit closed; the other 26 are the run itself.
Both user decisions were resolved on the day — see Conflicts.

## Reconciliation

The third party (Codex CLI) ran `codex exec --sandbox read-only` against a
detached worktree at base commit 5e0b433, using
`references/third-party-audit-prompt.md` verbatim. It never saw this auditor's
findings, threat model or diffs. It returned exactly two findings.

**Data note:** the code is Marty's own local tooling — no client-adjacent or
sensitive material — so sending it to Codex was acceptable. Mistral Vibe (the
preferred auditor) is not installed on this machine; Codex was the fallback per
the skill's order.

### Both found

| A | B | Agreement |
|---|---|---|
| A-001 | B-001 (Critical) | `applescript()` is unrestricted same-user code execution and nullifies the GUI-only and sensitive-app safeguards, contradicting the stated no-shell/no-file-IO scope. Independent agreement on the top finding, at the same severity. Codex's suggested fix was stronger than mine — "remove the generic AppleScript tool, or replace it with narrowly scoped allowlisted operations". See Conflicts. |
| A-002 | B-002 (High) | Scoped `vision=True` silently falls back to whole-screen capture. Identical diagnosis, identical severity, and Codex's recommended fix ("return no image and a scoped error; require an explicit flag for fallback") is exactly what was implemented. |

### One auditor only

Everything else was found by this auditor alone. Codex returned **only** those
two findings, so the gap is broad rather than a specific blind spot. What its
sweep did not reach:

- **Secondary code paths.** It read the two tools with the largest stated
  security claims and stopped. `_app_url`'s path traversal (A-011),
  `_resolve_point`'s unbounded xy (A-007) and stale-coordinate fallback
  (A-008), and the per-app element budget (A-010) all sit one call deeper than
  the tool bodies.
- **Semantics of the audit log.** A-004 requires noticing that the *documented*
  mandatory first line makes the logged field a constant — a two-step
  inference across doc and code that a single-pass read misses.
- **Locale reasoning.** A-006 needs the observation that display names are
  localized *and* that this particular bundle id shares no substring with its
  English name. This auditor's finder verified it against the real Info.plist.
- **Robustness and lifecycle.** Temp-file accumulation (A-003), ref-cache
  growth (A-017), unaudited exits (A-015), the launch-timeout lie (A-013).

Conversely, Codex found nothing this auditor missed. On dependencies it
reported that `mcp==1.28.1` and `starlette==1.3.1` "are at the fixes for the
advisories checked", which is more than this auditor could establish with no
scanner installed — recorded, but not independently confirmed here, so it is
not carried as a finding either way.

Worth noting for future runs: the multi-agent sweep's own adversarial stage
killed 6 of its 61 raw findings, including a claimed audit-log path-traversal
and a world-readable-log finding whose stated impact did not hold on a
single-user Mac. Those are the shape of false positive this process is meant to
catch before it reaches this table.

### Conflicts — resolved by Marty, 2026-07-27

Both were put to Marty and both came back the same day. His answers and what
was done are recorded under each. The original arguments are kept verbatim so a
later reader can see what the decision was made against.

**A-005 → Marty took the recommendation: leave the element tree readable.**
No code change; the screenshot refusal from 9362b86 stands. Status closed.

**A-001 → Marty chose containment and delegated the design.** Implemented in
a4d9893 as a who-writes-it boundary rather than a what-it-does one: scripts live
in `scripts/`, the model selects by name and supplies argv, and raw script text
needs `"allow_raw_applescript": true` (default false). SPEC.md carries an
amendment note, since this supersedes the v1.0 signature. Rationale for
preferring this over the two alternatives is below.

**A-005 — should `see()` refuse blocklisted apps entirely?**
The screenshot half is fixed: writing a bitmap of a password manager to disk
has no defensible upside. The element tree is deliberately left readable, and
that half is Marty's call.

- *Case for blocking it too:* the threat model's named stake is credentials
  being read out of a password manager. `_label()` falls back to `AXValue`, so
  a revealed field's contents would enter the transcript. Exfiltration is the
  higher-value attack, and the control currently only stops typing.
- *Case for leaving it:* the config key is literally `input_blocklist` and the
  README describes it as refusing *input*. Reading a locked vault is harmless
  and legitimately useful — "is 1Password showing an unlock prompt?" is a
  reasonable question for an automation to ask.
- *Recommendation:* leave the tree readable, as now. The screenshot was the
  indefensible part and it is closed. If you want the stricter posture, the
  one-line change is a `blocklist_hit()` call on the resolved app in `see()`.

**A-001 — should `applescript()` be gated in code, not just described?**
Codex recommended removing the generic tool or replacing it with an allowlist.
Two of the sweep's finders proposed scanning script text for `do shell script`
and blocklisted app names.

- *Case for a scanner:* it raises the cost of a casual injection and makes the
  boundary enforced rather than merely stated.
- *Case against:* AppleScript is Turing-complete and the check is trivially
  evaded — `tell application (item 1 of argv)`, `run script`, or any string
  built at runtime. A control that looks like a boundary but is not is worse
  than an honestly documented gap, because it invites reliance.
- *Recommendation at the time:* keep the tool unrestricted and accurately
  described, or redesign to an allowlist if containment is wanted.

**What was built (a4d9893).** A third option, chosen over both of the above.
A shipped-template allowlist would have been too rigid — the tool exists
precisely for what the other four cannot do, and a fixed catalogue cannot
anticipate that. A text scanner was rejected for the reason given above. The
implemented boundary is neither:

> AppleScript cannot be sandboxed, and any check on script *text* is defeated
> by building the string at runtime. So the boundary is not what a script may
> do — it is **who writes it**. The adversary is a prompt-injected model, not
> Marty. Scripts in `scripts/` are user-authored and run unrestricted; the
> model may only pick one by name and supply argv.

This keeps full expressive power (Marty can write any script, and adding one is
dropping a file in a directory — no restart, the catalogue is read per call)
while removing the injected model's ability to author code. It is the same
argv discipline the server already applied to *data*, extended to *code*.

The escape hatch is deliberate and deliberately loud: `allow_raw_applescript`
restores the old behaviour for development, defaults to false, and every
refused attempt is audited with the script's fingerprint — visible in
`audit.jsonl` as `refused: raw script text disabled` alongside a sha256 of what
was attempted.

*Residual risk, stated plainly:* a script in `scripts/` is unrestricted once it
runs, so a model that can persuade Marty to add a file, or that reaches the
filesystem through another tool (Desktop Commander can write files), is not
contained by this. The boundary raises the bar from "one tool call" to "get a
file into a reviewed directory"; it is not a sandbox, and nothing available on
macOS would make it one.

## Bigger picture

*Project-level observations, explicitly not findings and not counted above.*

- **The security model was one tool wide; it no longer is.** Four tools were
  carefully bounded and the fifth was unbounded, so the effective posture of
  the server was "whatever AppleScript can do". After a4d9893 the default
  posture is bounded on all five, and the remaining gap is a directory Marty
  controls rather than a tool argument the model controls. The blocklist and
  focus guard still prevent *accidents* rather than a determined adversary —
  the docs say so plainly now.
- **`server.py` is 1150 lines against SPEC's 600–900 budget** (865 at base; 1050
  after the first fix pass, then the containment redesign, the `press_combo` fix
  and the 2026-07-28 review fixes). I did not delete security code to satisfy a line count,
  and I flag rather than silently bust the spec. If the budget matters more
  than the margin, the natural split is a `_input.py` for the CGEvent
  primitives — but "one file" is itself a stated feature, so this is a real
  trade-off and Marty's call.
- **`verify.py` is not CI-runnable, and it proved it twice.** It needs a live
  Mac with Accessibility, and it drives TextEdit and the clipboard. Test 4
  failed once on a macOS Automation consent dialog — environment — and later
  stopped passing entirely, which turned out to be a genuine latent defect
  (see below). Both looked identical from the outside, and checking against
  base told the two apart only in the sense that neither was a regression *from
  this audit*; it did not tell a fault in the machine from a fault in the code.
  Only the hermetic suite did that. `verify.py` is an acceptance ritual, not a
  regression net — but note it is what surfaced the defect in the first place,
  by being run twice in a row with no human touching the keyboard.

### Closed: verify.py test 4, end of session — a real defect in `press_combo`

Diagnosed and fixed in 4bd37c3 (**A-027**), later the same day, while adding
acceptance test 8. Root cause: **`press_combo` left the modifier logically held.** It
posted key-down and key-up both carrying `flags` and never cleared them, so the
modifier stayed asserted in the session's flag state. The next
`CGEventKeyboardSetUnicodeString` was then read as a shortcut and dropped.
Keycode events were unaffected — `cmd+n` still opened a document while typing
went nowhere, which is what made it look like a broken HID path rather than a
stuck flag.

Why it was intermittent: physical key presses reset the flag state, so it only
bit runs with no human at the keyboard in between. Test 4's own batch is click →
type → cmd+a → cmd+c, so typing happens *before* the combos — the first run
passes and arms the failure for the next one. Interactive re-runs kept passing;
two back-to-back automated runs did not.

Evidence, in both directions: `CGEventSourceFlagsState` read `0x20100000`
(command asserted) immediately after a run and `0x20000000` once cleared;
clearing it restored Unicode typing on the spot; a single `press_combo('cmd+a')`
re-broke it. The fix posts one flags-cleared event after the key-up, guarded so
a plain key still posts exactly key-down and key-up.

**The not-a-regression finding held exactly as recorded** — it fails identically
on base because the defect was latent in base, not introduced by the audit. The
lesson is that the two are not the same claim: "not a regression from this
audit" was true and load-bearing, and it did not exonerate the code. Both earlier hypotheses stay **refuted**, and are restated here so the
anti-retread record survives: (a) leftover TextEdit documents making verify.py's
geometric text-area pick ambiguous — refuted, and independently reconfirmed,
because the failure reproduces with exactly one document open; (b)
`execute_step` returning early because `AXPress` succeeds on a text area without
moving focus — refuted, because `AXPress` there actually fails with -25206
(`kAXErrorActionUnsupported`), so the real mouse-click fallback does run.

The warning that stood here — that changing input code to chase an environment
fault would be the worst outcome of an audit — was the right instinct and is
kept deliberately. What justified crossing it was hermetic evidence: the four
discriminating tests pinning this in `test_audit_fixes.py` monkeypatch the event
calls, fire no real input, need no live Mac, and **fail against the previous
implementation** (a fifth pins the plain-key path and passes on base by design).
An environment fault cannot fail a mocked unit test. Acceptance evidence alone
would not have been enough to touch this code.
- **`scripts/` is now the thing to guard.** The audit moved the trust boundary
  onto a directory, which is a better place for it but not a free one: anything
  landing there runs unrestricted. Worth reviewing it the way you would review
  a crontab, and worth noticing that Desktop Commander can write to it.
- **Consider whether `applescript` needs to be in the same server.** Splitting
  it into a separate MCP server would let Claude Desktop grant or withhold it
  independently, which is the only way "four bounded tools" becomes a property
  of the deployment rather than a sentence in a README.
- **No scanner tooling is installed.** `pip-audit` in the venv and `gitleaks`
  via Homebrew would make the dependency and secrets halves of the next audit
  evidence-based rather than manual.

---

# Accuracy review — 2026-07-28
Reviewing: the 2026-07-27 audit above · Branch: audit/2026-07-27 · Method: five
parallel reviewers over the code, each claim re-checked against `git show
5e0b433:<file>`, then verified by hand.

An audit is only worth its trustworthiness, so this pass audited the audit: is
every claim above true, does every fix do what its row says, did the audit break
anything, and do the tests it cites actually verify anything? Rows corrected in
place — a log that knowingly states something false is worse than no log — with
each amendment marked and dated so the original claim stays visible.

## What was wrong

**One finding was simply wrong and is withdrawn.** A-021 claimed `press_combo`'s
comment described behaviour the code did not have. It did not: the base comment
reads `"cmd++" is not a thing; treat a bare + as the = key`, which is accurate
and says the opposite of what the row asserted. The auditor misread it. Marked
WITHDRAWN rather than deleted.

**Two security fixes were incomplete**, both found by reading the code rather
than the audit:

- *A-007 failed open.* `_check_inside` returned without checking whenever the
  target exposed no accessibility windows — TextEdit with no document open, a
  menu-bar extra — which is exactly the target an injected caller would choose
  to get an unchecked click. The row said "Points outside the target's windows
  are now refused" with no caveat. Now fails closed.
- *A-005 covered only single-app scope.* `see(all=True, vision=True)` set
  `single = None`, skipped the blocklist entirely and took a full-screen grab,
  so a visible password manager still reached the transcript. Worse,
  `_screenshot`'s own error text recommended that exact call as the workaround
  when a window could not be found — the code was steering the model into the
  hole. Every target is now checked, and the steering text is gone.

**Two cited tests verified nothing.** `test_app_launch_rejects_paths` and
`test_named_script_rejects_paths` pass identically against the unguarded base
code, because every path they name is one that does not exist — the base
implementation returns None for all of them for the wrong reason. The
underlying fixes are real (base resolves `../../System/Applications/Calculator`
to a launchable bundle; the guard rejects it), but the evidence cited for them
was not. Both now carry a case that resolves, and the named-script test asserts
the rejection *message* rather than merely that an error occurred.

**Smaller inaccuracies:** A-016's timeout message reported the caller's uncapped
value rather than what was waited; A-022 said 49 pins where `requirements.txt`
has 39; A-027 claimed 5 discriminating tests where 4 discriminate and the fifth
pins an unchanged path; the status counts covered 26 of 27 rows; the line and
suite counts had gone stale. `applescript()` called with no arguments returned
the catalogue without writing an audit line, quietly breaking the very
every-call-is-logged invariant A-015 was about.

**One integrity regression, now repaired.** Commit d1e9226 deleted the text
describing the two refuted hypotheses for the test-4 failure while keeping the
sentence that referred to them, leaving a dangling "(a)" with no antecedent and
losing the -25206 evidence. The whole point of recording a refuted hypothesis is
so the next audit does not retread it. Restored.

## What held up

The load-bearing claims survived. 77 individual claims were re-checked and
confirmed, including: no path reaches `osa()` with model-authored script text
while `allow_raw_applescript` is false (probed exhaustively, including
`name`+`script` together, empty and None names); the blocklist genuinely holds
on non-English locales; `_screenshot` never substitutes a full-screen grab for
an app-scoped request and always removes its temp directory; the element budget
is genuinely shared across apps; every `app()` exit path audits; A-027's
mechanism and its pre-existence in base.

A-027's headline claim was re-verified independently: `press_combo` posts
key-down and key-up both carrying the modifier flags, the release event does
clear them, and the `if flags:` guard means a plain key still posts exactly two
events. The flag arithmetic quoted in that section is internally consistent —
`0x20100000` and `0x20000000` differ by exactly `0x100000`, the command bit.

`type_text` now pins its own flags to zero as well. The `press_combo` release
addresses contamination that `press_combo` itself causes; it does nothing about
a modifier the user is physically holding when `act()` fires, which produces the
same swallowed-text symptom.

## Bigger picture

- **The tests were the weak link, not the fixes.** Every fix examined was real;
  two of the tests certifying them were not. A test written from the same
  understanding that produced the fix inherits its blind spots — the cheap
  discipline that catches this is to revert the fix and confirm the test fails,
  which is now done for every security-relevant test added in this pass.
- **`_check_inside` and the A-005 branch were both "fixed" in the narrow case
  the test exercised** and open in the general one. Both holes were in the
  *shape* of the guard (single target, non-empty rects), not its logic.
- **The deployed server still advertises the pre-fix instructions.** Claude
  Desktop holds the old `instructions` string until it is restarted, so the
  model is still being told this server cannot run shell commands.
