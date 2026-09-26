"""Ed25519 signature verification in pure Python - verify only, never sign.

**Why the plugin carries its own.** What the receiver installs as root is decided by a release index
signed with Ed25519 ([ADR-0015](../../docs/adr/0015-signed-self-update.md), decision 2), and the
receiver's package manager cannot check a signature of its own (opkg 0.6.3: "GPG signature checking
not supported"). The receiver's Python may or may not ship `cryptography` - the plugin's package
does not depend on it, and another image need not have it - so the check has to come with the
plugin. It costs about 40 ms per signature on an armv7 receiver (measured at `nice 19`), less than
`cryptography`'s own start-up there, and it runs on every image the same way. It never runs on
enigma2's main loop: the check worker and the update helper call it.

**What it is.** RFC 8032 section 5.1.7, in the shape of the RFC's own reference code (section 6):
extended twisted Edwards coordinates, double-and-add. It checks the unbatched, cofactorless
equation `[S]B = R + [k]A` - the one OpenSSL checks, and so the one the companion integration's
`cryptography` checks - with strict decoding, so both halves refuse exactly the same inputs:

- a public key of 32 bytes and a signature of 64, nothing else;
- `S` below the group order `L`: `S + L` names the same point, so a verifier that skips this
  accepts a second signature for a message it has already seen signed;
- a point's `y` below the field prime, and no "negative zero" `x`: a non-canonical encoding of a
  point is not the point, so `R` has exactly one spelling;
- both coordinates compared: two points that share `x` differ in `y`, and a comparison of one
  coordinate accepts a signature forged from that ambiguity (the vectors carry one).

**What it is not.** Constant-time. It handles only public data - an embedded public key, a
published index and its signature - so there is no secret for timing to reveal. It has no signing
code; the index is signed in CI with OpenSSL, and `tools/sign-index.py` signs offline with it too.

`verify` never raises for input of the right type: a malformed key or signature is `False`.
"""

import hashlib

# The field prime, the order of the base point, the curve constant and a square root of -1.
P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)

PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64

# Points are (X, Y, Z, T) with x = X/Z, y = Y/Z and x*y = T/Z.
_IDENTITY = (0, 1, 1, 0)


def _add(one, two):
    """The sum of two points - the unified formula, so it doubles as well."""
    a = (one[1] - one[0]) * (two[1] - two[0]) % P
    b = (one[1] + one[0]) * (two[1] + two[0]) % P
    c = 2 * one[3] * two[3] * D % P
    d = 2 * one[2] * two[2] % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _multiply(scalar, point):
    result = _IDENTITY
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(one, two):
    """Whether two points are the same point: both coordinates, projectively."""
    if (one[0] * two[2] - two[0] * one[2]) % P:
        return False
    return (one[1] * two[2] - two[1] * one[2]) % P == 0


def _recover_x(y, sign):
    """The `x` of the point with this `y` and this sign of `x`, or None when there is none."""
    if y >= P:
        # Non-canonical: y and y - P would name the same point with two spellings.
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        # x = 0 has no negative; a set sign bit is a second spelling of the same point.
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


def decode_point(encoded):
    """The point a 32-byte encoding names, or None when it names none (or not canonically)."""
    if len(encoded) != 32:
        return None
    y = int.from_bytes(encoded, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


_B_Y = 4 * pow(5, P - 2, P) % P
_B_X = _recover_x(_B_Y, 0)
B = (_B_X, _B_Y, 1, _B_X * _B_Y % P)


def verify(public_key, message, signature):
    """Whether `signature` is `public_key`'s Ed25519 signature over `message` (all bytes)."""
    if not all(isinstance(value, (bytes, bytearray)) for value in (public_key, message, signature)):
        return False
    if len(public_key) != PUBLIC_KEY_BYTES or len(signature) != SIGNATURE_BYTES:
        return False
    public_key, message, signature = bytes(public_key), bytes(message), bytes(signature)
    a_point = decode_point(public_key)
    r_point = decode_point(signature[:32])
    if a_point is None or r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % L
    return _equal(_multiply(s, B), _add(r_point, _multiply(k, a_point)))
