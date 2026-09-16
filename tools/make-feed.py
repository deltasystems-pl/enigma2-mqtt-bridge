#!/usr/bin/env python3
"""Turn a directory of IPKs into an opkg feed index.

    tools/make-feed.py feed/

Writes ``Packages`` and ``Packages.gz`` next to the IPKs. The release workflow
runs this on the ``gh-pages`` branch so that

    src/gz enigma2-mqtt-bridge https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed

is a working opkg feed and updates arrive through the receiver's normal plugin
browser.

Standard library only: this has to run on whatever Python the runner happens to
have, and the IPK is a plain ``ar`` archive whose control member is a gzipped
tar, both of which are cheaper to read here than to shell out for.
"""

import argparse
import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

AR_MAGIC = b"!<arch>\n"

# The order opkg and the Debian tooling conventionally use. Fields the control
# file does not carry are simply skipped.
CONTROL_FIELDS = (
    "Package",
    "Version",
    "Architecture",
    "Section",
    "Priority",
    "Depends",
    "Maintainer",
    "Homepage",
    "License",
    "Description",
)


def read_ar_member(data: bytes, wanted: str) -> bytes:
    """Return one member of an ``ar`` archive held in memory."""
    if not data.startswith(AR_MAGIC):
        raise ValueError("not an ar archive")
    pos = len(AR_MAGIC)
    while pos + 60 <= len(data):
        header = data[pos : pos + 60]
        name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as exc:
            raise ValueError("malformed ar header") from exc
        start = pos + 60
        if name == wanted:
            return data[start : start + size]
        pos = start + size + (size % 2)
    raise KeyError(wanted)


def read_control(ipk: Path) -> dict[str, str]:
    """Parse the control stanza out of an IPK.

    Continuation lines (the indented body of ``Description``) stay attached to
    their field, because opkg expects them back verbatim in the index.
    """
    blob = ipk.read_bytes()
    control_tar = read_ar_member(blob, "control.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(control_tar), mode="r:gz") as tar:
        member = None
        for candidate in ("./control", "control"):
            try:
                member = tar.getmember(candidate)
                break
            except KeyError:
                continue
        if member is None:
            raise KeyError(f"{ipk.name}: no control file in control.tar.gz")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise KeyError(f"{ipk.name}: control file is not a regular file")
        text = extracted.read().decode("utf-8")

    fields: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and key is not None:
            fields[key] += "\n" + line
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
    return fields


def stanza(ipk: Path, prefix: str) -> str:
    fields = read_control(ipk)
    blob = ipk.read_bytes()

    lines = []
    for name in CONTROL_FIELDS:
        if name in fields:
            lines.append(f"{name}: {fields[name]}")
    for name, value in fields.items():
        if name not in CONTROL_FIELDS:
            lines.append(f"{name}: {value}")

    filename = f"{prefix}{ipk.name}" if prefix else ipk.name
    lines.append(f"Filename: {filename}")
    lines.append(f"Size: {len(blob)}")
    lines.append(f"SHA256sum: {hashlib.sha256(blob).hexdigest()}")
    # MD5 is not a security choice here: opkg's index format specifies this field
    # and older clients read it. SHA256sum above is the one that is checked.
    lines.append(f"MD5Sum: {hashlib.md5(blob).hexdigest()}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="directory holding the .ipk files")
    parser.add_argument(
        "--prefix",
        default="",
        help="path prefix written into Filename: (default: the bare file name)",
    )
    args = parser.parse_args(argv)

    directory: Path = args.directory
    if not directory.is_dir():
        print(f"make-feed.py: not a directory: {directory}", file=sys.stderr)
        return 2

    ipks = sorted(directory.glob("*.ipk"))
    if not ipks:
        print(f"make-feed.py: no .ipk files in {directory}", file=sys.stderr)

    index = "\n".join(stanza(ipk, args.prefix) for ipk in ipks)
    raw = index.encode("utf-8")

    packages = directory / "Packages"
    packages.write_bytes(raw)
    # mtime=0 keeps the compressed index reproducible.
    with open(directory / "Packages.gz", "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(raw)

    for ipk in ipks:
        print(f"  indexed {ipk.name} ({ipk.stat().st_size} bytes)")
    print(f"  wrote   {packages} and {packages}.gz ({len(ipks)} package(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
