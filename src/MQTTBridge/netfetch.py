"""One GET, bounded in time and size, that never follows a redirect.

Shared by the update check, which runs inside enigma2, and the update helper
(`updatehelper.py`), which runs outside it as a copy in the transaction directory - so this
module is standard library only, 3.9-safe, and imports nothing of the plugin's: the helper
copies it beside itself and imports it from there, and a file the package manager is about to
replace must never be what the helper is still reading.

Two kinds of address, one rule each:

- **The origin** (`https_get`): TLS with a context built for the call -
  `ssl.create_default_context()`, asserted to verify the certificate and the host name - so a
  third-party plugin that switched verification off for the whole process (by replacing
  `ssl._create_default_https_context`) cannot switch it off here.
- **The relay** (`relay_get`): the companion integration's short-lived address on the local
  network. Plain HTTP is allowed there and only there, and HTTPS without certificate
  verification, because the relay is a courier and not an authority: every byte it serves is
  checked against a signed index the receiver verified itself before anything uses it. The
  worst a relay can do is make the install fail.

`http.client` is used directly, so a redirect is an answer that is refused, never followed. Each
body is read with a ceiling of one byte past its cap, so an answer with no end is never read
whole and one byte too many is visible to the caller before anything is parsed.
"""

import http.client
import socket
import threading
from urllib.parse import urlsplit


class Unreachable(Exception):
    """The address could not be asked: no route, no answer in time, or TLS that did not verify."""


def verified_context():
    """A TLS context for one request that verifies the certificate and the host name."""
    import ssl

    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise Unreachable("this Python's default TLS context does not verify certificates")
    return context


def unverified_context():
    """A TLS context for the relay only: encrypted, not authenticated (see the module)."""
    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _get(factory, host, port, path, cap, timeout, context=None):
    expired = threading.Event()
    held = {}

    def expire():
        expired.set()
        sock = getattr(held.get("connection"), "sock", None)
        if sock is not None:
            try:
                # The plain socket's shutdown, under a TLS socket too: it wakes the blocked read
                # without the TLS layer being torn down from a second thread.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except (OSError, TypeError, ValueError):
                pass

    watchdog = threading.Timer(timeout, expire)
    watchdog.daemon = True
    try:
        if context is None:
            connection = held["connection"] = factory(host, port, timeout=timeout)
        else:
            connection = held["connection"] = factory(host, port, timeout=timeout,
                                                      context=context)
        watchdog.start()
        try:
            # Connected here, not inside `request`: a watchdog that fires while the connection is
            # still being made has no socket to shut down, so the deadline is asked again before
            # a byte is sent. Resolving the name is the one part nothing here can interrupt.
            connection.connect()
            if expired.is_set():
                raise Unreachable(f"no connection within {timeout} s")
            connection.request("GET", path or "/", headers={
                "User-Agent": "enigma2-mqtt-bridge",
                "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            # A bytearray: an eight-megabyte package read in chunks into a `bytes` would be
            # copied whole once per chunk.
            body = bytearray()
            while len(body) <= cap:
                chunk = response.read(min(65536, cap + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if expired.is_set():
                raise Unreachable(f"no whole answer within {timeout} s")
            return response.status, bytes(body)
        finally:
            watchdog.cancel()
            connection.close()
    except Unreachable:
        raise
    except (OSError, ValueError, http.client.HTTPException) as error:
        if expired.is_set():
            raise Unreachable(f"no whole answer within {timeout} s") from error
        raise Unreachable(str(error)) from error


def https_get(url, cap, timeout, connection_factory=None):
    """`(status, body)` of one verified GET of `url`, the body at most `cap + 1` bytes.

    Never follows a redirect: a 3xx is returned as the status it is. `timeout` bounds the whole
    exchange - connection, request, response headers and body. A socket timeout alone bounds each
    read, and a peer that sends one header line a second never trips it; so a watchdog shuts the
    socket down at the deadline, whatever the exchange is waiting for, and the blocked read ends;
    and a connection that took longer than the deadline to make is dropped before the request is
    sent. Resolving the host name is not bounded by it: the resolver cannot be interrupted.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("only an https address is fetched, not " + url)
    factory = connection_factory or http.client.HTTPSConnection
    return _get(factory, parts.hostname, parts.port or 443, parts.path, cap, timeout,
                context=verified_context())


def relay_get(url, cap, timeout):
    """`(status, body)` of one GET of a relay address - `http`, or `https` unverified.

    Only for the companion integration's relay: what it returns is checked against the signed
    index before it is used, so neither the channel nor the server is trusted here.
    """
    parts = urlsplit(url)
    if parts.scheme == "http" and parts.hostname:
        path = parts.path + ("?" + parts.query if parts.query else "")
        return _get(http.client.HTTPConnection, parts.hostname, parts.port or 80, path, cap,
                    timeout)
    if parts.scheme == "https" and parts.hostname:
        path = parts.path + ("?" + parts.query if parts.query else "")
        return _get(http.client.HTTPSConnection, parts.hostname, parts.port or 443, path, cap,
                    timeout, context=unverified_context())
    raise ValueError("a relay address is http or https")
