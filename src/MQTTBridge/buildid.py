"""Which build of the plugin is running, and which one is waiting on disk.

A version number cannot tell a development build from the release that carries
the same number: the commit after 0.3.0 still says 0.3.0 until a release pull
request changes it. Home Assistant's update entity compares version strings, so
to it a receiver running a development build is up to date, and a receiver whose
files were replaced by one is too. `info.build` is what tells them apart
([ADR-0015](../../docs/adr/0015-signed-self-update.md), decision 1).

**Supplied by the builder, read here, never worked out here.**
`tools/make-buildinfo.py` writes `buildinfo.py` into the package when it is
built - the commit, its time, whether the tracked files matched it, and the
build's flavour - and this module only reads it. There is no git on a receiver,
and anything this module could infer at runtime would be a guess about a tree
it has never seen. A copy of the source that nobody built - a checkout the tests
run from, a directory copied by hand - has no `buildinfo.py`, and is reported as
exactly that: no commit, and `null` for everything the builder would have said.

**Two builds, not one.** What runs is what enigma2 imported at start, and it
keeps running after the package manager has replaced the files underneath it -
until the interface restarts, the receiver runs one build and has another on
disk. So the build this process loaded is captured once, when this module is
first imported (which is when the plugin is), and the file on disk is read again
whenever `info` is built and every ten minutes (`CHECK_MILLISECONDS`), so that a
staged build shows up as `on_disk` without anybody asking. The running build is
imported with the rest of the plugin, once; the file on disk is read, never
imported: an import of a changed file would be a first import of new code into a
process that is still running the old code, and the text is only literals. Any
file the reader cannot use - unreadable, oversized, not literals, or literals
that cannot even be built - is "no build id", never an exception: the reader runs
inside the connect handler, and an exception there would cost the whole session.

**What a consumer shows.** `display_version` is the one rule: the plain version
for a release build, and for a build nobody can name the commit of (every plugin
up to 0.3.x, which has no build id at all); `0.3.0+g1a2b3c4` for everything else.
The part after `+` is a local label, never a pre-release suffix - opkg and PEP 440
disagree about how `0.4.0rc1` sorts against `0.4.0`, and `+g<sha>` is not ordered
by either. The companion integration applies the same rule and additionally holds
a release build's commit against the signed release index, which this side
cannot see.
"""

import ast
import os
import re

FILE_NAME = "buildinfo.py"
ON_DISK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE_NAME)

RELEASE = "release"

# How often the file on disk is compared with the build that is running. Only the
# plugin's own files change it, and only the package manager changes those, so
# this is a backstop: whatever runs opkg asks again straight afterwards.
CHECK_MILLISECONDS = 10 * 60 * 1000

# The file is four literals; anything much larger was not written by the builder.
MAX_BYTES = 4096

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_FLAVOUR = re.compile(r"^[a-z][a-z_]{0,31}$")
_NAMES = {"COMMIT": "commit", "COMMIT_TIME": "time", "DIRTY": "dirty", "FLAVOUR": "flavour"}


def validated(values):
    """`{commit, time, dirty, flavour}`, or None when anything is missing or malformed.

    A flavour this version does not know is kept: a later builder may have more
    of them, and every consumer reads an unknown flavour as "not a release".
    """
    commit = values.get("commit")
    when = values.get("time")
    dirty = values.get("dirty")
    flavour = values.get("flavour")
    if not isinstance(commit, str) or (commit and not _COMMIT.match(commit)):
        return None
    # bool is an int in Python, and `True` is not a time.
    if isinstance(when, bool) or not isinstance(when, int) or when < 0:
        return None
    if not isinstance(dirty, bool):
        return None
    if not isinstance(flavour, str) or not _FLAVOUR.match(flavour):
        return None
    return {"commit": commit, "time": when, "dirty": dirty, "flavour": flavour}


def parse(text):
    """The build id in the text of a `buildinfo.py`, read without running it - or None.

    Never raises. `literal_eval` alone can raise SyntaxError and ValueError for
    what is not a literal, TypeError for a literal that cannot be built - a
    dictionary keyed by a list - and RecursionError or MemoryError for a deep
    one, and a caller that has to list them all will one day miss one.
    """
    try:
        return _parse(text)
    except Exception:
        return None


def _parse(text):
    values = {}
    for node in ast.parse(text).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in _NAMES:
            continue
        values[_NAMES[target.id]] = ast.literal_eval(node.value)
    return validated(values)


def read(path=ON_DISK_PATH):
    """The build id of the `buildinfo.py` at `path`, or None when there is no usable one."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return parse(text)


def _loaded():
    try:
        from . import buildinfo
    except Exception:
        # ImportError for a copy nobody built; anything else for a file that is
        # not what the builder writes. Neither may stop the plugin loading.
        return None
    return validated({
        "commit": getattr(buildinfo, "COMMIT", None),
        "time": getattr(buildinfo, "COMMIT_TIME", None),
        "dirty": getattr(buildinfo, "DIRTY", None),
        "flavour": getattr(buildinfo, "FLAVOUR", None),
    })


# The build this process runs: fixed at the first import, which is the plugin's.
LOADED = _loaded()


def report(loaded, on_disk):
    """`info.build`: the running build, and the one on disk when that is another build.

    `time` 0 is the builder saying it did not know, and is published as `null`.
    `on_disk` is the commit of the file on disk when its build id differs in any
    way from the running one - an unreadable or missing file is not evidence of a
    different build, so it is `null` then too.
    """
    if loaded is None:
        payload = {"commit": "", "time": None, "dirty": None, "flavour": None}
    else:
        payload = {
            "commit": loaded["commit"],
            "time": loaded["time"] or None,
            "dirty": loaded["dirty"],
            "flavour": loaded["flavour"],
        }
    payload["on_disk"] = None if on_disk is None or on_disk == loaded else on_disk["commit"]
    return payload


def display_version(version, build):
    """`0.3.0` for a release build or one whose commit is unknown, else `0.3.0+g<sha7>`.

    `build` is a build id or an `info.build` payload; both carry `commit`,
    `dirty` and `flavour`. A release build is one whose builder said `release`
    about a clean tree - the builder refuses to say it about anything else.
    """
    if not build or not build.get("commit"):
        return version
    if build.get("flavour") == RELEASE and build.get("dirty") is False:
        return version
    return "{}+g{}".format(version, build["commit"][:7])
