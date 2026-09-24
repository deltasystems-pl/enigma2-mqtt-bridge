"""The `Components.config` stub behaves as OpenViX 6.6's does, where the plugin can notice.

Every other test in this suite trusts these classes to be the receiver's. The
rules pinned here were read from the receiver's own `Components/config.pyc`:
a value is kept exactly as it was assigned, notifiers run only when it changes,
and a selection never raises. „Changes" is the image's own comparison: the
`str()` of the choice for a selection, and for an integer the `str()` of the
one-element list it keeps, which tells `5` from `"5"`. A stub that tidied
values up — `1` into `True`, `"5"` into `5`, `None` into `""` — would let the
plugin write the wrong type
and pass, while the box kept the wrong type in memory until its next start.
"""

from Components.config import (
    ConfigInteger,
    ConfigPassword,
    ConfigSelection,
    ConfigText,
    ConfigYesNo,
)


def _counting(element):
    calls = []
    element.addNotifier(lambda changed: calls.append(changed.value), initial_call=False)
    return calls


def test_a_yes_no_keeps_the_object_it_is_given():
    element = ConfigYesNo(default=False)
    element.value = 1
    assert element.value == 1
    assert element.value is not True
    element.value = "no"
    assert element.value == "no"


def test_an_integer_keeps_the_object_it_is_given():
    element = ConfigInteger(default=4, limits=(0, 20))
    element.value = "9"
    assert element.value == "9"


def test_a_text_keeps_the_object_it_is_given():
    element = ConfigText(default="")
    element.value = None
    assert element.value is None
    password = ConfigPassword(default="")
    password.value = 1234
    assert password.value == 1234


def test_notifiers_run_only_when_the_value_changes():
    for element, first, second in (
        (ConfigYesNo(default=False), True, False),
        (ConfigInteger(default=4), 5, 6),
        (ConfigText(default=""), "a", "b"),
        (ConfigSelection(choices=["x", "y"], default="x"), "y", "x"),
    ):
        calls = _counting(element)
        element.value = element.value
        assert calls == [], element
        element.value = first
        element.value = first
        element.value = second
        assert calls == [first, second], element


def test_an_integer_compares_the_text_of_the_list_it_keeps():
    """The image keeps `[value]`: `[5]` and `['5']` differ, so `"5"` after `5` notifies."""
    integer = ConfigInteger(default=5)
    calls = _counting(integer)
    integer.value = "5"
    assert calls == ["5"]
    assert integer.value == "5"
    integer.value = "5"
    assert calls == ["5"]


def test_a_selection_compares_the_text_of_the_choice():
    """`2` finds the choice `"2"`, which is stored — and it is a change only from `"1"`."""
    selection = ConfigSelection(choices=[("1", "one"), ("2", "two")], default="1")
    calls = _counting(selection)
    selection.value = 2
    assert selection.value == "2"
    assert calls == ["2"]
    selection.value = 2
    selection.value = "2"
    assert calls == ["2"]


def test_a_selection_replaces_an_unknown_value_with_its_default():
    element = ConfigSelection(choices=["info", "debug"], default="info")
    element.value = "debug"
    element.value = "verbose"
    assert element.value == "info"


def test_add_notifier_calls_the_notifier_at_once_unless_told_not_to():
    element = ConfigYesNo(default=True)
    calls = []

    def notifier(changed):
        calls.append(changed.value)

    element.addNotifier(notifier)
    assert calls == [True]
    element.removeNotifier(notifier)
    element.value = False
    assert calls == [True]
    element.addNotifier(notifier, initial_call=False)
    assert calls == [True]
    element.value = True
    assert calls == [True, True]
