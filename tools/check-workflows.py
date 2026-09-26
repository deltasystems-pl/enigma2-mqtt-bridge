#!/usr/bin/env python3
"""Refuse a workflow change that would weaken the signing chain.

    tools/check-workflows.py [--workflows .github/workflows]

`index.yml` runs this on every pull request. The main key signs the release index in CI
(ADR-0015, decision 2), so the files under `.github/workflows/` decide who can reach it; the
`main` ruleset makes a change to them go through a pull request, and this is what that pull
request is checked against. It fails when:

- any workflow is triggered by `pull_request_target` - the one trigger that runs a fork's code
  with this repository's token and secrets;
- any `uses:` is not pinned to a full 40-digit commit SHA with its version in a comment beside it
  (a tag can be moved; a commit cannot) - local actions and digest-pinned images aside;
- the key, `secrets.INDEX_SIGNING_KEY`, is referenced anywhere but in the environment of one step,
  the sign step of `publish-index.yml`'s `sign` job;
- that job is not in the `release-signing` environment with `permissions: {}`, uses any action but
  `actions/checkout` and `actions/download-artifact`, or installs anything;
- the sign step does not declare `shell: bash` (which is `bash -eo pipefail`), or contains
  tracing (`set -x`, `set -v`, `set -o xtrace`, `bash -x`), a command that could print or copy
  what passes through it (`xxd`, `od`, `hexdump`, `tee`, `printenv`, `export`, a bare `env`, a
  `base64` that encodes), expands the key anywhere but in the emptiness test and the one
  `printf '%s'` that pipes it into `env -u INDEX_SIGNING_KEY base64 -d`, or runs `base64` or
  `openssl` with the key still in their environment;
- any workflow passes `--keyset`, which only the tests and the rehearsal may use;
- `publish-index.yml` or `emergency-index.yml` leaves the `publish-index` concurrency group, or
  one of `publish-index.yml`'s jobs can run on a ref other than `main`.

It reads the files with a small YAML reader of its own - the runner's Python has no YAML module,
and this must run without installing one - that understands exactly the subset these workflows
use and **fails on anything else**, rather than guessing. The tests hold it to PyYAML's reading of
every workflow in the repository.
"""

from __future__ import annotations

import argparse
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
CHAIN = ("publish-index.yml", "emergency-index.yml")
CONCURRENCY = "publish-index"
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


def _steps(workflow):
    for job_name, job in (workflow.get("jobs") or {}).items():
        for number, step in enumerate(job.get("steps") or [], 1):
            yield job_name, job, number, step


def _raw_uses_lines(text):
    for number, line in enumerate(text.split("\n"), 1):
        match = re.match(r"\s*(-\s+)?uses:\s*(.*)$", line)
        if match:
            yield number, match.group(2)


def _words(script):
    """The shell words of `script`, comments and quoting removed, one list per line."""
    for line in script.split("\n"):
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            yield list(lexer)
        except ValueError:
            # A line that continues a quote from the previous one; its words still count.
            yield line.split()


FORBIDDEN = ("xxd", "od", "hexdump", "tee", "printenv", "declare", "export", "typeset")
_EXPANDS_SECRET = re.compile(r"\$\{?" + SECRET + r"\b")
_SEPARATORS = ("|", "||", "&&", ";", "{", "}", "(", ")", "!", "if", "then", "else", "do")


def _command_word(words, position):
    """The command word of the simple command the word at `position` belongs to."""
    start = 0
    for index in range(position):
        if words[index] in _SEPARATORS:
            start = index + 1
    return words[start] if start < len(words) else "", start


def check_sign_step(step, where):
    problems = []
    if step.get("shell") != "bash":
        problems.append(f"{where}: must declare `shell: bash` (bash -eo pipefail), exactly")
    script = step.get("run") or ""
    for words in _words(script):
        for position, word in enumerate(words):
            previous = words[:position]
            if word in FORBIDDEN:
                problems.append(f"{where}: `{word}` could print or copy the key")
            if word == "set" and any(
                (flag.startswith("-") and "x" in flag) or flag in ("xtrace", "verbose", "-v")
                or (flag.startswith("-") and "v" in flag)
                for flag in words[position + 1:position + 3]
            ):
                problems.append(f"{where}: tracing (`set -x`, `set -v`) prints the key")
            if word in ("bash", "sh") and any(
                flag.startswith("-") and ("x" in flag or "v" in flag)
                for flag in words[position + 1:]
            ):
                problems.append(f"{where}: tracing (`{word} -x`) prints the key")
            if word == "env" and (position + 1 == len(words) or words[position + 1] != "-u"):
                problems.append(f"{where}: `env` other than `env -u {SECRET} <command>` prints "
                                "or passes on the environment")
            if word == "base64":
                rest = words[position + 1:]
                if not any(flag in ("-d", "--decode") for flag in rest[:2]):
                    problems.append(f"{where}: a `base64` that encodes could print the key")
            if word in ("base64", "openssl") and previous[-3:] != ["env", "-u", SECRET]:
                problems.append(
                    f"{where}: `{word}` must run under `env -u {SECRET}`, so the key is not "
                    "in its environment"
                )
            if _EXPANDS_SECRET.search(word):
                # The key may be expanded in two places only: the emptiness test, and the one
                # `printf '%s'` that pipes it straight into `env -u ... base64 -d`.
                command, start = _command_word(words, position)
                if command in ("[", "test"):
                    continue
                piped = words[position + 1:position + 7] == ["|", "env", "-u", SECRET, "base64",
                                                               "-d"]
                if command == "printf" and words[start + 1:position] == ["%s"] and piped:
                    continue
                problems.append(f"{where}: the key is expanded outside the one printf that "
                                "pipes it into base64 -d")
    if not _EXPANDS_SECRET.search(script):
        problems.append(f"{where}: does not use the key - is this still the sign step?")
    return problems


def check(workflows=WORKFLOWS):
    problems = []
    files = sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml"))
    loaded = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            loaded[path.name] = load(text)
        except YAMLError as error:
            problems.append(f"{path.name}: not in the YAML subset this check reads: {error}")
            continue
        triggers = loaded[path.name].get("on")
        if isinstance(triggers, str):
            triggers = [triggers]
        if "pull_request_target" in (triggers or []):
            problems.append(f"{path.name}: pull_request_target runs a fork's code with this "
                            "repository's token and secrets")
        code = "\n".join(_strip_comment(line) if line.lstrip().startswith("#") else line
                         for line in text.split("\n"))
        if "--keyset" in code:
            problems.append(f"{path.name}: --keyset is for the tests and the rehearsal only")
        for number, value in _raw_uses_lines(text):
            reference = _strip_comment(value).strip().strip("'\"")
            if reference.startswith("./") or _DOCKER.fullmatch(reference):
                continue
            if not _PINNED.fullmatch(reference):
                problems.append(f"{path.name}:{number}: `{reference}` is not pinned to a full "
                                "commit SHA")
            elif not _VERSION_COMMENT.search(value):
                problems.append(f"{path.name}:{number}: `{reference}` has no `# vX.Y.Z` comment "
                                "saying which version the SHA is")

    sign_steps = []
    for name, workflow in loaded.items():
        for job_name, job, number, step in _steps(workflow):
            if SECRET in str(step.get("env") or {}):
                sign_steps.append((name, job_name, job, number, step))
        text = (workflows / name).read_text(encoding="utf-8")
        mentions = len(re.findall(r"secrets\." + SECRET, text))
        expected = 1 if name == SIGN_WORKFLOW else 0
        if mentions != expected:
            problems.append(f"{name}: secrets.{SECRET} appears {mentions} times, expected "
                            f"{expected}")
    if len(sign_steps) != 1:
        problems.append(f"{len(sign_steps)} steps carry {SECRET}; exactly one - the sign step - "
                        "may")
    for name, job_name, job, number, step in sign_steps:
        where = f"{name}: job {job_name}, step {number}"
        if (name, job_name) != (SIGN_WORKFLOW, SIGN_JOB):
            problems.append(f"{where}: the key belongs to {SIGN_WORKFLOW}'s {SIGN_JOB} job only")
        env = step.get("env") or {}
        if env.get(SECRET) != "${{ secrets." + SECRET + " }}":
            problems.append(f"{where}: {SECRET} must come from secrets.{SECRET} and nothing else")
        problems.extend(check_sign_step(step, where))
        if job.get("environment") != ENVIRONMENT:
            problems.append(f"{where}: the job must run in the {ENVIRONMENT} environment")
        if job.get("permissions") != {}:
            problems.append(f"{where}: the job must declare `permissions: {{}}`")
        for _job, _j, other_number, other in _steps({"jobs": {job_name: job}}):
            uses = other.get("uses")
            if uses and uses.split("@")[0] not in SIGN_JOB_ACTIONS:
                problems.append(f"{name}: job {job_name}, step {other_number}: `{uses}` - the "
                                f"sign job uses only {', '.join(SIGN_JOB_ACTIONS)}")
            run = other.get("run") or ""
            if re.search(r"\b(pip|pip3|setup-python|apt-get|apt|npm|curl|wget)\b", run):
                problems.append(f"{name}: job {job_name}, step {other_number}: the sign job "
                                "installs and downloads nothing")

    for name in CHAIN:
        workflow = loaded.get(name)
        if workflow is None:
            problems.append(f"{name}: missing")
            continue
        concurrency = workflow.get("concurrency") or {}
        if concurrency.get("group") != CONCURRENCY or concurrency.get("cancel-in-progress") is not \
                False:
            problems.append(f"{name}: must run in the `{CONCURRENCY}` concurrency group, never "
                            "cancelled")
        if workflow.get("permissions") != {}:
            problems.append(f"{name}: must declare `permissions: {{}}` at the top")
        for job_name, job in (workflow.get("jobs") or {}).items():
            if MAIN_ONLY not in str(job.get("if") or ""):
                problems.append(f"{name}: job {job_name} must be refused off `main` "
                                f"(`if: {MAIN_ONLY}`)")
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
