#!/usr/bin/env python3
"""Refuse a workflow change that would weaken the signing chain.

    tools/check-workflows.py [--workflows .github/workflows]

`index.yml` runs this on every pull request. The main key signs the release index in CI
(ADR-0015, decision 2), so the files under `.github/workflows/` decide who can reach it; the
`main` ruleset makes a change to them go through a pull request, and this is what that pull
request is checked against. It is a deny-list around an allow-list: the one step that holds the
key may run only what is listed, and everything around it is checked for the ways found to reach
the key or to loosen a guard. It fails when:

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
- the sign job is not in `release-signing` with `permissions: {}`; declares `env`, `defaults`,
  `container` or `services` (or the workflow declares `env` or `defaults`); uses any action but
  `actions/checkout` and `actions/download-artifact`; installs or downloads anything; or has a
  step that touches `GITHUB_PATH`, `GITHUB_ENV`, `BASH_ENV`, `ENV`, `PATH` or `LD_PRELOAD` - the
  ways an earlier step changes what a later one runs;
- the sign step's text is not the one pinned here by sha256 (any change updates the pin in the
  same pull request, where review sees both), does not declare `shell: bash` (`bash -eo
  pipefail`), or runs anything but the emptiness test, `echo`, `exit`, `umask`, `printf` and
  `/usr/bin/env -u INDEX_SIGNING_KEY` in front of `/usr/bin/base64 -d` or
  `/usr/bin/openssl pkeyutl -sign` - so `xxd`, `od`, `tee`, `printenv`, `export`, an interpreter
  or any other program is refused by name; contains tracing, a redirection, `||` or `&&`, or a
  command substitution; or expands the key anywhere but in the emptiness test and the one
  `printf '%s'` that pipes it into `base64 -d`;
- any workflow passes `--keyset`, which only the tests and the rehearsal may use;
- `publish-index.yml`, `emergency-index.yml` or `index.yml` lets a failed guard pass - a
  `continue-on-error`, a `||` or `set +e` in a step, an `always()`, `failure()` or `cancelled()`
  condition;
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

# The sign step's exact text. Any change to it must change this pin in the same pull request, so
# a change to the one step that holds the key can never pass review unnoticed: the diff shows the
# new text and the new hash side by side.
SIGN_STEP_SHA256 = "ecc2adbd7d0ac03570be08db8af3d29a262720f1bb11c57049ef8ad63c408bfe"

# What the sign step may run: the emptiness test and its error, umask, the two printfs, and the
# two programs - by absolute path, under `/usr/bin/env -u INDEX_SIGNING_KEY`.
ENV = "/usr/bin/env"
PROGRAMS = {"/usr/bin/base64": ["-d"], "/usr/bin/openssl": ["pkeyutl", "-sign"]}
ALLOWED_COMMANDS = ("[", "echo", "exit", "umask", "printf", ENV)
_KEYWORDS = ("if", "then", "else", "elif", "fi", "{", "}", "!")
_SEPARATORS = ("|", ";")
_EXPANDS_SECRET = re.compile(r"\$\{?" + SECRET + r"\b")

# Environment files and variables a step can use to change what a later step runs.
_STEERING = re.compile(r"\b(GITHUB_PATH|GITHUB_ENV|BASH_ENV|ENV|LD_PRELOAD|PATH)\b")
_GUARD_BYPASS = re.compile(r"\|\||\bset\s+\+e\b")
_STATUS_FUNCTIONS = re.compile(r"\b(always|failure|cancelled)\s*\(")


def _steps(workflow):
    for job_name, job in (workflow.get("jobs") or {}).items():
        for number, step in enumerate(job.get("steps") or [], 1):
            yield job_name, job, number, step


def _code(text):
    """The workflow text without its full-line comments."""
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.split("\n"))


def _words(script):
    """The shell words of `script`, comments and quoting removed, one list per line; a line ended
    by a backslash continues on the next."""
    for line in script.replace("\\\n", " ").split("\n"):
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            yield list(lexer)
        except ValueError:
            # A line that continues a quote from the previous one; its words still count.
            yield line.split()


def _commands(words):
    """`(position, word)` of every command word on a line: the first word that is not a keyword
    after the start and after every separator."""
    expect = True
    for position, word in enumerate(words):
        if word in _SEPARATORS:
            expect = True
        elif word in _KEYWORDS:
            continue
        elif expect:
            yield position, word
            expect = False


def check_sign_step(step, where):
    problems = []
    if step.get("shell") != "bash":
        problems.append(f"{where}: must declare `shell: bash` (bash -eo pipefail), exactly")
    script = step.get("run") or ""
    pinned = hashlib.sha256(script.encode("utf-8")).hexdigest()
    if pinned != SIGN_STEP_SHA256:
        problems.append(f"{where}: the sign step's text is not the pinned one (its sha256 is "
                        f"{pinned}); a change to it must update SIGN_STEP_SHA256 in "
                        "tools/check-workflows.py in the same pull request")
    for words in _words(script):
        for position, word in enumerate(words):
            if word == "set" and any(
                (flag.startswith("-") and ("x" in flag or "v" in flag))
                or flag in ("xtrace", "verbose")
                for flag in words[position + 1:position + 3]
            ):
                problems.append(f"{where}: tracing (`set -x`, `set -v`) prints the key")
            if word in ("bash", "sh") and any(
                flag.startswith("-") and ("x" in flag or "v" in flag)
                for flag in words[position + 1:]
            ):
                problems.append(f"{where}: tracing (`{word} -x`) prints the key")
            if "$(" in word or "`" in word or word in ("<(", ">("):
                problems.append(f"{where}: `{word}` runs a command inside a word")
            if word and set(word) <= set("<>&") or word in ("||", "&&"):
                problems.append(f"{where}: `{word}` - no redirection, background or `||`/`&&` "
                                "in the sign step")
            if _EXPANDS_SECRET.search(word):
                # The key may be expanded in two places only: the emptiness test, and the one
                # `printf '%s'` that pipes it straight into `/usr/bin/env -u ... base64 -d`.
                starts = [start for start, _word in _commands(words) if start <= position]
                start = starts[-1] if starts else 0
                command = words[start] if start < len(words) else ""
                if command in ("[", "test"):
                    continue
                piped = words[position + 1:position + 7] == [
                    "|", ENV, "-u", SECRET, "/usr/bin/base64", "-d"]
                if command == "printf" and words[start + 1:position] == ["%s"] and piped:
                    continue
                problems.append(f"{where}: the key is expanded outside the one printf that "
                                "pipes it into base64 -d")
        for position, command in _commands(words):
            if command not in ALLOWED_COMMANDS:
                if command == "env":
                    problems.append(f"{where}: `env` other than `{ENV} -u {SECRET} <program>` "
                                    "prints or passes on the environment")
                elif command in ("base64", "openssl"):
                    problems.append(f"{where}: `{command}` must run by absolute path under "
                                    f"`{ENV} -u {SECRET}`, so the key is not in its "
                                    "environment")
                else:
                    problems.append(f"{where}: `{command}` - the sign step runs nothing but "
                                    f"{', '.join(ALLOWED_COMMANDS)}")
                continue
            if command == ENV:
                rest = words[position + 1:]
                program = rest[2] if len(rest) > 2 else ""
                if rest[:2] != ["-u", SECRET] or program not in PROGRAMS:
                    problems.append(f"{where}: `{ENV}` only as `{ENV} -u {SECRET}` before "
                                    f"{' or '.join(PROGRAMS)}")
                elif program.endswith("base64") and rest[3:4] != ["-d"]:
                    problems.append(f"{where}: a `base64` that encodes could print the key")
                elif rest[3:3 + len(PROGRAMS[program])] != PROGRAMS[program]:
                    problems.append(f"{where}: `{program}` only as "
                                    f"`{program} {' '.join(PROGRAMS[program])}`")
    for words in _words(script):
        for position, word in enumerate(words):
            if word in ("base64", "openssl", "/usr/bin/base64", "/usr/bin/openssl") and \
                    words[max(0, position - 3):position] != [ENV, "-u", SECRET]:
                problems.append(f"{where}: `{word}` must run under `{ENV} -u {SECRET}`, so the "
                                "key is not in its environment")
    if not _EXPANDS_SECRET.search(script):
        problems.append(f"{where}: does not use the key - is this still the sign step?")
    return problems


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

    sign_steps = []
    for name, workflow in loaded.items():
        for job_name, job, number, step in _steps(workflow):
            if SECRET in str(step.get("env") or {}):
                sign_steps.append((name, job_name, job, number, step))
    if len(sign_steps) != 1:
        problems.append(f"{len(sign_steps)} steps carry {SECRET}; exactly one - the sign step - "
                        "may")
    for name, job_name, job, number, step in sign_steps:
        where = f"{name}: job {job_name}, step {number}"
        if (name, job_name) != (SIGN_WORKFLOW, SIGN_JOB):
            problems.append(f"{where}: the key belongs to {SIGN_WORKFLOW}'s {SIGN_JOB} job only")
        env = step.get("env") or {}
        if env != {SECRET: "${{ secrets." + SECRET + " }}"}:
            problems.append(f"{where}: the step's environment is {SECRET} from secrets.{SECRET} "
                            "and nothing else")
        problems.extend(check_sign_step(step, where))
        if _environment_name(job) != ENVIRONMENT:
            problems.append(f"{where}: the job must run in the {ENVIRONMENT} environment")
        if job.get("permissions") != {}:
            problems.append(f"{where}: the job must declare `permissions: {{}}`")
        for key in ("env", "defaults", "container", "services", "uses"):
            if key in job:
                problems.append(f"{name}: job {job_name} may not declare `{key}`: it would reach "
                                "the sign step")
        for key in ("env", "defaults"):
            if key in loaded[name]:
                problems.append(f"{name}: may not declare `{key}` at the top: it would reach the "
                                "sign step")
        for _job, _j, other_number, other in _steps({"jobs": {job_name: job}}):
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
            for number, step in enumerate(job.get("steps") or [], 1):
                if not isinstance(step, dict):
                    continue
                if _GUARD_BYPASS.search(step.get("run") or ""):
                    problems.append(f"{name}: job {job_name}, step {number}: `||` or `set +e` "
                                    "lets a failed guard pass")
                if _STATUS_FUNCTIONS.search(str(step.get("if") or "")):
                    problems.append(f"{name}: job {job_name}, step {number}: a step that runs "
                                    "after a failure skips the guard before it")
            if _STATUS_FUNCTIONS.search(str(job.get("if") or "")):
                problems.append(f"{name}: job {job_name} runs after a failed job it needs")
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


def sign_step_script(workflows=WORKFLOWS):
    """The sign step's `run` text, as the runner would get it - for the rehearsal."""
    workflow = load((workflows / SIGN_WORKFLOW).read_text(encoding="utf-8"))
    for job_name, _job, _number, step in _steps(workflow):
        if job_name == SIGN_JOB and SECRET in str(step.get("env") or {}):
            return step["run"]
    raise YAMLError(f"{SIGN_WORKFLOW} has no sign step")


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
