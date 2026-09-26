"""The build id comes from the builder, and a release build is what it says it is.

`tools/make-buildinfo.py` decides what `buildinfo.py` says - the commit, its time, whether the
tracked files matched it, the flavour - and `tools/build-ipk.sh` puts that into every package.
These tests run it against throwaway git repositories, and against trees that have no git at all,
because the two places the plugin is built from are exactly those: a checkout, and a source archive
the companion integration unpacks without its `.git`.
"""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "make-buildinfo.py"

GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main")

# A fixed commit time, so the expected epoch is a constant rather than "whenever the test ran".
COMMITTED_AT = 1790000000
VERSION = "1.2.3"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("make_buildinfo", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo, *args):
    environment = dict(os.environ)
    environment["GIT_COMMITTER_DATE"] = environment["GIT_AUTHOR_DATE"] = f"@{COMMITTED_AT} +0000"
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        environment.pop(name, None)
    result = subprocess.run(
        [*GIT, *args], cwd=repo, check=True, capture_output=True, text=True, env=environment
    )
    return result.stdout.strip()


def _tree(path, version=VERSION, changelog=True):
    """The files a build reads: a version, a changelog, something tracked."""
    source = path / "src" / "MQTTBridge"
    source.mkdir(parents=True, exist_ok=True)
    (source / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n"
        + (f"## [{version}] - 2026-09-26\n\n- a release\n" if changelog else ""),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def checkout(tmp_path):
    """A committed tree, the way a builder's checkout is."""
    repo = _tree(tmp_path / "checkout")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a commit")
    return repo


def _head(repo):
    return _git(repo, "rev-parse", "HEAD")


def _decide(tool, root, version=VERSION, **environ):
    return tool.decide(Path(root), version, environ)


# ------------------------------------------------------------ from a checkout --


def test_a_checkout_names_its_head_its_time_and_a_clean_tree(tool, checkout):
    info = _decide(tool, checkout)
    assert info == {
        "commit": _head(checkout),
        "time": COMMITTED_AT,
        "dirty": False,
        "flavour": "development",
    }


def test_a_tracked_change_makes_the_build_dirty(tool, checkout):
    (checkout / "src" / "MQTTBridge" / "version.py").write_text(
        '__version__ = "1.2.3"\n# edited\n', encoding="utf-8"
    )
    assert _decide(tool, checkout)["dirty"] is True


def test_an_untracked_file_does_not(tool, checkout):
    # The build copies named directories; a stray file outside git's view cannot make the package
    # something other than its commit, and `dist/` is exactly such a file after every build.
    (checkout / "notes.txt").write_text("scratch\n", encoding="utf-8")
    (checkout / "src" / "MQTTBridge" / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    assert _decide(tool, checkout)["dirty"] is False


def test_a_repository_without_a_commit_names_none(tool, tmp_path):
    repo = _tree(tmp_path / "fresh")
    _git(repo, "init", "-q")
    info = _decide(tool, repo)
    assert info["commit"] == "" and info["dirty"] is False and info["time"] == 0


# -------------------------------------------------------- without a checkout --


def test_a_tree_without_git_names_no_commit_and_no_time(tool, tmp_path):
    info = _decide(tool, _tree(tmp_path / "exported"))
    assert info == {"commit": "", "time": 0, "dirty": False, "flavour": "development"}


def test_a_tree_inside_another_repository_does_not_borrow_its_commit(tool, checkout):
    # The integration's bundle builder unpacks a `git archive` with no .git. Unpacked inside some
    # other checkout, a bare `git rev-parse HEAD` would answer with that checkout's commit and
    # `git log -1` with its time - a build id for code the package does not contain.
    inner = _tree(checkout / "vendor" / "plugin")
    info = _decide(tool, inner)
    assert info["commit"] == ""
    assert info["time"] == 0
    assert info["dirty"] is False


def test_the_builder_can_name_the_commit(tool, tmp_path):
    commit = "a" * 40
    info = _decide(tool, _tree(tmp_path / "exported"), MQTTBRIDGE_BUILD_COMMIT=commit,
                   SOURCE_DATE_EPOCH="1790000123")
    assert info == {"commit": commit, "time": 1790000123, "dirty": False, "flavour": "development"}


@pytest.mark.parametrize("given", ["A" * 40, "a" * 39, "a" * 41, "g" * 40, "HEAD", "a" * 64])
def test_a_commit_that_is_not_a_commit_id_is_refused(tool, tmp_path, given):
    with pytest.raises(tool.BuildRefused, match="40-digit"):
        _decide(tool, _tree(tmp_path / "exported"), MQTTBRIDGE_BUILD_COMMIT=given)


def test_a_named_commit_must_be_the_checkout_being_built(tool, checkout):
    with pytest.raises(tool.BuildRefused, match="checkout being built"):
        _decide(tool, checkout, MQTTBRIDGE_BUILD_COMMIT="b" * 40)
    assert _decide(tool, checkout, MQTTBRIDGE_BUILD_COMMIT=_head(checkout))["commit"] == _head(
        checkout
    )


def test_the_epoch_given_wins_and_must_be_a_number(tool, checkout):
    assert _decide(tool, checkout, SOURCE_DATE_EPOCH="42")["time"] == 42
    with pytest.raises(tool.BuildRefused, match="whole number"):
        _decide(tool, checkout, SOURCE_DATE_EPOCH="yesterday")


# ------------------------------------------------------------------ flavours --


def test_the_flavour_is_development_unless_the_builder_says_otherwise(tool, checkout):
    assert _decide(tool, checkout)["flavour"] == "development"
    assert _decide(tool, checkout, MQTTBRIDGE_BUILD_FLAVOUR="acceptance")["flavour"] == (
        "acceptance"
    )


def test_an_unknown_flavour_is_refused(tool, checkout):
    with pytest.raises(tool.BuildRefused, match="not one of"):
        _decide(tool, checkout, MQTTBRIDGE_BUILD_FLAVOUR="nightly")


def test_a_release_is_built_clean_at_its_tag(tool, checkout):
    _git(checkout, "tag", "v" + VERSION)
    info = _decide(tool, checkout, MQTTBRIDGE_BUILD_FLAVOUR="release")
    assert info["flavour"] == "release"
    assert info["dirty"] is False
    assert info["commit"] == _head(checkout)


def test_a_release_without_its_tag_is_refused(tool, checkout):
    with pytest.raises(tool.BuildRefused, match="does not carry v1.2.3"):
        _decide(tool, checkout, MQTTBRIDGE_BUILD_FLAVOUR="release")


def test_a_release_at_another_versions_tag_is_refused(tool, checkout):
    _git(checkout, "tag", "v1.2.2")
    with pytest.raises(tool.BuildRefused, match="does not carry v1.2.3"):
        _decide(tool, checkout, MQTTBRIDGE_BUILD_FLAVOUR="release")


def test_a_dirty_release_is_refused(tool, checkout):
    _git(checkout, "tag", "v" + VERSION)
    (checkout / "src" / "MQTTBridge" / "version.py").write_text(
        '__version__ = "1.2.3"\n# edited\n', encoding="utf-8"
    )
    with pytest.raises(tool.BuildRefused, match="clean tree"):
        _decide(tool, checkout, MQTTBRIDGE_BUILD_FLAVOUR="release")


def test_a_release_stamped_with_another_time_is_refused(tool, checkout):
    _git(checkout, "tag", "v" + VERSION)
    with pytest.raises(tool.BuildRefused, match="not the time of the commit"):
        _decide(tool, checkout, MQTTBRIDGE_BUILD_FLAVOUR="release", SOURCE_DATE_EPOCH="1")


def test_a_release_without_its_changelog_section_is_refused(tool, tmp_path):
    repo = _tree(tmp_path / "checkout", changelog=False)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a commit")
    _git(repo, "tag", "v" + VERSION)
    with pytest.raises(tool.BuildRefused, match="CHANGELOG.md"):
        _decide(tool, repo, MQTTBRIDGE_BUILD_FLAVOUR="release")


@pytest.mark.parametrize("version", ["1.2.3rc1", "1.2.3-rc.1", "1.2.3+g0123456", "1.2"])
def test_a_release_is_a_plain_version(tool, tmp_path, version):
    repo = _tree(tmp_path / "checkout", version=version)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a commit")
    _git(repo, "tag", "v" + version)
    with pytest.raises(tool.BuildRefused, match="plain N.N.N"):
        _decide(tool, repo, version=version, MQTTBRIDGE_BUILD_FLAVOUR="release")


def test_a_release_without_git_must_name_its_commit(tool, tmp_path):
    exported = _tree(tmp_path / "exported")
    with pytest.raises(tool.BuildRefused, match="must name its commit"):
        _decide(tool, exported, MQTTBRIDGE_BUILD_FLAVOUR="release")
    info = _decide(tool, exported, MQTTBRIDGE_BUILD_FLAVOUR="release",
                   MQTTBRIDGE_BUILD_COMMIT="c" * 40, SOURCE_DATE_EPOCH="1790000000")
    assert info["flavour"] == "release" and info["commit"] == "c" * 40


# ------------------------------------------------------------ the command line --


def test_the_file_is_written_and_says_what_was_decided(tool, checkout, tmp_path):
    from MQTTBridge import buildid

    output = tmp_path / "buildinfo.py"
    status = tool.main(
        ["--root", str(checkout), "--version", VERSION, "--output", str(output)], environ={}
    )
    assert status == 0
    assert buildid.read(str(output)) == _decide(tool, checkout)


def test_the_file_is_the_same_bytes_every_time(tool, checkout, tmp_path):
    first, second = tmp_path / "one.py", tmp_path / "two.py"
    for output in (first, second):
        tool.main(["--root", str(checkout), "--version", VERSION, "--output", str(output)],
                  environ={})
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().decode("ascii")


def test_a_refused_build_writes_nothing_and_says_why(tool, checkout, tmp_path, capsys):
    output = tmp_path / "buildinfo.py"
    status = tool.main(
        ["--root", str(checkout), "--version", VERSION, "--output", str(output)],
        environ={"MQTTBRIDGE_BUILD_FLAVOUR": "release"},
    )
    assert status == 1
    assert not output.exists()
    assert "does not carry v1.2.3" in capsys.readouterr().err


def test_the_epoch_is_printed_for_the_archive(tool, checkout, tmp_path, capsys):
    assert tool.main(["--root", str(checkout), "--epoch"], environ={}) == 0
    assert capsys.readouterr().out.strip() == str(COMMITTED_AT)
    assert tool.main(["--root", str(_tree(tmp_path / "bare")), "--epoch"], environ={}) == 0
    assert capsys.readouterr().out.strip() == "0"
