#!/usr/bin/env python3
"""Acceptance tests 1-8 from SPEC.md, run live against this Mac.

Run: .venv/bin/python verify.py

Tests 1-5, 7 and 8 drive the real machine: they post a notification, run
AppleScript, snapshot Finder, type into a scratch TextEdit document, try to
touch a blocklisted app, and capture a window. Test 6 is a pure unit test and is
delegated to pytest.

Two safety rules this script keeps: the clipboard is saved before test 4 and
restored after, and TextEdit documents it did not create are never closed.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import server

ROOT = Path(__file__).resolve().parent
RESULTS: list[tuple[str, bool | None, str]] = []

CLIP_READ = 'on run argv\n\ttry\n\t\treturn (the clipboard as text)\n\ton error\n\t\treturn ""\n\tend try\nend run\n'
CLIP_WRITE = 'on run argv\n\tset the clipboard to item 1 of argv\n\treturn "ok"\nend run\n'
TEXTEDIT_NEW = ('on run argv\n\ttell application "TextEdit"\n\t\tactivate\n'
                '\t\tmake new document\n\tend tell\n\treturn "ok"\nend run\n')
# Closes the front document only if its text is exactly what we typed, so a
# document this script did not create can never be destroyed.
TEXTEDIT_CLOSE = ('on run argv\n\ttell application "TextEdit"\n'
                  '\t\tif (count of documents) is 0 then return "no documents"\n'
                  '\t\tif (text of document 1) is not equal to (item 1 of argv) then '
                  'return "front document is not ours - left open"\n'
                  '\t\tclose document 1 saving no\n'
                  '\t\treturn "closed"\n\tend tell\nend run\n')
TEXTEDIT_COUNT = ('on run argv\n\ttell application "TextEdit" to return '
                  '(count of documents) as text\nend run\n')

NOTIFY_TEXT = 'test — "quotes" é 🙂'
TYPED_TEXT = 'Ligne — "était" \'ok\''


def record(name: str, ok: bool | None, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    mark = {True: "PASS", False: "FAIL", None: "UNVERIFIED"}[ok]
    print(f"\n[{mark}] {name}\n    {detail}")


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


# -- 1 -----------------------------------------------------------------------
def test_1_notify() -> None:
    banner("Test 1 — notify with quotes, em dash, accent and emoji")
    result = server.notify(message=NOTIFY_TEXT, title="Mac-Commander",
                           subtitle="acceptance test 1")
    print("  sent message:", repr(NOTIFY_TEXT))
    print("  returned:", json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        record("1. notify", False, f"exit {result['exit_code']}: {result['stderr']}")
        return
    record("1. notify", True,
           "osascript returned ok with no error. Whether the banner actually "
           "appeared on screen is a human observation this script cannot make — "
           "check Notification Centre.")


# -- 2 -----------------------------------------------------------------------
def test_2_applescript_roundtrip() -> None:
    banner("Test 2 — applescript argv round-trip via a named script")
    catalogue = server.applescript()
    print("  catalogue:", json.dumps(catalogue["scripts"]))
    print("  raw script text allowed:", catalogue["raw_allowed"])
    arg = 'quote " and em dash — and é and 🙂'
    result = server.applescript(name="echo-argv", args=[arg])
    print("  arg in :", repr(arg))
    print("  stdout :", repr(result["stdout"]))
    print("  exit   :", result["exit_code"], "stderr:", repr(result["stderr"]))
    ok = result["exit_code"] == 0 and result["stdout"] == arg
    record("2. applescript round-trip", ok,
           "byte-exact through scripts/echo-argv.applescript"
           if ok else "argument did not survive the round-trip")


def test_2b_raw_script_refused() -> None:
    banner("Test 2b — model-authored script text is refused by default (A-001)")
    result = server.applescript(script='on run argv\n\treturn do shell script "id -un"\nend run\n')
    print("  applescript(script=...):", json.dumps(result, ensure_ascii=False)[:220])
    ok = result.get("ok") is False and "raw AppleScript is disabled" in result.get("error", "")
    record("2b. raw script refused", ok,
           "refused, and the error names the catalogue instead"
           if ok else "raw script text was NOT refused")


# -- 3 -----------------------------------------------------------------------
def test_3_see_finder() -> None:
    banner("Test 3 — see(app='Finder') is scoped to Finder alone")
    snapshot = server.see(app="Finder")
    if "error" in snapshot:
        record("3. see(Finder)", False, snapshot["error"])
        return
    apps = snapshot["apps"]
    print("  frontmost:", json.dumps(snapshot["frontmost"], ensure_ascii=False))
    print("  scope:", snapshot["scope"], "| apps in payload:", [a["name"] for a in apps])
    print("  windows:", json.dumps(apps[0]["windows"], ensure_ascii=False)[:300])
    print(f"  elements: {snapshot['elements_returned']}")
    for element in apps[0]["elements"][:8]:
        print("   ", json.dumps(element, ensure_ascii=False))
    if len(apps[0]["elements"]) > 8:
        print(f"    ... {len(apps[0]['elements']) - 8} more")

    finder_pid = apps[0]["pid"]
    # Every ref handed out must belong to Finder's process — nothing leaks in
    # from the other running apps.
    strays = [e["ref"] for a in apps for e in a["elements"]
              if server._REFS[e["ref"]]["pid"] != finder_pid]
    ok = (len(apps) == 1 and apps[0]["name"] == "Finder"
          and snapshot["elements_returned"] > 0 and not strays)
    record("3. see(Finder)", ok,
           f"1 app in payload ({apps[0]['name']}, pid {finder_pid}), "
           f"{snapshot['elements_returned']} elements, 0 refs from other pids"
           if ok else f"apps={[a['name'] for a in apps]} strays={strays}")


# -- 4 -----------------------------------------------------------------------
def test_4_textedit_end_to_end() -> None:
    banner("Test 4 — end-to-end on TextEdit (clipboard saved and restored)")
    saved = server.osa(CLIP_READ, [])
    print("  saved clipboard:", repr(saved["stdout"][:70]))
    before = server.osa(TEXTEDIT_COUNT, [])
    docs_before = int(before["stdout"]) if before["stdout"].isdigit() else 0
    print("  TextEdit documents already open:", docs_before)

    try:
        # osa() is the internal primitive; the TextEdit scripts here are test
        # fixtures, not part of the shipped catalogue.
        opened = server.osa(TEXTEDIT_NEW, [])
        print("  applescript activate + new document:", json.dumps(opened))
        if opened["exit_code"] != 0:
            record("4. TextEdit end-to-end", False, f"could not open TextEdit: {opened['stderr']}")
            return
        time.sleep(1.2)

        snapshot = server.see(app="TextEdit")
        target_app = snapshot["apps"][0]
        main = next((w for w in target_app["windows"] if w["main"]), None) or target_app["windows"][0]
        print("  main window:", json.dumps(main, ensure_ascii=False))
        area = next((e for e in target_app["elements"]
                     if e["role"] == "AXTextArea" and _inside(e.get("xy"), main)), None)
        if area is None:
            record("4. TextEdit end-to-end", False,
                   "no AXTextArea found inside the new document's window")
            return
        print("  clicking ref:", json.dumps(area, ensure_ascii=False))

        result = server.act(target="TextEdit", steps=[
            {"kind": "click", "ref": area["ref"]},
            {"kind": "type", "text": TYPED_TEXT},
            {"kind": "key", "combo": "cmd+a"},
            {"kind": "key", "combo": "cmd+c"},
        ])
        print("  act():", json.dumps(result, ensure_ascii=False))
        time.sleep(0.4)

        clip = server.osa(CLIP_READ, [])
        print("  typed    :", repr(TYPED_TEXT))
        print("  clipboard:", repr(clip["stdout"]))
        ok = bool(result.get("ok")) and clip["stdout"] == TYPED_TEXT
        record("4. TextEdit end-to-end", ok,
               "clipboard matches the typed text byte-for-byte"
               if ok else "clipboard did not match the typed text")
    finally:
        closed = server.osa(TEXTEDIT_CLOSE, [TYPED_TEXT])
        print("  close document:", json.dumps(closed, ensure_ascii=False))
        after = server.osa(TEXTEDIT_COUNT, [])
        remaining = int(after["stdout"]) if after["stdout"].isdigit() else -1
        if remaining == 0:
            print("  quit TextEdit:", json.dumps(server.app(action="quit", name="TextEdit")))
        else:
            print(f"  left TextEdit running — {remaining} pre-existing document(s) still open")
        restored = server.osa(CLIP_WRITE, [saved["stdout"]])
        print("  clipboard restored:", restored["exit_code"] == 0,
              "(text only — an image or file on the clipboard cannot be preserved)")


def _inside(xy, window) -> bool:
    if not xy or not window.get("pos") or not window.get("size"):
        return False
    x, y = xy
    wx, wy = window["pos"]
    ww, wh = window["size"]
    return wx <= x <= wx + ww and wy <= y <= wy + wh


# -- 5 -----------------------------------------------------------------------
def test_5_blocklist() -> None:
    banner("Test 5 — act() against a blocklisted app is refused")
    result = server.act(target="System Settings",
                        steps=[{"kind": "key", "combo": "cmd+a"}])
    print("  act():", json.dumps(result, ensure_ascii=False))
    ok = (result["ok"] is False and result["steps_completed"] == 0
          and "refused" in result["failed_step"]["reason"])
    record("5. blocklist refusal", ok,
           result["failed_step"]["reason"] if ok else "was not refused as expected")


# -- 6 -----------------------------------------------------------------------
def test_6_focus_abort_unit() -> None:
    banner("Test 6 — focus-abort unit test (pytest, frontmost mocked)")
    proc = subprocess.run([sys.executable, "-m", "pytest", "test_focus_abort.py", "-v"],
                          cwd=ROOT, capture_output=True, text=True)
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip())
    record("6. focus-abort unit test", proc.returncode == 0,
           "pytest exit 0" if proc.returncode == 0 else f"pytest exit {proc.returncode}")


# -- 7 -----------------------------------------------------------------------
def test_7_audit_log(start_line: int) -> None:
    banner("Test 7 — audit.jsonl has one line per call made above")
    if not server.AUDIT_PATH.exists():
        record("7. audit log", False, f"{server.AUDIT_PATH} does not exist")
        return
    lines = server.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    new = lines[start_line:]
    print(f"  {server.AUDIT_PATH} — {len(new)} line(s) appended by this run:\n")
    parsed = []
    for line in new:
        print("   ", line)
        parsed.append(json.loads(line))
    tools = [entry["tool"] for entry in parsed]
    print("\n  tools logged:", tools)
    ok = bool(parsed) and {"act", "applescript", "notify", "see"} <= set(tools)
    record("7. audit log", ok,
           f"{len(parsed)} well-formed JSONL lines covering {sorted(set(tools))}"
           if ok else f"expected act/applescript/notify/see, got {sorted(set(tools))}")


# -- 8 -----------------------------------------------------------------------
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _split_image(result):
    """see() returns the payload dict alone, or [json_text, Image] with a capture."""
    if isinstance(result, list) and len(result) == 2:
        payload = result[0]
        return (json.loads(payload) if isinstance(payload, str) else payload,
                getattr(result[1], "data", None))
    return result, None


def _capture_dirs() -> set[str]:
    """Scratch directories _screenshot() creates, which it must also remove."""
    return {str(p) for p in Path(tempfile.gettempdir()).glob("mac-commander-*")}


def test_8_vision() -> None:
    banner("Test 8 — see(vision=True) captures, refuses and cleans up")
    if not server.Quartz.CGPreflightScreenCaptureAccess():
        detail = ("Screen Recording is not granted to the process running this "
                  "script, so the capture path cannot execute. Grant it in System "
                  "Settings > Privacy & Security > Screen & System Audio Recording "
                  "and re-run. Note the permission belongs to the host app (e.g. "
                  "Terminal), not to python itself.")
        for name in ("8. vision capture", "8b. vision blocklist refusal"):
            record(name, None, detail)
        return

    pre_existing = _capture_dirs()

    # 8 — a windowed app yields a real PNG of that window alone.
    snapshot, png = _split_image(server.see(app="Finder", vision=True))
    notes = snapshot.get("notes", [])
    print("  scope:", snapshot.get("scope"), "| elements:", snapshot.get("elements_returned"))
    print("  notes:", json.dumps(notes, ensure_ascii=False))
    if png is None:
        # Finder with no window on screen is a legitimate state, and the server
        # is required to say so rather than substitute a whole-screen grab.
        record("8. vision capture", None,
               "no image returned — " + ("; ".join(notes) if notes else "no reason given")
               + ". Open a Finder window and re-run to exercise the capture itself.")
    else:
        print(f"  image: {len(png)} bytes, magic {png[:8]!r}")
        ok = png.startswith(PNG_MAGIC) and len(png) > len(PNG_MAGIC)
        record("8. vision capture", ok,
               f"{len(png)} bytes of valid PNG for Finder's window. That the pixels "
               "show the right window is a human observation this script cannot make."
               if ok else f"returned {len(png)} bytes that are not a PNG")

    # 8b — a blocklisted target loses the bitmap but keeps the element tree.
    # No password manager is guaranteed to be running, so Finder is blocklisted
    # for the length of this check and the real config is put back afterwards.
    original = list(server.CONFIG.get("input_blocklist") or [])
    try:
        server.CONFIG["input_blocklist"] = original + ["Finder"]
        snapshot, png = _split_image(server.see(app="Finder", vision=True))
    finally:
        server.CONFIG["input_blocklist"] = original
    notes = snapshot.get("notes", [])
    elements = snapshot.get("elements_returned", 0)
    print("  blocklist in force:", json.dumps(original + ["Finder"]))
    print("  notes:", json.dumps(notes, ensure_ascii=False))
    print("  elements still returned:", elements)
    print("  blocklist restored:", server.CONFIG["input_blocklist"] == original)
    ok = (png is None
          and any("no screenshot" in str(n) for n in notes)
          and elements > 0
          and server.CONFIG["input_blocklist"] == original)
    record("8b. vision blocklist refusal", ok,
           f"image withheld, {elements} elements still returned — reading a locked "
           "vault stays allowed, writing its pixels out does not"
           if ok else f"png={'present' if png else 'absent'} elements={elements} notes={notes}")

    # 8c — a capture of the user's screen must not be left behind on disk.
    leaked = sorted(_capture_dirs() - pre_existing)
    print("  scratch dirs left by this test:", leaked or "none")
    record("8c. no capture left on disk", not leaked,
           f"no mac-commander-* scratch directory survives in {tempfile.gettempdir()}"
           if not leaked else f"left behind: {leaked}")


def main() -> int:
    print("Mac-Commander acceptance tests")
    print("Accessibility trusted for this process:", server.AS.AXIsProcessTrusted())
    print("Screen Recording available:", server.Quartz.CGPreflightScreenCaptureAccess())
    print("audit log:", server.AUDIT_PATH)
    start = (len(server.AUDIT_PATH.read_text(encoding="utf-8").splitlines())
             if server.AUDIT_PATH.exists() else 0)

    test_1_notify()
    test_2_applescript_roundtrip()
    test_2b_raw_script_refused()
    test_3_see_finder()
    test_4_textedit_end_to_end()
    test_5_blocklist()
    test_6_focus_abort_unit()
    # 8 runs before 7 so its see() calls land in the range test 7 inspects.
    test_8_vision()
    test_7_audit_log(start)

    banner("Summary")
    for name, ok, detail in RESULTS:
        mark = {True: "PASS", False: "FAIL", None: "UNVERIFIED"}[ok]
        print(f"  {mark:<11} {name}")
    failed = [name for name, ok, _ in RESULTS if ok is False]
    print(f"\n  {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("  failed:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
