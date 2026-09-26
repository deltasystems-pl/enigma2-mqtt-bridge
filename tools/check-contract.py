#!/usr/bin/env python3
"""Check that docs/contract.json says what docs/TOPICS.md says, and keeps the last release's.

`docs/TOPICS.md` is the topic contract, written for people. `docs/contract.json` is the same
contract written for a machine: every state topic with its payload kind and retain flag, every
command, every `info` member with its type, every setting with its type and whether `cmd/config`
may write it, every capability name, the fixed topics outside the node's tree, the contract major,
the named in-major exceptions, and the planned additions. Two checks run on it:

1. **Agreement.** The prose is parsed at the places where it states the contract - the state-topic
   headings of section 1, the command table of section 2, the `info` field tables, the settings
   row and the read-only table, the capability sentence, the announcement and discovery topics,
   the "Contract version" section with its exception table, and the planned table - and every
   difference from the data is a problem. Neither side is the master: a change to one without the
   other fails, whichever it is.
2. **Promises kept** (`--previous`). Against `docs/contract.json` at the previous release tag:
   inside one contract major nothing may be removed and nothing retyped - a topic's payload kind or
   retain flag, an `info` member's type, a setting's type or the side of `cmd/config` it is on - and
   no named exception may be dropped or re-dated. A new choice of an enumerated setting is an
   addition; a choice taken away is a retype. A higher major allows anything; a lower one is
   refused. Planned rows are promises about a shape, not features, and are not compared.

**It fails closed.** Exit status 0: the files agree and, when asked, the previous release's
promises are kept. 1: a contract problem, each one printed. 2: the comparison that was asked for
could not be made - an unknown ref, no release tag reachable (a checkout without tags), or a tag
that should carry `docs/contract.json` and does not. "Nothing to compare against" is a pass only in
one case, said so in the output: no release tag in the repository carries the file yet, which is
true until the first release that ships it.

What the data deliberately does not hold: the fields of each state topic's payload. They are
tables in the prose, several with nested members, and a type there is a sentence ("integer or
`null`") more often than a word. The `info` members are the exception because a consumer reads
them to decide what the box can do. So a change to a payload field - which is where most consumer
breaks happen - is caught by review against TOPICS.md, not by this checker.

Standard library only, Python 3.9, because it runs in the same test matrix as the plugin.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOPICS_PATH = "docs/TOPICS.md"
CONTRACT_PATH = "docs/contract.json"

# Two headings still name the milestone that added them rather than the release; M2 shipped as
# 0.2.0. Mapped here so the data can speak in versions only.
MILESTONES = {"M2": "0.2.0"}

SINCE = re.compile(r"since (\d+\.\d+\.\d+|M\d+)")
BACKTICKED = re.compile(r"`([^`]+)`")
TOPIC_HEADING = re.compile(r"^### `<base>/<node>/(?P<name>[^`]+)`(?P<rest>.*)$")
CONTRACT_MAJOR = re.compile(r"current contract major is \*\*(\d+)\*\*")
WRITABLE_SETTING = re.compile(r"`([a-z_]+)` \(([^)]*)\)")

# The words the planned table uses in its Kind column, and where an implemented item of that
# kind lives in the data. An event topic is a node topic that is not retained, like `key`.
PLANNED_KINDS = {
    "info member": "info_members",
    "setting": "settings",
    "capability": "capabilities",
    "state topic": "state_topics",
    "event topic": "state_topics",
    "command": "commands",
    "consumer topic": "other_topics",
}

# The keys the data carries besides the contract itself.
META_KEYS = ("schema", "about")


class ContractParseError(ValueError):
    """TOPICS.md no longer has a shape this checker can read - fix one or the other."""


# ------------------------------------------------------------------- parsing --


def _sections(text: str, marker: str) -> list[tuple[str, list[str]]]:
    """Split `text` into (heading, lines) at every line starting with `marker`."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        if line.startswith(marker):
            sections.append((line, []))
        else:
            sections[-1][1].append(line)
    return sections


def _top_section(text: str, prefix: str) -> list[str]:
    """Return the lines of the one `## ` section whose heading starts with `prefix`."""
    found = [lines for heading, lines in _sections(text, "## ") if heading.startswith(prefix)]
    if len(found) != 1:
        raise ContractParseError(f"expected one section headed {prefix!r}, found {len(found)}")
    return found[0]


def _cells(line: str) -> list[str]:
    """Split a Markdown table row into cells, keeping an escaped `\\|` inside its cell."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    placeholder = "\x00"
    cells = body.replace("\\|", placeholder).split("|")
    return [cell.strip().replace(placeholder, "\\|") for cell in cells]


def _tables(lines: list[str], header: re.Pattern) -> list[list[list[str]]]:
    """Every table in `lines` whose header row matches `header`, as lists of body rows."""
    tables = []
    index = 0
    while index < len(lines):
        if header.match(lines[index]):
            rows = []
            index += 2  # the header and the |---| line
            while index < len(lines) and lines[index].startswith("|"):
                rows.append(_cells(lines[index]))
                index += 1
            tables.append(rows)
        else:
            index += 1
    return tables


def _since(text: str) -> str | None:
    match = SINCE.search(text)
    if not match:
        return None
    return MILESTONES.get(match.group(1), match.group(1))


def _first_name(cell: str) -> str:
    match = BACKTICKED.search(cell)
    if not match:
        raise ContractParseError(f"no `name` in table cell {cell!r}")
    return match.group(1)


def _plain_type(cell: str) -> str:
    return " ".join(cell.replace("`", "").split())


def _setting_type(description: str) -> str:
    """Reduce the prose after a setting's name to `bool`, `int` or `enum(a|b|c)`."""
    choices = BACKTICKED.findall(description)
    if choices:
        return "enum(" + "|".join(choices) + ")"
    if description.startswith("bool"):
        return "bool"
    if "integer" in description or "seconds" in description:
        return "int"
    raise ContractParseError(f"cannot tell the type of a setting from {description!r}")


def _state_topics(text: str) -> dict:
    topics = {}
    for heading, lines in _sections("\n".join(_top_section(text, "## 1.")), "### "):
        match = TOPIC_HEADING.match(heading)
        if not match:
            continue
        body = "\n".join(lines)
        if "raw bytes of a JPEG" in body:
            payload = "jpeg"
        elif "**Not JSON**" in body:
            payload = "text"
        else:
            payload = "json"
        entry = {"payload": payload, "retained": "**Not retained**" not in body}
        since = _since(match.group("rest"))
        if since:
            entry["since"] = since
        topics[match.group("name")] = entry
    if not topics:
        raise ContractParseError("section 1 has no `<base>/<node>/...` headings")
    return topics


def _commands(text: str) -> dict:
    tables = _tables(_top_section(text, "## 2."), re.compile(r"^\| Command \|"))
    if not tables:
        raise ContractParseError("section 2 has no command table")
    commands = {}
    for row in tables[0]:
        entry = {}
        since = _since(row[0])
        if since:
            entry["since"] = since
        commands[_first_name(row[0])] = entry
    return commands


def _info(text: str) -> tuple[dict, dict, list[str]]:
    sections = [
        lines
        for heading, lines in _sections("\n".join(_top_section(text, "## 1.")), "### ")
        if heading.startswith("### `<base>/<node>/info`")
    ]
    if len(sections) != 1:
        raise ContractParseError("section 1 has no single `info` heading")
    tables = _tables(sections[0], re.compile(r"^\| Field \| Type \|"))
    if len(tables) < 2:
        raise ContractParseError("the `info` section needs its field table and the `wol` table")
    members = {_first_name(row[0]): _plain_type(row[1]) for row in tables[0]}
    wol = {_first_name(row[0]): _plain_type(row[1]) for row in tables[1]}
    settings_rows = [row for row in tables[0] if _first_name(row[0]) == "settings"]
    if len(settings_rows) != 1:
        raise ContractParseError("the `info` table has no single `settings` row")
    return members, wol, settings_rows[0]


def _settings(text: str, settings_row: list[str]) -> tuple[dict, list[str]]:
    """The settings, from the `settings` row and the read-only table, which must agree."""
    problems = []
    meaning = settings_row[2]
    if "**Read-only**" not in meaning or "Writable through" not in meaning:
        raise ContractParseError(
            "the `settings` row no longer names its writable and read-only parts"
        )
    writable_part, read_only_part = meaning.split("**Read-only**", 1)
    settings = {}
    for name, description in WRITABLE_SETTING.findall(writable_part):
        settings[name] = {"type": _setting_type(description.strip()), "writable": True}
    read_only_part = read_only_part.split("(", 1)
    read_only_names = BACKTICKED.findall(read_only_part[0])
    if len(read_only_part) < 2 or not read_only_part[1].startswith("bool"):
        raise ContractParseError(
            "the `settings` row no longer says the read-only members are bools"
        )
    read_only_type = "bool"

    info_lines = [
        lines
        for heading, lines in _sections("\n".join(_top_section(text, "## 1.")), "### ")
        if heading.startswith("### `<base>/<node>/info`")
    ][0]
    tables = _tables(info_lines, re.compile(r"^\| Read-only member \|"))
    if len(tables) != 1:
        raise ContractParseError("the `info` section has no single read-only member table")
    table = {_first_name(row[0]): row[1].strip() for row in tables[0]}
    if set(table) != set(read_only_names):
        problems.append(
            "docs/TOPICS.md disagrees with itself: the `settings` row names read-only members "
            f"{sorted(read_only_names)}, the read-only table {sorted(table)}"
        )
    for name in read_only_names:
        entry = {"type": read_only_type, "writable": False}
        if table.get(name):
            entry["since"] = MILESTONES.get(table[name], table[name])
        settings[name] = entry

    # The payload of `cmd/config` is the other place the writable keys are spelled out.
    config_rows = [
        row
        for row in _tables(_top_section(text, "## 2."), re.compile(r"^\| Command \|"))[0]
        if _first_name(row[0]) == "config"
    ]
    if len(config_rows) != 1:
        raise ContractParseError("the command table has no single `config` row")
    payload = json.loads(_first_name(config_rows[0][1]))
    writable = sorted(name for name, entry in settings.items() if entry["writable"])
    if sorted(payload) != writable:
        problems.append(
            "docs/TOPICS.md disagrees with itself: `cmd/config`'s payload has "
            f"{sorted(payload)}, the `settings` row names {writable} as writable"
        )
    return settings, problems


def _capabilities(text: str) -> list[str]:
    body = "\n".join(_top_section(text, "## 1."))
    match = re.search(r"nothing else is ever in the list:(.*?)A build that", body, re.S)
    if not match:
        raise ContractParseError(
            "the capability sentence ('nothing else is ever in the list:') is gone"
        )
    return sorted(BACKTICKED.findall(match.group(1)))


def _other_topics(text: str) -> list[str]:
    announcement = BACKTICKED.findall("\n".join(_top_section(text, "## 3.")))
    if not announcement or not announcement[0].startswith("enigma2mqtt/"):
        raise ContractParseError("section 3 no longer starts with the announcement topic")
    tables = _tables(_top_section(text, "## 4."), re.compile(r"^\| Topic \|"))
    if not tables:
        raise ContractParseError("section 4 has no topic table")
    return sorted([announcement[0]] + [_first_name(row[0]) for row in tables[0]])


def _exceptions(text: str) -> list[dict]:
    """The named in-major exceptions: `| Exception | Release | ...` in "Contract version"."""
    tables = _tables(
        _top_section(text, "## Contract version"), re.compile(r"^\| Exception \| Release \|")
    )
    if len(tables) != 1:
        raise ContractParseError(
            "the Contract version section has no single `| Exception | Release |` table"
        )
    return [{"name": _first_name(row[0]), "release": row[1].strip()} for row in tables[0]]


def _planned(text: str) -> list[dict]:
    tables = _tables(_top_section(text, "## 5."), re.compile(r"^\| Kind \| Name \|"))
    if len(tables) != 1:
        raise ContractParseError("section 5 has no single `| Kind | Name |` table")
    planned = []
    for row in tables[0]:
        kind = row[0].strip().lower()
        if kind not in PLANNED_KINDS:
            raise ContractParseError(f"planned row of unknown kind {row[0]!r}")
        planned.append({"kind": kind, "name": _first_name(row[1])})
    return planned


def parse_topics(text: str) -> dict:
    """Read the contract out of TOPICS.md, in the shape of docs/contract.json."""
    parsed, _problems = _parse(text)
    return parsed


def _parse(text: str) -> tuple[dict, list[str]]:
    major = CONTRACT_MAJOR.findall(text)
    if len(major) != 1:
        raise ContractParseError("expected one 'current contract major is **N**' sentence")
    members, wol, settings_row = _info(text)
    settings, problems = _settings(text, settings_row)
    parsed = {
        "contract": int(major[0]),
        "state_topics": _state_topics(text),
        "commands": _commands(text),
        "info_members": members,
        "info_wol_members": wol,
        "settings": settings,
        "capabilities": _capabilities(text),
        "other_topics": _other_topics(text),
        "exceptions": _exceptions(text),
        "planned": _planned(text),
    }
    return parsed, problems


# -------------------------------------------------------------- comparing --


# What each part of the contract is called in a problem line.
LABELS = {
    "state_topics": "state topic",
    "commands": "command",
    "info_members": "info member",
    "info_wol_members": "info.wol member",
    "settings": "setting",
    "capabilities": "capability",
    "other_topics": "topic",
}


def _as_mapping(value) -> dict:
    """Lists of names become {name: None} so every part compares the same way."""
    if isinstance(value, list):
        return {name: None for name in value}
    return dict(value or {})


def check_consistency(topics_text: str, contract: dict) -> list[str]:
    """Every difference between the prose and the data, one line each; [] when they agree."""
    try:
        parsed, problems = _parse(topics_text)
    except ContractParseError as error:
        return [f"docs/TOPICS.md could not be read as a contract: {error}"]
    problems = list(problems)
    if parsed["contract"] != contract.get("contract"):
        problems.append(
            f"contract major: docs/TOPICS.md says {parsed['contract']}, "
            f"docs/contract.json says {contract.get('contract')}"
        )
    unknown = set(contract) - set(parsed) - set(META_KEYS)
    for key in sorted(unknown):
        problems.append(f"docs/contract.json has a part docs/TOPICS.md does not: {key!r}")
    for part, label in LABELS.items():
        prose = _as_mapping(parsed[part])
        data = _as_mapping(contract.get(part))
        for name in sorted(set(prose) - set(data)):
            problems.append(f"{label} {name!r} is in docs/TOPICS.md but not in docs/contract.json")
        for name in sorted(set(data) - set(prose)):
            problems.append(f"{label} {name!r} is in docs/contract.json but not in docs/TOPICS.md")
        for name in sorted(set(prose) & set(data)):
            if prose[name] != data[name]:
                problems.append(
                    f"{label} {name!r} differs: docs/TOPICS.md says {json.dumps(prose[name])}, "
                    f"docs/contract.json says {json.dumps(data[name])}"
                )

    prose_planned = {(row["kind"], row["name"]) for row in parsed["planned"]}
    data_planned = {(row.get("kind"), row.get("name")) for row in contract.get("planned", [])}
    for kind, name in sorted(prose_planned - data_planned):
        problems.append(
            f"planned {kind} {name!r} is in docs/TOPICS.md but not in docs/contract.json"
        )
    for kind, name in sorted(data_planned - prose_planned, key=str):
        problems.append(
            f"planned {kind} {name!r} is in docs/contract.json but not in docs/TOPICS.md"
        )
    for kind, name in sorted(prose_planned | data_planned, key=str):
        part = PLANNED_KINDS.get(kind)
        if part and name in _as_mapping(parsed[part]):
            problems.append(
                f"planned {kind} {name!r} is already implemented: it is in the contract's "
                f"{LABELS[part]}s, so it cannot also be a plan"
            )

    prose_exceptions = {row["name"]: row["release"] for row in parsed["exceptions"]}
    data_exceptions = _exception_map(contract)
    for name in sorted(set(prose_exceptions) | set(data_exceptions)):
        if name not in data_exceptions:
            problems.append(
                f"exception {name!r} is in docs/TOPICS.md but not in docs/contract.json"
            )
        elif name not in prose_exceptions:
            problems.append(
                f"exception {name!r} is in docs/contract.json but not in docs/TOPICS.md"
            )
        elif prose_exceptions[name] != data_exceptions[name]:
            problems.append(
                f"exception {name!r} differs: docs/TOPICS.md says release "
                f"{prose_exceptions[name]!r}, docs/contract.json says {data_exceptions[name]!r}"
            )
    return problems


def _exception_map(contract: dict) -> dict:
    return {
        row.get("name"): row.get("release")
        for row in contract.get("exceptions", [])
        if isinstance(row, dict)
    }


ENUM = re.compile(r"^enum\((.*)\)$")


def _enum_choices(value):
    """The choices of an `enum(a|b)` setting type, or None for any other type."""
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        match = ENUM.match(value["type"])
        if match:
            return set(match.group(1).split("|"))
    return None


def compare(old: dict, new: dict) -> list[str]:
    """What `new` takes away from `old` without the new contract major that would allow it."""
    old_major = old.get("contract")
    new_major = new.get("contract")
    if not isinstance(old_major, int) or not isinstance(new_major, int):
        return ["contract major: missing or not an integer"]
    if new_major < old_major:
        return [f"contract major went down, from {old_major} to {new_major}"]
    if new_major > old_major:
        return []
    problems = []
    for part, label in LABELS.items():
        before = _as_mapping(old.get(part))
        after = _as_mapping(new.get(part))
        for name in sorted(set(before) - set(after)):
            problems.append(
                f"{label} {name!r} was removed inside contract major {new_major}; "
                "that needs a new major"
            )
        for name in sorted(set(before) & set(after)):
            was, now = before[name], after[name]
            old_choices, new_choices = _enum_choices(was), _enum_choices(now)
            if old_choices is not None and new_choices is not None:
                # A new choice is an addition, as a new value of any enumeration is (TOPICS.md,
                # Contract version); a choice taken away is not.
                for choice in sorted(old_choices - new_choices):
                    problems.append(
                        f"{label} {name!r} lost the choice {choice!r} inside contract major "
                        f"{new_major}; that needs a new major"
                    )
                was = {key: value for key, value in was.items() if key != "type"}
                now = {key: value for key, value in now.items() if key != "type"}
            if isinstance(was, dict) and isinstance(now, dict):
                # `since` may be corrected; every other key is a promise.
                was = {key: value for key, value in was.items() if key != "since"}
                now = {key: value for key, value in now.items() if key != "since"}
            if was != now:
                problems.append(
                    f"{label} {name!r} changed inside contract major {new_major}, from "
                    f"{json.dumps(was)} to {json.dumps(now)}; that needs a new major"
                )

    # A named exception is part of the record for good: a consumer upgrading across it needs to
    # find it. It may only move from "unreleased" to the release that shipped it.
    old_exceptions, new_exceptions = _exception_map(old), _exception_map(new)
    for name in sorted(old_exceptions):
        if name not in new_exceptions:
            problems.append(f"exception {name!r} was dropped from the record")
        elif old_exceptions[name] != "unreleased" and new_exceptions[name] != old_exceptions[name]:
            problems.append(
                f"exception {name!r} was re-dated from {old_exceptions[name]!r} "
                f"to {new_exceptions[name]!r}"
            )
    return problems


# -------------------------------------------------------------------- CLI --


EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_CANNOT_COMPARE = 2
RELEASE_TAGS = "v[0-9]*"


class CannotCompare(RuntimeError):
    """The comparison that was asked for could not be made. Never a pass."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError as error:
        raise CannotCompare(f"git could not be run: {error}") from error


def _carries_contract(root: Path, ref: str) -> bool:
    return _git(root, "cat-file", "-e", f"{ref}:{CONTRACT_PATH}").returncode == 0


def _previous_contract(root: Path, previous: str) -> tuple[str | None, dict | None]:
    """(ref, contract at ref), or (None, None) in the one case where nothing is legitimate.

    Raises CannotCompare for everything else: an unknown ref, no release tag reachable, or a
    release tag without the file although an earlier one had it.
    """
    if previous != "tag":
        if _git(root, "rev-parse", "--verify", "--quiet", f"{previous}^{{commit}}").returncode:
            raise CannotCompare(f"{previous!r} is not a commit, tag or branch in this repository")
        if not _carries_contract(root, previous):
            raise CannotCompare(f"{previous} carries no {CONTRACT_PATH} to compare against")
    else:
        reachable = _git(root, "tag", "--list", RELEASE_TAGS, "--merged", "HEAD")
        if reachable.returncode or not reachable.stdout.split():
            raise CannotCompare(
                "no release tag is reachable from HEAD - a checkout without tags? "
                "(the CI job needs `fetch-depth: 0`)"
            )
        described = _git(root, "describe", "--tags", "--abbrev=0", "--match", RELEASE_TAGS)
        previous = described.stdout.strip()
        if described.returncode or not previous:
            raise CannotCompare(f"git describe found no release tag: {described.stderr.strip()}")
        if not _carries_contract(root, previous):
            every = _git(root, "tag", "--list", RELEASE_TAGS).stdout.split()
            carrying = [tag for tag in every if _carries_contract(root, tag)]
            if carrying:
                raise CannotCompare(
                    f"{previous}, the newest release tag reachable from HEAD, carries no "
                    f"{CONTRACT_PATH}, although {', '.join(sorted(carrying))} did"
                )
            print(
                f"no release tag carries {CONTRACT_PATH} yet (checked {', '.join(sorted(every))});"
                " nothing to compare against - legitimate only until the first release that"
                " ships the file"
            )
            return None, None
    shown = _git(root, "show", f"{previous}:{CONTRACT_PATH}")
    try:
        return previous, json.loads(shown.stdout)
    except ValueError as error:
        raise CannotCompare(f"{CONTRACT_PATH} at {previous} is not JSON: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--previous",
        metavar="REF",
        help="also compare against docs/contract.json at this git ref; "
        "'tag' means the newest release tag (v*) reachable from HEAD",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="the repository to check (default: the one this script is in)",
    )
    args = parser.parse_args(argv)
    root = args.root

    topics_text = (root / TOPICS_PATH).read_text(encoding="utf-8")
    contract = json.loads((root / CONTRACT_PATH).read_text(encoding="utf-8"))
    problems = check_consistency(topics_text, contract)
    for line in problems:
        print(f"{TOPICS_PATH} vs {CONTRACT_PATH}: {line}")
    if not problems:
        print(f"{TOPICS_PATH} and {CONTRACT_PATH} agree (contract major {contract['contract']})")

    if args.previous:
        try:
            ref, old = _previous_contract(root, args.previous)
        except CannotCompare as error:
            print(f"cannot compare with the previous release: {error}")
            return EXIT_CANNOT_COMPARE
        if old is not None:
            found = compare(old, contract)
            for line in found:
                print(f"{CONTRACT_PATH} against {ref}: {line}")
            if not found:
                print(f"{CONTRACT_PATH} keeps every promise {ref} made")
            problems += found
    return EXIT_PROBLEMS if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
