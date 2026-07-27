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
| A-001 | server.py:543 (instructions), :843 (tool); README.md:5 | Critical | The server's own MCP instructions and README stated it "does not read or write files and does not run shell commands", while `applescript()` runs arbitrary AppleScript — reaching the shell via `do shell script`, reading and writing files, and bound by neither the blocklist nor the focus guard. | The instructions string is text the **model** reads to decide what is safe to call. A false capability claim makes it treat an unbounded tool as a sandboxed one. Both auditors independently ranked this first; Codex rated it Critical. | FIXED@9362b86 | Executed `do shell script "echo …; id -un"` through `osa()` — exit 0, returned the username. Post-fix asserted by `test_instructions_do_not_claim_the_server_cannot_run_shell_commands` and `test_applescript_docstring_warns_it_is_unbounded`. |
| A-002 | server.py:613-630 | High | `see(app=X, vision=True)` silently captured the entire screen whenever no layer-0 window could be found for X; nothing in the payload said the scope had widened. | Routine trigger (app hidden, minimised or windowless), and README promised "of the target app's window …, not the whole screen". Hands back every other visible window — the exact whole-desktop leak scoping exists to prevent. | FIXED@9362b86 | `test_scoped_capture_refuses_rather_than_grabbing_the_whole_screen` fails the test if `screencapture` is invoked unscoped for a named app. |
| A-003 | server.py:617 | Medium | Every `see(vision=True)` wrote a PNG of the screen into a fresh `mkdtemp` directory that nothing ever deleted. | Screen contents at rest, accumulating for the life of the machine. `Image(path=)` reads lazily after return, so the file could not simply be unlinked — bytes are now read up front and passed as `Image(data=)`. | FIXED@9362b86 | `test_capture_removes_its_temp_directory` asserts the directory is gone before return. |
| A-004 | server.py:858 | Medium | Every `applescript` audit line recorded the literal string `on run argv` — the documented mandatory first line — plus an argument count. Nothing about what ran. | The append-only log is the *only* detective control over the one tool that can do arbitrary damage. 50 existing log lines confirmed: every applescript row identical. A clipboard read and an exfiltration script were indistinguishable. | FIXED@9362b86 | `test_script_fingerprint_distinguishes_scripts`; visible live in `verify.py` output as `sha256:1c16b4b068a21152 43c/3L return item 1 of argv`. |
| A-005 | server.py:549-610 | Medium | `see()` consulted no blocklist, so `see(app="1Password", vision=true)` would return the password manager's element tree **and** write a bitmap of its window. `_label()` falls back to `AXValue`, so a revealed field's contents enter the transcript. | The threat model's named stake is credentials being read out of a password manager. Reading is the higher-value attack; the control only covered typing. | FIXED@9362b86 (screenshot) / USER-DECISION (element tree) | Screenshot refusal implemented and asserted; tree access deliberately unchanged — see Conflicts. |
| A-006 | server.py:36, config.json | Medium | The blocklist matched substrings of the **localized display name**. "System Settings" therefore guarded nothing outside English: its bundle id is `com.apple.systempreferences`, which shares no substring with its English name. | One of the three shipped entries silently protected nothing on a French, German or Japanese Mac — no refusal, no warning, an `ok` audit line. | FIXED@9362b86 | Bundle id confirmed via `defaults read`. Parametrised test covers English/French/German/Japanese names; `test_benign_apps_are_not_blocked` guards against over-blocking. |
| A-007 | server.py:453-456 | Medium | A raw `xy` in an `act()` step was never bounds-checked against the target's windows. | Mouse events post at `kCGHIDEventTap` and land wherever the pointer is, not in the process the guard verified. A click aimed at a blocklisted app's window landed there while a permitted app held focus — a blocklist bypass through `act()` itself, detected only by the after-check, once the click had fired. | FIXED@9362b86 | `test_xy_outside_the_target_windows_is_refused` / `test_xy_inside_the_target_window_is_allowed`. |
| A-008 | server.py:449 | Medium | When a ref's element no longer existed, `_resolve_point` silently substituted the coordinates cached at snapshot time. | The UI having moved is exactly when the stale point is most likely to be over something else — a click delivered to whatever now occupies those pixels. | FIXED@9362b86 | `test_dead_ref_does_not_fall_back_to_cached_coordinates`. |
| A-009 | server.py:641 (docstring) | Medium | The `act()` docstring promised "input can never land in the wrong window". Focus is proven at the two step boundaries only; a chunked type or a 15-event drag fires over hundreds of ms between those checks. | The model calibrates its trust from this text. The mechanism detects a mid-step steal after the fact — the README was honest ("may have landed elsewhere"), the model-facing docstring was not. | FIXED@9362b86 | Docstring now states the real guarantee. Measured: 2000 chars ≈ 125 chunks ≈ 750ms; drag ≈ 285ms. |
| A-010 | server.py:585-588 | Medium | `max_elements` was enforced per app rather than across the snapshot, and the truncation note fired whenever the *sum* crossed the cap even if nothing was truncated. | `see(all=True)` could return `max_elements` × number-of-apps — the context bloat the cap exists to prevent — while the note misreported both ways. | FIXED@9362b86 | Budget now shared via `budget["count"]`; note driven by an explicit `truncated` flag. |
| A-011 | server.py:710-722 | Medium | `_app_url()` built `Path(folder) / f"{name}.app"` from the caller-supplied name. `Path("/Applications") / "/tmp/Evil.app"` discards the left operand; `../` escapes the same way. | Launches any bundle on disk rather than the five app folders. Marty runs Desktop Commander alongside this server, which *can* write files — so "write a bundle to /tmp, then launch it by path" is a live cross-tool chain, not a hypothetical. Raised from the finder's Low for that reason. | FIXED@9362b86 | Demonstrated: `/tmp/Evil` → `/private/tmp/Evil.app`, `../../../../tmp/Evil` → `/private/tmp/Evil.app`. Five rejection cases tested; `test_app_url_still_resolves_a_real_bundle_id` guards the legitimate path. |
| A-012 | verify.py | Medium | `app()`, the capture path, the blocklist's bundle-id behaviour and launch-path handling had **no automated coverage**. `verify.py` needs a live Mac with Accessibility, so `test_focus_abort.py` (5 tests, focus only) was the entire CI-runnable suite. | A security control with no hermetic test regresses silently. | FIXED@9362b86 | `test_audit_fixes.py` added: 28 tests, each pinned to a finding. Suite now 33 hermetic tests. |
| A-013 | server.py:771-790 | Low | `app(action="launch")` returned `ok=True` when its 15s wait expired without the app finishing launching, as long as some app matched the name. | Tells the caller it is safe to drive an app that is not ready. | FIXED@9362b86 | Returns an explicit not-finished-launching error. |
| A-014 | server.py:733-740 | Low | `_place_window` coerced unvalidated caller data with `float()` and indexing, so a malformed `window` dict raised out of the tool — losing the audit line and returning an unstructured error. | Every other failure in this server is structured; this one escaped to FastMCP. | FIXED@9362b86 | Wrapped; the error is reported in `result["window"]`. |
| A-015 | server.py:758-761 | Low | `app()` returned on an unrecognised action without writing an audit line. | README states every `see`/`act`/`app`/`notify`/`applescript` call appends one line. It did not. | FIXED@9362b86 | `test_unknown_app_action_is_audited`. |
| A-016 | server.py:79-102 | Low | The caller-supplied `timeout` had a floor but no ceiling. | The server is single-threaded; `applescript(timeout=10**9)` wedges all five tools indefinitely. | FIXED@9362b86 | Capped at 300s; `test_osa_timeout_is_bounded`. |
| A-017 | server.py:133-135 | Low | `_REFS` was pruned only when an app reappeared under a different pid, so repeated `see()` on a live app grew it for the life of the process, each entry pinning an `AXUIElement`. | A long-lived server session accumulates unboundedly. Read-verified from the code, not live-demonstrated. | FIXED@9362b86 | Bounded at 20 000 with oldest-first eviction; `test_ref_cache_is_bounded`. |
| A-018 | server.py:88 | Low | `osa()` executed the bare name `osascript`, resolved through the PATH inherited from whatever launched the server. | An earlier writable PATH entry substitutes the interpreter that runs every AppleScript. Requires PATH control, so Low. | FIXED@9362b86 | Absolute `/usr/bin/osascript`; `test_osa_uses_an_absolute_binary_path`. |
| A-019 | server.py:603-606 | Low | Any capture failure was reported to the caller as a missing Screen Recording permission. | Sends the user to fix a permission that is already granted, hiding the real cause. | FIXED@9362b86 | `_screenshot` returns the actual error; `test_capture_reports_missing_permission_distinctly`. |
| A-020 | server.py:41-51 | Low | `_load_config` wrote `config.json` at import with no exception handling. | A read-only install directory would stop the server from starting at all. | FIXED@9362b86 | Wrapped; falls back to defaults with a stderr note. |
| A-021 | server.py:373 | Low | `press_combo`'s comment claimed it treated `"cmd++"` as the `=` key; that input actually raises "more than one non-modifier key". Only a trailing `"cmd+"` reaches the branch. | Comment describes behaviour the code does not have. Cosmetic — `cmd+shift+=` works — but the comment misleads the next reader. | FIXED@9362b86 | Comment corrected to match; behaviour unchanged. |
| A-022 | requirements.txt | Low | All 49 pins are version-only: no lockfile, no hashes, no `--require-hashes`. | The process holds Accessibility and Screen Recording; a compromised package inherits both. | PROPOSED | Not fixed — adding hashes changes the documented install flow. |
| A-023 | server.py:839 | Info | `notify()` writes the first 120 characters of the message body verbatim to the audit log. | Not a defect (a notification is displayed on screen anyway), but it is the one tool whose content reaches the log. Now documented in README. | FIXED@9362b86 (documented) | README "Audit log" section. |
| A-024 | server.py:41-56 | Info | `config.json` is read once at import and never re-read; editing the blocklist requires restarting the server. | Worth knowing when changing the blocklist — the change is not live. | DEFERRED | Behaviour confirmed by reading; no fix applied. |
| A-025 | repo-wide | Info | No secrets in the working tree or in any of the 6 commits. No scanner is installed on this machine — gitleaks, trufflehog, pip-audit and osv-scanner are all absent — so this is a manual `git log -p` sweep plus targeted pattern searches, not a tool result. | Recording the method so the next audit knows what this baseline is worth. | n/a | `git log -p --all` filtered for key/token/password/PEM/AWS/Slack/GitHub patterns: no hits. |
| A-026 | config.json | Info | `config.json` is committed, so `_load_config`'s auto-create branch is unreachable in a fresh clone — and a committed config **overrides** `DEFAULT_CONFIG` wholesale. | Found by testing the A-006 fix: editing `DEFAULT_CONFIG` alone changed nothing, because the shipped file replaced the list. Anyone hardening the defaults must edit both. | FIXED@9362b86 | Both updated; `blocklist_hit` re-verified against the loaded config. |

**Severity counts:** 1 Critical · 1 High · 10 Medium · 10 Low · 4 Info.
**Status counts:** 22 FIXED · 1 PROPOSED · 1 DEFERRED · 1 USER-DECISION (A-005, partially fixed).

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

### Conflicts (user decision)

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
- *Recommendation:* keep the tool unrestricted and now-accurately described,
  which is what was implemented. If you want real containment, the effective
  version is an **allowlist** of named script templates the server ships,
  with `applescript()` reduced to choosing one and supplying argv — a
  significant redesign, and a genuine change to what the tool is for. That is
  yours to decide, not mine to apply.

## Bigger picture

*Project-level observations, explicitly not findings and not counted above.*

- **The security model is one tool wide.** Four tools are carefully bounded and
  the fifth is unbounded, which means the effective posture of this server is
  "whatever AppleScript can do". That is a defensible design for a personal
  tool, but the honest framing is that the blocklist and focus guard prevent
  *accidents*, not a determined injected model. The docs now say so.
- **`server.py` is 1050 lines against SPEC's 600–900 budget.** The audit fixes
  added ~190 lines net. I did not delete security code to satisfy a line count,
  and I flag rather than silently bust the spec. If the budget matters more
  than the margin, the natural split is a `_input.py` for the CGEvent
  primitives — but "one file" is itself a stated feature, so this is a real
  trade-off and Marty's call.
- **`verify.py` is not CI-runnable** — it needs a live Mac, Accessibility, and
  it drives TextEdit and the clipboard. During this audit its test 4 failed
  once on a macOS Automation consent dialog, which looks exactly like a code
  regression until you check. The hermetic suite added here is what should gate
  changes; `verify.py` is an acceptance ritual, not a regression net.
- **Consider whether `applescript` needs to be in the same server.** Splitting
  it into a separate MCP server would let Claude Desktop grant or withhold it
  independently, which is the only way "four bounded tools" becomes a property
  of the deployment rather than a sentence in a README.
- **No scanner tooling is installed.** `pip-audit` in the venv and `gitleaks`
  via Homebrew would make the dependency and secrets halves of the next audit
  evidence-based rather than manual.
