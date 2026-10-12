# Borderless

A minimal Mouse Without Borders–style KVM bridge for a **Mac + Windows** pair:
one keyboard and mouse (attached to the Mac) controls both machines. Move the
cursor off the edge of the Mac screen and it appears on the Windows screen;
keyboard input and clipboard text follow it.

```
┌──────────────┐   your LAN (TCP, JSON lines)   ┌────────────────┐
│  Mac          │ ─────────────────────────────► │  Windows        │
│ server_mac.py │   mouse / keys / clipboard     │ client_windows. │
│ (captures)    │ ◄───────────────────────────── │ py (injects)    │
└──────────────┘        clipboard back           └────────────────┘
```

The keyboard/mouse must be physically attached to the **Mac** — capture runs
there. The Windows machine only injects events.

## Setup — Mac (server)

```bash
cd borderless
python3 -m venv .venv
.venv/bin/pip install -r requirements-mac.txt
.venv/bin/python server_mac.py --token pick-a-secret
```

On first run macOS will ask for permissions. Grant your terminal app both:

- **System Settings → Privacy & Security → Accessibility**
- **System Settings → Privacy & Security → Input Monitoring**

then restart the server. When macOS asks whether the app may accept incoming
network connections, allow it.

Find the Mac's LAN IP with `ipconfig getifaddr en0`.

## Setup — Windows (client)

Install Python 3 from python.org (tick "Add to PATH"), then:

```
cd borderless
python -m pip install pynput
python client_windows.py <MAC_IP> --token pick-a-secret
```

(`pynput` is the only dependency — clipboard access uses the built-in Windows
API. Use `python -m pip` rather than bare `pip` so the package lands in the
same Python you run the script with.)

No inbound firewall rule is needed on Windows — the client dials out to the
Mac.

## Using it

- **Which side is Windows on?** Set it at launch with `--win-side left` or
  `--win-side right` (default `right`). The chosen side is saved to
  `config.json`, so later runs reuse it even without the flag.
- **Switch to Windows:** push the cursor off that edge of the Mac screen.
- **Switch back to the Mac:** push the cursor off the opposite (left) edge of
  the Windows screen.
- **Panic/toggle hotkey:** `Ctrl+Alt+Q` flips control at any time — use it if
  the cursor ever gets stuck on the wrong side.
- **Clipboard:** copy text on either machine, paste on the other (text only).
- **Cmd key:** while controlling Windows, Mac `Cmd` acts as `Ctrl` so
  Cmd+C/Cmd+V work as expected. Pass `--no-swap-cmd-ctrl` on the client to get
  the Windows key instead.
- **Option key:** acts as `Alt`, so Option+Tab is Alt+Tab and Option+F4 is
  Alt+F4.
- **Keyboard layout:** keys are sent as physical key positions (scan codes),
  and Windows applies its own keyboard layout. Set the Windows layout to match
  your Mac keyboard.
- **Dropped connections:** the Mac and Windows machines exchange a heartbeat
  every second. If the network drops while Windows has control, the Mac takes
  control back after about 4 seconds, and Windows releases any keys or mouse
  buttons that were held down. The client then reconnects on its own.

## Options

Server (`server_mac.py`):

| Flag | Default | Meaning |
|---|---|---|
| `--port` | 24801 | TCP port to listen on |
| `--win-side` | right | which side of the Mac the Windows machine sits on |
| `--token` | (none) | shared secret the client must present |
| `--speed` | 1.0 | mouse speed multiplier on the Windows screen |

Client (`client_windows.py`):

| Flag | Default | Meaning |
|---|---|---|
| `--port` | 24801 | must match the server |
| `--token` | (none) | must match the server |
| `--invert-scroll` | off | flip scroll direction if it feels backwards |
| `--no-swap-cmd-ctrl` | off | send Cmd as the Windows key instead of Ctrl |

## Security notes

- Traffic is **not encrypted**. Run it only on a trusted home LAN, or better,
  over a [Tailscale](https://tailscale.com) connection between the two
  machines (then use the Tailscale IP as `<MAC_IP>`).
- Always set `--token`; without it, anything on the network that can reach the
  port can receive your keystrokes' destination and send clipboard text.

## Limitations

- One direction: keyboard/mouse on the Mac drive both machines (like Mouse
  Without Borders's primary machine).
- Multiple **Mac** displays work: the cursor travels across them and only hands
  off at the outer edge, returning to the same display on the way back. The
  **Windows** side uses its primary display only.
- Text clipboard only — no files or images, no drag-and-drop across machines.
- Injected input can't reach elevated (UAC/admin) windows on Windows unless
  the client itself runs as administrator.
- Media keys (volume, play/pause) are forwarded as Windows media keys. The Mac
  `fn` key has no Windows equivalent and is not forwarded.
- On ISO keyboards, the `§` key maps to the extra ISO key next to left Shift.
- Heartbeat and scan codes need both sides updated. A mix of old and new
  versions still works, falling back to the old behavior.
