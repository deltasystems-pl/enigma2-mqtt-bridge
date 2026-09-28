"""The lock-staleness rule of the companion integration's RELEASED 0.3.1 installer, verbatim.

Copied from `custom_components/enigma2_mqtt/installer_helper.py` at the integration's tag
`v0.3.1` (deltasystems-pl/hass-enigma2-mqtt), unchanged but for this docstring and the
imports it needs, so that the plugin's update helper is tested against the code that is in
the field - not against a description of it (TRANSACTION.md, section 2.5). Never edit the
functions below; replace the file from a later released tag instead.

The integration is MIT-licensed; its licence, which applies to the code below:

    MIT License

    Copyright (c) 2026 deltasystems-pl

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
UPTIME_PATH = Path("/proc/uptime")
STALE_LOCK_SECONDS = 30 * 60


def boot_id() -> str:
    """Return this boot's identifier, or an empty string if the kernel has none."""
    try:
        return BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def uptime() -> float | None:
    """Return seconds since the receiver booted, or None if the kernel does not say."""
    try:
        return float(UPTIME_PATH.read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _age_of(lock_dir: Path) -> float | None:
    try:
        return time.time() - lock_dir.stat().st_mtime
    except OSError:
        return None


def _is_stale(lock_dir: Path) -> str:
    """Return why an existing lock may be reclaimed, or an empty string if it may not.

    The lock is a directory on the receiver's flash, so it survives the one failure it
    cannot survive: a box pulled out of the wall halfway through an install comes back
    with a lock nobody holds and refuses every later attempt for ever. A lock written
    under a different boot is held by a process that no longer exists, and a lock older
    than any plausible install has outlived its owner either way.

    Age is measured against uptime rather than the clock whenever the lock was taken
    under this same boot. Many receivers have no battery-backed clock: they boot in
    1970 and jump to the real time the moment NTP answers, which can be minutes into an
    install. A wall-clock age would then read as decades and reclaim a live lock.
    Uptime cannot jump; across boots the boot id has already settled the question.
    """
    owner = lock_dir / "owner.json"
    try:
        recorded = json.loads(owner.read_text(encoding="ascii"))
    except (OSError, ValueError):
        # The record is written atomically, so it is never half there: it is either a
        # claim that has not got to it yet - a matter of milliseconds - or one that died
        # in between. Only age tells those apart, and the directory's own mtime is the
        # only age an owner-less lock has.
        age = _age_of(lock_dir)
        if age is not None and age > STALE_LOCK_SECONDS:
            return f"its owner record is unreadable and it is {int(age)} seconds old"
        return ""
    if not isinstance(recorded, dict):
        return "its owner record is not an object"
    current = boot_id()
    recorded_boot = recorded.get("boot_id")
    if current and isinstance(recorded_boot, str) and recorded_boot and recorded_boot != current:
        return "it was claimed before the receiver last rebooted"

    now_uptime = uptime()
    recorded_uptime = recorded.get("uptime")
    if (
        current
        and recorded_boot == current
        and now_uptime is not None
        and isinstance(recorded_uptime, (int, float))
        and not isinstance(recorded_uptime, bool)
    ):
        held = now_uptime - recorded_uptime
        if held > STALE_LOCK_SECONDS:
            return f"it has been held for {int(held)} seconds"
        return ""

    started = recorded.get("started")
    if not isinstance(started, int) or isinstance(started, bool):
        return "its start time is missing or malformed"
    age = int(time.time()) - started
    if age > STALE_LOCK_SECONDS:
        return f"it has been held for {age} seconds"
    return ""
