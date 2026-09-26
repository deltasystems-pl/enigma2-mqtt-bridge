"""`tools/check-workflows.py`: the workflows that reach the signing key stay as they were reviewed.

Each rule is shown to bite by breaking the repository's own workflows in exactly one way and
running the check on the copy. Where the change is to the sign job, most cases are run twice: as
is, where the pin on the sign job's text refuses it, and **re-pinned** - as the author of such a
pull request would re-pin it - so that each rule behind the pin is tested on its own merits. The
YAML reader the check carries - the runner's Python has none - is held to PyYAML's reading of every
workflow in the repository.
"""

import hashlib
import re
import shutil
from pathlib import Path

import indexlab
import pytest

check_workflows = indexlab.tool("check-workflows")
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SIGN = "publish-index.yml"


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


def test_the_steps_the_rehearsal_runs_are_the_fixed_texts():
    assert check_workflows.sign_step_script(WORKFLOWS) == check_workflows.SIGN_STEP
    assert check_workflows.hash_step_script(WORKFLOWS) == check_workflows.HASH_STEP
    assert "-inkey /dev/stdin" in check_workflows.SIGN_STEP


def test_the_sign_job_is_pinned():
    text = (WORKFLOWS / SIGN).read_text(encoding="utf-8")
    job = check_workflows.job_text(text, "sign")
    assert job.startswith("  sign:\n") and "  publish:" not in job
    assert hashlib.sha256(job.encode()).hexdigest() == check_workflows.SIGN_JOB_SHA256


def test_the_key_is_used_before_any_code_from_the_repository_runs():
    workflow = check_workflows.load((WORKFLOWS / SIGN).read_text(encoding="utf-8"))
    steps = workflow["jobs"]["sign"]["steps"]
    kinds = [step.get("uses", "").split("@")[0] or step["run"][:16] for step in steps]
    sign = next(number for number, step in enumerate(steps)
                if "INDEX_SIGNING_KEY" in str(step.get("env")))
    assert kinds[:sign] == ["actions/download-artifact", "printf '%s  inde"]
    assert kinds[sign + 1] == "actions/checkout"


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


def _problems(copy, monkeypatch, repin):
    if repin:
        text = (copy / SIGN).read_text(encoding="utf-8")
        job = check_workflows.job_text(text, "sign") or ""
        monkeypatch.setattr(check_workflows, "SIGN_JOB_SHA256",
                            hashlib.sha256(job.encode()).hexdigest())
    return check_workflows.check(copy)


CHECKOUT = "      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0\n"
ANCHOR = "          umask 077\n"
PRINTF = ("printf '%s' \"$INDEX_SIGNING_KEY\" | /usr/bin/env -u INDEX_SIGNING_KEY "
          "/usr/bin/base64 -d")
OPENSSL = "} | /usr/bin/env -u INDEX_SIGNING_KEY /usr/bin/openssl pkeyutl"
CHECK_RUN = ("        run: python3 tools/make-index.py check --index \"$INDEX/releases.json\" "
             "--sha256 \"$EXPECTED\"\n")
NOT_FIXED = "the sign step is not the fixed one"
PIN = "the sign job's text is not the pinned one"

# Changes to the sign step: refused by the pin as they stand, and by the fixed text once re-pinned.
SIGN_STEP_CHANGES = [
    (ANCHOR, "          set -x\n" + ANCHOR),
    (ANCHOR, "          set -o xtrace\n" + ANCHOR),
    (PRINTF, PRINTF + " | xxd"),
    (PRINTF, PRINTF + " | tee key.der"),
    (ANCHOR, "          printenv\n" + ANCHOR),
    (ANCHOR, "          echo \"$INDEX_SIGNING_KEY\" | base64 -d\n" + ANCHOR),
    (ANCHOR, "          export INDEX_SIGNING_KEY\n" + ANCHOR),
    ("/usr/bin/base64 -d", "/usr/bin/base64"),
    (OPENSSL, "} | openssl pkeyutl"),
    (ANCHOR, ANCHOR + "          python3 -c 'import os; "
                      "print(os.environ[\"INDEX_SIGNING_KEY\"])'\n"),
    (ANCHOR, ANCHOR + "          cat /proc/self/environ\n"),
    (ANCHOR, ANCHOR + "          printf '%s' x > key.b64\n"),
    # The second review's slice leaks: an indirect expansion, the emptiness test's own error, and
    # free OpenSSL arguments loading a provider from the artifact directory.
    (ANCHOR, ANCHOR + "          printf -v v '%s' INDEX_SIGNING_KEY\n"
                      "          echo \"${!v:0:22}\"\n"),
    ("        run: |\n          if [ -z \"${INDEX_SIGNING_KEY:+set}\" ]; then\n",
     "        run: |\n          [ \"${INDEX_SIGNING_KEY:0:22}\" -gt 0 ]\n"
     "          if [ -z \"${INDEX_SIGNING_KEY:+set}\" ]; then\n"),
    ("-inkey /dev/stdin", "-provider-path index -provider evil -inkey /dev/stdin"),
    ("-inkey /dev/stdin", "-config index/evil.cnf -inkey /dev/stdin"),
    ("-inkey /dev/stdin", "-engine evil -inkey /dev/stdin"),
    ("          umask 077\n", "          umask 0077\n"),
]


@pytest.mark.parametrize("old, new", SIGN_STEP_CHANGES)
@pytest.mark.parametrize("repin", [False, True], ids=["as-is", "re-pinned"])
def test_any_change_to_the_sign_step_is_refused(copy, monkeypatch, old, new, repin):
    _edit(copy, SIGN, old, new)
    problems = _problems(copy, monkeypatch, repin)
    assert any(NOT_FIXED in problem for problem in problems), problems
    assert any(PIN in problem for problem in problems) is not repin


# Changes elsewhere in the sign job, each re-pinned, so the rule behind the pin is what refuses.
SIGN_JOB_CHANGES = [
    ("    environment: release-signing\n", "", "release-signing environment"),
    ("    permissions: {}\n    outputs:", "    permissions:\n      contents: read\n    outputs:",
     "permissions: {}"),
    ("        shell: bash\n        working-directory: ${{ runner.temp }}\n        env:\n"
     "          INDEX_SIGNING_KEY", "        working-directory: ${{ runner.temp }}\n        env:\n"
     "          INDEX_SIGNING_KEY", "shell: bash"),
    ("    runs-on: ubuntu-24.04\n    environment", "    runs-on: self-hosted\n    environment",
     "only a fresh hosted"),
    ("    environment: release-signing\n", "    environment: release-signing\n    env:\n"
     "      BASH_ENV: x.sh\n", "may not declare `env`"),
    (CHECK_RUN, "        run: |\n          python3 tools/make-index.py check --index "
     "\"$INDEX/releases.json\" --sha256 \"$EXPECTED\"\n          sudo cp /tmp/x /usr/bin/openssl\n",
     "becomes root"),
    (CHECK_RUN, "        run: |\n          python3 tools/make-index.py check --index "
     "\"$INDEX/releases.json\" --sha256 \"$EXPECTED\"\n          nohup watch-env &\n",
     "becomes root, runs in the background"),
    (CHECK_RUN, "        run: |\n          python3 tools/make-index.py check --index "
     "\"$INDEX/releases.json\" --sha256 \"$EXPECTED\"\n"
     "          echo \"$PWD\" >> \"$GITHUB_PATH\"\n",
     "`GITHUB_PATH` - no step of the sign job"),
    (CHECK_RUN, "        run: |\n          if ! python3 tools/make-index.py check --index "
     "\"$INDEX/releases.json\" --sha256 \"$EXPECTED\"; then echo skipped; fi\n",
     "a guard inside a condition"),
    ("      - name: The file build hashed\n", CHECKOUT + "\n      - name: The file build hashed\n",
     "before the sign step the job may only download the artifact"),
    ("      - name: The file build hashed\n",
     "      - name: Early\n        shell: bash\n        run: python3 tools/anything.py\n\n"
     "      - name: The file build hashed\n", "before the sign step"),
    ("--check --strict", "--check --strict --ignore-missing", "before the sign step"),
    ("      - name: The file build hashed\n",
     "      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0\n\n"
     "      - name: The file build hashed\n", "the sign job uses only"),
    ("    if: github.ref == 'refs/heads/main'\n    # A fresh",
     "    if: github.ref == 'refs/heads/main' || true\n    # A fresh",
     "must be refused off `main`"),
]


@pytest.mark.parametrize("old, new, words", SIGN_JOB_CHANGES)
def test_each_weakening_of_the_sign_job_is_refused_behind_the_pin(copy, monkeypatch, old, new,
                                                                   words):
    assert check_workflows.check(copy) == []
    _edit(copy, SIGN, old, new)
    assert any(PIN in problem for problem in check_workflows.check(copy))
    problems = _problems(copy, monkeypatch, repin=True)
    assert any(words in problem for problem in problems), problems


NEW_WORKFLOW = ("name: X\n\non:\n  push:\n    branches: [main]\n\npermissions: {{}}\n\njobs:\n"
                "  x:\n    runs-on: ubuntu-latest\n{job}    steps:\n{steps}")


def _new(copy, name, job="", steps="      - run: echo hi\n", on=None):
    text = NEW_WORKFLOW.format(job=job, steps=steps)
    if on is not None:
        text = text.replace("  push:\n    branches: [main]\n", on)
    (copy / name).write_text(text, encoding="utf-8")


# Everything outside the sign job.
@pytest.mark.parametrize("change, words", [
    (lambda copy: _edit(copy, "ci.yml", "  pull_request:\n", "  pull_request_target:\n"),
     "pull_request_target"),
    (lambda copy: _edit(copy, "ci.yml", CHECKOUT, "      - uses: actions/checkout@v4\n"),
     "not pinned"),
    (lambda copy: _edit(copy, "release.yml", "@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65 # v2.6.2",
                        "@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65"), "no `# vX.Y.Z` comment"),
    (lambda copy: _edit(copy, "index.yml", "@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0",
                        "@11d5960a # v4.4.0"), "not pinned"),
    (lambda copy: _edit(copy, "ci.yml", CHECKOUT,
                        CHECKOUT + "\n      - {uses: evil/action@main}\n"),
     "`evil/action@main` is not pinned"),
    (lambda copy: _edit(copy, SIGN, "          SIGNATURE: ${{ needs.sign.outputs.signature }}\n",
                        "          SIGNATURE: ${{ needs.sign.outputs.signature }}\n"
                        "          LEAK: ${{ secrets.INDEX_SIGNING_KEY }}\n"), "appears 2 times"),
    (lambda copy: _edit(copy, "ci.yml", "      - name: ruff\n", "      - name: ruff\n        env:\n"
                        "          INDEX_SIGNING_KEY: ${{ secrets.INDEX_SIGNING_KEY }}\n"),
     "exactly one"),
    (lambda copy: _new(copy, "x.yml", job="    environment: release-signing\n",
                       steps="      - env:\n          ALL: ${{ toJSON(secrets) }}\n"
                             "        run: echo \"$ALL\"\n"),
     "only publish-index.yml's sign job may"),
    (lambda copy: _new(copy, "x.yml", steps="      - env:\n          K: ${{ secrets['A'] }}\n"
                                             "        run: echo hi\n"),
     "`secrets` appears 1 times, expected 0"),
    (lambda copy: _new(copy, "x.yml", job="    environment: release-signing\n",
                       on="  workflow_run:\n    workflows: [CI]\n    types: [completed]\n"),
     "only publish-index.yml's sign job may"),
    (lambda copy: _new(copy, "x.yml",
                       job="    environment:\n      name: ${{ 'release-' }}signing\n"),
     "computes its environment"),
    (lambda copy: _edit(copy, SIGN, "--out releases.json --summary",
                        "--keyset k.json --out releases.json --summary"), "--keyset"),
    (lambda copy: _edit(copy, SIGN, "  group: publish-index\n",
                        "  group: publish-index-${{ github.ref }}\n"), "concurrency group"),
    (lambda copy: _edit(copy, "emergency-index.yml", "  group: emergency-index\n",
                        "  group: publish-index\n"), "`emergency-index` concurrency group"),
    (lambda copy: _edit(copy, "emergency-index.yml", "cancel-in-progress: false",
                        "cancel-in-progress: true"), "concurrency group"),
    (lambda copy: _edit(copy, SIGN, "    needs: [build, sign]\n    if: github.ref == "
                        "'refs/heads/main'\n", "    needs: [build, sign]\n"),
     "must be refused off `main`"),
    (lambda copy: _edit(copy, SIGN, "    permissions:\n      contents: write\n",
                        "    environment: release-signing\n    permissions:\n"
                        "      contents: write\n"),
     "only sign may"),
    (lambda copy: _edit(copy, SIGN, "permissions: {}\n\n# One chain", "permissions: {}\n\n"
                        "defaults:\n  run:\n    shell: sh\n\n# One chain"),
     "may not declare `defaults` at the top"),
    (lambda copy: _edit(copy, "emergency-index.yml", "permissions: {}\n\n# A group",
                        "permissions:\n  contents: write\n\n# A group"), "at the top"),
    # The guards of the chain and of index.yml: structure, not spelling.
    (lambda copy: _edit(copy, SIGN, "--sig index/releases.json.sig --sha256 \"$EXPECTED\"\n",
                        "--sig index/releases.json.sig --sha256 \"$EXPECTED\" | cat\n"),
     "something after a guard decides the step's status (`|`)"),
    (lambda copy: _edit(copy, SIGN, "--sig index/releases.json.sig --sha256 \"$EXPECTED\"\n",
                        "--sig index/releases.json.sig --sha256 \"$EXPECTED\" && true\n"
                        "          echo done\n"),
     "something after a guard decides the step's status (`&&`)"),
    (lambda copy: _edit(copy, SIGN, "--sig index/releases.json.sig --sha256 \"$EXPECTED\"\n",
                        "--sig index/releases.json.sig --sha256 \"$EXPECTED\" || true\n"),
     "`||` lets a failed guard pass"),
    (lambda copy: _edit(copy, SIGN, "      - name: Accept the pair as a reader would\n",
                        "      - name: Accept the pair as a reader would\n        if: false\n"),
     "a step condition can skip a guard"),
    (lambda copy: _edit(copy, SIGN, "      - name: Push to gh-pages\n",
                        "      - name: Push to gh-pages\n        if: always()\n"),
     "a step condition can skip a guard"),
    (lambda copy: _edit(copy, SIGN, "      - name: Push to gh-pages\n        shell: bash\n",
                        "      - name: Push to gh-pages\n"),
     "declares `shell: bash`"),
    (lambda copy: _edit(copy, SIGN, "          git push origin HEAD:gh-pages\n",
                        "          git push origin HEAD:gh-pages; true\n"),
     "something after a guard decides"),
    (lambda copy: _edit(copy, SIGN, "          set -e\n" if False else "          git worktree add",
                        "          set +e\n          git worktree add"),
     "`set +e` lets a failed guard pass"),
    (lambda copy: _edit(copy, "index.yml", "      - name: The workflows\n",
                        "      - name: The workflows\n        continue-on-error: true\n"),
     "continue-on-error"),
    (lambda copy: _edit(copy, "index.yml", "        run: python3 tools/make-index.py tree\n",
                        "        run: python3 tools/make-index.py tree | tee tree.log\n"),
     "something after a guard"),
    (lambda copy: _edit(copy, "emergency-index.yml",
                        "          python3 tools/make-index.py verify",
                        "          true && python3 tools/make-index.py verify"),
     "a guard in a list can be skipped or ignored"),
])
def test_each_weakening_is_refused(copy, change, words):
    assert check_workflows.check(copy) == []
    change(copy)
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
    _edit(copy, "ci.yml", CHECKOUT, "      - uses: actions/checkout@v4\n")
    assert check_workflows.main(["--workflows", str(copy)]) == 1
    assert re.search(r"::error::ci\.yml: job \w+, step \d+: `actions/checkout@v4` is not pinned",
                     capsys.readouterr().out)
    assert check_workflows.main(["--workflows", str(WORKFLOWS)]) == 0


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
