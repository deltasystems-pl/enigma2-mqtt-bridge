"""`tools/check-workflows.py`: the workflows that reach the signing key stay as they were reviewed.

Each rule is shown to bite by breaking the repository's own `publish-index.yml` (or another
workflow) in exactly one way and running the check on the copy. The YAML reader the check carries
- the runner's Python has none - is held to PyYAML's reading of every workflow in the repository.
"""

import re
import shutil
from pathlib import Path

import indexlab
import pytest

check_workflows = indexlab.tool("check-workflows")
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def test_the_repositorys_workflows_pass():
    assert check_workflows.check(WORKFLOWS) == []


def _normalised(value):
    # PyYAML reads YAML 1.1, where the bare key `on` is the boolean true; GitHub reads it as "on".
    if isinstance(value, dict):
        return {("on" if key is True else key): _normalised(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalised(item) for item in value]
    return value


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def test_the_reader_reads_every_workflow_as_pyyaml_does(path):
    yaml = pytest.importorskip("yaml")
    text = path.read_text(encoding="utf-8")
    assert check_workflows.load(text) == _normalised(yaml.safe_load(text))


@pytest.mark.parametrize("text", [
    "a: &anchor 1\nb: *anchor\n",
    "a:\n\tb: 1\n",
    "---\na: 1\n",
    "a: >-\n  one\n    more indented\n",
    "a: \"escaped \\n\"\n",
    "a: 1\na: 2\n",
    "a:\n  b: 1\n   c: 2\n",
    "a: [1, 2\n",
    "a: 'unterminated\n",
])
def test_the_reader_refuses_what_it_does_not_understand(text):
    with pytest.raises(check_workflows.YAMLError):
        check_workflows.load(text)


def test_the_sign_step_is_found_where_the_rehearsal_looks():
    script = check_workflows.sign_step_script(WORKFLOWS)
    assert "openssl pkeyutl -sign -rawin -keyform DER" in script
    assert "-inkey /dev/stdin" in script


# ------------------------------------------------------- one change at a time --


@pytest.fixture()
def copy(tmp_path):
    target = tmp_path / "workflows"
    shutil.copytree(WORKFLOWS, target)
    return target


def _edit(copy, name, old, new, count=1):
    path = copy / name
    text = path.read_text(encoding="utf-8")
    assert text.count(old) >= count, f"{old!r} is not in {name}"
    path.write_text(text.replace(old, new, count), encoding="utf-8")


SIGN = "publish-index.yml"
PRINTF = ("printf '%s' \"$INDEX_SIGNING_KEY\" | /usr/bin/env -u INDEX_SIGNING_KEY "
          "/usr/bin/base64 -d")
OPENSSL = "} | /usr/bin/env -u INDEX_SIGNING_KEY /usr/bin/openssl pkeyutl"
BASE64 = "/usr/bin/env -u INDEX_SIGNING_KEY /usr/bin/base64 -d"
CHECK_STEP = ("        run: python3 tools/make-index.py check --index index/releases.json "
              "--sha256 \"$EXPECTED\"\n")
CHECKOUT = "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n"
ANCHOR = "          umask 077\n"


@pytest.mark.parametrize("name, old, new, words", [
    ("ci.yml", "  pull_request:\n", "  pull_request_target:\n", "pull_request_target"),
    ("ci.yml", "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0",
     "actions/checkout@v4", "not pinned"),
    ("release.yml", "@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65 # v2.6.2",
     "@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65", "no `# vX.Y.Z` comment"),
    ("index.yml", "@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0",
     "@11d5960a # v4.4.0", "not pinned"),
    (SIGN, "          SIGNATURE: ${{ needs.sign.outputs.signature }}\n",
     "          SIGNATURE: ${{ needs.sign.outputs.signature }}\n"
     "          LEAK: ${{ secrets.INDEX_SIGNING_KEY }}\n", "appears 2 times"),
    ("ci.yml", "      - name: ruff\n", "      - name: ruff\n        env:\n"
     "          INDEX_SIGNING_KEY: ${{ secrets.INDEX_SIGNING_KEY }}\n", "exactly one"),
    (SIGN, "    environment: release-signing\n", "", "release-signing environment"),
    (SIGN, "    permissions: {}\n    outputs:", "    permissions:\n      contents: read\n"
     "    outputs:", "permissions: {}"),
    (SIGN, "        shell: bash\n", "", "shell: bash"),
    (SIGN, "        shell: bash\n", "        shell: bash -x {0}\n", "shell: bash"),
    (SIGN, "          umask 077\n", "          set -x\n          umask 077\n", "tracing"),
    (SIGN, "          umask 077\n", "          set -eux\n          umask 077\n", "tracing"),
    (SIGN, "          umask 077\n", "          set -o xtrace\n          umask 077\n", "tracing"),
    (SIGN, "          umask 077\n", "          set -v\n          umask 077\n", "tracing"),
    (SIGN, PRINTF, PRINTF + " | xxd", "`xxd`"),
    (SIGN, PRINTF, PRINTF + " | od -c", "`od`"),
    (SIGN, PRINTF, PRINTF + " | hexdump", "`hexdump`"),
    (SIGN, PRINTF, PRINTF + " | tee key.der", "`tee`"),
    (SIGN, "          umask 077\n", "          printenv\n          umask 077\n", "`printenv`"),
    (SIGN, "          umask 077\n", "          env\n          umask 077\n", "`env` other than"),
    (SIGN, "          umask 077\n", "          echo \"$INDEX_SIGNING_KEY\" | base64 -d\n"
     "          umask 077\n", "expanded outside"),
    (SIGN, "          umask 077\n", "          copy=$INDEX_SIGNING_KEY\n          umask 077\n",
     "expanded outside"),
    (SIGN, "          umask 077\n", "          export INDEX_SIGNING_KEY\n          umask 077\n",
     "`export`"),
    (SIGN, BASE64, "/usr/bin/env -u INDEX_SIGNING_KEY /usr/bin/base64", "encodes"),
    (SIGN, BASE64, "base64 -d", "must run under"),
    (SIGN, OPENSSL, "} | openssl pkeyutl", "must run under"),
    (SIGN, OPENSSL, "} | /usr/bin/env -u INDEX_SIGNING_KEY openssl pkeyutl",
     "only as `/usr/bin/env -u INDEX_SIGNING_KEY` before"),
    (SIGN, OPENSSL, "} | openssl pkeyutl", "must run by absolute path"),
    (SIGN, "- uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0"
     "\n        with:\n          name: unsigned-index\n          path: index\n\n      - name: The",
     "- uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0\n\n"
     "      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0"
     "\n        with:\n          name: unsigned-index\n          path: index\n\n      - name: The",
     "sign job uses only"),
    (SIGN, "run: python3 tools/make-index.py check --index",
     "run: pip install x && python3 tools/make-index.py check --index", "installs"),
    (SIGN, "--out releases.json --summary", "--keyset k.json --out releases.json --summary",
     "--keyset"),
    (SIGN, "  group: publish-index\n", "  group: publish-index-${{ github.ref }}\n",
     "concurrency group"),
    ("emergency-index.yml", "cancel-in-progress: false", "cancel-in-progress: true",
     "concurrency group"),
    (SIGN, "    needs: [build, sign]\n    if: github.ref == 'refs/heads/main'\n",
     "    needs: [build, sign]\n", "must be refused off `main`"),
    (SIGN, "    permissions:\n      contents: write\n",
     "    environment: release-signing\n    permissions:\n      contents: write\n",
     "only sign may"),
    ("emergency-index.yml", "permissions: {}\n\n# A group", "permissions:\n  contents: write\n\n"
     "# A group", "at the top"),
])
def test_each_weakening_is_refused(copy, name, old, new, words):
    assert check_workflows.check(copy) == []
    _edit(copy, name, old, new)
    problems = check_workflows.check(copy)
    assert any(words in problem for problem in problems), problems


def test_a_comment_about_a_trigger_is_not_the_trigger(copy):
    _edit(copy, "ci.yml", "permissions:\n", "# pull_request_target is never used here\n"
          "permissions:\n")
    assert check_workflows.check(copy) == []


def test_a_missing_chain_workflow_is_noticed(copy):
    (copy / "emergency-index.yml").unlink()
    assert "emergency-index.yml: missing" in check_workflows.check(copy)


def test_the_tool_reports_and_fails(copy, capsys):
    _edit(copy, "ci.yml", "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0",
          "actions/checkout@v4")
    assert check_workflows.main(["--workflows", str(copy)]) == 1
    assert re.search(r"::error::ci\.yml: job \w+, step \d+: `actions/checkout@v4` is not pinned",
                     capsys.readouterr().out)
    assert check_workflows.main(["--workflows", str(WORKFLOWS)]) == 0


# ------------------------------------------------ the bypasses a review found --

NEW_WORKFLOW = ("name: X\n\non:\n  push:\n    branches: [main]\n\npermissions: {{}}\n\njobs:\n"
                "  x:\n    runs-on: ubuntu-latest\n{job}    steps:\n{steps}")


def _new(copy, name, job="", steps="      - run: echo hi\n", on=None):
    text = NEW_WORKFLOW.format(job=job, steps=steps)
    if on is not None:
        text = text.replace("  push:\n    branches: [main]\n", on)
    (copy / name).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("change, words", [
    # A new workflow that asks for the environment and dumps every secret into its steps.
    (lambda copy: _new(copy, "x.yml", job="    environment: release-signing\n",
                       steps="      - env:\n          ALL: ${{ toJSON(secrets) }}\n"
                             "        run: echo \"$ALL\"\n"),
     "only publish-index.yml's sign job may"),
    (lambda copy: _new(copy, "x.yml", steps="      - env:\n          ALL: ${{ toJSON(secrets) }}\n"
                                             "        run: echo \"$ALL\"\n"),
     "`secrets` appears 1 times, expected 0"),
    (lambda copy: _new(copy, "x.yml", steps="      - env:\n          K: ${{ secrets['A'] }}\n"
                                             "        run: echo hi\n"),
     "`secrets` appears 1 times, expected 0"),
    (lambda copy: _new(copy, "x.yml", job="    environment: release-signing\n",
                       on="  workflow_run:\n    workflows: [CI]\n    types: [completed]\n"),
     "only publish-index.yml's sign job may"),
    (lambda copy: _new(copy, "x.yml",
                       job="    environment:\n      name: ${{ 'release-' }}signing\n"),
     "computes its environment"),
    # A flow-style step with an unpinned action.
    (lambda copy: _edit(copy, "ci.yml", CHECKOUT,
                        CHECKOUT + "\n      - {uses: evil/action@main}\n"),
     "`evil/action@main` is not pinned"),
    # The sign step running an interpreter, reading its environment, a subshell, a redirection.
    (lambda copy: _edit(copy, SIGN, ANCHOR, ANCHOR + "          python3 -c 'import os; "
                        "print(os.environ[\"INDEX_SIGNING_KEY\"])'\n"),
     "`python3` - the sign step runs nothing but"),
    (lambda copy: _edit(copy, SIGN, ANCHOR, ANCHOR + "          cat /proc/self/environ\n"),
     "`cat` - the sign step runs nothing but"),
    (lambda copy: _edit(copy, SIGN, ANCHOR, ANCHOR + "          echo \"$(python3 -V)\"\n"),
     "runs a command inside a word"),
    (lambda copy: _edit(copy, SIGN, ANCHOR, ANCHOR + "          printf '%s' x > key.b64\n"),
     "no redirection"),
    (lambda copy: _edit(copy, SIGN, ANCHOR, ANCHOR + "          umask 077 || true\n"),
     "no redirection, background or `||`/`&&`"),
    # Earlier steps of the sign job steering what the sign step runs.
    (lambda copy: _edit(copy, SIGN, CHECK_STEP, "        run: |\n          python3 "
                        "tools/make-index.py check --index index/releases.json --sha256 "
                        "\"$EXPECTED\"\n          echo \"$PWD/index\" >> \"$GITHUB_PATH\"\n"),
     "`GITHUB_PATH` - no step of the sign job"),
    (lambda copy: _edit(copy, SIGN, CHECK_STEP, "        run: |\n          python3 "
                        "tools/make-index.py check --index index/releases.json --sha256 "
                        "\"$EXPECTED\"\n          echo \"BASH_ENV=x.sh\" >> \"$GITHUB_ENV\"\n"),
     "no step of the sign job may change what a later step runs"),
    (lambda copy: _edit(copy, SIGN, "    environment: release-signing\n",
                        "    environment: release-signing\n    env:\n      BASH_ENV: x.sh\n"),
     "may not declare `env`"),
    (lambda copy: _edit(copy, SIGN, "permissions: {}\n\n# One chain",
                        "permissions: {}\n\ndefaults:\n  run:\n    shell: sh\n\n# One chain"),
     "may not declare `defaults` at the top"),
    # Guards loosened.
    (lambda copy: _edit(copy, SIGN, "    if: github.ref == 'refs/heads/main'\n    runs-on: "
                        "ubuntu-latest\n    environment:",
                        "    if: github.ref == 'refs/heads/main' || true\n    runs-on: "
                        "ubuntu-latest\n    environment:"),
     "must be refused off `main`"),
    (lambda copy: _edit(copy, SIGN, "        run: python3 tools/make-index.py check --index",
                        "        continue-on-error: true\n        run: python3 tools/make-index.py "
                        "check --index"),
     "continue-on-error lets a failed guard pass"),
    (lambda copy: _edit(copy, SIGN, "--sig index/releases.json.sig --sha256 \"$EXPECTED\"\n",
                        "--sig index/releases.json.sig --sha256 \"$EXPECTED\" || true\n"),
     "`||` or `set +e` lets a failed guard pass"),
    (lambda copy: _edit(copy, SIGN, "      - name: Push to gh-pages\n",
                        "      - name: Push to gh-pages\n        if: always()\n"),
     "runs after a failure"),
    (lambda copy: _edit(copy, "index.yml", "      - name: The workflows\n",
                        "      - name: The workflows\n        continue-on-error: true\n"),
     "continue-on-error"),
    (lambda copy: _edit(copy, "emergency-index.yml", "  group: emergency-index\n",
                        "  group: publish-index\n"),
     "`emergency-index` concurrency group"),
])
def test_each_bypass_is_refused(copy, change, words):
    assert check_workflows.check(copy) == []
    change(copy)
    problems = check_workflows.check(copy)
    assert any(words in problem for problem in problems), problems


def test_the_sign_step_is_pinned():
    import hashlib

    script = check_workflows.sign_step_script(WORKFLOWS)
    assert hashlib.sha256(script.encode()).hexdigest() == check_workflows.SIGN_STEP_SHA256


def test_a_harmless_change_to_the_sign_step_needs_a_new_pin(copy):
    _edit(copy, SIGN, "          umask 077\n", "          umask 0077\n")
    problems = check_workflows.check(copy)
    assert problems and all("not the pinned one" in problem for problem in problems), problems


@pytest.mark.parametrize("condition, ok", [
    ("github.ref == 'refs/heads/main'", True),
    ("github.ref == 'refs/heads/main' && (a || (b && c))", True),
    ("github.ref == 'refs/heads/main' && (a) || true", False),
    ("github.ref == 'refs/heads/main' || true", False),
    ("true || github.ref == 'refs/heads/main'", False),
    ("github.ref == 'refs/heads/main' && (a == ')')", True),
    ("github.ref == 'refs/heads/main' && (a", False),
    ("", False),
])
def test_the_main_only_condition_is_read_exactly(condition, ok):
    assert check_workflows._main_only(condition) is ok
