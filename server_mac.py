#!/usr/bin/env python3
"""Borderless server — run on the Mac (the machine your keyboard/mouse are attached to).

Captures mouse and keyboard input. When the cursor crosses the configured screen
edge, input is suppressed locally and streamed to the Windows client instead,
Mouse Without Borders style. Clipboard text and images are synced in both
directions. The connection is TLS-encrypted and paired with a shared key.

Hotkey: Ctrl+Alt+Q toggles control between the Mac and the Windows machine.
"""

import argparse
import json
import os
import queue
import shutil
import socket
import ssl
import subprocess
import threading
import time

import Quartz
from AppKit import (NSBitmapImageFileTypeBMP, NSBitmapImageFileTypePNG,
                    NSBitmapImageRep, NSPasteboard, NSPasteboardTypePNG,
                    NSPasteboardTypeString, NSPasteboardTypeTIFF)
from Foundation import NSData
from pynput import keyboard, mouse

import protocol as P

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CERT_PATH = os.path.join(HERE, "tls_cert.pem")
KEY_PATH = os.path.join(HERE, "tls_key.pem")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(values, remove=()):
    cfg = load_config()
    cfg.update(values)
    for name in remove:
        cfg.pop(name, None)
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def ensure_certificate():
    """Create the self-signed TLS certificate on first run."""
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        return
    openssl = shutil.which("openssl") or "/usr/bin/openssl"
    print("[borderless] creating a TLS certificate for this Mac ...")
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", KEY_PATH, "-out", CERT_PATH, "-days", "3650",
         "-subj", "/CN=borderless"],
        check=True, capture_output=True)
    os.chmod(KEY_PATH, 0o600)




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


class MacClipboard:
    """Text and images on the macOS pasteboard. Images travel as Windows DIBs."""

    def __init__(self):
        self.pb = NSPasteboard.generalPasteboard()

    def change_count(self):
        return self.pb.changeCount()

    def read(self):
        text = self.pb.stringForType_(NSPasteboardTypeString)
        if text is not None:
            return "text", str(text).encode("utf-8")
        for kind in (NSPasteboardTypePNG, NSPasteboardTypeTIFF):
            data = self.pb.dataForType_(kind)
            if data is None:
                continue
            rep = NSBitmapImageRep.imageRepWithData_(data)
            if rep is None:
                continue
            bmp = rep.representationUsingType_properties_(NSBitmapImageFileTypeBMP, {})
            if bmp is not None:
                return "dib", bytes(bmp)[14:]
        return None

    def write(self, fmt, data):
        if fmt == "text":
            self.pb.clearContents()
            self.pb.setString_forType_(data.decode("utf-8", "replace"),
                                       NSPasteboardTypeString)
        elif fmt == "dib":
            bmp = P.dib_to_bmp(P.make_dib_opaque(data))
            rep = NSBitmapImageRep.imageRepWithData_(
                NSData.dataWithBytes_length_(bmp, len(bmp)))
            if rep is None:
                raise ValueError("could not decode the image from Windows")
            png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
            self.pb.clearContents()
            self.pb.setData_forType_(png, NSPasteboardTypePNG)
            self.pb.setData_forType_(rep.TIFFRepresentation(), NSPasteboardTypeTIFF)
        else:
            raise ValueError(f"unknown clipboard format {fmt!r}")
        return self.pb.changeCount()


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

        self.auth_key = P.derive_auth_key(args.key)
        self.tls_ctx = None          # created in run(), after the certificate exists
        self.cert_der = None

        self.speed = args.speed
        self.remote = False          # True while the Windows machine has control
        self.entered_at = 0.0        # when control last moved to Windows
        self.chan = None             # the authenticated main connection
        self.conn_lock = threading.Lock()

        # Windows desktop, in physical pixels relative to its top-left corner.
        self.win_w = 1920.0
        self.win_h = 1080.0
        self.win_mons = [(0.0, 0.0, 1920.0, 1080.0)]
        self.rx = 0.0                # virtual cursor position on the Windows desktop
        self.ry = 0.0
        self.park = (self.mac_w / 2.0, self.mac_h / 2.0)  # cursor clamp point

        self.sendq = queue.Queue(maxsize=2000)
        self.mouse_ctl = mouse.Controller()
        self.pressed = set()         # currently held modifier keys (local tracking)

        self.clipboard = MacClipboard()
        self.clip_lock = threading.Lock()
        self.clip_last_digest = None  # last clipboard content sent or received
        self.clip_own_cc = None       # pasteboard change count of our own write
        self.clip_offer = None        # (id, fmt, compressed) waiting to be fetched
        self.clip_seq = 0

    # ---------------- networking ----------------

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.args.bind, self.args.port))
        srv.listen(4)
        print(f"[borderless] listening on {self.args.bind}:{self.args.port} (TLS)")
        print(f"[borderless] Mac screen: {self.mac_w:.0f}x{self.mac_h:.0f}, "
              f"hand-off edge: {self.edge}")
        while True:
            raw, addr = srv.accept()
            threading.Thread(target=self.handle_conn, args=(raw, addr[0]),
                             daemon=True).start()

    def handle_conn(self, raw, ip):
        try:
            chan = P.accept_tls(raw, self.tls_ctx)
        except (ssl.SSLError, OSError) as exc:
            print(f"[borderless] TLS handshake with {ip} failed ({exc}). "
                  f"Is client_windows.py up to date?")
            raw.close()
            return
        try:
            hello = P.authenticate_server(chan, self.auth_key, self.cert_der)
        except P.AuthError as exc:
            print(f"[borderless] rejected {ip}: {exc}")
            chan.close()
            return
        except (OSError, ConnectionError, ValueError) as exc:
            print(f"[borderless] handshake with {ip} failed: {exc}")
            chan.close()
            return
        if hello.get("role") == "data":
            self.handle_data(chan)
        else:
            self.main_loop(chan, hello, ip)

    def main_loop(self, chan, hello, ip):
        self.update_win_screens(hello)
        with self.conn_lock:
            old, self.chan = self.chan, chan
        if old is not None:
            old.close()
        print(f"[borderless] client {ip} paired — Windows desktop "
              f"{self.win_w:.0f}x{self.win_h:.0f} on {len(self.win_mons)} "
              f"display(s). Move the cursor off the {self.edge} edge.")
        try:
            while True:
                msg = chan.recv_msg(timeout=P.HEARTBEAT_TIMEOUT)
                t = msg.get("t")
                if t == "clip":
                    try:
                        self.apply_clip(msg["fmt"], P.decompress(P.unb64(msg["data"])))
                    except Exception as exc:
                        print(f"[borderless] could not apply clipboard from Windows: {exc}")
                elif t == "screens":
                    self.update_win_screens(msg)
                    print(f"[borderless] Windows desktop changed: "
                          f"{self.win_w:.0f}x{self.win_h:.0f} on "
                          f"{len(self.win_mons)} display(s)")
        except socket.timeout:
            print(f"[borderless] no reply from client for "
                  f"{P.HEARTBEAT_TIMEOUT:.0f}s — dropping the connection")
        except (OSError, ConnectionError):
            pass
        finally:
            chan.close()
            with self.conn_lock:
                if self.chan is chan:
                    self.chan = None
                    if self.remote:
                        self.exit_remote(reason="client disconnected")
            print("[borderless] client disconnected")

    def handle_data(self, chan):
        """Serve one bulk clipboard transfer, then close."""
        try:
            req = chan.recv_msg(timeout=P.AUTH_TIMEOUT)
            if req.get("t") == "fetch":
                offer = self.clip_offer
                if offer is None or offer[0] != req.get("id"):
                    chan.send({"t": "blob", "ok": False})
                    return
                _, fmt, blob = offer
                chan.send({"t": "blob", "ok": True, "fmt": fmt, "size": len(blob)})
                chan.send_bytes(blob, timeout=120)
                chan.recv_msg(timeout=120)  # wait for "done" so nothing is cut off
            elif req.get("t") == "push":
                size = int(req.get("size", 0))
                if not 0 < size <= P.MAX_CLIP_SIZE:
                    chan.send({"t": "done", "ok": False})
                    return
                blob = chan.recv_exact(size, timeout=120)
                chan.send({"t": "done", "ok": True})
                self.apply_clip(req["fmt"], P.decompress(blob))
        except Exception as exc:
            print(f"[borderless] clipboard transfer failed: {exc}")
        finally:
            chan.close()

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
            chan = self.chan
        if chan is None:
            return
        try:
            chan.send(msg, timeout=2.0)
        except (OSError, ConnectionError):
            # A send that can't complete means the link is dead; closing the
            # channel makes main_loop clean up and return control to the Mac.
            chan.close()

    def heartbeat_loop(self):
        while True:
            time.sleep(P.HEARTBEAT_INTERVAL)
            if self.chan is not None:
                self.send({"t": "ping"})

    # ---------------- Windows desktop geometry ----------------

    def update_win_screens(self, msg):
        mons = msg.get("mons") or [[0, 0, msg.get("vw", 1920), msg.get("vh", 1080)]]
        mons = [tuple(float(v) for v in m[:4]) for m in mons if m[2] > 0 and m[3] > 0]
        if not mons:
            return
        self.win_mons = mons
        self.win_w = float(msg.get("vw") or max(x + w for x, _, w, _ in mons))
        self.win_h = float(msg.get("vh") or max(y + h for _, y, _, h in mons))
        if self.remote:
            self.rx, self.ry = self.win_clamp(self.rx, self.ry)

    def win_mon_at(self, x, y):
        for m in self.win_mons:
            mx, my, mw, mh = m
            if mx <= x < mx + mw and my <= y < my + mh:
                return m
        return None

    def win_clamp(self, x, y, current=None):
        """Keep (x, y) on a Windows display.

        Like Windows itself, the cursor only moves onto another display when
        the new point lies inside it; otherwise it stays pinned to the edge of
        the display it is on. With no current display, use the nearest one.
        """
        if self.win_mon_at(x, y) is not None:
            return x, y
        if current is not None:
            mx, my, mw, mh = current
            return max(mx, min(mx + mw - 1, x)), max(my, min(my + mh - 1, y))
        best = None
        for mx, my, mw, mh in self.win_mons:
            cx = max(mx, min(mx + mw - 1, x))
            cy = max(my, min(my + mh - 1, y))
            dist = (cx - x) ** 2 + (cy - y) ** 2
            if best is None or dist < best[0]:
                best = (dist, cx, cy)
        return best[1], best[2]

    def send_move(self):
        # 0–65535 across the whole Windows virtual desktop, independent of
        # resolution and display scaling (the scale SendInput expects).
        nx = int((self.rx + 0.5) * 65536 / self.win_w)
        ny = int((self.ry + 0.5) * 65536 / self.win_h)
        self.send({"t": "move", "x": max(0, min(65535, nx)),
                   "y": max(0, min(65535, ny))})

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
            if self.chan is None:
                return
        self.enter_from = from_display
        right = self.edge == "right"
        # Land just inside the Windows edge that faces the Mac, on whichever
        # Windows display is at that height.
        x0, y0 = self.win_clamp(0.0 if right else self.win_w - 1,
                                max(0.0, min(1.0, rel_y)) * self.win_h)
        self.rx, self.ry = self.win_clamp(x0 + 15 if right else x0 - 15, y0)
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
        self.send_move()
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
        nx = self.rx + dx
        ny = self.ry + dy
        cur = self.win_mon_at(self.rx, self.ry) or self.win_mons[0]
        # Brief grace period after hand-off so edge jitter can't bounce
        # control straight back to the Mac.
        if time.time() - self.entered_at > 0.3:
            mx, my, mw, mh = cur
            edge_y = max(my, min(my + mh - 1, ny))
            # Return only past the outer edge of the Windows display the cursor
            # is on, where no other Windows display continues the desktop.
            if (self.edge == "right" and nx < mx
                    and self.win_mon_at(nx, edge_y) is None):
                self.exit_remote()
                return
            if (self.edge == "left" and nx >= mx + mw
                    and self.win_mon_at(nx, edge_y) is None):
                self.exit_remote()
                return
        self.rx, self.ry = self.win_clamp(nx, ny, cur)
        self.send_move()

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
        # Windows scan code. The name/char fields are the fallback for media
        # keys, which have no key code.
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
        last_cc = self.clipboard.change_count()
        while True:
            time.sleep(0.5)
            cc = self.clipboard.change_count()
            if cc == last_cc:
                continue
            last_cc = cc
            with self.clip_lock:
                if cc == self.clip_own_cc:
                    continue
            try:
                item = self.clipboard.read()
            except Exception as exc:
                print(f"[borderless] could not read the Mac clipboard: {exc}")
                continue
            if item is None:
                continue
            fmt, data = item
            if len(data) > P.MAX_CLIP_SIZE:
                print(f"[borderless] clipboard too large to share "
                      f"({P.human_size(len(data))})")
                continue
            digest = P.clip_digest(fmt, data)
            with self.clip_lock:
                if digest == self.clip_last_digest:
                    continue
                self.clip_last_digest = digest
            self.offer_clip(fmt, data)

    def offer_clip(self, fmt, data):
        if self.chan is None:
            return
        blob = P.compress(data)
        if len(blob) <= P.INLINE_CLIP_LIMIT:
            self.send({"t": "clip", "fmt": fmt, "data": P.b64(blob)})
        else:
            # Too big for the main link: announce it, and the client fetches
            # it over a separate connection.
            self.clip_seq += 1
            self.clip_offer = (self.clip_seq, fmt, blob)
            self.send({"t": "clipoffer", "id": self.clip_seq, "fmt": fmt,
                       "size": len(blob)})
        print(f"[borderless] clipboard → Windows ({fmt}, {P.human_size(len(data))})")

    def apply_clip(self, fmt, data):
        with self.clip_lock:
            self.clip_last_digest = P.clip_digest(fmt, data)
            self.clip_own_cc = self.clipboard.write(fmt, data)
        print(f"[borderless] clipboard ← Windows ({fmt}, {P.human_size(len(data))})")

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

        ensure_certificate()
        self.tls_ctx = P.server_context(CERT_PATH, KEY_PATH)
        with open(CERT_PATH) as f:
            self.cert_der = ssl.PEM_cert_to_DER_cert(f.read())

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


def print_key_banner(key):
    line = "─" * 44
    print(f"[borderless] {line}")
    print("[borderless] Pairing key — enter it on the Windows machine:")
    print(f"[borderless]     {key}")
    print(f"[borderless] {line}")


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
    p.add_argument("--speed", type=float, default=cfg.get("speed", 1.0),
                   help="mouse speed multiplier on the Windows screen")
    p.add_argument("--key", help="use this pairing key instead of the saved one")
    p.add_argument("--new-key", action="store_true",
                   help="generate a new pairing key (the client must be updated)")
    p.add_argument("--show-key", action="store_true",
                   help="print the saved pairing key")
    p.add_argument("--token", help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.token:
        print("[borderless] note: --token is no longer used; the connection is "
              "now paired with a key (see below)")
    if args.key and not P.key_is_valid(args.key):
        p.error("--key needs at least 16 characters from: " + P.KEY_ALPHABET +
                " (or use --new-key to generate one)")

    key = args.key or cfg.get("key")
    new_key = args.new_key or not P.key_is_valid(key or "")
    if new_key and not args.key:
        key = P.generate_key()
    args.key = P.format_key(key)

    save_config({"edge": args.edge, "speed": args.speed, "port": args.port,
                 "key": args.key}, remove=("token", "panel_port"))
    if new_key or args.show_key:
        print_key_banner(args.key)
    Server(args).run()


if __name__ == "__main__":
    main()
