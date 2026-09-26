"""The whole build, run for real: the package carries its build id and stays reproducible.

`tools/build-ipk.sh` is run here end to end, on a committed copy of this working tree, because the
two promises it makes are properties of the whole script and not of any part of it:

- the same commit, timestamp and flavour give **the same bytes**, twice in a row, and whether or not
  the tree has its `.git` - the companion integration rebuilds the package from a `git archive`
  that has none, and a bundle that differs from the released package by one byte cannot be checked
  against it;
- a build **names only its own commit**, and a `release` build is refused unless it is one.

The build needs `msgfmt`, `ar`, `tar` and `gzip`. Where they are missing - the test matrix does not
install gettext - these tests skip, except in the CI job that builds the package, which sets
`MQTTBRIDGE_REQUIRE_BUILD_TOOLS=1` so that a skip there is a failure.
"""

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from MQTTBridge import buildid
from MQTTBridge.version import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MEMBER = "./usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/buildinfo.py"
PACKAGE = "enigma2-plugin-extensions-mqttbridge"

GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main")
COMMITTED_AT = 1790000000
BUILD_ENVIRONMENT = ("SOURCE_DATE_EPOCH", "MQTTBRIDGE_BUILD_COMMIT", "MQTTBRIDGE_BUILD_FLAVOUR",
                     "MQTTBRIDGE_BUILD_ORIGIN", "MQTTBRIDGE_BUILD_INDEX_KEYS",
                     "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


@pytest.fixture(scope="module", autouse=True)
def build_tools():
    missing = [tool for tool in ("bash", "git", "msgfmt", "ar", "tar", "gzip")
               if shutil.which(tool) is None]
    if missing:
        message = "the package build needs " + ", ".join(missing)
        if os.environ.get("MQTTBRIDGE_REQUIRE_BUILD_TOOLS"):
            pytest.fail(message)
        pytest.skip(message)


def _environment(**extra):
    environment = {k: v for k, v in os.environ.items() if k not in BUILD_ENVIRONMENT}
    environment["GIT_COMMITTER_DATE"] = environment["GIT_AUTHOR_DATE"] = f"@{COMMITTED_AT} +0000"
    environment.update(extra)
    return environment


def _git(repo, *args):
    return subprocess.run(
        [*GIT, *args], cwd=repo, check=True, capture_output=True, text=True,
        env=_environment(),
    ).stdout.strip()


@pytest.fixture(scope="module")
def checkout(tmp_path_factory):
    """This working tree - uncommitted changes included - committed into a repository of its own."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT, capture_output=True, check=False,
    )
    if listed.returncode:
        pytest.skip("the tests are not running in a git checkout")
    repo = tmp_path_factory.mktemp("build") / "checkout"
    for name in filter(None, listed.stdout.decode("utf-8").split("\0")):
        source = REPO_ROOT / name
        if not source.is_file():
            continue  # deleted in the working tree
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the tree under test")
    return repo


def _exported(checkout, destination):
    """`git archive HEAD`, unpacked: the tree the integration's bundle builder builds from."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"], cwd=checkout, capture_output=True, check=True
    ).stdout
    destination.mkdir(parents=True)
    # The same filter the integration's builder unpacks with, where this Python has it.
    safely = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(destination, **safely)
    assert not (destination / ".git").exists()
    return destination


def _build(root, expect_success=True, **extra):
    result = subprocess.run(
        ["bash", "tools/build-ipk.sh", "--allow-unreleased"],
        cwd=root, capture_output=True, text=True, env=_environment(**extra),
    )
    if not expect_success:
        return result
    assert result.returncode == 0, result.stdout + result.stderr
    ipk = root / "dist" / f"{PACKAGE}_{__version__}_all.ipk"
    return ipk.read_bytes()


def _members(ipk):
    """The members of an `ar` archive - for an IPK, `debian-binary` and the two tarballs."""
    assert ipk[:8] == b"!<arch>\n"
    members, offset = {}, 8
    while offset < len(ipk):
        header = ipk[offset:offset + 60]
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        members[name] = ipk[offset + 60:offset + 60 + size]
        offset += 60 + size + (size % 2)
    return members


def _buildinfo(ipk):
    with tarfile.open(fileobj=io.BytesIO(_members(ipk)["data.tar.gz"]), mode="r:gz") as data:
        member = data.getmember(PLUGIN_MEMBER)
        text = data.extractfile(member).read().decode("ascii")
    return buildid.parse(text), member


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------- the tests --


def test_two_builds_of_one_commit_are_the_same_bytes(checkout):
    first = _build(checkout)
    second = _build(checkout)
    assert _sha(first) == _sha(second)
    info, member = _buildinfo(first)
    assert info == {
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "time": COMMITTED_AT,
        "dirty": False,
        "flavour": "development",
    }
    assert member.mtime == COMMITTED_AT


def test_a_source_archive_builds_the_same_bytes_when_the_builder_names_the_commit(
    checkout, tmp_path
):
    exported = _exported(checkout, tmp_path / "exported")
    from_git = _build(checkout)
    from_archive = _build(
        exported,
        SOURCE_DATE_EPOCH=str(COMMITTED_AT),
        MQTTBRIDGE_BUILD_COMMIT=_git(checkout, "rev-parse", "HEAD"),
    )
    assert _sha(from_archive) == _sha(from_git)


def test_a_source_archive_without_a_named_commit_still_builds(checkout, tmp_path):
    # What the integration's bundle builder does today: a clean export, its commit's time, and
    # no commit id. The package builds; it is simply a build nobody can name the commit of.
    exported = _exported(checkout, tmp_path / "exported")
    info, _member = _buildinfo(_build(exported, SOURCE_DATE_EPOCH=str(COMMITTED_AT)))
    assert info == {"commit": "", "time": COMMITTED_AT, "dirty": False, "flavour": "development"}
    assert buildid.display_version(__version__, info) == __version__


def test_a_source_archive_inside_another_checkout_borrows_nothing(checkout, tmp_path):
    host = tmp_path / "host"
    shutil.copytree(checkout, host)
    nested = _exported(checkout, host / "vendor" / "plugin")
    info, member = _buildinfo(_build(nested))
    assert info["commit"] == ""
    assert info["time"] == 0 and member.mtime == 0


def test_a_release_build_is_refused_off_its_tag_and_made_at_it(checkout, tmp_path):
    repo = tmp_path / "release"
    shutil.copytree(checkout, repo)
    shutil.rmtree(repo / "dist", ignore_errors=True)
    refused = _build(repo, expect_success=False, MQTTBRIDGE_BUILD_FLAVOUR="release")
    assert refused.returncode != 0
    assert f"does not carry v{__version__}" in refused.stderr
    assert not list((repo / "dist").glob("*.ipk")), "a refused release leaves no package"

    _git(repo, "tag", f"v{__version__}")
    info, _member = _buildinfo(_build(repo, MQTTBRIDGE_BUILD_FLAVOUR="release"))
    assert info["flavour"] == "release" and info["dirty"] is False
    assert buildid.display_version(__version__, info) == __version__


def _check_release_package():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_release_package", REPO_ROOT / "tools" / "check-release-package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_release_read_back_accepts_the_release_package_and_nothing_else(checkout, tmp_path):
    # What release.yml runs before it publishes: the package's own modules, imported from the
    # unpacked package, must say this commit, this time, clean, release - and the release keys.
    check = _check_release_package()
    repo = tmp_path / "release"
    shutil.copytree(checkout, repo)
    shutil.rmtree(repo / "dist", ignore_errors=True)
    _git(repo, "tag", f"v{__version__}")
    commit = _git(repo, "rev-parse", "HEAD")
    released = repo / "dist" / "released.ipk"
    released.write_bytes(_build(repo, MQTTBRIDGE_BUILD_FLAVOUR="release"))
    found = check.check(released, commit, COMMITTED_AT)
    assert found["origin"] == "https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/"
    assert found["keys"] == ["5de3b24c97e88660", "c72fd83e3e514a25"]

    development = repo / "dist" / "development.ipk"
    development.write_bytes(_build(repo))
    with pytest.raises(check.Refused, match="build id is"):
        check.check(development, commit, COMMITTED_AT)
    with pytest.raises(check.Refused, match="build id is"):
        check.check(released, commit, COMMITTED_AT + 1)


@pytest.mark.parametrize("old, new, words", [
    # The spare's line, swapped for a throwaway test key (id and rank consistent, so it loads).
    ('SPARE = _key("c72fd83e3e514a25", 2, "1F2ajhsDoTuqAGdV2QOHdRl8hV4B0kNE0xwVGrpHdfs=", 0)',
     None, "not the release keys"),
    ('ORIGIN = "https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/"',
     'ORIGIN = "https://mirror.example/feed/"', "fetches from"),
])
def test_the_release_read_back_asks_the_package_what_it_trusts(checkout, tmp_path, old, new,
                                                                words):
    # A release package built from a tree whose keys or origin were changed - every release check
    # but this one is about the build, not about what the package will trust.
    import json

    if new is None:
        vectors = json.loads((REPO_ROOT / "tests" / "vectors" / "release-index.json").read_text())
        key = vectors["test_keys"]["t3"]
        new = f'SPARE = _key("{key["key_id"]}", 2, "{key["public"]}", 0)'
    check = _check_release_package()
    repo = tmp_path / "changed"
    shutil.copytree(checkout, repo)
    shutil.rmtree(repo / "dist", ignore_errors=True)
    trust_py = repo / "src" / "MQTTBridge" / "trust.py"
    text = trust_py.read_text(encoding="utf-8")
    assert text.count(old) == 1
    trust_py.write_text(text.replace(old, new), encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "other keys")
    _git(repo, "tag", f"v{__version__}")
    path = repo / "dist" / "changed.ipk"
    path.write_bytes(_build(repo, MQTTBRIDGE_BUILD_FLAVOUR="release"))
    with pytest.raises(check.Refused, match=words):
        check.check(path, _git(repo, "rev-parse", "HEAD"), COMMITTED_AT)


def test_an_acceptance_build_carries_its_test_settings_and_is_no_release(checkout, tmp_path):
    import json

    vectors = json.loads((REPO_ROOT / "tests" / "vectors" / "release-index.json").read_text())
    keys = json.dumps(vectors["keysets"]["test"]["keys"])
    repo = tmp_path / "acceptance"
    shutil.copytree(checkout, repo)
    shutil.rmtree(repo / "dist", ignore_errors=True)
    refused = _build(repo, expect_success=False, MQTTBRIDGE_BUILD_ORIGIN="https://lab.example/f/")
    assert refused.returncode != 0 and "only an acceptance build" in refused.stderr
    assert not list((repo / "dist").glob("*.ipk")), "a refused build leaves no package"

    ipk = _build(repo, MQTTBRIDGE_BUILD_FLAVOUR="acceptance",
                 MQTTBRIDGE_BUILD_ORIGIN="https://lab.example/f/", MQTTBRIDGE_BUILD_INDEX_KEYS=keys)
    info, _member = _buildinfo(ipk)
    assert info["flavour"] == "acceptance"
    path = repo / "dist" / "acceptance.ipk"
    path.write_bytes(ipk)
    check = _check_release_package()
    with pytest.raises(check.Refused, match="test origin or test index keys"):
        check.check(path, _git(repo, "rev-parse", "HEAD"), COMMITTED_AT)
