"""The `Screens.MessageBox` and `AddPopup` stubs behave as OpenViX 6.6's do.

Read from the receiver's own `Screens/MessageBox.pyc` and `Tools/Notifications.pyc`,
disassembled under Python 3.12, which is what that image runs. Line numbers below
are the image's.

The one that matters most is the default type. On the receiver a MessageBox
opened without a type is a **question** (`TYPE_YESNO`, the first argument
default, line 29), with Yes and No to choose from. The stub used to default to
an information popup, so any test that opened one without saying which kind got
a passive popup where the receiver shows somebody a question - and a rule that
must treat "a question is open" differently from "an information popup is open"
could pass its tests on exactly the case it has to refuse.
"""

import inspect

from Screens.MessageBox import MessageBox
from Tools.Notifications import AddPopup

# `MessageBox.__init__`'s parameters and defaults, in order (MessageBox.pyc 29:
# thirteen defaults for the last thirteen of fifteen parameters).
IMAGE_MESSAGE_BOX_PARAMETERS = [
    ("session", inspect.Parameter.empty),
    ("text", inspect.Parameter.empty),
    ("type", 0),
    ("timeout", 0),
    ("close_on_any_key", False),
    ("default", True),
    ("enable_input", True),
    ("msgBoxID", None),
    ("picon", True),
    ("simple", False),
    ("wizard", False),
    ("list", None),
    ("skin_name", None),
    ("timeout_default", None),
    ("title", None),
]


def _parameters(function):
    return [
        (parameter.name, parameter.default)
        for parameter in inspect.signature(function).parameters.values()
        if parameter.name != "self"
    ]


def test_the_stub_message_box_is_a_question_by_default_like_the_image():
    box = MessageBox(None, "Really?")

    assert box.type == MessageBox.TYPE_YESNO
    assert box.list == [("Yes", True), ("No", False)]


def test_the_stub_message_box_has_the_images_five_types():
    # MessageBox.pyc 15-19.
    assert (
        MessageBox.TYPE_YESNO,
        MessageBox.TYPE_INFO,
        MessageBox.TYPE_WARNING,
        MessageBox.TYPE_ERROR,
        MessageBox.TYPE_MESSAGE,
    ) == (0, 1, 2, 3, 4)


def test_the_stub_message_box_takes_the_images_arguments_and_defaults():
    assert _parameters(MessageBox.__init__) == IMAGE_MESSAGE_BOX_PARAMETERS


def test_a_type_the_image_does_not_know_becomes_a_message():
    # MessageBox.pyc 36-39: anything outside range(TYPE_MESSAGE + 1).
    assert MessageBox(None, "", type=7).type == MessageBox.TYPE_MESSAGE
    assert MessageBox(None, "", type=-1).type == MessageBox.TYPE_MESSAGE


def test_the_timeout_is_kept_as_an_integer():
    # MessageBox.pyc 40.
    assert MessageBox(None, "", MessageBox.TYPE_INFO, "5").timeout == 5
    assert MessageBox(None, "", MessageBox.TYPE_INFO).timeout == 0


def test_only_a_question_has_answers_to_choose_from():
    # MessageBox.pyc 91-101: the list is the caller's or Yes/No for a question,
    # and always empty for every other type, whatever was passed.
    answers = [("Save", "save"), ("No", False)]

    assert MessageBox(None, "", default=False).list == [("No", False), ("Yes", True)]
    assert MessageBox(None, "", list=answers).list == answers
    for kind in (
        MessageBox.TYPE_INFO,
        MessageBox.TYPE_WARNING,
        MessageBox.TYPE_ERROR,
        MessageBox.TYPE_MESSAGE,
    ):
        assert MessageBox(None, "", kind, list=answers).list == []


def test_add_popup_needs_a_type_and_a_timeout_like_the_image():
    # Notifications.pyc 68: `AddPopup(text, type, timeout, id=None)` - no default
    # for either, so a caller that leaves one out raises on the receiver.
    assert _parameters(AddPopup) == [
        ("text", inspect.Parameter.empty),
        ("type", inspect.Parameter.empty),
        ("timeout", inspect.Parameter.empty),
        ("id", None),
    ]
