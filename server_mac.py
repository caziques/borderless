#!/usr/bin/env python3
"""Borderless server — run on the Mac (the machine your keyboard/mouse are attached to).

Captures mouse and keyboard input. When the cursor crosses the configured screen
edge, input is suppressed locally and streamed to the Windows client instead,
Mouse Without Borders style. Clipboard text is synced in both directions.

Hotkey: Ctrl+Alt+Q toggles control between the Mac and the Windows machine.
"""

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

import Quartz
from pynput import keyboard, mouse

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

HEARTBEAT_INTERVAL = 1.0     # seconds between pings to the client
HEARTBEAT_TIMEOUT = 4.0      # silence after which the client is considered gone


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(values):
    cfg = load_config()
    cfg.update(values)
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass

CTRL_KEYS = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}
ALT_KEYS = {keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr}

MOUSE_DOWN_TYPES = {
    Quartz.kCGEventLeftMouseDown: 0,
    Quartz.kCGEventRightMouseDown: 1,
    Quartz.kCGEventOtherMouseDown: 2,
}
MOUSE_UP_TYPES = {
    Quartz.kCGEventLeftMouseUp: 0,
    Quartz.kCGEventRightMouseUp: 1,
    Quartz.kCGEventOtherMouseUp: 2,
}
MOUSE_MOVE_TYPES = {
    Quartz.kCGEventMouseMoved,
    Quartz.kCGEventLeftMouseDragged,
    Quartz.kCGEventRightMouseDragged,
    Quartz.kCGEventOtherMouseDragged,
}


def clipboard_get():
    try:
        return subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=2
        ).stdout
    except Exception:
        return None


def clipboard_set(text):
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=2)
    except Exception:
        pass


class Server:
    def __init__(self, args):
        self.args = args
        self.edge = args.edge
        bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        self.mac_w = bounds.size.width
        self.mac_h = bounds.size.height
        self.displays = []           # (x, y, w, h) of every Mac display
        self.displays_at = 0.0       # when the list was last refreshed
        self.enter_from = None       # display the cursor left the Mac from
        self.refresh_displays()

        self.speed = args.speed
        self.remote = False          # True while the Windows machine has control
        self.entered_at = 0.0        # when control last moved to Windows
        self.conn = None
        self.conn_lock = threading.Lock()
        self.conn_hb = False         # client answers pings (older clients don't)
        self.last_rx = 0.0           # when the active client last sent anything
        self.win_w = 1920.0          # replaced by the client's hello
        self.win_h = 1080.0
        self.rx = 0.0                # virtual cursor position on the Windows screen
        self.ry = 0.0
        self.park = (self.mac_w / 2.0, self.mac_h / 2.0)  # cursor clamp point

        self.sendq = queue.Queue(maxsize=2000)
        self.mouse_ctl = mouse.Controller()
        self.pressed = set()         # currently held modifier keys (local tracking)
        self.last_clip_sent = None
        self.last_clip_received = None

    # ---------------- networking ----------------

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.args.bind, self.args.port))
        srv.listen(1)
        print(f"[borderless] listening on {self.args.bind}:{self.args.port}")
        print(f"[borderless] Mac screen: {self.mac_w:.0f}x{self.mac_h:.0f}, "
              f"hand-off edge: {self.edge}")
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print(f"[borderless] client connected from {addr[0]}")
            threading.Thread(target=self.recv_loop, args=(conn,), daemon=True).start()

    def recv_loop(self, conn):
        f = conn.makefile("r", encoding="utf-8", newline="\n")
        authed = False
        try:
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                t = msg.get("t")
                if authed:
                    self.last_rx = time.time()
                if t == "hello":
                    if self.args.token and msg.get("token") != self.args.token:
                        print("[borderless] client rejected: bad token")
                        conn.close()
                        return
                    self.win_w = float(msg.get("w", 1920))
                    self.win_h = float(msg.get("h", 1080))
                    with self.conn_lock:
                        old = self.conn
                        self.conn = conn
                        self.conn_hb = bool(msg.get("hb"))
                        self.last_rx = time.time()
                    if not self.conn_hb:
                        print("[borderless] note: client is an older version without "
                              "heartbeat support; update client_windows.py")
                    if old:
                        try:
                            old.close()
                        except OSError:
                            pass
                    authed = True
                    print(f"[borderless] windows screen: "
                          f"{self.win_w:.0f}x{self.win_h:.0f} — ready. "
                          f"Move the cursor off the {self.edge} edge.")
                elif t == "clip" and authed:
                    text = msg.get("text", "")
                    if text != self.last_clip_received:
                        self.last_clip_received = text
                        self.last_clip_sent = text
                        clipboard_set(text)
        except OSError:
            pass
        finally:
            with self.conn_lock:
                if self.conn is conn:
                    self.conn = None
                    if self.remote:
                        self.exit_remote(reason="client disconnected")
            print("[borderless] client disconnected")

    def send(self, msg):
        try:
            self.sendq.put_nowait(msg)
        except queue.Full:
            # Drop movement spam rather than stall the event tap.
            if msg.get("t") != "move":
                try:
                    self.sendq.get_nowait()
                    self.sendq.put_nowait(msg)
                except queue.Empty:
                    pass

    def send_loop(self):
        while True:
            msg = self.sendq.get()
            # Coalesce bursts of move messages into the latest one.
            while msg.get("t") == "move":
                try:
                    nxt = self.sendq.get_nowait()
                except queue.Empty:
                    break
                if nxt.get("t") == "move":
                    msg = nxt
                else:
                    self._send_now(msg)
                    msg = nxt
                    break
            self._send_now(msg)

    def _send_now(self, msg):
        with self.conn_lock:
            conn = self.conn
        if conn is None:
            return
        try:
            conn.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        except OSError:
            with self.conn_lock:
                if self.conn is conn:
                    self.conn = None
            if self.remote:
                self.exit_remote(reason="send failed")

    def heartbeat_loop(self):
        while True:
            time.sleep(HEARTBEAT_INTERVAL)
            with self.conn_lock:
                conn = self.conn
                hb = self.conn_hb
                silent = time.time() - self.last_rx
            if conn is None:
                continue
            self.send({"t": "ping"})
            if not hb or silent <= HEARTBEAT_TIMEOUT:
                continue
            print(f"[borderless] no reply from client for {silent:.0f}s — "
                  f"dropping the connection")
            with self.conn_lock:
                if self.conn is conn:
                    self.conn = None
                    if self.remote:
                        self.exit_remote(reason="heartbeat timeout")
            # A dead peer never sends FIN, so the reader and writer threads can
            # block indefinitely; shutdown() unblocks both.
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    # ---------------- displays ----------------

    def refresh_displays(self):
        try:
            err, ids, cnt = Quartz.CGGetActiveDisplayList(16, None, None)
            if not err and cnt:
                rects = []
                for did in ids[:cnt]:
                    b = Quartz.CGDisplayBounds(did)
                    rects.append((b.origin.x, b.origin.y,
                                  b.size.width, b.size.height))
                self.displays = rects
        except Exception:
            pass
        if not self.displays:
            self.displays = [(0.0, 0.0, self.mac_w, self.mac_h)]
        self.displays_at = time.time()

    def display_at(self, x, y):
        for rect in self.displays:
            dx, dy, dw, dh = rect
            if dx <= x < dx + dw and dy <= y < dy + dh:
                return rect
        return None

    # ---------------- mode switching ----------------

    def enter_remote(self, rel_y, from_display):
        with self.conn_lock:
            if self.conn is None:
                return
        self.enter_from = from_display
        self.ry = max(0.0, min(self.win_h - 1, rel_y * self.win_h))
        self.rx = 15.0 if self.edge == "right" else self.win_w - 16.0
        self.entered_at = time.time()
        self.remote = True
        # Suppressing the events isn't enough: the WindowServer moves the cursor
        # sprite below the event tap, so it keeps drifting (onto other Mac
        # displays). Decouple the physical mouse from the cursor and hide it —
        # deltas still read fine via kCGMouseEventDeltaX/Y while decoupled.
        # Decoupling alone is unreliable across macOS versions / multi-display
        # setups, so we ALSO clamp: warp the cursor back to a fixed point on
        # every move (see mouse_intercept). Park it at the hand-off edge of the
        # display we left from — NOT the primary centre — so returning from
        # Windows lands back on THIS display (e.g. Mac-2) instead of jumping
        # straight to the primary and skipping it.
        pdx, pdy, pdw, pdh = from_display
        park_x = pdx + pdw - 5 if self.edge == "right" else pdx + 4
        park_y = pdy + max(0.0, min(1.0, rel_y)) * pdh
        self.park = (park_x, park_y)
        self.freeze_cursor(True)
        self.send({"t": "move", "x": int(self.rx), "y": int(self.ry)})
        print("[borderless] → control handed to Windows")

    def exit_remote(self, reason="edge"):
        self.remote = False
        self.send({"t": "reset"})
        # Re-couple the cursor to the mouse before warping it back, so the
        # warp sticks and the pointer reappears at the hand-off edge.
        self.freeze_cursor(False)
        dx, dy, dw, dh = self.enter_from or self.displays[0]
        mac_x = dx + dw - 5 if self.edge == "right" else dx + 4
        mac_y = dy + (self.ry / self.win_h) * dh
        try:
            self.mouse_ctl.position = (mac_x, mac_y)
        except Exception:
            pass
        print(f"[borderless] ← control returned to Mac ({reason})")

    def freeze_cursor(self, frozen):
        """Decouple (freeze + hide) or re-couple the Mac cursor from the mouse."""
        try:
            Quartz.CGAssociateMouseAndMouseCursorPosition(not frozen)
        except Exception:
            pass
        try:
            if frozen:
                Quartz.CGDisplayHideCursor(Quartz.CGMainDisplayID())
            else:
                Quartz.CGDisplayShowCursor(Quartz.CGMainDisplayID())
        except Exception:
            pass

    def toggle(self):
        if self.remote:
            self.exit_remote(reason="hotkey")
        else:
            try:
                x, y = self.mouse_ctl.position
            except Exception:
                x, y = 0, 0
            d = self.display_at(x, y) or self.displays[0]
            self.enter_remote((y - d[1]) / d[3], d)

    # ---------------- mouse ----------------

    def on_move(self, x, y):
        if self.remote:
            return
        if time.time() - self.displays_at > 5.0:
            self.refresh_displays()
        d = self.display_at(x, y)
        if d is None:
            return
        dx, dy, dw, dh = d
        # Hand off only at an edge with no Mac display on the other side —
        # otherwise the cursor is just crossing between Mac monitors.
        if (self.edge == "right" and x >= dx + dw - 1
                and self.display_at(x + 8, y) is None):
            self.enter_remote((y - dy) / dh, d)
        elif (self.edge == "left" and x <= dx
                and self.display_at(x - 8, y) is None):
            self.enter_remote((y - dy) / dh, d)

    def mouse_intercept(self, event_type, event):
        try:
            if not self.remote:
                return event
            if event_type in MOUSE_MOVE_TYPES:
                dx = Quartz.CGEventGetDoubleValueField(
                    event, Quartz.kCGMouseEventDeltaX)
                dy = Quartz.CGEventGetDoubleValueField(
                    event, Quartz.kCGMouseEventDeltaY)
                # Synthetic warps (our own clamp, below) carry no HID delta;
                # ignore them so they aren't echoed to Windows.
                if dx == 0 and dy == 0:
                    return None
                self.remote_move(dx * self.speed, dy * self.speed)
                # Clamp: pin the physical cursor to a safe spot on the primary
                # display so it can't sync-move onto a second Mac screen. The
                # associate(True) cancels the ~250ms HID-delta suppression that
                # a warp otherwise triggers, keeping Windows motion smooth.
                try:
                    Quartz.CGWarpMouseCursorPosition(self.park)
                    Quartz.CGAssociateMouseAndMouseCursorPosition(True)
                except Exception:
                    pass
                return None
            if event_type in MOUSE_DOWN_TYPES:
                self.send({"t": "click", "b": MOUSE_DOWN_TYPES[event_type], "d": 1})
                return None
            if event_type in MOUSE_UP_TYPES:
                self.send({"t": "click", "b": MOUSE_UP_TYPES[event_type], "d": 0})
                return None
            if event_type == Quartz.kCGEventScrollWheel:
                dy = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGScrollWheelEventDeltaAxis1)
                dx = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGScrollWheelEventDeltaAxis2)
                if dx or dy:
                    self.send({"t": "scroll", "dx": int(dx), "dy": int(dy)})
                return None
            return None
        except Exception:
            return event

    def remote_move(self, dx, dy):
        self.rx += dx
        self.ry += dy
        # Brief grace period after hand-off so edge jitter can't bounce
        # control straight back to the Mac.
        can_return = time.time() - self.entered_at > 0.3
        if can_return and self.edge == "right" and self.rx <= 0:
            self.exit_remote()
            return
        if can_return and self.edge == "left" and self.rx >= self.win_w - 1:
            self.exit_remote()
            return
        self.rx = max(0.0, min(self.win_w - 1, self.rx))
        self.ry = max(0.0, min(self.win_h - 1, self.ry))
        self.send({"t": "move", "x": int(self.rx), "y": int(self.ry)})

    # ---------------- keyboard ----------------

    def is_hotkey(self, key):
        ctrl = any(k in self.pressed for k in CTRL_KEYS)
        alt = any(k in self.pressed for k in ALT_KEYS)
        if not (ctrl and alt):
            return False
        if isinstance(key, keyboard.KeyCode):
            if key.char in ("q", "Q", "\x11"):
                return True
            if key.vk == 12:  # kVK_ANSI_Q
                return True
        return False

    def on_press(self, key):
        if key in CTRL_KEYS or key in ALT_KEYS:
            self.pressed.add(key)
        if self.is_hotkey(key):
            self.toggle()
            return
        if self.remote:
            self.send({"t": "key", "d": 1, **self.key_fields(key)})

    def on_release(self, key):
        self.pressed.discard(key)
        if self.remote:
            self.send({"t": "key", "d": 0, **self.key_fields(key)})

    @staticmethod
    def key_fields(key):
        # "mvk" is the physical macOS key code, which the client maps to a
        # Windows scan code. The name/char fields stay as a fallback for media
        # keys and for clients that predate scan-code support.
        if isinstance(key, keyboard.Key):
            fields = {"name": key.name}
            code = key.value
        else:
            fields = {}
            if key.char is not None:
                fields["char"] = key.char
            if key.vk is not None:
                fields["vk"] = key.vk
            code = key
        if code.vk is not None and not getattr(code, "_is_media", None):
            fields["mvk"] = code.vk
        return fields

    def key_intercept(self, event_type, event):
        return None if self.remote else event

    # ---------------- clipboard ----------------

    def clip_loop(self):
        self.last_clip_sent = clipboard_get()
        while True:
            time.sleep(1.0)
            text = clipboard_get()
            if text is None or text == self.last_clip_sent:
                continue
            self.last_clip_sent = text
            if text != self.last_clip_received:
                self.send({"t": "clip", "text": text})

    # ---------------- lifecycle ----------------

    def run(self):
        preflight = getattr(Quartz, "CGPreflightListenEventAccess", None)
        if preflight is not None and not preflight():
            print("[borderless] WARNING: this process does not have input-monitoring "
                  "access yet.\n  Grant it in System Settings → Privacy & Security → "
                  "Input Monitoring (and Accessibility),\n  then restart. macOS "
                  "should prompt you on first run.")
            requester = getattr(Quartz, "CGRequestListenEventAccess", None)
            if requester is not None:
                requester()

        import atexit
        atexit.register(self.freeze_cursor, False)

        threading.Thread(target=self.send_loop, daemon=True).start()
        threading.Thread(target=self.clip_loop, daemon=True).start()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()

        mouse_listener = mouse.Listener(
            on_move=self.on_move,
            darwin_intercept=self.mouse_intercept,
        )
        kb_listener = keyboard.Listener(
            on_press=self.on_press,
            on_release=self.on_release,
            darwin_intercept=self.key_intercept,
        )
        mouse_listener.start()
        kb_listener.start()
        print("[borderless] input listeners running (Ctrl+Alt+Q toggles control)")
        try:
            self.serve()
        except KeyboardInterrupt:
            print("\n[borderless] shutting down")
        finally:
            # Never leave the cursor frozen/hidden system-wide on exit.
            self.freeze_cursor(False)


def main():
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bind", default="0.0.0.0", help="address to listen on")
    p.add_argument("--port", type=int, default=cfg.get("port", 24801),
                   help="TCP port (default 24801)")
    p.add_argument("--win-side", dest="edge", choices=["left", "right"],
                   default=cfg.get("edge", "right"),
                   help="which side of the Mac the Windows machine sits on "
                        "(cross that edge to hand off control)")
    p.add_argument("--token", default=cfg.get("token", ""),
                   help="shared secret; client must send the same")
    p.add_argument("--speed", type=float, default=cfg.get("speed", 1.0),
                   help="mouse speed multiplier on the Windows screen")
    args = p.parse_args()
    save_config({"edge": args.edge, "speed": args.speed, "port": args.port,
                 "token": args.token})
    Server(args).run()


if __name__ == "__main__":
    main()
