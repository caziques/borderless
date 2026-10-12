#!/usr/bin/env python3
"""Borderless client — run on the Windows machine.

Connects to the Mac server, receives mouse/keyboard events, and injects them
locally. Also syncs clipboard text in both directions. Reconnects automatically
if the connection drops.
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import socket
import threading
import time

from pynput import keyboard, mouse

HEARTBEAT_TIMEOUT = 4.0   # seconds of silence from the Mac before reconnecting

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]


def clipboard_get():
    """Read clipboard text, or None if the clipboard is busy/unavailable."""
    if not _user32.OpenClipboard(0):
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def clipboard_set(text):
    if not _user32.OpenClipboard(0):
        return
    try:
        _user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return
        ptr = _kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, buf, size)
        _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)
    finally:
        _user32.CloseClipboard()

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

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008


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

def set_dpi_aware():
    """Opt out of DPI virtualization so cursor coords are real pixels."""
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


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmPositionX", ctypes.c_long),
        ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", ctypes.c_ulong),
        ("dmDisplayFixedOutput", ctypes.c_ulong),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    ]


def screen_size():
    """Physical resolution of the primary display, immune to DPI scaling."""
    ENUM_CURRENT_SETTINGS = -1
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    try:
        if _user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS,
                                        ctypes.byref(dm)) and dm.dmPelsWidth:
            return int(dm.dmPelsWidth), int(dm.dmPelsHeight)
    except Exception:
        pass
    return _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


class Client:
    def __init__(self, args):
        self.args = args
        self.mouse = mouse.Controller()
        self.kb = keyboard.Controller()
        if args.size:
            self.w, self.h = (int(v) for v in args.size.lower().split("x"))
        else:
            self.w, self.h = screen_size()
        self.sock = None
        self.sock_lock = threading.Lock()
        self.send_lock = threading.Lock()   # pong and clipboard share the socket
        self.last_clip_sent = None
        self.last_clip_received = None
        # Track what we've actually pressed so "reset" releases only those —
        # a blind release injects stray "up" events (a lone right-up opens a
        # context menu; a lone Win-up opens the Start menu).
        self.pressed_buttons = set()
        self.pressed_keys = set()

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
            self.mouse.position = (msg["x"], msg["y"])
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
        elif t == "reset":
            self.release_all()
        elif t == "ping":
            self.send({"t": "pong"})
        elif t == "clip":
            text = msg.get("text", "")
            if text != self.last_clip_received:
                self.last_clip_received = text
                self.last_clip_sent = text
                try:
                    clipboard_set(text)
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
        try:
            self.last_clip_sent = clipboard_get()
        except Exception:
            pass
        while True:
            time.sleep(1.0)
            try:
                text = clipboard_get()
            except Exception:
                continue
            if text is None or text == self.last_clip_sent:
                continue
            self.last_clip_sent = text
            if text != self.last_clip_received:
                self.send({"t": "clip", "text": text})

    def send(self, msg):
        with self.sock_lock:
            sock = self.sock
        if sock is None:
            return
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        try:
            with self.send_lock:
                sock.sendall(data)
        except OSError:
            pass

    # ---------------- lifecycle ----------------

    def run(self):
        print(f"[borderless] windows screen: {self.w}x{self.h}")
        threading.Thread(target=self.clip_loop, daemon=True).start()
        while True:
            try:
                self.connect_once()
            except socket.timeout:
                print(f"[borderless] no heartbeat from the Mac for "
                      f"{HEARTBEAT_TIMEOUT:.0f}s; reconnecting in 3s")
            except (OSError, ConnectionError) as exc:
                print(f"[borderless] connection lost ({exc}); retrying in 3s")
            with self.sock_lock:
                sock, self.sock = self.sock, None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            # Don't leave keys or buttons held down if the link died mid-press.
            self.release_all()
            time.sleep(3)

    def connect_once(self):
        print(f"[borderless] connecting to {self.args.server}:{self.args.port} ...")
        sock = socket.create_connection((self.args.server, self.args.port), timeout=10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)
        with self.sock_lock:
            self.sock = sock
        self.send({"t": "hello", "token": self.args.token,
                   "w": self.w, "h": self.h, "hb": 1})
        print("[borderless] connected — waiting for the Mac to hand over control")
        f = sock.makefile("r", encoding="utf-8", newline="\n")
        heartbeat_seen = False
        for line in f:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("t") == "ping" and not heartbeat_seen:
                # Only enforce the read timeout once the server proves it
                # sends pings, so an older server doesn't cause reconnect loops.
                heartbeat_seen = True
                sock.settimeout(HEARTBEAT_TIMEOUT)
            self.handle(msg)
        raise ConnectionError("server closed the connection")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("server", help="IP or hostname of the Mac running server_mac.py")
    p.add_argument("--port", type=int, default=24801, help="TCP port (default 24801)")
    p.add_argument("--token", default="", help="shared secret; must match the server")
    p.add_argument("--invert-scroll", action="store_true",
                   help="flip scroll direction if it feels backwards")
    p.add_argument("--size", default="",
                   help="override detected screen size, e.g. --size 1920x1080")
    p.add_argument("--no-swap-cmd-ctrl", dest="swap_cmd_ctrl", action="store_false",
                   help="send Mac Cmd as the Windows key instead of Ctrl")
    p.set_defaults(swap_cmd_ctrl=True)
    args = p.parse_args()
    set_dpi_aware()
    Client(args).run()


if __name__ == "__main__":
    main()
