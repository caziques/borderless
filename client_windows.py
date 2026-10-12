#!/usr/bin/env python3
"""Borderless client — run on the Windows machine.

Connects to the Mac server over TLS, receives mouse/keyboard events, and injects
them locally. Also syncs clipboard text and images in both directions.
Reconnects automatically if the connection drops.

Needs protocol.py from the same folder.
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import socket
import ssl
import sys
import threading
import time

from pynput import keyboard, mouse

import protocol as P

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "client_config.json")

CF_UNICODETEXT = 13
CF_DIB = 8
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
_kernel32.GlobalSize.restype = ctypes.c_size_t


# ---------------- clipboard ----------------

class ClipboardBusy(Exception):
    pass


def _open_clipboard(retries=1):
    for attempt in range(retries):
        if _user32.OpenClipboard(0):
            return
        time.sleep(0.05)
    raise ClipboardBusy()


def clipboard_sequence():
    return _user32.GetClipboardSequenceNumber()


def clipboard_read():
    """Return ("text", utf-8 bytes), ("dib", bytes), or None."""
    _open_clipboard()
    try:
        if _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            handle = _user32.GetClipboardData(CF_UNICODETEXT)
            ptr = _kernel32.GlobalLock(handle) if handle else None
            if not ptr:
                return None
            try:
                text = ctypes.c_wchar_p(ptr).value or ""
            finally:
                _kernel32.GlobalUnlock(handle)
            return "text", text.encode("utf-8")
        if _user32.IsClipboardFormatAvailable(CF_DIB):
            handle = _user32.GetClipboardData(CF_DIB)
            ptr = _kernel32.GlobalLock(handle) if handle else None
            if not ptr:
                return None
            try:
                data = ctypes.string_at(ptr, _kernel32.GlobalSize(handle))
            finally:
                _kernel32.GlobalUnlock(handle)
            return "dib", data
        return None
    finally:
        _user32.CloseClipboard()


def clipboard_write(fmt, data):
    """Replace the clipboard; returns the new clipboard sequence number."""
    if fmt == "text":
        buf = ctypes.create_unicode_buffer(data.decode("utf-8", "replace"))
        payload = ctypes.string_at(buf, ctypes.sizeof(buf))
        fmt_id = CF_UNICODETEXT
    elif fmt == "dib":
        payload = data
        # macOS sends a BITMAPV5HEADER (124 bytes); Windows synthesizes the
        # plain CF_DIB format from CF_DIBV5 for apps that only read that.
        header_size = int.from_bytes(data[0:4], "little")
        fmt_id = CF_DIBV5 if header_size == 124 else CF_DIB
    else:
        raise ValueError(f"unknown clipboard format {fmt!r}")
    _open_clipboard(retries=20)
    try:
        _user32.EmptyClipboard()
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            raise MemoryError("GlobalAlloc failed")
        ptr = _kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, payload, len(payload))
        _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(fmt_id, handle):
            _kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData failed")
    finally:
        _user32.CloseClipboard()
    return clipboard_sequence()


BUTTONS = {0: mouse.Button.left, 1: mouse.Button.right, 2: mouse.Button.middle}

# macOS ANSI virtual keycodes → characters. Used when the Mac sends a
# non-printable char (e.g. Ctrl+A arrives as '\x01' with vk=0).
MAC_VK_CHARS = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x", 8: "c",
    9: "v", 11: "b", 12: "q", 13: "w", 14: "e", 15: "r", 16: "y", 17: "t",
    18: "1", 19: "2", 20: "3", 21: "4", 22: "6", 23: "5", 24: "=", 25: "9",
    26: "7", 27: "-", 28: "8", 29: "0", 30: "]", 31: "o", 32: "u", 33: "[",
    34: "i", 35: "p", 37: "l", 38: "j", 39: "'", 40: "k", 41: ";", 42: "\\",
    43: ",", 44: "/", 45: "n", 46: "m", 47: ".", 50: "`",
}

# macOS virtual key code → (PC set-1 scan code, extended flag). Injecting scan
# codes sends the physical key, so Windows applies its own keyboard layout and
# modifiers: Option+A arrives as Alt+A (not "å"), and shortcuts, games, and
# non-US layouts behave as they would on a native keyboard.
MAC_TO_SCAN = {
    # Letters
    0: (0x1E, False), 11: (0x30, False), 8: (0x2E, False), 2: (0x20, False),
    14: (0x12, False), 3: (0x21, False), 5: (0x22, False), 4: (0x23, False),
    34: (0x17, False), 38: (0x24, False), 40: (0x25, False), 37: (0x26, False),
    46: (0x32, False), 45: (0x31, False), 31: (0x18, False), 35: (0x19, False),
    12: (0x10, False), 15: (0x13, False), 1: (0x1F, False), 17: (0x14, False),
    32: (0x16, False), 9: (0x2F, False), 13: (0x11, False), 7: (0x2D, False),
    16: (0x15, False), 6: (0x2C, False),
    # Number row
    18: (0x02, False), 19: (0x03, False), 20: (0x04, False), 21: (0x05, False),
    23: (0x06, False), 22: (0x07, False), 26: (0x08, False), 28: (0x09, False),
    25: (0x0A, False), 29: (0x0B, False), 27: (0x0C, False), 24: (0x0D, False),
    # Punctuation
    33: (0x1A, False), 30: (0x1B, False), 41: (0x27, False), 39: (0x28, False),
    42: (0x2B, False), 43: (0x33, False), 47: (0x34, False), 44: (0x35, False),
    50: (0x29, False),  # ` ~
    10: (0x56, False),  # ISO § key → the extra ISO key next to left Shift
    # Editing and whitespace
    36: (0x1C, False), 48: (0x0F, False), 49: (0x39, False), 51: (0x0E, False),
    53: (0x01, False), 117: (0x53, True), 114: (0x52, True),  # Help → Insert
    # Modifiers (Cmd is handled separately; see resolve_scan)
    56: (0x2A, False), 60: (0x36, False), 59: (0x1D, False), 62: (0x1D, True),
    58: (0x38, False), 61: (0x38, True), 57: (0x3A, False),
    110: (0x5D, True),  # context menu → Apps key
    # Navigation
    123: (0x4B, True), 124: (0x4D, True), 125: (0x50, True), 126: (0x48, True),
    115: (0x47, True), 119: (0x4F, True), 116: (0x49, True), 121: (0x51, True),
    # Function keys
    122: (0x3B, False), 120: (0x3C, False), 99: (0x3D, False), 118: (0x3E, False),
    96: (0x3F, False), 97: (0x40, False), 98: (0x41, False), 100: (0x42, False),
    101: (0x43, False), 109: (0x44, False), 103: (0x57, False), 111: (0x58, False),
    105: (0x64, False), 107: (0x65, False), 113: (0x66, False), 106: (0x67, False),
    64: (0x68, False), 79: (0x69, False), 80: (0x6A, False),
    # Keypad
    82: (0x52, False), 83: (0x4F, False), 84: (0x50, False), 85: (0x51, False),
    86: (0x4B, False), 87: (0x4C, False), 88: (0x4D, False), 89: (0x47, False),
    91: (0x48, False), 92: (0x49, False), 65: (0x53, False), 67: (0x37, False),
    69: (0x4E, False), 78: (0x4A, False), 75: (0x35, True), 76: (0x1C, True),
    81: (0x59, False), 71: (0x45, False),  # Clear → Num Lock
    # Volume keys (only sent as key codes when Fn is held on a Mac laptop)
    72: (0x30, True), 73: (0x2E, True), 74: (0x20, True),
}
MAC_CMD_L, MAC_CMD_R, MAC_CAPS_LOCK = 55, 54, 57

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member; it must be present so sizeof(INPUT)
    # matches what SendInput expects.
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT


def send_scan(scan, extended, down):
    flags = KEYEVENTF_SCANCODE
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if not down:
        flags |= KEYEVENTF_KEYUP
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def move_abs(nx, ny):
    """Move the cursor to (nx, ny) on the 0–65535 virtual-desktop scale."""
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx=nx, dy=ny, mouseData=0,
                          dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                          | MOUSEEVENTF_VIRTUALDESK,
                          time=0, dwExtraInfo=0)
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


# ---------------- displays ----------------

def set_dpi_aware():
    """Opt out of DPI virtualization so display coords are real pixels."""
    try:
        # Per-monitor v2 (Windows 10 1703+)
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


def monitor_rects():
    """Every display as (left, top, width, height) in physical pixels."""
    rects = []
    proto = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    callback_type = proto(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                          ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def collect(hmon, hdc, rect, data):
        r = rect.contents
        rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    try:
        _user32.EnumDisplayMonitors(None, None, callback_type(collect), 0)
    except Exception:
        pass
    if not rects:
        rects = [(0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1))]
    return rects


def desktop_layout(size_override=""):
    """Displays relative to the virtual desktop's top-left corner."""
    if size_override:
        w, h = (int(v) for v in size_override.lower().split("x"))
        return {"vw": w, "vh": h, "mons": [[0, 0, w, h]]}
    rects = monitor_rects()
    vx = min(r[0] for r in rects)
    vy = min(r[1] for r in rects)
    vw = max(r[0] + r[2] for r in rects) - vx
    vh = max(r[1] + r[3] for r in rects) - vy
    mons = sorted([r[0] - vx, r[1] - vy, r[2], r[3]] for r in rects)
    return {"vw": vw, "vh": vh, "mons": mons}


# ---------------- config ----------------

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


class Client:
    def __init__(self, args):
        self.args = args
        self.mouse = mouse.Controller()
        self.kb = keyboard.Controller()
        self.auth_key = P.derive_auth_key(args.key)
        self.tls_ctx = P.client_context()
        self.layout = desktop_layout(args.size)
        self.chan = None
        self.chan_lock = threading.Lock()
        # Track what we've actually pressed so "reset" releases only those —
        # a blind release injects stray "up" events (a lone right-up opens a
        # context menu; a lone Win-up opens the Start menu).
        self.pressed_buttons = set()
        self.pressed_keys = set()

        self.clip_lock = threading.Lock()
        self.clip_last_digest = None   # last clipboard content sent or received
        self.clip_own_seq = None       # sequence number of our own write

    # ---------------- key mapping ----------------

    def resolve_scan(self, mvk):
        if mvk in (MAC_CMD_L, MAC_CMD_R):
            right = mvk == MAC_CMD_R
            if self.args.swap_cmd_ctrl:
                return (0x1D, right)              # Ctrl
            return (0x5C if right else 0x5B, True)  # Windows key
        return MAC_TO_SCAN.get(mvk)

    def resolve_key(self, msg):
        name = msg.get("name")
        if name:
            if self.args.swap_cmd_ctrl and name in ("cmd", "cmd_l", "cmd_r"):
                return keyboard.Key.ctrl_l
            return getattr(keyboard.Key, name, None)
        char = msg.get("char")
        if char and char.isprintable():
            return char
        vk = msg.get("vk")
        if vk in MAC_VK_CHARS:
            return MAC_VK_CHARS[vk]
        return char or None

    # ---------------- event handling ----------------

    def handle(self, msg):
        t = msg.get("t")
        if t == "move":
            move_abs(int(msg["x"]), int(msg["y"]))
        elif t == "click":
            btn = BUTTONS.get(msg.get("b", 0), mouse.Button.left)
            if msg.get("d"):
                self.mouse.press(btn)
                self.pressed_buttons.add(btn)
            else:
                self.mouse.release(btn)
                self.pressed_buttons.discard(btn)
        elif t == "scroll":
            dy = msg.get("dy", 0)
            dx = msg.get("dx", 0)
            if self.args.invert_scroll:
                dy, dx = -dy, -dx
            self.mouse.scroll(dx, dy)
        elif t == "key":
            self.handle_key(msg)
        elif t == "reset":
            self.release_all()
        elif t == "ping":
            self.send({"t": "pong"})
        elif t == "clip":
            try:
                self.apply_clip(msg["fmt"], P.decompress(P.unb64(msg["data"])))
            except Exception as exc:
                print(f"[borderless] could not apply clipboard from the Mac: {exc}")
        elif t == "clipoffer":
            threading.Thread(target=self.fetch_clip, args=(msg.get("id"),),
                             daemon=True).start()

    def handle_key(self, msg):
        down = bool(msg.get("d"))
        scan = self.resolve_scan(msg["mvk"]) if "mvk" in msg else None
        if scan is not None:
            if msg["mvk"] == MAC_CAPS_LOCK:
                # macOS reports Caps Lock as "down" when it turns on and
                # "up" when it turns off; Windows toggles on every press,
                # so each Mac event becomes one full tap.
                send_scan(*scan, True)
                send_scan(*scan, False)
                return
            send_scan(*scan, down)
            if down:
                self.pressed_keys.add(scan)
            else:
                self.pressed_keys.discard(scan)
            return
        key = self.resolve_key(msg)
        if key is None:
            return
        try:
            if down:
                self.kb.press(key)
                self.pressed_keys.add(key)
            else:
                self.kb.release(key)
                self.pressed_keys.discard(key)
        except Exception:
            pass

    def release_all(self):
        # Release only what we're actually holding, so no stray up-events
        # (right-up → context menu, Win-up → Start menu) leak to Windows.
        for key in list(self.pressed_keys):
            try:
                if isinstance(key, tuple):
                    send_scan(*key, False)
                else:
                    self.kb.release(key)
            except Exception:
                pass
        self.pressed_keys.clear()
        for btn in list(self.pressed_buttons):
            try:
                self.mouse.release(btn)
            except Exception:
                pass
        self.pressed_buttons.clear()

    # ---------------- clipboard ----------------

    def clip_loop(self):
        last_seq = clipboard_sequence()
        while True:
            time.sleep(0.5)
            seq = clipboard_sequence()
            if seq == last_seq:
                continue
            try:
                item = clipboard_read()
            except ClipboardBusy:
                continue  # another app has it open; try again next round
            except Exception as exc:
                print(f"[borderless] could not read the clipboard: {exc}")
                last_seq = seq
                continue
            last_seq = seq
            with self.clip_lock:
                if seq == self.clip_own_seq:
                    continue
            if item is None:
                continue
            fmt, data = item
            if len(data) > P.MAX_CLIP_SIZE:
                print("[borderless] clipboard too large to share")
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
            # Too big for the main link: push it over a separate connection.
            threading.Thread(target=self.push_clip, args=(fmt, blob),
                             daemon=True).start()
        print(f"[borderless] clipboard → Mac ({fmt}, {P.human_size(len(data))})")

    def apply_clip(self, fmt, data):
        with self.clip_lock:
            self.clip_last_digest = P.clip_digest(fmt, data)
            self.clip_own_seq = clipboard_write(fmt, data)
        print(f"[borderless] clipboard ← Mac ({fmt}, {P.human_size(len(data))})")

    def data_connection(self):
        chan = P.connect_tls(self.args.server, self.args.port, self.tls_ctx)
        try:
            P.authenticate_client(chan, self.auth_key, {"role": "data"})
        except Exception:
            chan.close()
            raise
        return chan

    def fetch_clip(self, offer_id):
        chan = None
        try:
            chan = self.data_connection()
            chan.send({"t": "fetch", "id": offer_id})
            header = chan.recv_msg(timeout=30)
            if not header.get("ok"):
                return
            size = int(header["size"])
            if not 0 < size <= P.MAX_CLIP_SIZE:
                return
            blob = chan.recv_exact(size, timeout=120)
            chan.send({"t": "done"})
            self.apply_clip(header["fmt"], P.decompress(blob))
        except Exception as exc:
            print(f"[borderless] clipboard download failed: {exc}")
        finally:
            if chan is not None:
                chan.close()

    def push_clip(self, fmt, blob):
        chan = None
        try:
            chan = self.data_connection()
            chan.send({"t": "push", "fmt": fmt, "size": len(blob)})
            chan.send_bytes(blob, timeout=120)
            chan.recv_msg(timeout=60)  # "done": the Mac has everything
        except Exception as exc:
            print(f"[borderless] clipboard upload failed: {exc}")
        finally:
            if chan is not None:
                chan.close()

    # ---------------- connection ----------------

    def send(self, msg):
        with self.chan_lock:
            chan = self.chan
        if chan is None:
            return
        try:
            chan.send(msg, timeout=5.0)
        except (OSError, ConnectionError):
            chan.close()

    def screens_loop(self):
        """Tell the Mac when displays are added, removed, or rearranged."""
        while True:
            time.sleep(3.0)
            if self.args.size:
                continue
            try:
                layout = desktop_layout()
            except Exception:
                continue
            if layout != self.layout:
                self.layout = layout
                print(f"[borderless] displays changed: {layout['vw']}x{layout['vh']} "
                      f"on {len(layout['mons'])} display(s)")
                self.send({"t": "screens", **layout})

    def run(self):
        print(f"[borderless] Windows desktop: {self.layout['vw']}x{self.layout['vh']} "
              f"on {len(self.layout['mons'])} display(s)")
        threading.Thread(target=self.clip_loop, daemon=True).start()
        threading.Thread(target=self.screens_loop, daemon=True).start()
        while True:
            try:
                self.connect_once()
            except P.AuthError as exc:
                print(f"[borderless] pairing failed: {exc}")
                print("[borderless] check the key: on the Mac, run "
                      "server_mac.py --show-key, then start this client "
                      "with --key <key>")
                sys.exit(2)
            except socket.timeout:
                print(f"[borderless] no heartbeat from the Mac for "
                      f"{P.HEARTBEAT_TIMEOUT:.0f}s; reconnecting in 3s")
            except ssl.SSLError as exc:
                print(f"[borderless] TLS failed ({exc}). Is server_mac.py up to "
                      f"date? Retrying in 3s")
            except (OSError, ConnectionError) as exc:
                print(f"[borderless] connection lost ({exc}); retrying in 3s")
            with self.chan_lock:
                chan, self.chan = self.chan, None
            if chan is not None:
                chan.close()
            # Don't leave keys or buttons held down if the link died mid-press.
            self.release_all()
            time.sleep(3)

    def connect_once(self):
        print(f"[borderless] connecting to {self.args.server}:{self.args.port} ...")
        chan = P.connect_tls(self.args.server, self.args.port, self.tls_ctx)
        try:
            P.authenticate_client(chan, self.auth_key,
                                  {"role": "main", **self.layout})
        except Exception:
            chan.close()
            raise
        with self.chan_lock:
            self.chan = chan
        print("[borderless] connected and paired (TLS) — waiting for the Mac "
              "to hand over control")
        while True:
            self.handle(chan.recv_msg(timeout=P.HEARTBEAT_TIMEOUT))


def main():
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("server", nargs="?", default=cfg.get("server"),
                   help="IP or hostname of the Mac (saved for next time)")
    p.add_argument("--key", default=cfg.get("key"),
                   help="pairing key shown by server_mac.py (saved for next time)")
    p.add_argument("--port", type=int, default=cfg.get("port", 24801),
                   help="TCP port (default 24801)")
    p.add_argument("--invert-scroll", action="store_true",
                   help="flip scroll direction if it feels backwards")
    p.add_argument("--size", default="",
                   help="override the detected desktop, e.g. --size 1920x1080")
    p.add_argument("--no-swap-cmd-ctrl", dest="swap_cmd_ctrl", action="store_false",
                   help="send Mac Cmd as the Windows key instead of Ctrl")
    p.add_argument("--token", help=argparse.SUPPRESS)
    p.set_defaults(swap_cmd_ctrl=True)
    args = p.parse_args()

    if args.token:
        p.error("--token is replaced by the pairing key: use --key <key> "
                "(run server_mac.py --show-key on the Mac to see it)")
    if not args.server:
        p.error("give the Mac's IP address or hostname, for example: "
                "python client_windows.py 192.168.1.20 --key <key>")
    if not args.key or not P.key_is_valid(args.key):
        p.error("a valid pairing key is required: --key <key> "
                "(run server_mac.py --show-key on the Mac to see it)")
    args.key = P.format_key(args.key)
    save_config({"server": args.server, "key": args.key, "port": args.port})

    set_dpi_aware()
    Client(args).run()


if __name__ == "__main__":
    main()
