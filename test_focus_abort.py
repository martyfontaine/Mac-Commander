#!/usr/bin/env python3
"""Acceptance test 6 — the focus-abort path, with frontmost mocked.

This is the bug the server exists to prevent: the Claude client steals focus
mid-batch and a cmd+a/cmd+c lands on the wrong window. Here we flip frontmost
underneath act() and assert it stops, names the step, and names the thief —
without firing a single real event.

Run: .venv/bin/python -m pytest test_focus_abort.py -v
"""

import pytest

import server

TARGET = {"name": "TextEdit", "bundle_id": "com.apple.TextEdit", "pid": 4242}
THIEF = {"name": "Claude", "bundle_id": "com.anthropic.claudefordesktop", "pid": 99}
STEPS = [
    {"kind": "click", "xy": [100, 100]},
    {"kind": "type", "text": "hello"},
    {"kind": "key", "combo": "cmd+a"},
    {"kind": "key", "combo": "cmd+c"},
]


class FakeRunningApp:
    """Stands in for NSRunningApplication."""

    def __init__(self, front):
        self._front = front
        self.activated = 0

    def processIdentifier(self):
        return self._front["pid"]

    def localizedName(self):
        return self._front["name"]

    def bundleIdentifier(self):
        return self._front["bundle_id"]

    def activateWithOptions_(self, options):
        self.activated += 1


@pytest.fixture
def rig(monkeypatch):
    """Patch out everything that touches the real machine.

    act() calls frontmost() in this order:
      #1 activation wait, then per step i: #pre(i), #post(i).
    So a scripted list of return values pins the flip to an exact boundary.
    """
    executed = []
    audit_lines = []

    def install(sequence):
        remaining = list(sequence)

        def fake_frontmost():
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        monkeypatch.setattr(server, "frontmost", fake_frontmost)

    monkeypatch.setattr(server, "find_running", lambda name: FakeRunningApp(TARGET))
    monkeypatch.setattr(server, "execute_step",
                        lambda step, pid: executed.append((step, pid)) or "stubbed")
    monkeypatch.setattr(server, "audit",
                        lambda *a: audit_lines.append(a))
    return {"install": install, "executed": executed, "audit": audit_lines}


def test_focus_lost_before_a_step_aborts_the_batch(rig):
    # activate, s0 pre/post, s1 pre/post all fine; s2's pre-check sees the thief.
    rig["install"]([TARGET] * 5 + [THIEF])
    result = server.act(target="TextEdit", steps=STEPS, settle_ms=0)

    assert result["ok"] is False
    assert result["steps_completed"] == 2
    assert result["failed_step"]["index"] == 2
    assert "focus lost before step" in result["failed_step"]["reason"]
    assert "Claude" in result["failed_step"]["reason"]
    assert result["failed_step"]["frontmost"] == THIEF
    # cmd+a and cmd+c never fired.
    assert len(rig["executed"]) == 2
    assert [s["kind"] for s, _ in rig["executed"]] == ["click", "type"]
    assert rig["audit"], "a refused/aborted batch must still be audited"


def test_focus_lost_after_a_step_aborts_the_batch(rig):
    # activate, s0 pre/post, s1 pre all fine; s1's post-check sees the thief.
    rig["install"]([TARGET] * 4 + [THIEF])
    result = server.act(target="TextEdit", steps=STEPS, settle_ms=0)

    assert result["ok"] is False
    assert result["steps_completed"] == 2          # step 1 did run
    assert result["failed_step"]["index"] == 1     # ...but its effect is suspect
    assert "focus lost after step executed" in result["failed_step"]["reason"]
    assert result["failed_step"]["frontmost"] == THIEF
    assert len(rig["executed"]) == 2


def test_target_never_reaches_the_front(rig):
    rig["install"]([THIEF])
    result = server.act(target="TextEdit", steps=STEPS, settle_ms=0)

    assert result["ok"] is False
    assert result["failed_step"]["index"] == -1
    assert "did not come to the front" in result["failed_step"]["reason"]
    assert rig["executed"] == []


def test_batch_completes_when_focus_holds(rig):
    rig["install"]([TARGET])
    result = server.act(target="TextEdit", steps=STEPS, settle_ms=0)

    assert result["ok"] is True
    assert result["steps_completed"] == 4
    assert len(rig["executed"]) == 4


def test_blocklisted_target_is_refused_before_anything_resolves(rig):
    rig["install"]([TARGET])
    result = server.act(target="System Settings", steps=STEPS, settle_ms=0)

    assert result["ok"] is False
    assert result["steps_completed"] == 0
    assert "refused" in result["failed_step"]["reason"]
    assert "System Settings" in result["failed_step"]["reason"]
    assert rig["executed"] == []
