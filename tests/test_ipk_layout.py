"""The control template must stay packageable.

A missing field or a stray architecture is the kind of mistake that only shows
up when a feed rejects the package or a receiver refuses to install it, long
after the commit. The template is cheap to assert here instead.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL = REPO_ROOT / "CONTROL" / "control"

REQUIRED_FIELDS = (
    "Package",
    "Version",
    "Architecture",
    "Section",
    "Priority",
    "Depends",
    "Maintainer",
    "Homepage",
    "License",
    "Description",
)


def read_control_fields():
    fields = {}
    key = None
    for line in CONTROL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and key is not None:
            fields[key] += "\n" + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
    return fields


def test_all_required_fields_present():
    fields = read_control_fields()
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    assert not missing, f"CONTROL/control is missing: {', '.join(missing)}"


def test_package_name_and_architecture():
    fields = read_control_fields()
    assert fields["Package"] == "enigma2-plugin-extensions-mqttbridge"
    # Pure Python: one package installs on every receiver.
    assert fields["Architecture"] == "all"
    assert fields["License"] == "GPL-2.0-or-later"


def test_version_is_a_build_placeholder():
    # The build substitutes this from version.py; a literal version here would
    # silently go stale.
    assert read_control_fields()["Version"] == "@VERSION@"


def test_depends_are_image_packages():
    depends = [d.strip() for d in read_control_fields()["Depends"].split(",")]
    assert "python3-core" in depends
    # Anything not in an image feed would have to be vendored instead.
    assert all(d.startswith("python3-") for d in depends), depends


def test_description_has_a_synopsis_and_a_body():
    description = read_control_fields()["Description"]
    synopsis, _, body = description.partition("\n")
    assert synopsis.strip(), "the first Description line is the synopsis"
    assert body.strip(), "the extended description is what a plugin browser shows"
