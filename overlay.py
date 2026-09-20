#!/usr/bin/env python3
"""Mac-Commander overlay — a rose-gold halo saying "Claude has the con".

server.py spawns this helper the first time a tool touches the desktop,
writes "ping" to its stdin when a tool call starts and "hide" when the call
returns. A ping lights the halo at once; a hide starts a LINGER countdown,
and only when that runs out with no new ping does the halo fade. So a burst
of tool calls with thinking gaps between them reads as one sustained glow —
"Claude has the con" for the whole job — rather than a strobe on every call.

A ping fades in one click-through, borderless window
per screen: a rose-gold glow around the screen edges, plus a small pill at
the bottom centre of the main screen reading "Claude has the con". EOF on
stdin — the server exiting, however it exits — quits the helper, so the
halo can never outlive the server.

Why a separate process: the server has no run loop between tool calls, so a
window shown there could never fade itself out afterwards; and keeping
AppKit windows out of the server keeps its input path small and auditable.

This process draws and reads nothing. Every window sets ignoresMouseEvents,
so it cannot swallow the clicks the server posts, and it never takes key
focus, so it cannot steal the keyboard it is reporting on. Its stdin
protocol is three fixed words — ping, hide, EOF — chosen by server.py, never
by the model.
"""

from __future__ import annotations

import sys
import threading

import objc
import AppKit
import Quartz
from Foundation import NSMakePoint, NSMakeRect, NSObject

LABEL = "Claude has the con"
GLOW = 36.0  # how far the halo bleeds inward from each screen edge, in points
LINGER = 15.0  # seconds the halo stays lit after the last "hide" before fading;
               # a "ping" inside this window cancels the fade. Tune to taste.


def _rose(r: float, g: float, b: float, a: float):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


class HaloView(AppKit.NSView):
    """The halo for one screen; the label pill only where show_label is set."""

    show_label = False   # True on the main screen's view only
    label_y = 12.0       # pill bottom inset — set above the Dock via visibleFrame

    def drawRect_(self, dirty):
        bounds = self.bounds()
        w, h = bounds.size.width, bounds.size.height
        # Rose gold: a bright rim at the very edge, bleeding inward to nothing.
        # The four gradients overlap in the corners, which paints them twice —
        # deliberately: a halo is brightest where the edges meet.
        grad = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
            _rose(0.97, 0.73, 0.69, 0.60), _rose(0.72, 0.43, 0.47, 0.0))
        grad.drawInRect_angle_(NSMakeRect(0, 0, w, GLOW), 90.0)          # bottom
        grad.drawInRect_angle_(NSMakeRect(0, h - GLOW, w, GLOW), 270.0)  # top
        grad.drawInRect_angle_(NSMakeRect(0, 0, GLOW, h), 0.0)           # left
        grad.drawInRect_angle_(NSMakeRect(w - GLOW, 0, GLOW, h), 180.0)  # right
        _rose(0.99, 0.80, 0.76, 0.90).setStroke()
        rim = AppKit.NSBezierPath.bezierPathWithRect_(NSMakeRect(1.0, 1.0, w - 2.0, h - 2.0))
        rim.setLineWidth_(2.0)
        rim.stroke()
        if self.show_label:
            self._draw_label(w)

    @objc.python_method
    def _draw_label(self, w):
        font = AppKit.NSFont.systemFontOfSize_weight_(13.0, AppKit.NSFontWeightSemibold)
        text = AppKit.NSAttributedString.alloc().initWithString_attributes_(LABEL, {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: _rose(1.0, 0.93, 0.91, 0.96),
        })
        size = text.size()
        pad_x, pad_y = 16.0, 7.0
        pill_w, pill_h = size.width + pad_x * 2, size.height + pad_y * 2
        x = (w - pill_w) / 2.0
        y = float(self.label_y)
        pill = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x, y, pill_w, pill_h), pill_h / 2.0, pill_h / 2.0)
        _rose(0.13, 0.07, 0.08, 0.62).setFill()
        pill.fill()
        _rose(0.93, 0.62, 0.60, 0.80).setStroke()
        pill.setLineWidth_(1.0)
        pill.stroke()
        text.drawAtPoint_(NSMakePoint(x + pad_x, y + pad_y))


class Overlay(NSObject):
    """Owns the windows, the fade animations and the linger timer.

    Every method here runs on the main thread: the stdin reader hands its
    lines over via performSelectorOnMainThread, because AppKit is
    main-thread-only and this class is the only thing that touches it.
    """

    def init(self):
        self = objc.super(Overlay, self).init()
        if self is None:
            return None
        self.windows = []
        self.fade_timer = None
        self.linger_timer = None  # one-shot countdown armed by hideCmd, disarmed by pingCmd
        self.fade_from = 0.0
        self.fade_target = 0.0
        self.fade_step = 0
        self.fade_steps = 1
        # A window ordered front before NSApplication finishes launching is
        # silently dropped — and the first ping usually arrives exactly then,
        # because the server writes it the moment it spawns this process. So
        # pings are held until applicationDidFinishLaunching: (this object is
        # the app delegate) and replayed once ordering actually works.
        self.ready = False
        self.pending = False
        return self

    def applicationDidFinishLaunching_(self, note):
        # Windows are created here, not before run(): an NSWindow born before
        # the app finishes launching never comes up when ordered front. The
        # held ping is then replayed via a zero-ish timer rather than called
        # directly — ordering a window during the launch turn itself is also
        # silently dropped by the window server; one turn later it sticks.
        self.rebuild()
        self.ready = True
        if self.pending:
            self.pending = False
            self.performSelector_withObject_afterDelay_("pingCmd", None, 0.05)

    def rebuild(self):
        for win in self.windows:
            win.orderOut_(None)
        self.windows = []
        for index, screen in enumerate(AppKit.NSScreen.screens() or []):
            frame = screen.frame()
            win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame, AppKit.NSWindowStyleMaskBorderless,
                AppKit.NSBackingStoreBuffered, False)
            win.setReleasedWhenClosed_(False)  # Python owns the lifetime, not close()
            win.setOpaque_(False)
            win.setBackgroundColor_(AppKit.NSColor.clearColor())
            win.setHasShadow_(False)
            win.setIgnoresMouseEvents_(True)   # the server's clicks pass through
            win.setLevel_(Quartz.CGWindowLevelForKey(Quartz.kCGScreenSaverWindowLevelKey))
            win.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorStationary
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
            view = HaloView.alloc().initWithFrame_(
                NSMakeRect(0, 0, frame.size.width, frame.size.height))
            if index == 0:  # screens()[0] is the screen with the menu bar
                view.show_label = True
                # visibleFrame excludes the Dock, so the pill sits just above it.
                visible = screen.visibleFrame()
                view.label_y = (visible.origin.y - frame.origin.y) + 12.0
            win.setContentView_(view)
            win.setAlphaValue_(0.0)
            self.windows.append(win)

    def pingCmd(self):
        if not self.ready:
            self.pending = True
            return
        self._disarm_linger()  # a new call landed inside the quiet period: stay lit
        self._fade(1.0, 0.25, order_front=True)

    def hideCmd(self):
        # Don't fade yet. Arm a one-shot countdown; if no ping arrives before
        # it fires, lingerFired_ does the fade-out. Back-to-back calls with
        # thinking gaps between them therefore read as one sustained glow.
        self.pending = False
        self._disarm_linger()
        self.linger_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            LINGER, self, "lingerFired:", None, False)

    def lingerFired_(self, timer):
        # The quiet period ran out with no new ping: the job is over.
        self.linger_timer = None
        # A slightly slower fade-out than in, so the drop reads as a settle,
        # not a snap.
        self._fade(0.0, 0.6)

    @objc.python_method
    def _disarm_linger(self):
        if self.linger_timer is not None:
            self.linger_timer.invalidate()
            self.linger_timer = None

    def screensChanged_(self, note):
        was_visible = any(win.alphaValue() > 0 for win in self.windows)
        self.rebuild()
        if was_visible:
            self.pingCmd()

    def quitCmd(self):
        AppKit.NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _fade(self, alpha, duration, order_front=False):
        """Timer-stepped fade with direct setAlphaValue_ calls.

        Not the animator proxy: in a background (Prohibited-policy) app the
        animator updates the model value but its animation never reaches the
        window server, so the window stays invisible at alpha 0 — verified
        via CGWindowListCopyWindowInfo. Plain setAlphaValue_ commits at once.
        """
        if self.fade_timer is not None:
            self.fade_timer.invalidate()
            self.fade_timer = None
        if order_front:
            for win in self.windows:
                win.orderFrontRegardless()
        self.fade_from = float(self.windows[0].alphaValue()) if self.windows else 0.0
        self.fade_target = float(alpha)
        self.fade_step = 0
        self.fade_steps = max(1, round(duration / 0.033))
        self.fade_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.033, self, "fadeTick:", None, True)

    def fadeTick_(self, timer):
        self.fade_step += 1
        t = min(1.0, self.fade_step / self.fade_steps)
        value = self.fade_from + (self.fade_target - self.fade_from) * t
        for win in self.windows:
            win.setAlphaValue_(value)
        if t >= 1.0:
            timer.invalidate()
            self.fade_timer = None
            if self.fade_target == 0.0:
                for win in self.windows:
                    win.orderOut_(None)


def _watch_stdin(overlay: Overlay) -> None:
    """Forward stdin lines to the main thread; EOF or any error quits the app."""
    try:
        for line in sys.stdin.buffer:
            cmd = line.strip()
            if cmd == b"ping":
                overlay.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "pingCmd", None, False)
            elif cmd == b"hide":
                overlay.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "hideCmd", None, False)
    except Exception:
        pass
    overlay.performSelectorOnMainThread_withObject_waitUntilDone_("quitCmd", None, False)


def main() -> None:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
    overlay = Overlay.alloc().init()
    app.setDelegate_(overlay)  # NSApp holds it weakly; `overlay` here keeps it alive
    AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        overlay, "screensChanged:",
        AppKit.NSApplicationDidChangeScreenParametersNotification, None)
    threading.Thread(target=_watch_stdin, args=(overlay,), daemon=True).start()
    app.run()


if __name__ == "__main__":
    main()
