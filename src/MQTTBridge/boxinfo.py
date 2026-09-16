"""What the receiver can say about itself.

Everything here is best effort and nothing here raises: an image that does not
ship `boxbranding`, a box whose first network interface is not `eth0`, a
`/proc/uptime` that reads oddly — each falls back rather than taking the plugin
down. The `info` topic is honest about what could not be determined by saying
`unknown` instead of guessing.

None of these calls may block the main thread, which is why the outbound address
is found with a connectionless UDP socket (the kernel picks the route and
nothing is sent) and never by running `ip` in a subprocess.
"""

import os
import re
import socket

UNKNOWN = "unknown"

# An address nothing routes: TEST-NET-1 from RFC 5737. Asking the kernel which
# interface it would use to reach it is how the box learns its own LAN address
# without a DNS lookup or a packet.
UNROUTED_PROBE = "192.0.2.1"

_NOT_ALLOWED = re.compile(r"[^a-z0-9_]+")
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_NULL_MAC = "00:00:00:00:00:00"


def sanitise(value):
    """Lowercase ASCII with `_` for everything else — what a topic segment may hold."""
    return _NOT_ALLOWED.sub("_", str(value or "").strip().lower()).strip("_")


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _boxbranding():
    """The image's own description of itself, where the image ships it."""
    try:
        import boxbranding

        return boxbranding
    except Exception:
        return None


def _branding_call(name):
    module = _boxbranding()
    if module is None:
        return ""
    function = getattr(module, name, None)
    if function is None:
        return ""
    try:
        value = function()
    except Exception:
        return ""
    if value is None:
        return ""
    return str(value).strip()


def _key_values(path):
    """Parse the `key=value` files OE images leave in /etc."""
    values = {}
    for line in _read_text(path).splitlines():
        if "=" not in line:
            continue
        key, _sep, value = line.partition("=")
        values[key.strip().lower()] = value.strip().strip("'\"")
    return values


def box_type():
    """The machine name, lowercase — `vuuno4kse` and friends."""
    value = _branding_call("getBoxType")
    if not value:
        value = _read_text("/proc/stb/info/boxtype")
    if not value:
        value = _read_text("/proc/stb/info/model")
    if not value:
        value = _read_text("/etc/hostname")
    return sanitise(value) or UNKNOWN


def _looks_like_a_mac(value):
    return bool(_MAC.match(value or "")) and value != _NULL_MAC


def mac_address():
    """The Wake-on-LAN target: `eth0` if it exists, otherwise the first real interface."""
    candidate = _read_text("/sys/class/net/eth0/address").lower()
    if _looks_like_a_mac(candidate):
        return candidate
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        names = []
    for name in names:
        if name == "lo":
            continue
        candidate = _read_text("/sys/class/net/" + name + "/address").lower()
        if _looks_like_a_mac(candidate):
            return candidate
    return ""


def mac_suffix(mac=None):
    """The last six hex digits — the half of the node id that makes it unique."""
    source = mac_address() if mac is None else mac
    digits = re.sub(r"[^0-9a-f]", "", str(source or "").lower())
    return digits[-6:] if len(digits) >= 6 else ""


def derive_node_id():
    """`<boxtype>_<mac6>`, stable across reinstalls because both halves are hardware."""
    boxtype = box_type()
    suffix = mac_suffix()
    node = boxtype if not suffix else boxtype + "_" + suffix
    return sanitise(node) or "enigma2"


def image_version():
    """Image name and version, as the box reports it."""
    distro = _branding_call("getImageDistro")
    version = _branding_call("getImageVersion")
    build = _branding_call("getImageBuild")
    if distro and version:
        if build:
            return distro + " " + version + "." + build
        return distro + " " + version

    values = _key_values("/etc/image-version")
    distro = distro or values.get("distro", "") or values.get("creator", "")
    version = version or values.get("imageversion", "") or values.get("version", "")
    if distro and version:
        return distro + " " + version
    return distro or version or UNKNOWN


def enigma_version():
    """The enigma2 / OE flavour version, when the binary will tell us."""
    try:
        from enigma import getEnigmaVersionString

        value = getEnigmaVersionString()
    except Exception:
        return UNKNOWN
    value = str(value or "").strip()
    return value or UNKNOWN


def uptime_seconds():
    text = _read_text("/proc/uptime")
    try:
        return int(float(text.split()[0]))
    except (IndexError, ValueError):
        return 0


def _is_ipv4_literal(value):
    try:
        socket.inet_pton(socket.AF_INET, value)
    except (OSError, ValueError, AttributeError):
        return False
    return True


def local_ip(host=None):
    """The address this box would use to reach `host`.

    A hostname is deliberately not resolved: a DNS lookup on the main thread can
    stall the user interface for seconds, and an unrouted probe address answers
    the same question for every broker on the same network.
    """
    target = host if host and _is_ipv4_literal(str(host).strip()) else UNROUTED_PROBE
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(0.5)
        probe.connect((str(target), 9))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        try:
            probe.close()
        except OSError:
            pass
