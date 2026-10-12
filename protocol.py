"""Borderless wire protocol, shared by the Mac server and the Windows client.

Every connection is TLS. The Mac's certificate is self-signed, so TLS alone
can't tell the client who it is talking to; instead both sides prove they know
the pairing key with an HMAC that also covers the server certificate's
fingerprint. A machine in the middle presents a different certificate, so its
proofs never match, and it can't forge new ones without the key.

Messages are JSON lines. Bulk data (large clipboard contents) follows a JSON
header line as raw bytes, on a separate short-lived connection so it never
delays mouse and keyboard traffic on the main one.
"""

import base64
import hashlib
import hmac
import json
import secrets
import select
import socket
import ssl
import threading
import time
import zlib

PROTOCOL_VERSION = 2

HEARTBEAT_INTERVAL = 1.0     # seconds between pings from the Mac
HEARTBEAT_TIMEOUT = 4.0      # silence after which either side drops the link
AUTH_TIMEOUT = 10.0

INLINE_CLIP_LIMIT = 1024 * 1024          # compressed bytes sent on the main link
MAX_CLIP_SIZE = 100 * 1024 * 1024        # uncompressed clipboard size cap

# Lowercase letters and digits, minus look-alikes (0/o, 1/l/i). 24 characters
# give about 119 bits of entropy and are easy to type on either machine.
KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
KEY_LENGTH = 24


class AuthError(Exception):
    pass


# ---------------- pairing key ----------------

def generate_key():
    raw = "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))
    return format_key(raw)


def normalize_key(key):
    return "".join(ch for ch in key.lower() if ch.isalnum())


def format_key(key):
    k = normalize_key(key)
    return "-".join(k[i:i + 4] for i in range(0, len(k), 4))


def key_is_valid(key):
    k = normalize_key(key or "")
    return len(k) >= 16 and all(ch in KEY_ALPHABET for ch in k)


def derive_auth_key(key):
    return hashlib.pbkdf2_hmac("sha256", normalize_key(key).encode(),
                               b"borderless-pairing-v2", 200_000)


def _proof(auth_key, role, ns, nc, cert_fp):
    return hmac.new(auth_key, role + ns + nc + cert_fp, hashlib.sha256).hexdigest()


def cert_fingerprint(der):
    return hashlib.sha256(der).digest()


# ---------------- TLS channel ----------------

class Channel:
    """A TLS connection safe to read and write from different threads.

    OpenSSL forbids using one SSL object from two threads at once, so the
    socket is non-blocking and every SSL call runs under a lock. Threads wait
    for readiness with select() outside the lock, so a reader waiting for data
    never holds up a writer.
    """

    def __init__(self, tls_sock):
        self.sock = tls_sock
        self.peer_cert = tls_sock.getpeercert(binary_form=True)
        self.sock.setblocking(False)
        self.lock = threading.Lock()        # guards every SSL call
        self.send_lock = threading.Lock()   # keeps whole messages together
        self.buf = bytearray()
        self.closed = False

    def _wait(self, for_write, deadline):
        if self.closed:
            raise ConnectionError("connection closed")
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise socket.timeout("timed out")
        try:
            fd = self.sock.fileno()
            if fd < 0:
                raise ConnectionError("connection closed")
            if for_write:
                _, ready, _ = select.select([], [fd], [], remaining)
            else:
                ready, _, _ = select.select([fd], [], [], remaining)
        except (ValueError, OSError):
            raise ConnectionError("connection closed")
        if not ready and deadline is not None and time.monotonic() >= deadline:
            raise socket.timeout("timed out")

    def send_bytes(self, data, timeout=5.0):
        with self.send_lock:
            self._send_all(memoryview(data), timeout)

    def _send_all(self, view, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        while view:
            with self.lock:
                if self.closed:
                    raise ConnectionError("connection closed")
                try:
                    sent = self.sock.send(view[:256 * 1024])
                    view = view[sent:]
                    continue
                except ssl.SSLWantWriteError:
                    for_write = True
                except ssl.SSLWantReadError:
                    for_write = False
            self._wait(for_write, deadline)

    def send(self, msg, timeout=5.0):
        self.send_bytes((json.dumps(msg, separators=(",", ":")) + "\n").encode(),
                        timeout)

    def _fill(self, deadline):
        with self.lock:
            if self.closed:
                raise ConnectionError("connection closed")
            try:
                chunk = self.sock.recv(256 * 1024)
                if not chunk:
                    raise ConnectionError("connection closed by peer")
                self.buf += chunk
                return
            except ssl.SSLWantReadError:
                for_write = False
            except ssl.SSLWantWriteError:
                for_write = True
            except ssl.SSLZeroReturnError:
                raise ConnectionError("connection closed by peer")
        self._wait(for_write, deadline)

    def recv_msg(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                line = bytes(self.buf[:nl])
                del self.buf[:nl + 1]
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    return msg
                continue
            if len(self.buf) > 4 * INLINE_CLIP_LIMIT:
                raise ConnectionError("message too large")
            self._fill(deadline)

    def recv_exact(self, n, timeout=60.0):
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self.buf) < n:
            self._fill(deadline)
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass


def server_context(cert_path, key_path):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def client_context():
    # The certificate is self-signed; identity comes from the pairing-key
    # proofs, which cover its fingerprint (see authenticate_client).
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def accept_tls(raw_sock, ctx):
    raw_sock.settimeout(AUTH_TIMEOUT)
    raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return Channel(ctx.wrap_socket(raw_sock, server_side=True))


def connect_tls(host, port, ctx):
    raw = socket.create_connection((host, port), timeout=AUTH_TIMEOUT)
    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        return Channel(ctx.wrap_socket(raw))
    except Exception:
        raw.close()
        raise


# ---------------- handshake ----------------

def authenticate_server(chan, auth_key, own_cert_der):
    """Run the server half of the handshake. Returns the client's hello."""
    ns = secrets.token_bytes(16)
    chan.send({"t": "auth", "v": PROTOCOL_VERSION, "ns": ns.hex()})
    hello = chan.recv_msg(timeout=AUTH_TIMEOUT)
    if hello.get("t") != "hello":
        raise AuthError("expected hello")
    try:
        nc = bytes.fromhex(hello.get("nc", ""))
    except ValueError:
        raise AuthError("malformed hello")
    fp = cert_fingerprint(own_cert_der)
    expected = _proof(auth_key, b"client", ns, nc, fp)
    if len(nc) != 16 or not hmac.compare_digest(expected, str(hello.get("proof", ""))):
        try:
            chan.send({"t": "denied"})
        except (OSError, ConnectionError):
            pass
        raise AuthError("wrong pairing key")
    chan.send({"t": "welcome", "proof": _proof(auth_key, b"server", ns, nc, fp)})
    return hello


def authenticate_client(chan, auth_key, hello_fields):
    """Run the client half of the handshake with the given hello fields."""
    auth = chan.recv_msg(timeout=AUTH_TIMEOUT)
    if auth.get("t") != "auth":
        raise AuthError("unexpected greeting from server")
    if auth.get("v") != PROTOCOL_VERSION:
        raise AuthError(f"server speaks protocol v{auth.get('v')}, "
                        f"this client speaks v{PROTOCOL_VERSION}")
    ns = bytes.fromhex(auth["ns"])
    nc = secrets.token_bytes(16)
    fp = cert_fingerprint(chan.peer_cert)
    hello = dict(hello_fields, t="hello", v=PROTOCOL_VERSION, nc=nc.hex(),
                 proof=_proof(auth_key, b"client", ns, nc, fp))
    chan.send(hello)
    reply = chan.recv_msg(timeout=AUTH_TIMEOUT)
    if reply.get("t") == "denied":
        raise AuthError("the Mac rejected the pairing key")
    expected = _proof(auth_key, b"server", ns, nc, fp)
    if reply.get("t") != "welcome" or not hmac.compare_digest(
            expected, str(reply.get("proof", ""))):
        raise AuthError("the server could not prove it knows the pairing key "
                        "(wrong key, or something is intercepting the connection)")


# ---------------- clipboard payloads ----------------

def compress(data):
    return zlib.compress(data, 6)


def decompress(blob):
    d = zlib.decompressobj()
    out = d.decompress(blob, MAX_CLIP_SIZE + 1)
    if len(out) > MAX_CLIP_SIZE or d.unconsumed_tail:
        raise ValueError("clipboard data too large")
    return out


def b64(data):
    return base64.b64encode(data).decode("ascii")


def unb64(text):
    return base64.b64decode(text)


def human_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def clip_digest(fmt, data):
    return hashlib.sha256(fmt.encode() + b"\0" + data).digest()


def dib_to_bmp(dib):
    """Prefix a Windows DIB with a BMP file header."""
    header_size = int.from_bytes(dib[0:4], "little")
    bpp = int.from_bytes(dib[14:16], "little")
    compression = int.from_bytes(dib[16:20], "little")
    colors_used = int.from_bytes(dib[32:36], "little")
    masks = 0
    if header_size == 40:
        if compression == 3:      # BI_BITFIELDS: three masks follow the header
            masks = 12
        elif compression == 6:    # BI_ALPHABITFIELDS: four masks
            masks = 16
    colors = colors_used or (1 << bpp if bpp <= 8 else 0)
    offset = 14 + header_size + masks + colors * 4
    return (b"BM" + (14 + len(dib)).to_bytes(4, "little") + b"\0\0\0\0"
            + offset.to_bytes(4, "little") + dib)


def make_dib_opaque(dib):
    """Set alpha to 255 in a 32-bit BI_RGB DIB.

    Windows leaves that byte undefined (usually 0), and macOS would read 0
    as fully transparent.
    """
    header_size = int.from_bytes(dib[0:4], "little")
    bpp = int.from_bytes(dib[14:16], "little")
    compression = int.from_bytes(dib[16:20], "little")
    if header_size != 40 or bpp != 32 or compression != 0:
        return dib
    out = bytearray(dib)
    pixels = out[header_size:]
    pixels[3::4] = b"\xff" * len(pixels[3::4])
    out[header_size:] = pixels
    return bytes(out)
