"""A repository with releases, a feed and a published index, made up in a temporary directory.

`tools/make-index.py` reads four things: the releases API, the packages, the `gh-pages` branch and
the git tags. The tests that exercise it - and the rehearsal of the whole build, sign and publish
chain - need all four to agree the way the real ones do, and to disagree on purpose one at a time.
This builds them: small but real IPKs (an `ar` archive with a control file, and a data tarball that
may carry a `buildinfo.py`), a git repository with a tag per release, a `gh-pages` branch serving
the feed, and the API's answer with each asset's size and `sha256:` digest.

Signing uses the vectors' throwaway test keys and OpenSSL; `openssl_can_sign()` says whether this
machine can.
"""

import base64
import gzip
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((REPO_ROOT / "tests" / "vectors" / "release-index.json").read_text("ascii"))
PACKAGE = "enigma2-plugin-extensions-mqttbridge"
BUILDINFO = "./usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/buildinfo.py"
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")

GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main")


def tool(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
                                                  REPO_ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def openssl_can_sign():
    if shutil.which("openssl") is None:
        return False
    probe = subprocess.run(["openssl", "pkeyutl", "-help"], capture_output=True, text=True)
    return "-rawin" in probe.stdout + probe.stderr


def seed_of(name):
    return bytes.fromhex(VECTORS["test_keys"][name]["seed"])


def keyset_of(*names_and_ranks):
    """A key set file's content: [(name, rank)] of the vectors' test keys, baseline 0."""
    items = []
    for name, rank in names_and_ranks:
        key = VECTORS["test_keys"][name]
        items.append({"key_id": key["key_id"], "rank": rank, "public": key["public"],
                      "baseline": 0})
    return items


def sign(seed, message):
    """A raw Ed25519 signature by OpenSSL, the key on stdin as the sign job passes it."""
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "message"
        path.write_bytes(message)
        return subprocess.run(
            ["openssl", "pkeyutl", "-sign", "-rawin", "-keyform", "DER", "-inkey", "/dev/stdin",
             "-in", str(path)],
            input=PKCS8_PREFIX + seed, capture_output=True, check=True,
        ).stdout


def _tar(members):
    """A gzipped tar of {name: bytes}, fixed times and owners."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 1790000000, 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buffer.getvalue(), mtime=0)


def ipk(version, package=PACKAGE, depends="python3-core, python3-json (>= 3.9)", buildinfo=None):
    control = (f"Package: {package}\nVersion: {version}\nArchitecture: all\n"
               f"Depends: {depends}\nDescription: a test package\n").encode()
    data = {"./usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/plugin.py": b"# test\n"}
    if buildinfo is not None:
        data[BUILDINFO] = buildinfo.encode()
    members = [("debian-binary", b"2.0\n"), ("control.tar.gz", _tar({"./control": control})),
               ("data.tar.gz", _tar(data))]
    out = b"!<arch>\n"
    for name, content in members:
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n"
        out += header.encode("ascii") + content + (b"\n" if len(content) % 2 else b"")
    return out


class Lab:
    """A repository, its releases, its feed and its published index."""

    def __init__(self, root):
        self.root = Path(root)
        self.repo = self.root / "repo"
        self.assets = self.root / "assets"
        self.repo.mkdir(parents=True)
        self.assets.mkdir()
        self.releases = []
        self.git("init", "-q")
        (self.repo / "README").write_text("test\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "start")
        self.git("checkout", "-q", "--orphan", "gh-pages")
        self.git("rm", "-rfq", ".")
        (self.repo / "feed").mkdir()
        (self.repo / "feed" / ".keep").write_text("")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "the feed")
        self.git("checkout", "-q", "main")

    def git(self, *args, check=True):
        environment = dict(os.environ)
        environment["GIT_COMMITTER_DATE"] = environment["GIT_AUTHOR_DATE"] = \
            "@1790000000 +0000"
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            environment.pop(name, None)
        return subprocess.run([*GIT, *args], cwd=self.repo, capture_output=True, text=True,
                              check=check, env=environment).stdout.strip()

    def release(self, version, compatibility="contract = 1\nmin_integration = none\n"
                "self_update = false\n", blob=None, feed_blob=None, digest=None, size=None,
                with_buildinfo=False, buildinfo_extra="", annotated=True, publish=True):
        """Commit, tag and publish a release; each argument spoils one thing on purpose."""
        if compatibility is not None:
            (self.repo / "COMPATIBILITY").write_text(compatibility)
        elif (self.repo / "COMPATIBILITY").exists():
            (self.repo / "COMPATIBILITY").unlink()
        (self.repo / "VERSION").write_text(version + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"release {version}")
        commit = self.git("rev-parse", "HEAD")
        if annotated:
            self.git("tag", "-a", f"v{version}", "-m", f"v{version}")
        else:
            self.git("tag", f"v{version}")
        if blob is None:
            info = None
            if with_buildinfo:
                info = (f'COMMIT = "{commit}"\nCOMMIT_TIME = 1790000000\nDIRTY = False\n'
                        'FLAVOUR = "release"\n' + buildinfo_extra)
            blob = ipk(version, buildinfo=info)
        name = f"{PACKAGE}_{version}_all.ipk"
        (self.assets / name).write_bytes(blob)
        if publish:
            self.feed(name, blob if feed_blob is None else feed_blob)
        self.releases.insert(0, {
            "tag_name": f"v{version}", "draft": False, "prerelease": False,
            "assets": [{"name": name, "size": len(blob) if size is None else size,
                        "digest": digest or "sha256:" + hashlib.sha256(blob).hexdigest(),
                        "browser_download_url": f"https://example.invalid/{name}"}],
        })
        return commit

    def feed(self, name, blob):
        """Put a file on gh-pages/feed, as release.yml and the publish job do."""
        self.git("checkout", "-q", "gh-pages")
        (self.repo / "feed" / name).write_bytes(blob)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"feed {name}")
        self.git("checkout", "-q", "main")

    def publish(self, index, sig):
        self.feed("releases.json", index)
        self.feed("releases.json.sig", sig)

    def releases_json(self):
        path = self.root / "releases.json"
        path.write_text(json.dumps(self.releases))
        return path

    def policy(self, floor="0.1.0", withdrawn=None, corrections=None):
        return {"schema": 1, "floor": floor, "withdrawn": withdrawn or {},
                "corrections": corrections or {}}

    def source(self):
        return tool("make-index").LocalAssets(self.releases_json(), self.assets)


def b64(data):
    return base64.b64encode(data).decode()
