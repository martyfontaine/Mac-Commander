#!/usr/bin/env python3
"""Mac-Commander — a minimal macOS GUI-automation MCP server.

Five tools: see, act, app, notify, applescript. No file I/O, no shell for the
caller — Desktop Commander owns that layer.

Two rules shape every line below:
  1. AppleScript is never assembled by string interpolation. The script text is
     a constant handed to `osascript -` on stdin; every dynamic value travels
     as an argv entry into an `on run argv` handler. No exceptions.
  2. No synthetic input fires without proving the target app is frontmost
     immediately before it, and re-proving it immediately after.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import AppKit
import ApplicationServices as AS
import Quartz
from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
from Foundation import NSURL
from mcp.server.fastmcp import FastMCP, Image

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    "input_blocklist": ["1Password", "Passwords", "System Settings"],
    "audit_log": "audit.jsonl",
}


def _load_config() -> dict:
    """Read config.json beside this file, creating it with defaults if absent."""
    path = ROOT / "config.json"
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        return {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8"))}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"mac-commander: config.json unreadable ({exc}); using defaults", file=sys.stderr)
        return dict(DEFAULT_CONFIG)


CONFIG = _load_config()
_log_name = str(CONFIG.get("audit_log") or "audit.jsonl")
AUDIT_PATH = Path(_log_name) if os.path.isabs(_log_name) else ROOT / _log_name


def audit(tool: str, target: Any, summary: str, result: str) -> None:
    """Append one JSONL line. Append-only: this server never reads or rewrites it."""
    try:
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "tool": tool,
            "target": target,
            "summary": summary,
            "result": result,
        }
        with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as exc:  # a broken log must never break a call
        print(f"mac-commander: audit write failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# AppleScript — script on stdin, values on argv, never interpolated
# --------------------------------------------------------------------------

def osa(script: str, args: list[str], timeout: int = 30) -> dict:
    """Run `osascript - <args>` with `script` piped in verbatim.

    The spec calls for `osascript /dev/stdin <args>`; on macOS 26 osascript
    cannot read a device file ("I/O error (bummers)") because it seeks the
    program. `osascript -` is the same contract that actually works: program on
    stdin, all dynamic values as argv. Verified byte-exact for quotes, em
    dashes and emoji.
    """
    argv = ["osascript", "-", *[str(a) for a in args]]
    try:
        proc = subprocess.run(
            argv,
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=max(1, int(timeout)),
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": f"timed out after {timeout}s"}
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace").rstrip("\n"),
        "stderr": proc.stderr.decode("utf-8", "replace").strip(),
    }


NOTIFY_SCRIPT = """on run argv
	set theMessage to item 1 of argv
	set theTitle to item 2 of argv
	set theSubtitle to item 3 of argv
	set theSound to item 4 of argv
	if theSound is "" then
		display notification theMessage with title theTitle subtitle theSubtitle
	else
		display notification theMessage with title theTitle subtitle theSubtitle sound name theSound
	end if
	return "ok"
end run
"""


# --------------------------------------------------------------------------
# Accessibility
# --------------------------------------------------------------------------

INTERACTIVE_ROLES = {
    "AXButton", "AXCheckBox", "AXColorWell", "AXComboBox", "AXDisclosureTriangle",
    "AXIncrementor", "AXLink", "AXMenuButton", "AXMenuItem", "AXPopUpButton",
    "AXRadioButton", "AXSearchField", "AXSlider", "AXStepper", "AXTabButton",
    "AXTextArea", "AXTextField", "AXToolbarButton",
}
MAX_DEPTH = 18
MAX_NODES = 4000

_REFS: dict[str, dict] = {}      # "e17" -> {el, pid, app, center}
_APP_PIDS: dict[str, int] = {}   # app key -> pid it was last snapshotted at
_ref_seq = 0


def _attr(el, name):
    err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
    return val if err == AS.kAXErrorSuccess else None


def _geometry(el) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """Return ((x, y), (w, h)) in screen points, or (None, None) on failure."""
    pos = size = None
    raw = _attr(el, AS.kAXPositionAttribute)
    if raw is not None:
        ok, pt = AS.AXValueGetValue(raw, AS.kAXValueTypeCGPoint, None)
        if ok:
            pos = (float(pt.x), float(pt.y))
    raw = _attr(el, AS.kAXSizeAttribute)
    if raw is not None:
        ok, sz = AS.AXValueGetValue(raw, AS.kAXValueTypeCGSize, None)
        if ok:
            size = (float(sz.width), float(sz.height))
    return pos, size


def _center(el) -> tuple[float, float] | None:
    pos, size = _geometry(el)
    if pos is None or size is None:
        return None
    return (pos[0] + size[0] / 2.0, pos[1] + size[1] / 2.0)


def _label(el) -> str:
    """Best human-readable name, in the order macOS apps actually populate them."""
    for name in (AS.kAXTitleAttribute, AS.kAXDescriptionAttribute, AS.kAXHelpAttribute):
        val = _attr(el, name)
        if isinstance(val, str) and val.strip():
            return val.strip()[:120]
    val = _attr(el, AS.kAXValueAttribute)
    if isinstance(val, str) and val.strip():
        return val.strip()[:120]
    return ""


def _new_ref(el, pid: int, app_key: str, center) -> str:
    global _ref_seq
    _ref_seq += 1
    ref = f"e{_ref_seq}"
    _REFS[ref] = {"el": el, "pid": pid, "app": app_key, "center": center}
    return ref


def _invalidate(app_key: str, pid: int) -> None:
    """Drop cached refs for an app that has been relaunched under a new pid."""
    if _APP_PIDS.get(app_key) not in (None, pid):
        for ref in [r for r, v in _REFS.items() if v["app"] == app_key]:
            del _REFS[ref]
    _APP_PIDS[app_key] = pid


def _collect(el, pid: int, app_key: str, out: list, budget: dict, depth: int = 0) -> None:
    """Depth-first walk gathering interactive elements, bounded on every axis."""
    if depth > MAX_DEPTH or len(out) >= budget["max"] or budget["nodes"] >= MAX_NODES:
        return
    children = _attr(el, AS.kAXChildrenAttribute) or []
    for child in children:
        if len(out) >= budget["max"] or budget["nodes"] >= MAX_NODES:
            return
        budget["nodes"] += 1
        role = _attr(child, AS.kAXRoleAttribute)
        if role in INTERACTIVE_ROLES:
            center = _center(child)
            item = {
                "ref": _new_ref(child, pid, app_key, center),
                "role": role,
                "label": _label(child),
                "enabled": bool(_attr(child, AS.kAXEnabledAttribute)),
            }
            if center:
                item["xy"] = [round(center[0], 1), round(center[1], 1)]
            out.append(item)
        _collect(child, pid, app_key, out, budget, depth + 1)


def _snapshot(running, budget: dict) -> dict:
    """Windows + interactive elements for one NSRunningApplication."""
    pid = int(running.processIdentifier())
    app_key = str(running.bundleIdentifier() or running.localizedName() or pid)
    _invalidate(app_key, pid)
    app_el = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app_el, 2.0)

    windows = []
    for win in _attr(app_el, AS.kAXWindowsAttribute) or []:
        pos, size = _geometry(win)
        windows.append({
            "title": (_attr(win, AS.kAXTitleAttribute) or "")[:160],
            "pos": [round(pos[0], 1), round(pos[1], 1)] if pos else None,
            "size": [round(size[0], 1), round(size[1], 1)] if size else None,
            "main": bool(_attr(win, AS.kAXMainAttribute)),
        })

    elements: list[dict] = []
    _collect(app_el, pid, app_key, elements, budget)
    return {
        "name": str(running.localizedName() or ""),
        "bundle_id": str(running.bundleIdentifier() or ""),
        "pid": pid,
        "windows": windows,
        "elements": elements,
    }


# --------------------------------------------------------------------------
# Focus — the thing the old server got wrong
# --------------------------------------------------------------------------

_WS = AppKit.NSWorkspace.sharedWorkspace()


def frontmost() -> dict:
    """Who owns the keyboard right now. Pumping the run loop keeps NSWorkspace
    current in a process that has no run loop of its own."""
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, False)
    app = _WS.frontmostApplication()
    if app is None:
        return {"name": None, "bundle_id": None, "pid": None}
    return {
        "name": str(app.localizedName() or ""),
        "bundle_id": str(app.bundleIdentifier() or ""),
        "pid": int(app.processIdentifier()),
    }


def _running_apps() -> list:
    return list(_WS.runningApplications() or [])


def find_running(name: str):
    """Resolve a name or bundle id to a running app: exact, then case-insensitive,
    then prefix. Regular (Dock-visible) apps win over background ones."""
    if not name:
        return None
    needle = name.strip().lower()
    ranked = sorted(
        _running_apps(),
        key=lambda a: 0 if a.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular else 1,
    )
    for test in (
        lambda a: (a.bundleIdentifier() or "").lower() == needle,
        lambda a: (a.localizedName() or "").lower() == needle,
        lambda a: (a.localizedName() or "").lower().startswith(needle),
    ):
        for app in ranked:
            if test(app):
                return app
    return None


def wait_frontmost(pid: int, timeout: float = 3.0) -> dict | None:
    """Poll until `pid` owns the keyboard. Returns None on success, else the
    frontmost app that won instead."""
    deadline = time.monotonic() + timeout
    while True:
        front = frontmost()
        if front["pid"] == pid:
            return None
        if time.monotonic() >= deadline:
            return front
        time.sleep(0.05)


def blocklist_hit(*names) -> str | None:
    """Fail closed: any name or bundle id containing a blocked term is refused."""
    for entry in CONFIG.get("input_blocklist") or []:
        term = str(entry).strip().lower()
        if not term:
            continue
        for name in names:
            if name and term in str(name).lower():
                return str(entry)
    return None


# --------------------------------------------------------------------------
# Synthetic input
# --------------------------------------------------------------------------

_SRC = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)

MODIFIERS = {
    "cmd": Quartz.kCGEventFlagMaskCommand, "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "ctrl": Quartz.kCGEventFlagMaskControl, "control": Quartz.kCGEventFlagMaskControl,
    "alt": Quartz.kCGEventFlagMaskAlternate, "opt": Quartz.kCGEventFlagMaskAlternate,
    "option": Quartz.kCGEventFlagMaskAlternate, "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}
# US-layout virtual keycodes. Layout-dependent by nature; typing goes through
# Unicode instead, so this table only has to cover shortcut keys.
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "`": 50, "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "esc": 53, "help": 114, "home": 115, "pageup": 116,
    "forwarddelete": 117, "end": 119, "pagedown": 121, "left": 123, "right": 124,
    "down": 125, "up": 126, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
    "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


def type_text(text: str) -> None:
    """Post text as Unicode, not keycodes — é, —, " and 🙂 all survive."""
    for start in range(0, len(text), 16):
        chunk = text[start:start + 16]
        length = len(chunk.encode("utf-16-le")) // 2  # UniChar count, not code points
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(_SRC, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(event, length, chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.006)


def press_combo(combo: str) -> None:
    parts = [p.strip().lower() for p in str(combo).split("+")]
    flags, key = 0, None
    for part in parts:
        if part in MODIFIERS:
            flags |= MODIFIERS[part]
        elif part == "" and key is None:
            key = "="  # "cmd++" is not a thing; treat a bare + as the = key
        elif key is None:
            key = part
        else:
            raise ValueError(f"combo {combo!r} names more than one non-modifier key")
    if key is None:
        raise ValueError(f"combo {combo!r} has no key, only modifiers")
    if key not in KEYCODES:
        raise ValueError(f"unknown key {key!r} in combo {combo!r}")
    code = KEYCODES[key]
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(_SRC, code, down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.012)


def _post_mouse(kind, x: float, y: float, button, click_state: int = 1) -> None:
    event = Quartz.CGEventCreateMouseEvent(_SRC, kind, Quartz.CGPoint(x, y), button)
    if click_state > 1:
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, click_state)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def mouse_click(x: float, y: float, button: str = "left", clicks: int = 1) -> None:
    right = button == "right"
    down = Quartz.kCGEventRightMouseDown if right else Quartz.kCGEventLeftMouseDown
    up = Quartz.kCGEventRightMouseUp if right else Quartz.kCGEventLeftMouseUp
    btn = Quartz.kCGMouseButtonRight if right else Quartz.kCGMouseButtonLeft
    Quartz.CGWarpMouseCursorPosition(Quartz.CGPoint(x, y))
    _post_mouse(Quartz.kCGEventMouseMoved, x, y, btn)
    time.sleep(0.02)
    for n in range(1, max(1, min(int(clicks), 3)) + 1):
        _post_mouse(down, x, y, btn, n)
        time.sleep(0.02)
        _post_mouse(up, x, y, btn, n)
        time.sleep(0.04)


def mouse_scroll(x: float, y: float, dx: int, dy: int) -> None:
    Quartz.CGWarpMouseCursorPosition(Quartz.CGPoint(x, y))
    time.sleep(0.02)
    event = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitPixel, 2, int(dy), int(dx)
    )
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def mouse_drag(x1: float, y1: float, x2: float, y2: float) -> None:
    btn = Quartz.kCGMouseButtonLeft
    Quartz.CGWarpMouseCursorPosition(Quartz.CGPoint(x1, y1))
    _post_mouse(Quartz.kCGEventMouseMoved, x1, y1, btn)
    time.sleep(0.03)
    _post_mouse(Quartz.kCGEventLeftMouseDown, x1, y1, btn)
    time.sleep(0.05)
    steps = 12
    for i in range(1, steps + 1):
        t = i / steps
        _post_mouse(Quartz.kCGEventLeftMouseDragged, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, btn)
        time.sleep(0.015)
    _post_mouse(Quartz.kCGEventLeftMouseUp, x2, y2, btn)


# --------------------------------------------------------------------------
# Step execution
# --------------------------------------------------------------------------

def _resolve_point(step: dict, pid: int, ref_key: str, xy_key: str) -> tuple[float, float]:
    """Turn a ref or an explicit xy into live screen coordinates."""
    ref = step.get(ref_key)
    if ref:
        entry = _REFS.get(str(ref))
        if entry is None:
            raise ValueError(f"unknown ref {ref!r} — call see() first")
        if entry["pid"] != pid:
            raise ValueError(f"ref {ref!r} belongs to pid {entry['pid']}, not the target app")
        live = _center(entry["el"]) or entry["center"]
        if live is None:
            raise ValueError(f"ref {ref!r} has no on-screen position (element gone?)")
        return live
    xy = step.get(xy_key)
    if isinstance(xy, (list, tuple)) and len(xy) == 2:
        return (float(xy[0]), float(xy[1]))
    raise ValueError(f"step needs {ref_key!r} or {xy_key!r}")


def execute_step(step: dict, pid: int) -> str:
    """Run one step. Raises ValueError on anything malformed."""
    kind = str(step.get("kind") or step.get("action") or "").strip().lower()

    if kind == "click":
        button = str(step.get("button", "left")).lower()
        clicks = int(step.get("clicks", 1))
        ref = step.get("ref")
        # AX first: a press on the resolved element beats guessing pixels.
        if ref and button == "left" and clicks == 1:
            entry = _REFS.get(str(ref))
            if entry is None:
                raise ValueError(f"unknown ref {ref!r} — call see() first")
            if entry["pid"] != pid:
                raise ValueError(f"ref {ref!r} belongs to pid {entry['pid']}, not the target app")
            if AS.AXUIElementPerformAction(entry["el"], AS.kAXPressAction) == AS.kAXErrorSuccess:
                return f"AXPress {ref}"
        x, y = _resolve_point(step, pid, "ref", "xy")
        mouse_click(x, y, button, clicks)
        return f"{button} click x{clicks} at ({x:.0f},{y:.0f})"

    if kind == "type":
        text = step.get("text")
        if not isinstance(text, str):
            raise ValueError("type step needs a string 'text'")
        type_text(text)
        if step.get("submit"):
            time.sleep(0.05)
            press_combo("return")
        return f"typed {len(text)} chars"

    if kind == "key":
        combo = step.get("combo")
        if not combo:
            raise ValueError("key step needs 'combo'")
        press_combo(combo)
        return f"key {combo}"

    if kind == "scroll":
        x, y = _resolve_point(step, pid, "ref", "xy")
        mouse_scroll(x, y, int(step.get("dx", 0)), int(step.get("dy", 0)))
        return f"scroll dx={step.get('dx', 0)} dy={step.get('dy', 0)}"

    if kind == "drag":
        x1, y1 = _resolve_point(step, pid, "from_ref", "from_xy")
        x2, y2 = _resolve_point(step, pid, "to_ref", "to_xy")
        mouse_drag(x1, y1, x2, y2)
        return f"drag ({x1:.0f},{y1:.0f})->({x2:.0f},{y2:.0f})"

    if kind == "wait":
        ms = max(0, min(int(step.get("ms", 0)), 10000))
        time.sleep(ms / 1000.0)
        return f"wait {ms}ms"

    raise ValueError(f"unknown step kind {kind!r}")


def _summarize(steps: list) -> str:
    """Audit summary. Typed text is counted, never recorded — it may be a secret."""
    parts = []
    for step in steps or []:
        kind = str(step.get("kind") or step.get("action") or "?").lower()
        if kind == "type":
            parts.append(f"type({len(str(step.get('text', '')))} chars)")
        elif kind == "key":
            parts.append(f"key({step.get('combo')})")
        elif kind == "click":
            parts.append(f"click({step.get('ref') or step.get('xy')})")
        else:
            parts.append(kind)
    return ", ".join(parts) or "(no steps)"


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

mcp = FastMCP("mac-commander")


@mcp.tool()
def see(app: str | None = None, all: bool = False, vision: bool = False,
        max_elements: int = 150) -> Any:
    """Accessibility snapshot of one app (default: the frontmost one).

    Returns the frontmost app, the target app's windows, and its interactive
    elements with stable refs ("e17") usable by act(). Scoped to one app unless
    all=True. vision=True also captures a screenshot (Screen Recording
    permission required).
    """
    notes: list[str] = []
    if not AS.AXIsProcessTrusted():
        notes.append("Accessibility permission is not granted to this process; "
                     "the element tree will be empty. Grant it in System Settings "
                     "> Privacy & Security > Accessibility.")
    budget = {"max": max(1, min(int(max_elements), 1000)), "nodes": 0}
    front = frontmost()

    targets, scope = [], ""
    if all:
        scope = "desktop"
        targets = [a for a in _running_apps()
                   if a.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular]
    else:
        running = find_running(app) if app else _WS.frontmostApplication()
        if running is None:
            audit("see", app, "scope=app", "error: not running")
            return {"error": f"no running app matches {app!r}", "frontmost": front}
        scope = str(running.localizedName() or app or "")
        targets = [running]

    apps = [_snapshot(t, budget) for t in targets]
    total = sum(len(a["elements"]) for a in apps)
    if total >= budget["max"]:
        notes.append(f"element list truncated at max_elements={budget['max']}")

    payload = {
        "frontmost": front,
        "scope": scope,
        "apps": apps,
        "elements_returned": total,
        "notes": notes,
    }

    if not vision:
        audit("see", scope, f"all={all} vision=False", f"ok: {total} elements")
        return payload

    shot = _screenshot(targets[0] if len(targets) == 1 else None)
    if shot is None:
        notes.append("Screen Recording permission is missing, so no image was "
                     "captured. Grant it in System Settings > Privacy & Security "
                     "> Screen Recording. The element tree above is unaffected.")
        audit("see", scope, f"all={all} vision=True", "ok (no screen recording)")
        return payload
    audit("see", scope, f"all={all} vision=True", f"ok: {total} elements + image")
    return [json.dumps(payload, ensure_ascii=False), Image(path=shot)]


def _screenshot(running) -> str | None:
    """Capture one app's window if we can identify it, else the whole screen."""
    if not Quartz.CGPreflightScreenCaptureAccess():
        return None
    path = os.path.join(tempfile.mkdtemp(prefix="mac-commander-"), "shot.png")
    argv = ["/usr/sbin/screencapture", "-x", "-o"]
    if running is not None:
        pid = int(running.processIdentifier())
        for win in Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
            if win.get(Quartz.kCGWindowOwnerPID) == pid and win.get(Quartz.kCGWindowLayer) == 0:
                argv += ["-l", str(win.get(Quartz.kCGWindowNumber))]
                break
    try:
        subprocess.run(argv + [path], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if os.path.exists(path) and os.path.getsize(path) > 0 else None


@mcp.tool()
def act(target: str, steps: list[dict], settle_ms: int = 150) -> dict:
    """Run a batch of input steps against one app, atomically and focus-guarded.

    Every step verifies `target` is frontmost immediately before firing and
    again immediately after. If anything steals focus mid-batch the remaining
    steps are abandoned and the result names the step and the thief.

    Step kinds (each a dict with "kind"):
      click  {ref|xy, button:"left"|"right", clicks:1|2}
      type   {text, submit:false}
      key    {combo}   e.g. "cmd+a", "cmd+shift+4"
      scroll {ref|xy, dx, dy}
      drag   {from_ref|from_xy, to_ref|to_xy}
      wait   {ms}      capped at 10000
    """
    steps = list(steps or [])
    summary = _summarize(steps)
    settle = max(0, min(int(settle_ms), 5000)) / 1000.0

    def fail(index: int, reason: str, completed: int, front: dict | None = None) -> dict:
        result = {
            "ok": False,
            "steps_completed": completed,
            "failed_step": {"index": index, "reason": reason,
                            "frontmost": front if front is not None else frontmost()},
        }
        audit("act", target, summary, f"failed at step {index}: {reason}")
        return result

    blocked = blocklist_hit(target)
    if blocked:
        return fail(-1, f"refused: target matches input_blocklist entry {blocked!r}", 0)

    running = find_running(target)
    if running is None:
        return fail(-1, f"no running app matches {target!r} — launch it with app() first", 0)

    name, bundle = str(running.localizedName() or ""), str(running.bundleIdentifier() or "")
    blocked = blocklist_hit(name, bundle)
    if blocked:
        return fail(-1, f"refused: {name} matches input_blocklist entry {blocked!r}", 0)

    pid = int(running.processIdentifier())
    running.activateWithOptions_(AppKit.NSApplicationActivateAllWindows)
    thief = wait_frontmost(pid, 3.0)
    if thief is not None:
        return fail(-1, f"{name} did not come to the front within 3s", 0, thief)

    completed = 0
    for index, step in enumerate(steps):
        front = frontmost()
        if front["pid"] != pid:
            return fail(index, f"focus lost before step: expected {name}, "
                               f"{front['name']} is frontmost", completed, front)
        try:
            execute_step(step, pid)
        except Exception as exc:  # malformed step or a failed AX/CGEvent call
            return fail(index, f"{type(exc).__name__}: {exc}", completed)
        completed += 1
        time.sleep(settle)
        front = frontmost()
        if front["pid"] != pid:
            return fail(index, f"focus lost after step executed: expected {name}, "
                               f"{front['name']} is frontmost — this step's effect "
                               f"may have landed elsewhere", completed, front)

    audit("act", target, summary, f"ok: {completed} steps")
    return {"ok": True, "steps_completed": completed, "target": {"name": name, "pid": pid}}


def _app_url(name: str):
    """Locate an app bundle by bundle id or by name in the usual places."""
    if "." in name:
        url = _WS.URLForApplicationWithBundleIdentifier_(name)
        if url is not None:
            return url
    for folder in ("/Applications", "/Applications/Utilities", "/System/Applications",
                   "/System/Applications/Utilities", str(Path.home() / "Applications")):
        candidate = Path(folder) / f"{name}.app"
        if candidate.exists():
            return NSURL.fileURLWithPath_(str(candidate))
    running = find_running(name)
    return running.bundleURL() if running is not None else None


def _place_window(pid: int, window: dict) -> str:
    app_el = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app_el, 2.0)
    windows = _attr(app_el, AS.kAXWindowsAttribute) or []
    if not windows:
        return "no windows to place"
    win, done = windows[0], []
    move, size = window.get("move"), window.get("size")
    if move and len(move) == 2:
        value = AS.AXValueCreate(AS.kAXValueTypeCGPoint, Quartz.CGPoint(float(move[0]), float(move[1])))
        ok = AS.AXUIElementSetAttributeValue(win, AS.kAXPositionAttribute, value)
        done.append(f"move={'ok' if ok == AS.kAXErrorSuccess else f'AXError {ok}'}")
    if size and len(size) == 2:
        value = AS.AXValueCreate(AS.kAXValueTypeCGSize, Quartz.CGSize(float(size[0]), float(size[1])))
        ok = AS.AXUIElementSetAttributeValue(win, AS.kAXSizeAttribute, value)
        done.append(f"size={'ok' if ok == AS.kAXErrorSuccess else f'AXError {ok}'}")
    return ", ".join(done) or "nothing to place"


@mcp.tool()
def app(action: str, name: str, window: dict | None = None) -> dict:
    """Manage an application: launch | quit | switch | hide.

    `name` is an app name or bundle id. quit is a graceful terminate — this
    server never force-kills. `window` optionally repositions the app's front
    window: {"move": [x, y], "size": [w, h]}.
    """
    action = str(action).strip().lower()
    if action not in ("launch", "quit", "switch", "hide"):
        return {"ok": False, "error": f"unknown action {action!r}; "
                                      "use launch, quit, switch or hide"}

    if action == "launch":
        url = _app_url(name)
        if url is None:
            audit("app", name, action, "error: bundle not found")
            return {"ok": False, "error": f"no application bundle found for {name!r}"}
        config = AppKit.NSWorkspaceOpenConfiguration.configuration()
        config.setActivates_(True)
        _WS.openApplicationAtURL_configuration_completionHandler_(url, config, None)
        deadline = time.monotonic() + 15.0
        running = None
        while time.monotonic() < deadline:
            running = find_running(name)
            if running is not None and running.isFinishedLaunching():
                break
            time.sleep(0.15)
        if running is None:
            audit("app", name, action, "error: did not start")
            return {"ok": False, "error": f"{name} did not start within 15s"}
    else:
        running = find_running(name)
        if running is None:
            audit("app", name, action, "error: not running")
            return {"ok": False, "error": f"no running app matches {name!r}"}

    result: dict = {"ok": True, "action": action,
                    "app": {"name": str(running.localizedName() or ""),
                            "bundle_id": str(running.bundleIdentifier() or ""),
                            "pid": int(running.processIdentifier())}}
    if action == "quit":
        result["ok"] = bool(running.terminate())  # graceful only, never forceTerminate
        if not result["ok"]:
            result["error"] = "terminate request was refused (unsaved changes?)"
    elif action == "switch":
        running.activateWithOptions_(AppKit.NSApplicationActivateAllWindows)
        thief = wait_frontmost(int(running.processIdentifier()), 3.0)
        if thief is not None:
            result["ok"] = False
            result["error"] = f"did not come to the front within 3s; {thief['name']} has focus"
    elif action == "hide":
        result["ok"] = bool(running.hide())

    if window and action != "quit":
        result["window"] = _place_window(int(running.processIdentifier()), window)

    audit("app", name, action, "ok" if result["ok"] else f"error: {result.get('error')}")
    return result


@mcp.tool()
def notify(message: str, title: str = "Mac-Commander", subtitle: str | None = None,
           sound: str | None = None) -> dict:
    """Post a macOS notification banner. Handles any Unicode — quotes, em
    dashes, emoji — because nothing is interpolated into the script."""
    result = osa(NOTIFY_SCRIPT, [message, title, subtitle or "", sound or ""], timeout=15)
    ok = result["exit_code"] == 0
    audit("notify", title, message[:120], "ok" if ok else f"error: {result['stderr'][:200]}")
    return {"ok": ok, **result}


@mcp.tool()
def applescript(script: str, args: list[str] | None = None, timeout: int = 30) -> dict:
    """Run an AppleScript. The script is passed through untouched on stdin;
    `args` arrive as argv, so write it with an `on run argv` handler and read
    values from there. Never build a script by concatenating user data."""
    args = [str(a) for a in (args or [])]
    result = osa(script, args, timeout=timeout)
    first_line = script.strip().splitlines()[0][:80] if script.strip() else "(empty)"
    audit("applescript", first_line, f"{len(args)} args",
          "ok" if result["exit_code"] == 0 else f"exit {result['exit_code']}: {result['stderr'][:200]}")
    return result


if __name__ == "__main__":
    mcp.run()
