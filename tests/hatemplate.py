"""Home Assistant's template engine, as far as this plugin's templates reach it.

The discovery payloads carry Jinja templates that Home Assistant renders, and
the tests used to compare them as strings. That is how a `timestamp_utc` with
`+00:00` appended to it survived review and a release: every assertion matched
the string it expected to see, and none looked at what the string produces. The
tests render the templates instead, and this module is what they render with.

It is plain Jinja plus the part of Home Assistant that differs from plain Jinja
— and only the part these templates use. Home Assistant **adds** filters (such
as `timestamp_utc`) and **replaces** some of Jinja's own: `int`, `round`,
`float`, `random`, and the `range` global. A replaced filter is the trap. A
template that uses `int` renders under plain Jinja too, with Jinja's semantics
rather than Home Assistant's, and a test that passes that way is coverage of
the wrong engine.

So the environment starts **empty**. Every filter, test and global a template
may use is listed below by name, with where its behaviour comes from, and
`check()` refuses a template that reaches for anything else before it is
rendered. A template that needs a new filter means adding the filter here, on
purpose and with its provenance — never inheriting whatever Jinja happens to
call by that name.

🔴 The copies are of Home Assistant **2026.9.2**, the release the companion
integration's test harness pins (`pytest-homeassistant-custom-component`
0.13.365): `helpers/template/extensions/type_cast.py` (`int`),
`extensions/math.py` (`round`), `extensions/datetime.py` (`timestamp_utc`),
`helpers/template/helpers.py` (the error they raise) and `util/dt.py`
(`utc_from_timestamp`). Which names it replaces was measured rather than read:
its `TemplateEnvironment`, compared with a plain Jinja 3.1.6 `Environment`,
holds a different object under exactly the filter and global names above, and
under no test name. Being copies they can go stale with nothing here noticing;
move them together with that pin.
"""

import datetime
import json
import math

import jinja2
from jinja2 import nodes
from jinja2.defaults import DEFAULT_FILTERS, DEFAULT_TESTS
from jinja2.sandbox import ImmutableSandboxedEnvironment

# What an MQTT template is handed: the payload as text and, when the text is
# JSON, the parsed payload. Home Assistant also passes `entity_id`, `name` and
# `this`; no template here uses them, so they are not offered.
VARIABLES = ("value", "value_json")

_SENTINEL = object()


def _raise_no_default(function, value):
    """`helpers.raise_no_default`: a replaced filter raises where Jinja's shrugs."""
    raise ValueError(
        "Template error: " + function + " got invalid input '" + str(value)
        + "' when rendering template but no default was specified"
    )


def forgiving_int(value, default=_SENTINEL, base=10):
    """Home Assistant's `int`: Jinja's conversion, without the silent 0.

    Jinja's own filter answers 0 for anything it cannot convert, so a null
    epoch becomes 1970 and renders as a plausible date. Home Assistant's raises
    unless the template gave a default, and a value template that raises keeps
    the entity's previous state.
    """
    result = jinja2.filters.do_int(value, default=default, base=base)
    if result is _SENTINEL:
        _raise_no_default("int", value)
    return result


def forgiving_round(value, precision=0, method="common", default=_SENTINEL):
    """Home Assistant's `round`: an int at precision 0, and no rounding of None.

    Jinja's own returns a float at every precision — `1.0`, not `1`.
    """
    try:
        multiplier = float(10 ** precision)
        if method == "ceil":
            value = math.ceil(float(value) * multiplier) / multiplier
        elif method == "floor":
            value = math.floor(float(value) * multiplier) / multiplier
        elif method == "half":
            value = round(float(value) * 2) / 2
        else:
            value = round(float(value), precision)
        return int(value) if precision == 0 else value
    except (ValueError, TypeError):
        if default is _SENTINEL:
            _raise_no_default("round", value)
        return default


def timestamp_utc(value, default=_SENTINEL):
    """Home Assistant's `timestamp_utc`: an ISO string that already ends in `+00:00`.

    `dt_util.utc_from_timestamp` is `partial(datetime.fromtimestamp, tz=UTC)`.
    """
    try:
        return datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc).isoformat()
    except (ValueError, TypeError):
        if default is _SENTINEL:
            _raise_no_default("timestamp_utc", value)
        return default


# Every filter a template may use, and whose behaviour it is.
FILTERS = {
    # Replaced by Home Assistant; copied above.
    "int": forgiving_int,
    "round": forgiving_round,
    # Added by Home Assistant; copied above.
    "timestamp_utc": timestamp_utc,
    # Jinja's own, which Home Assistant keeps — the very same objects sit in its
    # environment. `default` is the one the plugin leans on hardest, and it
    # fires on an ABSENT key (Jinja's Undefined), not on a JSON null, which is a
    # value like any other.
    "default": DEFAULT_FILTERS["default"],
    "tojson": DEFAULT_FILTERS["tojson"],
    "count": DEFAULT_FILTERS["count"],
}

TESTS = {
    # Jinja's own; Home Assistant replaces no test.
    "none": DEFAULT_TESTS["none"],
}

GLOBALS = {}


def environment():
    """A sandboxed environment holding exactly what is listed above, and nothing else.

    Sandboxed and immutable because Home Assistant's is. The undefined type is
    Jinja's plain `Undefined`; Home Assistant's is a subclass of it that only
    adds logging, so an absent key renders empty, is falsy, and fails on
    attribute access exactly as it does there.
    """
    found = ImmutableSandboxedEnvironment(undefined=jinja2.Undefined)
    found.filters = dict(FILTERS)
    found.tests = dict(TESTS)
    found.globals = dict(GLOBALS)
    return found


ENVIRONMENT = environment()


def used_names(template):
    """({filters}, {tests}, {globals}) that a template refers to.

    Read off the parsed template rather than off its text, so that a filter in
    a `{% filter %}` block, a test inside an `{% if %}` and a bare function call
    are all found. A name counts as a global when the template is neither handed
    it (`VARIABLES`) nor assigns it itself (`{% set %}`, a loop target).
    """
    tree = ENVIRONMENT.parse(template)
    filters = {node.name for node in tree.find_all(nodes.Filter)}
    tests = {node.name for node in tree.find_all(nodes.Test)}
    names = list(tree.find_all(nodes.Name))
    assigned = {node.name for node in names if node.ctx in ("store", "param")}
    globals_ = {
        node.name
        for node in names
        if node.ctx == "load" and node.name not in assigned and node.name not in VARIABLES
    }
    return filters, tests, globals_


def check(template):
    """Refuse a template that uses anything this module does not explicitly own.

    Without this, a template reaching for a filter nobody has copied would still
    render wherever Jinja has one by that name — with Jinja's semantics, not
    Home Assistant's — and read as coverage. The empty environment makes most
    of that fail anyway; this makes it fail **before** rendering, for every
    branch of the template rather than only the branch one payload takes, and
    names what is missing.
    """
    filters, tests, globals_ = used_names(template)
    missing = sorted(
        ["filter " + name for name in filters - set(FILTERS)]
        + ["test " + name for name in tests - set(TESTS)]
        + ["global " + name for name in globals_ - set(GLOBALS)]
    )
    if missing:
        raise AssertionError(
            "the template uses " + ", ".join(missing) + ", which tests/hatemplate.py "
            "does not reproduce from Home Assistant: " + template
        )


def render(template, **variables):
    """Render as Home Assistant does: checked, rendered, and stripped."""
    check(template)
    return ENVIRONMENT.from_string(template).render(**variables).strip()


def render_payload(template, payload):
    """What an MQTT entity makes of a template and the payload it received.

    `payload` is the text on the wire; `value_json` is offered only when that
    text is JSON, as Home Assistant does.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8")
    variables = {"value": payload}
    try:
        variables["value_json"] = json.loads(payload)
    except ValueError:
        pass
    return render(template, **variables)
