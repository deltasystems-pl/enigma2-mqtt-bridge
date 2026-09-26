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
up to 0.3.x, which has no build id at all); `0.3.0+g1a2b3c4` for everything else,
and `0.3.0+g1a2b3c4.dirty` when the tracked files differed from that commit - a
build of a tree nobody can check out again must not look like the one that can.
The part after `+` is a local label, never a pre-release suffix - opkg and PEP 440
disagree about how `0.4.0rc1` sorts against `0.4.0`, and `+g<sha>` is not ordered
by either. The companion integration applies the same rule and additionally holds
a release build's commit against the signed release index, which this side
cannot see.

**Overrides, for acceptance builds only.** An `acceptance` build may carry
`ORIGIN` and `INDEX_KEYS` - another place to fetch releases from and other keys
to trust - so a hardware acceptance run can be driven by a test index. The
builder refuses both for any other flavour, and `trust.configured` honours them
for no other flavour; they are read here with the build id and never published
(`info.build` has no `origin`, see TOPICS.md).
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
_OVERRIDE_NAMES = {"ORIGIN": "origin", "INDEX_KEYS": "index_keys"}


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


def overrides_of(values):
    """`{"origin", "index_keys"}` from an acceptance build's literals, or None when it has none.

    Only the shape is checked here - a string, a list of objects; `trust` judges the values when it
    uses them. Absent or malformed is None: a reader that cannot use an override uses the embedded
    origin and keys, which is the safe side.
    """
    origin = values.get("origin")
    keys = values.get("index_keys")
    if origin is None and keys is None:
        return None
    if origin is not None and not isinstance(origin, str):
        return None
    if keys is not None and not (
        isinstance(keys, list) and keys and all(isinstance(item, dict) for item in keys)
    ):
        return None
    return {"origin": origin, "index_keys": keys}


def _loaded_overrides():
    try:
        from . import buildinfo
    except Exception:
        return None
    return overrides_of({
        "origin": getattr(buildinfo, "ORIGIN", None),
        "index_keys": getattr(buildinfo, "INDEX_KEYS", None),
    })


LOADED_OVERRIDES = _loaded_overrides()


def carries_overrides(text):
    """Whether a `buildinfo.py` names an override at all - well formed or not, or unreadable.

    For the checks that a release package carries none: there, an override the reader would
    ignore is still one the builder should have refused, so anything but a clear "no" is "yes".
    """
    try:
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Name) and node.id in _OVERRIDE_NAMES:
                return True
        return False
    except Exception:
        return True


def parse_overrides(text):
    """The overrides in the text of a `buildinfo.py`, read as literals, or None. Never raises."""
    try:
        values = {}
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in _OVERRIDE_NAMES:
                    values[_OVERRIDE_NAMES[target.id]] = ast.literal_eval(node.value)
        return overrides_of(values)
    except Exception:
        return None


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
    """`0.3.0` for a release build or one whose commit is unknown, else `0.3.0+g<sha7>`,
    with `.dirty` after it when the tracked files differed from that commit.

    `build` is a build id or an `info.build` payload; both carry `commit`,
    `dirty` and `flavour`. A release build is one whose builder said `release`
    about a clean tree - the builder refuses to say it about anything else.
    Only a `dirty` of exactly `true` adds the marker: `null` says nothing.
    """
    if not build or not build.get("commit"):
        return version
    if build.get("flavour") == RELEASE and build.get("dirty") is False:
        return version
    marker = ".dirty" if build.get("dirty") is True else ""
    return "{}+g{}{}".format(version, build["commit"][:7], marker)
