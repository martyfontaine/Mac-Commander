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

import functools
import hashlib
import json
import os
import shutil
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
    # Display names AND bundle ids. Display names are localized — on a French
    # Mac "System Settings" is "Réglages Système" — and that app's bundle id
    # (com.apple.systempreferences) shares no substring with its English name,
    # so both forms are needed for the guard to hold outside English.
    "input_blocklist": [
        "1Password", "com.1password.",
        "Passwords", "com.apple.Passwords",
        "System Settings", "com.apple.systempreferences",
        "Keychain Access", "com.apple.keychainaccess",
    ],
    "audit_log": "audit.jsonl",
    # Model-authored AppleScript is refused by default: it reaches the shell via
    # `do shell script` and is bound by neither the blocklist nor the focus
    # guard. Scripts in scripts/ are written by the user and run by name.
    "allow_raw_applescript": False,
    # The halo: for exactly as long as a tool call is executing, overlay.py
    # shows a rose-gold glow around every screen and "Claude has the con" at
    # the bottom centre — lit at call start, dropped the moment the call
    # returns. Cosmetic and click-through — it can never intercept input or
    # break a tool call.
    "overlay": True,
}


def _load_config() -> dict:
    """Read config.json beside this file, creating it with defaults if absent."""
    path = ROOT / "config.json"
    if not path.exists():
        try:
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:  # read-only install dir must not stop the server
            print(f"mac-commander: could not create config.json ({exc}); using defaults",
                  file=sys.stderr)
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

OSASCRIPT = "/usr/bin/osascript"
MAX_OSA_TIMEOUT = 300


def osa(script: str, args: list[str], timeout: int = 30) -> dict:
    """Run `osascript - <args>` with `script` piped in verbatim.

    The spec calls for `osascript /dev/stdin <args>`; on macOS 26 osascript
    cannot read a device file ("I/O error (bummers)") because it seeks the
    program. `osascript -` is the same contract that actually works: program on
    stdin, all dynamic values as argv. Verified byte-exact for quotes, em
    dashes and emoji.

    The binary is named by absolute path: PATH is inherited from whatever
    launched the server, so resolving `osascript` by name would let an earlier
    writable PATH entry substitute it. The timeout is bounded on both ends —
    this server is single-threaded, so an unbounded osascript blocks every tool.
    """
    argv = [OSASCRIPT, "-", *[str(a) for a in args]]
    effective = max(1, min(int(timeout), MAX_OSA_TIMEOUT))
    try:
        proc = subprocess.run(
            argv,
            input=script.encode("utf-8"),
            capture_output=True,
            timeout=effective,
        )
    except subprocess.TimeoutExpired:
        # Report what was actually waited, not what was asked for — they differ
        # whenever the caller's timeout was above the cap.
        return {"exit_code": -1, "stdout": "", "stderr": f"timed out after {effective}s"}
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


MAX_REFS = 20000


def _new_ref(el, pid: int, app_key: str, center) -> str:
    global _ref_seq
    _ref_seq += 1
    ref = f"e{_ref_seq}"
    _REFS[ref] = {"el": el, "pid": pid, "app": app_key, "center": center}
    # Invalidation only happens when an app returns under a new pid, so without
    # this a long-lived server pins an AXUIElement per element, per see(), for
    # the life of the process. Oldest first: dicts keep insertion order.
    while len(_REFS) > MAX_REFS:
        _REFS.pop(next(iter(_REFS)))
    return ref


def _invalidate(app_key: str, pid: int) -> None:
    """Drop cached refs for an app that has been relaunched under a new pid."""
    if _APP_PIDS.get(app_key) not in (None, pid):
        for ref in [r for r, v in _REFS.items() if v["app"] == app_key]:
            del _REFS[ref]
    _APP_PIDS[app_key] = pid


def _collect(el, pid: int, app_key: str, out: list, budget: dict, depth: int = 0) -> None:
    """Depth-first walk gathering interactive elements, bounded on every axis.

    budget["count"] spans the whole snapshot, not one app: max_elements used to
    be applied per app, so see(all=True) could return max_elements times the
    number of running apps.
    """
    if depth > MAX_DEPTH or budget["count"] >= budget["max"] or budget["nodes"] >= MAX_NODES:
        budget["truncated"] = budget["truncated"] or budget["count"] >= budget["max"]
        return
    children = _attr(el, AS.kAXChildrenAttribute) or []
    for child in children:
        if budget["count"] >= budget["max"] or budget["nodes"] >= MAX_NODES:
            budget["truncated"] = budget["truncated"] or budget["count"] >= budget["max"]
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
            budget["count"] += 1
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


def _pump() -> None:
    """NSWorkspace's app list, frontmostApplication and isTerminated are all
    KVO-updated. In a process with no run loop of its own they go stale — an
    app launched a second ago stays invisible — so pump before every read."""
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.02, False)


def frontmost() -> dict:
    """Who owns the keyboard right now."""
    _pump()
    app = _WS.frontmostApplication()
    if app is None:
        return {"name": None, "bundle_id": None, "pid": None}
    return {
        "name": str(app.localizedName() or ""),
        "bundle_id": str(app.bundleIdentifier() or ""),
        "pid": int(app.processIdentifier()),
    }


def _running_apps() -> list:
    _pump()
    return list(_WS.runningApplications() or [])


def find_running(name: str):
    """Resolve a name or bundle id to a running app: exact, then case-insensitive,
    then prefix. Regular (Dock-visible) apps win over background ones."""
    if not name:
        return None
    needle = name.strip().lower()
    ranked = sorted(
        [a for a in _running_apps() if not a.isTerminated()],
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
            # Pin the flags to none. Events inherit the session's flag state, so
            # a modifier left asserted — by a combo, or by the user physically
            # holding one — turns this text into a shortcut and it is dropped.
            # press_combo releases its own flags; this covers every other source.
            Quartz.CGEventSetFlags(event, 0)
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
            # A trailing plus ("cmd+") means the + key itself. "cmd++" splits to
            # two empty parts and still raises below — write "cmd+shift+=" for
            # that, which is what + is on a US layout.
            key = "="
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
    if flags:
        # The key-up above carries the modifier flags too, which leaves the
        # modifier logically held in the session's flag state. Unicode text
        # posted afterwards is then read as a shortcut and swallowed — a cmd+c
        # at the end of one act() batch silently eats the typing in the next.
        # Physical key presses reset this, which is why it only bites unattended
        # runs. Post one flags-cleared event to release it.
        release = Quartz.CGEventCreateKeyboardEvent(_SRC, 0, False)
        Quartz.CGEventSetFlags(release, 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, release)
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

def _window_rects(pid: int) -> list[tuple[float, float, float, float]]:
    """(x, y, w, h) for each of the pid's accessibility windows."""
    app_el = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app_el, 2.0)
    rects = []
    for win in _attr(app_el, AS.kAXWindowsAttribute) or []:
        pos, size = _geometry(win)
        if pos and size:
            rects.append((pos[0], pos[1], size[0], size[1]))
    return rects


def _check_inside(x: float, y: float, pid: int, what: str) -> None:
    """Refuse a point that is not over one of the target app's windows.

    Mouse events post at the HID tap, so they land wherever the pointer is, not
    in the process the focus guard verified. Unchecked, an explicit xy walked
    straight past the blocklist: aim at a password manager's window while a
    permitted app holds focus, and the after-check notices too late.
    """
    rects = _window_rects(pid)
    if not rects:
        # Fail closed. This used to return, leaving the check inert for any app
        # exposing no AX windows — TextEdit with no document open, a menu-bar
        # extra — which is exactly a target an injected caller could pick to get
        # an unchecked click. With no windows there is no point that IS inside
        # the target, so refusing is also the semantically correct answer; use a
        # ref from see() to drive a windowless app.
        raise ValueError(
            f"{what} ({x:.0f},{y:.0f}) cannot be verified: the target app exposes no "
            "accessibility windows to bound it against — refused. Use a ref from see() "
            "instead of raw coordinates."
        )
    if any(rx <= x <= rx + rw and ry <= y <= ry + rh for rx, ry, rw, rh in rects):
        return
    raise ValueError(
        f"{what} ({x:.0f},{y:.0f}) is outside every window of the target app — "
        "refused: an event there would land in whatever app owns that point"
    )


def _resolve_point(step: dict, pid: int, ref_key: str, xy_key: str) -> tuple[float, float]:
    """Turn a ref or an explicit xy into live screen coordinates."""
    ref = step.get(ref_key)
    if ref:
        entry = _REFS.get(str(ref))
        if entry is None:
            raise ValueError(f"unknown ref {ref!r} — call see() first")
        if entry["pid"] != pid:
            raise ValueError(f"ref {ref!r} belongs to pid {entry['pid']}, not the target app")
        # No fallback to entry["center"]: a ref whose element has gone means the
        # UI moved, and the cached point may now sit over something else.
        live = _center(entry["el"])
        if live is None:
            raise ValueError(f"ref {ref!r} no longer has an on-screen position "
                             "(the element is gone or moved) — call see() again")
        return live
    xy = step.get(xy_key)
    if isinstance(xy, (list, tuple)) and len(xy) == 2:
        x, y = float(xy[0]), float(xy[1])
        _check_inside(x, y, pid, f"{xy_key}")
        return (x, y)
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


SCRIPTS_DIR = ROOT / "scripts"


def _script_catalogue() -> list[str]:
    """Names of the user-authored scripts that applescript() will run."""
    if not SCRIPTS_DIR.is_dir():
        return []
    return sorted(p.stem for p in SCRIPTS_DIR.glob("*.applescript"))


def _named_script(name: str) -> tuple[str | None, str | None]:
    """Read scripts/<name>.applescript. Returns (text, error).

    The name indexes a directory, so it is a bare stem: separators, "..", "~"
    and absolute forms are refused before any path is built, for the same
    reason _app_url() refuses them.
    """
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("~"):
        return None, f"invalid script name {name!r}: use a bare name from the catalogue"
    path = SCRIPTS_DIR / f"{name}.applescript"
    try:
        if not path.is_file():
            return None, (f"no script named {name!r}. Available: "
                          f"{', '.join(_script_catalogue()) or '(none)'}")
        return path.read_text(encoding="utf-8"), None
    except (OSError, ValueError) as exc:  # is_file() raises too, e.g. on a NUL byte
        return None, f"could not read script {name!r}: {exc}"


def _script_fingerprint(script: str) -> str:
    """Identify an AppleScript in the audit log without transcribing it.

    Logging the first line recorded nothing, since every script opens with the
    documented `on run argv`; logging the body would write whatever it embeds
    into an append-only file. A hash, a size and the first real body line let
    two calls be told apart and a known script be matched later.
    """
    text = script or ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    lines = [ln.strip() for ln in text.splitlines()]
    body = next((ln for ln in lines
                 if ln and not ln.startswith("on run") and not ln.startswith("--")), "")
    return f"sha256:{digest} {len(text)}c/{len(lines)}L {body[:60]}"


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
# Overlay — the on-screen "Claude has the con" halo
# --------------------------------------------------------------------------

_overlay_proc: subprocess.Popen | None = None
_overlay_spawns = 0
MAX_OVERLAY_SPAWNS = 5  # a helper that keeps dying at startup stops being retried


def overlay_ping() -> None:
    """Light the halo: tell overlay.py the desktop is being driven right now.

    The overlay lives in its own process because this server has no run loop
    between tool calls — a window shown here could never fade itself out after
    the last call — and because keeping AppKit windows out of this process
    keeps the input path small and auditable. The helper hides itself after a
    quiet period and exits on stdin EOF, so it cannot outlive the server.

    Cosmetic only, so it must never break a tool call: every failure is
    swallowed. A helper that died is respawned on the next ping, a bounded
    number of times.
    """
    global _overlay_proc, _overlay_spawns
    if not CONFIG.get("overlay", True):
        return
    try:
        if _overlay_proc is not None and _overlay_proc.poll() is not None:
            _overlay_proc = None  # helper exited; reap it and start fresh
        if _overlay_proc is None:
            if _overlay_spawns >= MAX_OVERLAY_SPAWNS:
                return
            _overlay_spawns += 1
            _overlay_proc = subprocess.Popen(
                [sys.executable, str(ROOT / "overlay.py")],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        _overlay_proc.stdin.write(b"ping\n")
        _overlay_proc.stdin.flush()
    except OSError as exc:
        if _overlay_proc is None:  # Popen itself failed: overlay.py missing or unrunnable
            _overlay_spawns = MAX_OVERLAY_SPAWNS
            print(f"mac-commander: overlay disabled: {exc}", file=sys.stderr)
        else:  # the pipe broke mid-write; drop the handle and respawn next ping
            _overlay_proc = None


def overlay_hide() -> None:
    """Drop the halo: the tool call is over. Never spawns — a helper that is
    not already up has nothing to hide."""
    global _overlay_proc
    if _overlay_proc is None or _overlay_proc.poll() is not None:
        return
    try:
        _overlay_proc.stdin.write(b"hide\n")
        _overlay_proc.stdin.flush()
    except OSError:
        _overlay_proc = None  # broken pipe; the next ping respawns


def lit(fn):
    """Run a tool with the halo lit for exactly the duration of the call.

    Applied under @mcp.tool(), so FastMCP registers the wrapper;
    functools.wraps preserves the signature and docstring it reads for the
    tool schema. The finally guarantees the halo drops on every exit path,
    including exceptions — a lit halo with no call running would be a lie.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        overlay_ping()
        try:
            return fn(*args, **kwargs)
        finally:
            overlay_hide()
    return wrapper


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

mcp = FastMCP(
    "mac-commander",
    instructions=(
        "Controls the macOS desktop GUI: look at what is on screen, click "
        "buttons, type text, press keyboard shortcuts, manage app windows, "
        "post notifications and run AppleScript. Use it to automate any Mac "
        "application that has no API. The usual sequence is see() to find "
        "elements, then act() to drive them.\n\n"
        "Scope, stated accurately: see(), act(), app() and notify() do not read "
        "or write files and do not run shell commands, and act() additionally "
        "refuses blocklisted apps and verifies focus around every step. "
        "applescript() runs scripts the user wrote and keeps in scripts/, "
        "chosen by name — call it with no arguments to see the catalogue. "
        "Those scripts are unrestricted once running (AppleScript reaches the "
        "shell and any app), so they are the user's to author: you choose one "
        "and supply argv, you do not write the code. Supplying raw script text "
        "is refused unless the user has explicitly enabled it in config.json."
    ),
)


@mcp.tool()
@lit
def see(app: str | None = None, all: bool = False, vision: bool = False,
        max_elements: int = 150) -> Any:
    """Look at what is on screen in a macOS app — read its windows, buttons,
    text fields, menus and links through the Accessibility API.

    Call this before act() to find things to click or type into: it returns
    stable refs ("e17") that act() accepts. Also use it to check whether a
    dialog opened, what a window is titled, or whether a button is enabled.

    Scoped to one app — the frontmost one unless you name another — because a
    whole-desktop dump leaks every window title into the transcript. all=True
    is the explicit opt-in. vision=True adds a screenshot of the app's window
    (needs Screen Recording; without it you still get the tree plus a note).
    """
    notes: list[str] = []
    if not AS.AXIsProcessTrusted():
        notes.append("Accessibility permission is not granted to this process; "
                     "the element tree will be empty. Grant it in System Settings "
                     "> Privacy & Security > Accessibility.")
    budget = {"max": max(1, min(int(max_elements), 1000)), "nodes": 0,
              "count": 0, "truncated": False}
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
    if budget["truncated"]:
        notes.append(f"element list truncated at max_elements={budget['max']} "
                     f"(shared across all apps in this snapshot)")

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

    # A screenshot of a blocklisted app is a bitmap of a password manager. The
    # element tree is left alone — the key is named input_blocklist and reading
    # a locked vault is legitimate — but writing its pixels out is not.
    #
    # Checked against EVERY target, not just a single-app scope: an unscoped
    # capture is a picture of the whole desktop, so a blocklisted app that is
    # merely on screen ends up in it. Scoping the request does not narrow the
    # pixels when there is no window filter to apply.
    single = targets[0] if len(targets) == 1 else None
    for target in targets:
        blocked = blocklist_hit(target.localizedName(), target.bundleIdentifier())
        # For an unscoped capture, only an app actually showing a window can
        # land in the bitmap — refusing merely because it is running would mean
        # no desktop capture ever succeeds while a password manager sits idle.
        if blocked and (target is single or _window_id(target) is not None):
            notes.append(f"no screenshot: {target.localizedName()} matches "
                         f"input_blocklist entry {blocked!r}. The element tree is unaffected.")
            audit("see", scope, f"all={all} vision=True", f"ok: {total} elements, image refused")
            return payload

    png, err = _screenshot(single)
    if png is None:
        notes.append(err or "no image was captured")
        audit("see", scope, f"all={all} vision=True", f"ok: {total} elements, no image")
        return payload
    audit("see", scope, f"all={all} vision=True", f"ok: {total} elements + image")
    return [json.dumps(payload, ensure_ascii=False), Image(data=png, format="png")]


def _window_id(running) -> int | None:
    """The target app's frontmost normal-layer on-screen window, if it has one."""
    pid = int(running.processIdentifier())
    for win in Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []:
        if win.get(Quartz.kCGWindowOwnerPID) == pid and win.get(Quartz.kCGWindowLayer) == 0:
            return int(win.get(Quartz.kCGWindowNumber))
    return None


def _screenshot(running) -> tuple[bytes | None, str | None]:
    """Capture a window, or the whole screen only when explicitly unscoped.

    Returns (png_bytes, error). Bytes are read and the temp file removed before
    returning, so no capture of the user's screen is left on disk. An app-scoped
    request whose window cannot be found returns an error rather than falling
    back to a full-screen grab — that fallback fires for any hidden or
    windowless app and hands back every other visible window.
    """
    if not Quartz.CGPreflightScreenCaptureAccess():
        return None, ("Screen Recording permission is missing, so no image was captured. "
                      "Grant it in System Settings > Privacy & Security > Screen "
                      "Recording. The element tree above is unaffected.")
    argv = ["/usr/sbin/screencapture", "-x", "-o"]
    if running is not None:
        win_id = _window_id(running)
        if win_id is None:
            name = str(running.localizedName() or "the target app")
            return None, (f"{name} has no identifiable on-screen window, so no image was "
                          "captured. A whole-screen capture was NOT substituted — it would "
                          "have returned every other visible window. Unhide the app or open "
                          "a window, then ask again.")
        argv += ["-l", str(win_id)]
    tmpdir = tempfile.mkdtemp(prefix="mac-commander-")
    path = os.path.join(tmpdir, "shot.png")
    try:
        proc = subprocess.run(argv + [path], capture_output=True, timeout=20)
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            detail = proc.stderr.decode("utf-8", "replace").strip()[:160]
            return None, f"screencapture produced no image{': ' + detail if detail else ''}"
        with open(path, "rb") as fh:
            return fh.read(), None
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"screencapture failed: {type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@mcp.tool()
@lit
def act(target: str, steps: list[dict], settle_ms: int = 150) -> dict:
    """Control a macOS app — click buttons, type text, press keyboard
    shortcuts, scroll and drag — as one atomic, focus-guarded batch.

    This is the tool for driving any Mac GUI: filling in a form, pressing
    cmd+s, selecting and copying, choosing a menu item, dragging a file.
    Call see() first to get refs for the things you want to hit.

    Every step verifies `target` is frontmost immediately before firing and
    again immediately after. Focus is proven at those two boundaries, not
    continuously: one step can post many events (a long type is chunked, a drag
    is 15 events over ~300ms), so a steal mid-step is detected by the after
    check rather than prevented — the batch then stops and the result says the
    step's effect may have landed elsewhere. Remaining steps are abandoned and
    the result names the step and the thief.

    Explicit `xy` points are refused unless they fall inside one of the target
    app's windows, since a mouse event goes wherever the pointer is.

    Step kinds (each a dict with "kind"):
      click  {ref|xy, button:"left"|"right", clicks:1|2}
      type   {text, submit:false}    full Unicode: é — " ' 🙂 all survive
      key    {combo}   e.g. "cmd+a", "cmd+shift+4", "return", "escape"
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


APP_FOLDERS = ("/Applications", "/Applications/Utilities", "/System/Applications",
               "/System/Applications/Utilities", str(Path.home() / "Applications"))


def _app_url(name: str):
    """Locate an app bundle by bundle id or by name in the usual places.

    `name` is a bare application name, never a path. Separators are rejected
    before a path is built: `Path("/Applications") / "/tmp/Evil.app"` discards
    the left operand and yields /tmp/Evil.app, and "../" escapes the same way,
    so unchecked this launches any bundle on disk, not just the five folders.
    """
    if "/" in name or ".." in name or name.startswith("~"):
        return None
    if "." in name:
        url = _WS.URLForApplicationWithBundleIdentifier_(name)
        if url is not None:
            return url
    for folder in APP_FOLDERS:
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
@lit
def app(action: str, name: str, window: dict | None = None) -> dict:
    """Launch, quit, switch to or hide a macOS application, and move or resize
    its window.

    action is "launch" | "quit" | "switch" | "hide". `name` is an app name
    ("Safari") or a bundle id ("com.apple.Safari"). Use "switch" to bring an
    app to the front before act(), and "launch" if it is not running yet.

    quit is a graceful terminate that waits for the process to actually exit —
    it never force-kills, and says so if a save dialog is holding it open.
    `window` optionally places the front window: {"move": [x, y],
    "size": [w, h]}.
    """
    action = str(action).strip().lower()
    if action not in ("launch", "quit", "switch", "hide"):
        error = f"unknown action {action!r}; use launch, quit, switch or hide"
        audit("app", name, action, f"error: {error}")  # every call gets a line
        return {"ok": False, "error": error}

    if action == "launch":
        url = _app_url(name)
        if url is None:
            audit("app", name, action, "error: bundle not found")
            return {"ok": False, "error": f"no application bundle found for {name!r}"}
        config = AppKit.NSWorkspaceOpenConfiguration.configuration()
        config.setActivates_(True)
        _WS.openApplicationAtURL_configuration_completionHandler_(url, config, None)
        deadline = time.monotonic() + 15.0
        running, launched = None, False
        while time.monotonic() < deadline:
            running = find_running(name)
            if running is not None and running.isFinishedLaunching():
                launched = True
                break
            time.sleep(0.15)
        if running is None:
            audit("app", name, action, "error: did not start")
            return {"ok": False, "error": f"{name} did not start within 15s"}
        if not launched:
            # The process exists but never reported finished launching. Saying
            # ok here would tell the caller it is safe to drive the app.
            audit("app", name, action, "error: did not finish launching")
            return {"ok": False,
                    "error": f"{name} started but had not finished launching after 15s",
                    "app": {"name": str(running.localizedName() or ""),
                            "bundle_id": str(running.bundleIdentifier() or ""),
                            "pid": int(running.processIdentifier())}}
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
            result["error"] = "terminate request was refused"
        else:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not running.isTerminated():
                _pump()
                time.sleep(0.1)
            result["terminated"] = bool(running.isTerminated())
            if not result["terminated"]:
                result["ok"] = False
                result["error"] = ("quit was requested but the app is still running — "
                                   "it is probably showing a save or confirm dialog")
    elif action == "switch":
        running.activateWithOptions_(AppKit.NSApplicationActivateAllWindows)
        thief = wait_frontmost(int(running.processIdentifier()), 3.0)
        if thief is not None:
            result["ok"] = False
            result["error"] = f"did not come to the front within 3s; {thief['name']} has focus"
    elif action == "hide":
        running.hide()  # its BOOL return says NO even on success; trust the state instead
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not running.isHidden():
            _pump()
            time.sleep(0.1)
        result["ok"] = bool(running.isHidden())
        if not result["ok"]:
            result["error"] = "hide was requested but the app is still visible"

    if window and action != "quit":
        try:
            result["window"] = _place_window(int(running.processIdentifier()), window)
        except Exception as exc:  # malformed move/size must not escape the tool
            result["window"] = f"{type(exc).__name__}: {exc}"

    audit("app", name, action, "ok" if result["ok"] else f"error: {result.get('error')}")
    return result


@mcp.tool()
@lit
def notify(message: str, title: str = "Mac-Commander", subtitle: str | None = None,
           sound: str | None = None) -> dict:
    """Show a macOS notification banner to get the user's attention.

    Use it to tell them a long job has finished, that something needs a
    decision, or that a run failed. Any Unicode is safe — quotes, em dashes,
    accents and emoji all pass through untouched.
    """
    result = osa(NOTIFY_SCRIPT, [message, title, subtitle or "", sound or ""], timeout=15)
    ok = result["exit_code"] == 0
    audit("notify", title, message[:120], "ok" if ok else f"error: {result['stderr'][:200]}")
    return {"ok": ok, **result}


@mcp.tool()
@lit
def applescript(name: str | None = None, script: str | None = None,
                args: list[str] | None = None, timeout: int = 30) -> dict:
    """Run one of the user's AppleScripts by name — for anything the other
    tools do not cover: Finder, Mail, Music, Reminders, Calendar, System
    Events, or any app with a scripting dictionary.

    Call with no arguments to get the catalogue of available scripts.

    `name` picks a script the user wrote and reviewed in scripts/; `args` are
    delivered to its `on run argv` handler as argv, so any value is safe —
    quotes, em dashes, accents and emoji all survive untouched.

        applescript()                                    -> list what is available
        applescript(name="clipboard-read")
        applescript(name="clipboard-write", args=["hi"])

    You cannot supply script text yourself unless the user has set
    "allow_raw_applescript": true in config.json. This is deliberate:
    AppleScript reaches the shell through `do shell script`, reads and writes
    files, and is bound by neither the blocklist nor the focus guard, so
    model-authored script text is full user-level access to the machine. If a
    task needs a script that does not exist yet, say so and ask the user to add
    it to scripts/ — do not ask them to enable raw execution.

    Returns stdout, stderr and the exit code.
    """
    args = [str(a) for a in (args or [])]

    if not name and not script:
        catalogue = _script_catalogue()
        audit("applescript", "(catalogue)", "0 args",
              f"ok: listed {len(catalogue)} scripts")  # every call leaves a line
        return {"ok": True, "scripts": catalogue,
                "raw_allowed": bool(CONFIG.get("allow_raw_applescript")),
                "hint": "call applescript(name=..., args=[...]); add new scripts "
                        f"to {SCRIPTS_DIR}"}

    if name:
        text, error = _named_script(str(name))
        if text is None:
            audit("applescript", f"script:{name}", f"{len(args)} args", f"error: {error}")
            return {"ok": False, "error": error, "scripts": _script_catalogue()}
        label = f"script:{name}"
    else:
        if not CONFIG.get("allow_raw_applescript"):
            error = ("raw AppleScript is disabled. Use applescript(name=...) with one "
                     "of the user's reviewed scripts, or ask the user to add a new one "
                     f"to {SCRIPTS_DIR}. Available: "
                     f"{', '.join(_script_catalogue()) or '(none)'}")
            audit("applescript", _script_fingerprint(script or ""),
                  f"{len(args)} args", "refused: raw script text disabled")
            return {"ok": False, "error": error, "scripts": _script_catalogue()}
        text, label = script or "", _script_fingerprint(script or "")

    result = osa(text, args, timeout=timeout)
    audit("applescript", label,
          f"{len(args)} args, {sum(len(a) for a in args)} arg chars",
          "ok" if result["exit_code"] == 0 else f"exit {result['exit_code']}: {result['stderr'][:200]}")
    return {"ok": result["exit_code"] == 0, **result}


if __name__ == "__main__":
    mcp.run()
