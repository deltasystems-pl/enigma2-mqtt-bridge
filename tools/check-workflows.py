#!/usr/bin/env python3
"""Refuse a workflow change that would weaken the signing chain.

    tools/check-workflows.py [--workflows .github/workflows]

`index.yml` runs this on every pull request. The main key signs the release index in CI
(ADR-0015, decision 2), so the files under `.github/workflows/` decide who can reach it; the
`main` ruleset makes a change to them go through a pull request, and this is what that pull
request is checked against. The job that holds the key is pinned whole, and its two steps
before any repository code are fixed texts; behind the pin, every rule below is checked on its own,
so a pull request that re-pins still meets them. It fails when:

- any workflow is triggered by `pull_request_target` - the one trigger that runs a fork's code
  with this repository's token and secrets;
- any `uses:` - a step's or a job's, block or flow style - is not pinned to a full 40-digit commit
  SHA with its version in a comment beside it (a tag can be moved; a commit cannot) - local
  actions and digest-pinned images aside;
- the `secrets` context appears anywhere but once, as `${{ secrets.INDEX_SIGNING_KEY }}` in the
  environment of the sign step of `publish-index.yml`'s `sign` job - so `toJSON(secrets)`,
  `secrets['...']` and `secrets: inherit` are refused as well;
- any job but that one runs in, or any workflow but that one names, the `release-signing`
  environment, or any job computes its environment with an expression;
- the sign job's text is not the one pinned here by sha256 (any change updates the pin in the
  same pull request, where review sees both); it does not run in `release-signing` with
  `permissions: {}` on a fresh hosted `ubuntu-24.04` runner; it declares `env`, `defaults`,
  `container` or `services` (or the workflow declares `env` or `defaults`); it uses any action but
  `actions/checkout` and `actions/download-artifact`; it installs or downloads anything; a step of
  it becomes root, starts something in the background, touches `/usr`, or touches `GITHUB_PATH`,
  `GITHUB_ENV`, `BASH_ENV`, `ENV`, `PATH` or `LD_PRELOAD`; or anything but the artifact download
  and the hash check runs before the key is used - no checkout, no code from the repository;
- the sign step and the hash step are not, byte for byte, the texts written here (`SIGN_STEP`,
  `HASH_STEP`): the key is expanded once, into the pipeline that decodes it for OpenSSL's stdin,
  and OpenSSL's arguments are fixed - so no slice of the key can be printed and no provider,
  engine or configuration loaded into the process that reads it;
- any workflow passes `--keyset`, which only the tests and the rehearsal may use;
- `publish-index.yml`, `emergency-index.yml` or `index.yml` could let a failed guard pass: a run
  step without `shell: bash` (so without pipefail); a `continue-on-error`; a step condition other
  than build's release step; an `always()`, `failure()` or `cancelled()`; `set +e`, `trap`, `exec`
  or `||`; or a guard command (`make-index.py`, `check-workflows.py`, `rehearse-signing.py`,
  `sha256sum`, `git push`) inside `if`/`while`/`!`, in an `&&`/`;` list, or followed by `|`;
- `publish-index.yml` or `emergency-index.yml` leaves its own concurrency group, or has a job whose
  condition is not exactly `github.ref == 'refs/heads/main'`, alone or followed by `&& ( ... )`.

It reads the files with a small YAML reader of its own - the runner's Python has no YAML module,
and this must run without installing one - that understands exactly the subset these workflows
use and **fails on anything else**, rather than guessing. The tests hold it to PyYAML's reading of
every workflow in the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

SECRET = "INDEX_SIGNING_KEY"
SIGN_WORKFLOW = "publish-index.yml"
SIGN_JOB = "sign"
ENVIRONMENT = "release-signing"
SIGN_JOB_ACTIONS = ("actions/checkout", "actions/download-artifact")
# The workflows that publish the index, and the concurrency group each keeps to.
CHAIN = {"publish-index.yml": "publish-index", "emergency-index.yml": "emergency-index"}
MAIN_ONLY = "github.ref == 'refs/heads/main'"

_PINNED = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}", re.ASCII)
_DOCKER = re.compile(r"docker://[^@\s]+@sha256:[0-9a-f]{64}", re.ASCII)
_VERSION_COMMENT = re.compile(r"#\s*v?[0-9]+(\.[0-9]+)*\S*\s*$")


class YAMLError(Exception):
    pass


# ------------------------------------------------------------ the YAML subset --


def _strip_comment(text):
    """`text` without a trailing `# comment`. A quote opens a quoted scalar only where a scalar
    starts - after `: `, `- `, `[`, `{`, `,` or at the start - so `it's` in a plain scalar is
    just an apostrophe, and a `#` inside quotes is not a comment."""
    quote = None
    for position, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            before = text[:position].rstrip()
            if not before or before[-1] in ":-[{," :
                quote = char
        elif char == "#" and (position == 0 or text[position - 1] in " \t"):
            return text[:position].rstrip()
    if quote:
        raise YAMLError(f"unterminated quote in {text!r}")
    return text.rstrip()


def _scalar(text):
    text = text.strip()
    if text == "":
        return None
    if text[0] == '"':
        if len(text) < 2 or text[-1] != '"' or "\\" in text[1:-1]:
            raise YAMLError(f"unsupported double-quoted scalar {text!r}")
        return text[1:-1]
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'":
            raise YAMLError(f"unsupported single-quoted scalar {text!r}")
        return text[1:-1].replace("''", "'")
    if text[0] == "[":
        if text[-1] != "]":
            raise YAMLError(f"a flow sequence must close on its line: {text!r}")
        inner = text[1:-1].strip()
        return [_scalar(item) for item in _split_flow(inner)] if inner else []
    if text[0] == "{":
        if text[-1] != "}":
            raise YAMLError(f"a flow mapping must close on its line: {text!r}")
        inner = text[1:-1].strip()
        result = {}
        for item in _split_flow(inner) if inner else []:
            name, colon, value = item.partition(":")
            if not colon:
                raise YAMLError(f"not a mapping entry: {item!r}")
            result[_scalar(name)] = _scalar(value)
        return result
    if text[0] in "&*!|>%@`":
        raise YAMLError(f"unsupported YAML construct {text!r}")
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~"):
        return None
    if re.fullmatch(r"-?[0-9]+", text, re.ASCII):
        return int(text)
    return text


def _split_flow(inner):
    items, depth, quote, current = [], 0, None, ""
    for char in inner:
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(current)
            current = ""
            continue
        current += char
    items.append(current)
    return [item.strip() for item in items]


class _Lines:
    def __init__(self, text):
        if "\t" in text:
            raise YAMLError("a tab in a workflow")
        self.raw = text.split("\n")
        self.position = 0

    def peek(self):
        """(indent, content) of the next line that carries anything, or None."""
        while self.position < len(self.raw):
            line = self.raw[self.position]
            content = _strip_comment(line)
            if content.strip():
                if content.strip() in ("---", "..."):
                    raise YAMLError("document markers are not supported")
                return len(line) - len(line.lstrip(" ")), content.strip()
            self.position += 1
        return None


def _block_scalar(lines, parent_indent, style):
    if style not in ("|", "|-", "|+", ">", ">-", ">+"):
        raise YAMLError(f"unsupported block scalar header {style!r}")
    collected, indent = [], None
    while lines.position < len(lines.raw):
        line = lines.raw[lines.position]
        if line.strip() == "":
            collected.append("")
            lines.position += 1
            continue
        current = len(line) - len(line.lstrip(" "))
        if current <= parent_indent:
            break
        if indent is None:
            indent = current
        if current < indent:
            raise YAMLError("a block scalar line is indented less than its first line")
        collected.append(line[indent:])
        lines.position += 1
    while collected and collected[-1] == "":
        collected.pop()
    if style[0] == ">":
        # Folding keeps a newline around an empty or a more-indented line; this reader does not
        # reproduce that, so it refuses both rather than read an expression differently.
        if any(part == "" or part.startswith(" ") for part in collected):
            raise YAMLError("a folded scalar with an empty or a more-indented line")
        text = " ".join(collected)
    else:
        text = "\n".join(collected)
    return text if style.endswith("-") else text + "\n"


def _value(lines, indent, rest):
    """The value after `key:` or `- `: inline, a block scalar, or a nested block."""
    if rest in ("|", "|-", "|+", ">", ">-", ">+"):
        return _block_scalar(lines, indent, rest)
    if rest:
        return _scalar(rest)
    following = lines.peek()
    if following is None or following[0] <= indent and not (
        following[0] == indent and following[1].startswith("- ")
    ):
        return None
    return _block(lines, following[0])


def _key(text):
    name, colon, rest = text.partition(":")
    if not colon or (rest and not rest.startswith(" ")):
        raise YAMLError(f"not a mapping entry: {text!r}")
    return _scalar(name), rest.strip()


def _block(lines, indent):
    first = lines.peek()
    if first[1].startswith("- ") or first[1] == "-":
        return _sequence(lines, indent)
    return _mapping(lines, indent)


def _mapping(lines, indent):
    result = {}
    while True:
        line = lines.peek()
        if line is None or line[0] < indent:
            return result
        if line[0] > indent or line[1].startswith("- "):
            raise YAMLError(f"unexpected indentation at line {lines.position + 1}")
        name, rest = _key(line[1])
        if name in result:
            raise YAMLError(f"duplicate key {name!r} at line {lines.position + 1}")
        lines.position += 1
        result[name] = _value(lines, indent, rest)


def _sequence(lines, indent):
    result = []
    while True:
        line = lines.peek()
        if line is None or line[0] < indent or not (line[1].startswith("- ") or line[1] == "-"):
            if line is not None and line[0] > indent:
                raise YAMLError(f"unexpected indentation at line {lines.position + 1}")
            return result
        if line[0] > indent:
            raise YAMLError(f"unexpected indentation at line {lines.position + 1}")
        item = line[1][1:].strip()
        starts_scalar = item.startswith(("'", '"', "[", "{"))
        if item and not starts_scalar and re.match(r"[^:]+: |[^:]+:$", item):
            # "- key: value" starts a mapping whose keys sit two columns in.
            inner = indent + 2
            lines.raw[lines.position] = " " * inner + item
            result.append(_mapping(lines, inner))
        else:
            lines.position += 1
            result.append(_value(lines, indent, item))


def load(text):
    """The workflow in `text` as dicts, lists and scalars - or YAMLError for anything unusual."""
    lines = _Lines(text)
    first = lines.peek()
    if first is None:
        return {}
    if first[0] != 0:
        raise YAMLError("the document must start at column 0")
    result = _block(lines, 0)
    if lines.peek() is not None:
        raise YAMLError(f"unexpected content at line {lines.position + 1}")
    return result


# ---------------------------------------------------------------- the checks --

# The sign job's whole text, pinned: any change to the one job that holds the key must change this
# pin in the same pull request, so it can never pass review unnoticed - the diff shows the new
# text and the new hash side by side.
SIGN_JOB_SHA256 = "ff121e5c6561cdf033d6de2b67a245d548d331132e0e6270dfa9f09f97f93292"

# The two steps that run before any code from the repository, exactly - not a pattern. The sign
# step expands the key once, into the one pipeline that decodes it for OpenSSL's stdin, and
# nowhere else (`${INDEX_SIGNING_KEY:+set}` is "set" or nothing, never the key); OpenSSL's
# arguments are fixed, so no provider, engine or configuration can be loaded into the process that
# reads the key.
HASH_STEP = ("printf '%s  index/releases.json\\n' \"$EXPECTED\" "
             "| /usr/bin/sha256sum --check --strict")
SIGN_STEP = """if [ -z "${INDEX_SIGNING_KEY:+set}" ]; then
  echo "::error::the release-signing environment has no INDEX_SIGNING_KEY"
  exit 1
fi
umask 077
{
  printf '\\x30\\x2e\\x02\\x01\\x00\\x30\\x05\\x06\\x03\\x2b\\x65\\x70\\x04\\x22\\x04\\x20'
  printf '%s' "$INDEX_SIGNING_KEY" | /usr/bin/env -u INDEX_SIGNING_KEY /usr/bin/base64 -d
} | /usr/bin/env -u INDEX_SIGNING_KEY /usr/bin/openssl pkeyutl -sign -rawin -keyform DER \\
      -inkey /dev/stdin -in index/releases.json -out index/releases.json.raw-sig
"""
SIGN_RUNNER = "ubuntu-24.04"

# Environment files and variables a step can use to change what a later step runs.
_STEERING = re.compile(r"\b(GITHUB_PATH|GITHUB_ENV|BASH_ENV|ENV|LD_PRELOAD|PATH)\b")
# What a step of the sign job may not do at all: become root, leave something running, write
# where the programs the key passes through live.
_SIGN_JOB_FORBIDDEN = ("sudo", "su", "doas", "nohup", "setsid", "disown", "&", "at", "crontab",
                       "systemd-run")
_STATUS_FUNCTIONS = re.compile(r"\b(always|failure|cancelled)\s*\(")

# The commands whose failure must fail their step: in the chain workflows and index.yml each runs
# as a plain command - on its own line, or as `name=$(...)`, or last in a pipeline under pipefail -
# never inside `if`/`while`/`!`, never followed by `|`, `&&`, `||`, `;` or `&`.
_GUARDS = ("tools/make-index.py", "tools/check-workflows.py", "tools/rehearse-signing.py",
           "/usr/bin/sha256sum")
_CONDITIONAL = ("if", "then", "elif", "else", "while", "until", "do", "!", "case")
_AFTER_GUARD = ("|", "||", "&&", ";", "&")
_WEAKENING = ("set +e", "set +o", "trap ", "exec ", "|| ")
# The one step-level condition the chain carries: build's release step, which only runs for a
# release run and whose output is empty otherwise.
_ALLOWED_STEP_CONDITIONS = {("publish-index.yml", "build", "release"):
                            "github.event_name == 'workflow_run'"}


def _steps(workflow):
    for job_name, job in (workflow.get("jobs") or {}).items():
        for number, step in enumerate(job.get("steps") or [], 1):
            yield job_name, job, number, step


def _code(text):
    """The workflow text without its full-line comments."""
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.split("\n"))


def _words(script):
    """The shell words of `script`, comments and quoting removed, one list per logical line - a
    line ended by a backslash continues on the next."""
    for line in script.replace("\\\n", " ").split("\n"):
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            yield list(lexer)
        except ValueError:
            yield line.split()


def job_text(text, job_name):
    """The raw text of one job of a workflow: from `  <job>:` to the next job or the end."""
    lines = text.split("\n")
    start = next((index for index, line in enumerate(lines) if line == f"  {job_name}:"), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(lines))
                if lines[index][:2] == "  " and lines[index][2:3] not in (" ", "", "#")),
               len(lines))
    block = lines[start:end]
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block) + "\n"


def _environment_name(job):
    value = job.get("environment")
    if isinstance(value, dict):
        value = value.get("name")
    return value


def _main_only(condition):
    """Whether a job condition is `MAIN_ONLY`, or `MAIN_ONLY && ( ... )` with nothing after the
    group - so no `|| true` or anything else can loosen it."""
    condition = " ".join(str(condition or "").split())
    if condition == MAIN_ONLY:
        return True
    prefix = MAIN_ONLY + " && ("
    if not condition.startswith(prefix):
        return False
    depth, quote = 0, False
    rest = condition[len(prefix) - 1:]
    for position, char in enumerate(rest):
        if char == "'":
            quote = not quote
        elif not quote and char == "(":
            depth += 1
        elif not quote and char == ")":
            depth -= 1
            if depth == 0:
                return position == len(rest) - 1
    return False


def _uses(workflow):
    for job_name, job in (workflow.get("jobs") or {}).items():
        if job.get("uses"):
            yield job_name, None, job["uses"]
        for number, step in enumerate(job.get("steps") or [], 1):
            if isinstance(step, dict) and step.get("uses"):
                yield job_name, number, step["uses"]


def check_guards(run, where):
    """The structural guard rule for one `run` text of the chain or index.yml."""
    problems = []
    for weakening in _WEAKENING:
        if weakening in run:
            problems.append(f"{where}: `{weakening.strip()}` lets a failed guard pass")
    for words in _words(run):
        guard = next((position for position, word in enumerate(words)
                      if any(word.endswith(name) or word == name for name in _GUARDS)
                      or (word == "git" and "push" in words[position + 1:position + 2])), None)
        if guard is None:
            continue
        before, after = words[:guard], words[guard + 1:]
        if any(word in _CONDITIONAL for word in words):
            problems.append(f"{where}: a guard inside a condition can never fail its step")
        if any(word in _AFTER_GUARD for word in after):
            problems.append(f"{where}: something after a guard decides the step's status "
                            f"(`{next(word for word in after if word in _AFTER_GUARD)}`)")
        if any(word in ("||", "&&", ";", "&") for word in before):
            problems.append(f"{where}: a guard in a list can be skipped or ignored")
    return problems


def check(workflows=WORKFLOWS):
    problems = []
    files = sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml"))
    loaded, texts = {}, {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        texts[path.name] = text
        try:
            loaded[path.name] = load(text)
        except YAMLError as error:
            problems.append(f"{path.name}: not in the YAML subset this check reads: {error}")
            continue
        workflow = loaded[path.name]
        triggers = workflow.get("on")
        if isinstance(triggers, str):
            triggers = [triggers]
        if "pull_request_target" in (triggers or []):
            problems.append(f"{path.name}: pull_request_target runs a fork's code with this "
                            "repository's token and secrets")
        code = _code(text)
        if "--keyset" in code:
            problems.append(f"{path.name}: --keyset is for the tests and the rehearsal only")
        # Pins, from the parsed structure: a flow-style step `- {uses: ...}` is a step too.
        lines = text.split("\n")
        for job_name, number, reference in _uses(workflow):
            where = f"{path.name}: job {job_name}" + (f", step {number}" if number else "")
            reference = str(reference).strip()
            if reference.startswith("./") or _DOCKER.fullmatch(reference):
                continue
            if not _PINNED.fullmatch(reference):
                problems.append(f"{where}: `{reference}` is not pinned to a full commit SHA")
            elif not any(reference in line and _VERSION_COMMENT.search(line) for line in lines):
                problems.append(f"{where}: `{reference}` has no `# vX.Y.Z` comment saying which "
                                "version the SHA is")
        # The secrets context: one use in the whole repository, the sign step's key.
        mentions = len(re.findall(r"\bsecrets\b", code))
        expected = 1 if path.name == SIGN_WORKFLOW else 0
        if mentions != expected or (expected and "${{ secrets." + SECRET + " }}" not in code):
            problems.append(f"{path.name}: `secrets` appears {mentions} times, expected "
                            f"{expected} - the sign step's `${{{{ secrets.{SECRET} }}}}` is the "
                            "only secret any workflow reads")
        # The environment that releases the key: the sign job alone names it.
        for job_name, job in (workflow.get("jobs") or {}).items():
            name = _environment_name(job)
            if name is None:
                continue
            if "${{" in str(name):
                problems.append(f"{path.name}: job {job_name} computes its environment; name it")
            elif name == ENVIRONMENT and (path.name, job_name) != (SIGN_WORKFLOW, SIGN_JOB):
                problems.append(f"{path.name}: job {job_name} runs in {ENVIRONMENT}; only "
                                f"{SIGN_WORKFLOW}'s {SIGN_JOB} job may")
        if path.name != SIGN_WORKFLOW and ENVIRONMENT in code:
            problems.append(f"{path.name}: names the {ENVIRONMENT} environment; only "
                            f"{SIGN_WORKFLOW} may")

    problems.extend(check_sign_job(loaded, texts))

    for name in (*CHAIN, "index.yml"):
        workflow = loaded.get(name)
        if workflow is None:
            problems.append(f"{name}: missing")
            continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            if "continue-on-error" in job or any(
                isinstance(step, dict) and "continue-on-error" in step
                for step in job.get("steps") or []
            ):
                problems.append(f"{name}: job {job_name}: continue-on-error lets a failed guard "
                                "pass")
            if _STATUS_FUNCTIONS.search(str(job.get("if") or "")):
                problems.append(f"{name}: job {job_name} runs after a failed job it needs")
            for number, step in enumerate(job.get("steps") or [], 1):
                if not isinstance(step, dict):
                    continue
                where = f"{name}: job {job_name}, step {number}"
                condition = step.get("if")
                allowed = _ALLOWED_STEP_CONDITIONS.get((name, job_name, step.get("id")))
                if condition is not None and str(condition).strip() != allowed:
                    problems.append(f"{where}: a step condition can skip a guard - only build's "
                                    "release step may carry one")
                if "run" not in step:
                    continue
                if step.get("shell") != "bash":
                    problems.append(f"{where}: every run step here declares `shell: bash` "
                                    "(bash -eo pipefail), so a failure in a pipeline fails it")
                problems.extend(check_guards(step.get("run") or "", where))
    for name, group in CHAIN.items():
        workflow = loaded.get(name)
        if workflow is None:
            continue
        concurrency = workflow.get("concurrency") or {}
        if concurrency.get("group") != group or concurrency.get("cancel-in-progress") is not \
                False:
            problems.append(f"{name}: must run in the `{group}` concurrency group, never "
                            "cancelled")
        if workflow.get("permissions") != {}:
            problems.append(f"{name}: must declare `permissions: {{}}` at the top")
        for job_name, job in (workflow.get("jobs") or {}).items():
            if not _main_only(job.get("if")):
                problems.append(f"{name}: job {job_name} must be refused off `main` "
                                f"(`if: {MAIN_ONLY}`, or that `&& (...)` and nothing more)")
            if job_name != SIGN_JOB and job.get("environment"):
                problems.append(f"{name}: job {job_name} has an environment; only sign may")
    return problems


def check_sign_job(loaded, texts):
    problems = []
    sign_steps = []
    for name, workflow in loaded.items():
        for job_name, job, number, step in _steps(workflow):
            if isinstance(step, dict) and SECRET in str(step.get("env") or {}):
                sign_steps.append((name, job_name, job, number, step))
    if len(sign_steps) != 1:
        problems.append(f"{len(sign_steps)} steps carry {SECRET}; exactly one - the sign step - "
                        "may")
    for name, job_name, job, number, step in sign_steps:
        where = f"{name}: job {job_name}, step {number}"
        if (name, job_name) != (SIGN_WORKFLOW, SIGN_JOB):
            problems.append(f"{where}: the key belongs to {SIGN_WORKFLOW}'s {SIGN_JOB} job only")
            continue
        text = job_text(texts[name], job_name) or ""
        pinned = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if pinned != SIGN_JOB_SHA256:
            problems.append(f"{name}: job {job_name}: the sign job's text is not the pinned one "
                            f"(its sha256 is {pinned}); a change to it must update "
                            "SIGN_JOB_SHA256 in tools/check-workflows.py in the same pull request")
        if step.get("run") != SIGN_STEP:
            problems.append(f"{where}: the sign step is not the fixed one - it must be "
                            "tools/check-workflows.py's SIGN_STEP, byte for byte")
        if step.get("shell") != "bash":
            problems.append(f"{where}: must declare `shell: bash` (bash -eo pipefail), exactly")
        env = step.get("env") or {}
        if env != {SECRET: "${{ secrets." + SECRET + " }}"}:
            problems.append(f"{where}: the step's environment is {SECRET} from secrets.{SECRET} "
                            "and nothing else")
        if _environment_name(job) != ENVIRONMENT:
            problems.append(f"{where}: the job must run in the {ENVIRONMENT} environment")
        if job.get("permissions") != {}:
            problems.append(f"{where}: the job must declare `permissions: {{}}`")
        if job.get("runs-on") != SIGN_RUNNER:
            problems.append(f"{name}: job {job_name} runs on `{job.get('runs-on')}`; only a fresh "
                            f"hosted `{SIGN_RUNNER}` runner may hold the key")
        for key in ("env", "defaults", "container", "services", "uses"):
            if key in job:
                problems.append(f"{name}: job {job_name} may not declare `{key}`: it would reach "
                                "the sign step")
        for key in ("env", "defaults"):
            if key in loaded[name]:
                problems.append(f"{name}: may not declare `{key}` at the top: it would reach the "
                                "sign step")
        # Before the key: the artifact and its hash, and nothing from the repository.
        for other_number, other in enumerate(job.get("steps") or [], 1):
            other_where = f"{name}: job {job_name}, step {other_number}"
            if other is step:
                break
            uses = other.get("uses") if isinstance(other, dict) else None
            if uses and uses.split("@")[0] == "actions/download-artifact":
                continue
            if isinstance(other, dict) and other.get("run") == HASH_STEP and \
                    other.get("shell") == "bash":
                continue
            problems.append(f"{other_where}: before the sign step the job may only download the "
                            "artifact and check its hash - no checkout, no code from the "
                            "repository")
        for other_number, other in enumerate(job.get("steps") or [], 1):
            other_where = f"{name}: job {job_name}, step {other_number}"
            if not isinstance(other, dict):
                problems.append(f"{other_where}: not a step")
                continue
            uses = other.get("uses")
            if uses and uses.split("@")[0] not in SIGN_JOB_ACTIONS:
                problems.append(f"{other_where}: `{uses}` - the sign job uses only "
                                f"{', '.join(SIGN_JOB_ACTIONS)}")
            run = other.get("run") or ""
            if re.search(r"\b(pip|pip3|setup-python|apt-get|apt|npm|curl|wget)\b", run):
                problems.append(f"{other_where}: the sign job installs and downloads nothing")
            steering = _STEERING.findall(run) + [
                variable for variable in (other.get("env") or {}) if _STEERING.fullmatch(variable)]
            if steering:
                problems.append(f"{other_where}: `{steering[0]}` - no step of the sign job may "
                                "change what a later step runs")
            if other is not step and run != HASH_STEP:
                for words in _words(run):
                    bad = [word for word in words if word in _SIGN_JOB_FORBIDDEN
                           or word.startswith("/usr/")]
                    if bad:
                        problems.append(f"{other_where}: `{bad[0]}` - no step of the sign job "
                                        "becomes root, runs in the background or touches /usr")
    return problems


def sign_step_script(workflows=WORKFLOWS):
    """The sign step's `run` text, as the runner would get it - for the rehearsal."""
    return _sign_job_step(workflows, lambda step: SECRET in str(step.get("env") or {}))


def hash_step_script(workflows=WORKFLOWS):
    """The hash step's `run` text - for the rehearsal."""
    return _sign_job_step(workflows, lambda step: "sha256sum" in str(step.get("run") or ""))


def _sign_job_step(workflows, wanted):
    workflow = load((workflows / SIGN_WORKFLOW).read_text(encoding="utf-8"))
    for job_name, _job, _number, step in _steps(workflow):
        if job_name == SIGN_JOB and isinstance(step, dict) and wanted(step):
            return step["run"]
    raise YAMLError(f"{SIGN_WORKFLOW} has no such step in its sign job")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflows", type=Path, default=WORKFLOWS)
    args = parser.parse_args(argv)
    problems = check(args.workflows)
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1
    print(f"{len(list(args.workflows.glob('*.y*ml')))} workflows checked: nothing weakens the "
          "signing chain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
