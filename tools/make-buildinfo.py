#!/usr/bin/env python3
"""Decide what a build of the plugin is, and write it into the package as `buildinfo.py`.

    tools/make-buildinfo.py --epoch
    tools/make-buildinfo.py --version 0.3.0 --output <staging tree>/.../buildinfo.py

`tools/build-ipk.sh` runs this twice: once for the timestamp every member of the archive gets, and
once to write the build id into the staging tree. Nothing else writes `buildinfo.py`, and it is
never in git - a checkout of the source, and a copy of it made by hand, carry no build id at all.

**Why a build needs an id.** A version number says which release a build claims to be, not which
code it is. A development build of the commit after 0.3.0 still says 0.3.0 until the release pull
request bumps it, so a receiver running it and a receiver running the release look identical to
everything that reads `info.plugin` - Home Assistant's update entity compares version strings and
calls both of them current. The build id is what tells them apart (ADR-0015, decision 1): the
commit, its time, whether the tracked files matched it, and the build's flavour.

**Supplied by the builder, never guessed.** Every value comes from whoever builds the package:

- the commit from `MQTTBRIDGE_BUILD_COMMIT`, else from the git checkout the build runs in, else
  empty. "The checkout the build runs in" means a repository whose top is the tree being built.
  A source archive unpacked inside some other repository - the companion integration's bundle
  builder unpacks one without a `.git` - must not pick up that other repository's commit, which
  is what a bare `git rev-parse HEAD` would do. When both are given they must agree;
- the time from `SOURCE_DATE_EPOCH`, else the commit's own time, else 0 (unknown). It is the same
  number every file in the archive is stamped with, which is what keeps the package reproducible:
  the same commit, time and flavour produce the same bytes;
- `dirty` from the tracked files only (`git status --porcelain --untracked-files=no`). An
  untracked file does not enter the package - the build copies named directories - so it cannot
  make the build something other than its commit. Without a checkout it is false: the builder
  that supplied the commit is also the one vouching for the tree (the integration's bundle
  builder refuses a dirty tree before it exports one);
- the flavour from `MQTTBRIDGE_BUILD_FLAVOUR`, `development` unless the builder says otherwise.

Nothing is read on the receiver: the plugin only reads the file this writes. There is no git on a
receiver, and a guess made there would be a guess about somebody else's tree.

**The flavours.** `release` is the one flavour that asserts something, and it asserts it here, at
build time, where it can still be checked: the tree is a git checkout whose tracked files are
clean, its HEAD carries the tag `v<version>`, `SOURCE_DATE_EPOCH` - when given - is that commit's
time, the version is a plain `N.N.N`, and the changelog has its section. A release built without a
checkout cannot be checked that way, so it has to name its commit and its time, and the builder
that names them answers for the rest - clean tree, tag - which only the signed release index
confirms afterwards. That is the companion integration bundling a released plugin from its tag, and
its bundle is byte-identical to the released package only when it passes both
`MQTTBRIDGE_BUILD_COMMIT` and `MQTTBRIDGE_BUILD_FLAVOUR=release` (with `SOURCE_DATE_EPOCH`, which it
already passes).
`development` is every other build of the production code: a pull request's CI run, a developer's
package, a candidate. `acceptance` is a build made for a hardware acceptance run. A consumer reads
any flavour it does not know as "not a release".

**Test settings, for `acceptance` only.** A hardware acceptance run drives the update path with a
test index, served from a test origin and signed with throwaway keys, so its build may carry
`MQTTBRIDGE_BUILD_ORIGIN` (an `https://.../` address) and `MQTTBRIDGE_BUILD_INDEX_KEYS` (the key set
as JSON, `[{"key_id", "rank", "public", "baseline"}]`, `public` in base64). They are written into
`buildinfo.py` as `ORIGIN` and `INDEX_KEYS`, and only then: a build of any other flavour that is
given either is **refused**, so a `development` or `release` package can never trust anything but
the embedded keys and the published origin. That refusal is what lets `info.build` leave the
origin out - the flavour already says whether a build could have another one - and the release
workflow reads the package back to confirm it carries neither. The plugin honours them only in an
`acceptance` build as well (`trust.configured`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

COMMIT_ENV = "MQTTBRIDGE_BUILD_COMMIT"
FLAVOUR_ENV = "MQTTBRIDGE_BUILD_FLAVOUR"
EPOCH_ENV = "SOURCE_DATE_EPOCH"
ORIGIN_ENV = "MQTTBRIDGE_BUILD_ORIGIN"
INDEX_KEYS_ENV = "MQTTBRIDGE_BUILD_INDEX_KEYS"

RELEASE = "release"
DEVELOPMENT = "development"
ACCEPTANCE = "acceptance"
FLAVOURS = (RELEASE, DEVELOPMENT, ACCEPTANCE)

COMMIT = re.compile(r"^[0-9a-f]{40}$")
EPOCH = re.compile(r"^[0-9]+$")
RELEASE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

HEADER = '''"""What this build of the plugin is. Written by tools/make-buildinfo.py when the
package was built, and never in git: a checkout of the source carries no build id.
buildid.py reads it.
"""
'''


class BuildRefused(Exception):
    """The build cannot say truthfully what it is, so it is not made."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """`git <args>` in `root`, or None when there is no git to run."""
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError:
        return None


def own_repository(root: Path) -> bool:
    """Whether `root` is the top of a git work tree - its own, not one it happens to sit in."""
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.returncode or inside.stdout.strip() != "true":
        return False
    # Empty at the top of the work tree; `some/dir/` when `root` is a directory inside another
    # repository. Compared as a prefix rather than as paths, because git and the shell spell the
    # same directory differently on some systems.
    prefix = _git(root, "rev-parse", "--show-prefix")
    return prefix is not None and prefix.returncode == 0 and prefix.stdout.strip() == ""


def head_commit(root: Path) -> str:
    """HEAD's commit id, or "" when the repository has none (or not in the form we publish)."""
    result = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if result is None or result.returncode:
        return ""
    value = result.stdout.strip()
    return value if COMMIT.match(value) else ""


def head_time(root: Path) -> int | None:
    result = _git(root, "show", "-s", "--format=%ct", "HEAD")
    if result is None or result.returncode or not EPOCH.match(result.stdout.strip()):
        return None
    return int(result.stdout.strip())


def tracked_changes(root: Path) -> bool:
    result = _git(root, "status", "--porcelain", "--untracked-files=no")
    if result is None or result.returncode:
        # Not knowing is not the same as knowing it is clean.
        raise BuildRefused("git status failed, so whether the tree is clean is unknown")
    return bool(result.stdout.strip())


def head_tags(root: Path) -> list[str]:
    result = _git(root, "tag", "--points-at", "HEAD")
    if result is None or result.returncode:
        return []
    return result.stdout.split()


def epoch(root: Path, environ) -> int:
    """The build's timestamp: `SOURCE_DATE_EPOCH`, else the checkout's HEAD time, else 0."""
    given = environ.get(EPOCH_ENV, "").strip()
    if given:
        if not EPOCH.match(given):
            raise BuildRefused(f"{EPOCH_ENV}={given!r} is not a whole number of seconds")
        return int(given)
    if own_repository(root):
        found = head_time(root)
        if found is not None:
            return found
    return 0


def decide(root: Path, version: str, environ) -> dict:
    """The build id of the tree at `root`: commit, time, dirty and flavour. Refuses a lie."""
    given = environ.get(COMMIT_ENV, "").strip()
    if given and not COMMIT.match(given):
        raise BuildRefused(f"{COMMIT_ENV}={given!r} is not a 40-digit lowercase commit id")
    own = own_repository(root)
    checked_out = head_commit(root) if own else ""
    if given and checked_out and given != checked_out:
        raise BuildRefused(
            f"{COMMIT_ENV} names {given}, but the checkout being built is at {checked_out}"
        )
    commit = given or checked_out
    dirty = tracked_changes(root) if checked_out else False

    flavour = environ.get(FLAVOUR_ENV, "").strip() or DEVELOPMENT
    if flavour not in FLAVOURS:
        raise BuildRefused(
            f"{FLAVOUR_ENV}={flavour!r} is not one of {', '.join(FLAVOURS)}"
        )
    when = epoch(root, environ)
    if flavour == RELEASE:
        _release_or_refuse(root, version, commit, checked_out, dirty, when)
    return {"commit": commit, "time": when, "dirty": dirty, "flavour": flavour}


def _release_or_refuse(root, version, commit, checked_out, dirty, when):
    if not RELEASE_VERSION.match(version):
        raise BuildRefused(f"a release is a plain N.N.N, and version.py says {version!r}")
    if not commit:
        raise BuildRefused(
            "a release build must name its commit: build it in its git checkout, "
            f"or set {COMMIT_ENV}"
        )
    if when <= 0:
        # Without a checkout there is no commit time to fall back on, and 0 is "unknown": a
        # release published with an unknown time could never be ordered against another build.
        raise BuildRefused(
            f"a release build needs its commit's time: set {EPOCH_ENV} when there is no checkout"
        )
    if checked_out:
        if dirty:
            raise BuildRefused("a release build needs a clean tree: tracked files differ from HEAD")
        tag = f"v{version}"
        if tag not in head_tags(root):
            raise BuildRefused(
                f"a release build is built at its tag, and HEAD does not carry {tag}"
            )
        if when != head_time(root):
            raise BuildRefused(
                f"{EPOCH_ENV}={when} is not the time of the commit being released"
            )
    try:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError as error:
        raise BuildRefused(f"CHANGELOG.md cannot be read: {error}") from error
    if f"## [{version}]" not in changelog:
        raise BuildRefused(f"a release needs its section in CHANGELOG.md, and [{version}] has none")


def _trust():
    """The plugin's own `trust` module, from this tool's tree, to judge an override with."""
    src = str(Path(__file__).resolve().parents[1] / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from MQTTBridge import trust

    return trust


def decide_overrides(environ, flavour: str) -> dict | None:
    """The acceptance build's test origin and keys, None when there are none. Refuses them for
    any other flavour, and refuses a value the plugin could not use."""
    origin = environ.get(ORIGIN_ENV, "").strip()
    keys = environ.get(INDEX_KEYS_ENV, "").strip()
    if not origin and not keys:
        return None
    if flavour != ACCEPTANCE:
        named = " and ".join(name for name, value in ((ORIGIN_ENV, origin), (INDEX_KEYS_ENV, keys))
                             if value)
        raise BuildRefused(
            f"{named}: only an {ACCEPTANCE} build may carry another origin or other index keys, "
            f"and this is a {flavour} build"
        )
    trust = _trust()
    if origin and not trust.is_origin(origin):
        raise BuildRefused(f"{ORIGIN_ENV}={origin!r} is not an https://host/.../ address")
    parsed = None
    if keys:
        try:
            parsed = trust.keys_to_data(trust.keys_from_data(json.loads(keys)))
        except (ValueError, trust.Refused) as error:
            raise BuildRefused(f"{INDEX_KEYS_ENV} is not a usable key set: {error}") from error
    return {"origin": origin or None, "index_keys": parsed}


def render(info: dict, overrides: dict | None = None) -> str:
    """The file's text. Only literals, so it is read without running it."""
    text = (
        HEADER
        + "\n"
        + f'COMMIT = "{info["commit"]}"\n'
        + f"COMMIT_TIME = {int(info['time'])}\n"
        + f"DIRTY = {bool(info['dirty'])}\n"
        + f'FLAVOUR = "{info["flavour"]}"\n'
    )
    if overrides:
        # Test settings of an acceptance build, never anything else's (decide_overrides).
        if overrides.get("origin"):
            text += f"ORIGIN = {overrides['origin']!r}\n"
        if overrides.get("index_keys"):
            text += f"INDEX_KEYS = {overrides['index_keys']!r}\n"
    return text


def main(argv: list[str] | None = None, environ=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="the tree being built")
    parser.add_argument(
        "--epoch", action="store_true", help="print the build's timestamp and exit"
    )
    parser.add_argument("--version", help="the version the package is built as")
    parser.add_argument("--output", type=Path, help="where to write buildinfo.py")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        if args.epoch:
            print(epoch(args.root, environ))
            return 0
        if not args.version or args.output is None:
            parser.error("--version and --output are required unless --epoch is given")
        info = decide(args.root, args.version, environ)
        overrides = decide_overrides(environ, info["flavour"])
    except BuildRefused as error:
        print(f"make-buildinfo.py: {error}", file=sys.stderr)
        return 1
    args.output.write_text(render(info, overrides), encoding="utf-8")
    print(
        "build id: commit={} time={} dirty={} flavour={}".format(
            info["commit"] or "unknown", info["time"], info["dirty"], info["flavour"]
        )
    )
    if overrides:
        print("acceptance test settings: origin={} index keys={}".format(
            overrides["origin"] or "published",
            ",".join(item["key_id"] for item in overrides["index_keys"] or []) or "embedded",
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
