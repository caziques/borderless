# Borderless

A Mouse Without Borders–style software KVM for a **Mac and Windows** pair: one
keyboard and mouse, attached to the Mac, control both machines. When you move
the cursor off the edge of the Mac screen, it appears on the Windows screen.
Keyboard input and the clipboard (text and images) follow it.

```
┌───────────────┐   your LAN, TLS-encrypted     ┌─────────────────┐
│  Mac          │ ────────────────────────────► │  Windows        │
│ server_mac.py │   mouse, keys, clipboard      │ client_windows. │
│ (captures)    │ ◄──────────────────────────── │ py (injects)    │
└───────────────┘   clipboard back              └─────────────────┘
```

The keyboard and mouse must be attached to the **Mac**, because that's where
input is captured. The Windows machine only injects events.

## Set up the Mac (server)

1. Install and start the server:

   ```bash
   cd borderless
   python3 -m venv .venv
   .venv/bin/pip install -r requirements-mac.txt
   .venv/bin/python server_mac.py --win-side left
   ```

   Use `--win-side left` or `--win-side right` for the side of the Mac the
   Windows machine is on.

2. On first run, the server creates a TLS certificate and a **pairing key**,
   and prints the key:

   ```
   [borderless] Pairing key — enter it on the Windows machine:
   [borderless]     s3ub-2pnh-8pgk-ztnp-gw8y-cktv
   ```

   To see the key again later, run `server_mac.py --show-key`.

3. macOS asks for permissions on first run. Grant your terminal app both of
   these, then restart the server:

   - **System Settings > Privacy & Security > Accessibility**
   - **System Settings > Privacy & Security > Input Monitoring**

   If macOS asks whether the app can accept incoming network connections,
   allow it.

4. Find the Mac's address with `ipconfig getifaddr en0`, or its hostname with
   `scutil --get LocalHostName` (use it as `<name>.local`).

## Set up Windows (client)

1. Install Python 3 from python.org, and select **Add python.exe to PATH**
   during installation.
2. Download the client and its protocol module into a folder:

   ```
   curl -O https://raw.githubusercontent.com/caziques/borderless/main/client_windows.py
   curl -O https://raw.githubusercontent.com/caziques/borderless/main/protocol.py
   ```

3. Install the one dependency:

   ```
   python -m pip install pynput
   ```

4. Start the client with the Mac's address and the pairing key:

   ```
   python client_windows.py <MAC_ADDRESS> --key <PAIRING_KEY>
   ```

   The client saves both to `client_config.json`, so later you can start it
   with `python client_windows.py`.

Windows needs no inbound firewall rule, because the client connects out to the
Mac.

## Use it

- **Switch to Windows:** push the cursor off the Mac edge you chose with
  `--win-side`. That edge is saved in `config.json`.
- **Switch back to the Mac:** push the cursor off the Windows edge that faces
  the Mac.
- **Toggle hotkey:** `Ctrl+Alt+Q` switches control at any time. Use it if the
  cursor is ever on the wrong side.
- **Multiple displays:** both sides support them. On the Mac, the cursor moves
  between your displays normally and hands off only at the outer edge. On
  Windows, the cursor moves across all displays, and it returns to the Mac
  only from the display edge that faces the Mac.
- **Clipboard:** copy text or an image on either machine, and paste it on the
  other. If the clipboard holds both text and an image, the text is sent.
- **Cmd key:** while you control Windows, Mac `Cmd` acts as `Ctrl`, so
  Cmd+C and Cmd+V work. To send the Windows key instead, start the client
  with `--no-swap-cmd-ctrl`.
- **Option key:** acts as `Alt`, so Option+Tab is Alt+Tab.
- **Keyboard layout:** keys are sent as physical key positions (scan codes),
  and Windows applies its own keyboard layout. Set the Windows layout to match
  your Mac keyboard.
- **Dropped connections:** the two machines exchange a heartbeat every second.
  If the network drops while Windows has control, the Mac takes control back
  after about 4 seconds, and Windows releases any held keys or mouse buttons.
  The client reconnects on its own.

## Options

Server (`server_mac.py`):

| Flag | Default | Meaning |
|---|---|---|
| `--win-side` | right | Side of the Mac the Windows machine is on |
| `--port` | 24801 | TCP port to listen on |
| `--speed` | 1.0 | Mouse speed multiplier on the Windows screen |
| `--show-key` | | Print the saved pairing key |
| `--new-key` | | Generate a new pairing key |
| `--key` | | Use a specific pairing key |

Client (`client_windows.py`):

| Flag | Default | Meaning |
|---|---|---|
| `--key` | saved | Pairing key from the Mac |
| `--port` | 24801 | Must match the server |
| `--invert-scroll` | off | Reverse the scroll direction |
| `--no-swap-cmd-ctrl` | off | Send Cmd as the Windows key instead of Ctrl |
| `--size` | detected | Override the detected desktop size, for example `1920x1080` |

## Security

- **Encryption:** all traffic uses TLS 1.2 or later, including keystrokes and
  clipboard contents.
- **Pairing:** the Mac's certificate is self-signed. Instead of a certificate
  authority, both machines prove they know the pairing key with an HMAC that
  also covers the certificate's fingerprint. A machine in the middle presents
  a different certificate, so the proofs don't match and the connection is
  refused.
- **Pairing key:** 24 random characters, about 119 bits. If you think someone
  has seen it, run `server_mac.py --new-key` and update the client.
- **Secrets on disk:** `config.json` (pairing key), `client_config.json`,
  `tls_key.pem`, and `tls_cert.pem` stay on your machines and are excluded
  from Git.

## How it works

- **Mouse:** the Mac sends cursor positions on a 0–65535 scale across the
  whole Windows desktop, the scale Windows' `SendInput` uses. This makes
  positions independent of resolution and display scaling.
- **Keyboard:** the Mac sends physical macOS key codes, which the client maps
  to Windows scan codes.
- **Clipboard:** data is compressed. Items up to 1 MB compressed travel on the
  main connection. Larger items go over a separate short-lived connection, so
  they never delay mouse and keyboard input. Images travel in the Windows DIB
  format, and the Mac converts them to and from PNG and TIFF.

## Limitations

- Keyboard and mouse input goes one way: the Mac's devices control both
  machines.
- The clipboard is limited to text and images up to 100 MB. Files and drag
  and drop between machines aren't supported.
- Image transparency isn't kept between machines.
- Injected input can't reach elevated (administrator) windows on Windows
  unless the client runs as administrator.
- Media keys (volume, play/pause) arrive as Windows media keys. The Mac `fn`
  key has no Windows equivalent and isn't forwarded.
- On ISO keyboards, the `§` key maps to the extra ISO key next to left Shift.
- Version 2 (TLS) doesn't work with older clients or servers. Update both
  machines together.
