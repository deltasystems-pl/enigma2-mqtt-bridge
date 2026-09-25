"""The `Screens.MessageBox` and `AddPopup` stubs behave as OpenViX 6.6's do.

Read from the receiver's own `Screens/MessageBox.pyc`, `Tools/Notifications.pyc`,
`Screens/InfoBarGenerics.pyc` and `Screens/Screen.pyc`, disassembled under
Python 3.12, which is what that image runs, and from its `StartEnigma.py`. Line
numbers below are the image's.

The one that matters most is the default type. On the receiver a MessageBox
opened without a type is a **question** (`TYPE_YESNO`, the first argument
default, line 29), with Yes and No to choose from. The stub used to default to
an information popup, so any test that opened one without saying which kind got
a passive popup where the receiver shows somebody a question - and a rule that
must treat "a question is open" differently from "an information popup is open"
could pass its tests on exactly the case it has to refuse.

The second half pins **how** a popup gets onto the screen, so that a test can
build "an information popup over the info bar" the way the receiver does rather
than by hand: `AddPopup` queues it, the info bar opens it only while it is
executing, and the session stacks the info bar under it as a `(screen, shown)`
tuple.
"""

import inspect

import pytest
from conftest import MainLoop, ModalSession, NotifiableInfoBar
from Screens.MessageBox import MessageBox
from Tools.Notifications import (
    AddNotification,
    AddPopup,
    current_notifications,
    notifications,
)

from MQTTBridge import osd

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


def test_the_timeout_is_kept_as_an_integer_and_compared_as_given():
    # MessageBox.pyc 40 keeps `int(timeout)`; 105-107 then compare the argument
    # itself with 0, so a float is fine and a string raises, as on the receiver.
    box = MessageBox(None, "", MessageBox.TYPE_INFO, 5.7)
    assert box.timeout == 5
    assert box.timerRunning is True

    box = MessageBox(None, "", MessageBox.TYPE_INFO)
    assert box.timeout == 0
    assert box.timerRunning is False

    with pytest.raises(TypeError):
        MessageBox(None, "", MessageBox.TYPE_INFO, "5")


def test_only_a_question_has_answers_to_choose_from():
    # MessageBox.pyc 91-101: the list is the caller's if it is not empty, else
    # Yes/No for a question, and always empty for every other type, whatever
    # was passed.
    answers = [("Save", "save"), ("No", False)]

    assert MessageBox(None, "", default=False).list == [("No", False), ("Yes", True)]
    assert MessageBox(None, "", list=answers).list == answers
    assert MessageBox(None, "", list=[]).list == [("Yes", True), ("No", False)]
    for kind in (
        MessageBox.TYPE_INFO,
        MessageBox.TYPE_WARNING,
        MessageBox.TYPE_ERROR,
        MessageBox.TYPE_MESSAGE,
    ):
        assert MessageBox(None, "", kind, list=answers).list == []


def test_the_skin_names_follow_simple_wizard_and_skin_name():
    # MessageBox.pyc 82-90.
    assert MessageBox(None, "").skinName == ["MessageBox"]
    assert MessageBox(None, "", simple=True).skinName == ["MessageBoxSimple", "MessageBox"]
    assert MessageBox(None, "", wizard=True).skinName == ["MessageBoxWizard"]
    assert MessageBox(None, "", skin_name="Mine").skinName == ["Mine", "MessageBox"]


def test_add_popup_needs_a_type_and_a_timeout_like_the_image():
    # Notifications.pyc 68: `AddPopup(text, type, timeout, id=None)` - no default
    # for either, so a caller that leaves one out raises on the receiver.
    assert _parameters(AddPopup) == [
        ("text", inspect.Parameter.empty),
        ("type", inspect.Parameter.empty),
        ("timeout", inspect.Parameter.empty),
        ("id", None),
    ]


# ------------------------------------------------ a popup over the info bar --


def _info_bar():
    """A session whose first dialog is the info bar, with its OSD auto-hidden.

    StartEnigma opens the info bar as the first dialog; `execBegin` shows it
    (StartEnigma.py 86-87), and the info bar hides its own OSD a few seconds
    later, which is how it usually is when a popup arrives.
    """
    session = ModalSession()
    info_bar = session.open(NotifiableInfoBar)
    info_bar.hide()
    return session, info_bar


def test_add_popup_queues_a_simple_message_box_that_closes_on_any_key():
    # Notifications.pyc 72 and 12-13: without an info bar nothing drains the queue.
    AddPopup("hello", MessageBox.TYPE_INFO, 5, id="popup")

    assert len(notifications) == 1
    fnc, screen, args, kwargs, identifier = notifications[0]
    assert (fnc, screen, args, identifier) == (None, MessageBox, (), "popup")
    assert kwargs == {
        "text": "hello",
        "type": MessageBox.TYPE_INFO,
        "timeout": 5,
        "close_on_any_key": True,
        "simple": True,
    }


def test_a_popup_opens_over_the_executing_info_bar_as_on_the_receiver():
    session, info_bar = _info_bar()

    AddPopup("hello", MessageBox.TYPE_INFO, 5, id="popup")

    popup = session.current_dialog
    # The exact class the plugin imports, opened by `session.open` (3877).
    assert type(popup) is MessageBox
    assert popup.type == MessageBox.TYPE_INFO
    assert popup.list == []
    assert popup.close_on_any_key is True
    assert popup.skinName == ["MessageBoxSimple", "MessageBox"]
    # StartEnigma.py 130-133: a list of (dialog, shown) tuples, and the info bar
    # went on it with its OSD hidden.
    assert session.dialog_stack == [(info_bar, False)]
    assert type(session.dialog_stack[0]) is tuple
    assert info_bar.execing is False
    assert notifications == []
    assert current_notifications == [("popup", popup)]


def test_a_question_through_the_queue_is_a_question():
    # RecordTimer and InfoBarGenerics queue their questions with no type at all.
    session, info_bar = _info_bar()

    AddNotification(MessageBox, "A timer failed to record. Disable TV and try again?")

    question = session.current_dialog
    assert type(question) is MessageBox
    assert question.type == MessageBox.TYPE_YESNO
    assert question.list == [("Yes", True), ("No", False)]
    assert session.dialog_stack == [(info_bar, False)]


def test_a_second_notification_waits_until_the_popup_has_closed():
    # InfoBarGenerics 3849-3851: the info bar opens nothing while it is not
    # executing, and it is not while a popup is over it.
    session, info_bar = _info_bar()
    AddPopup("first", MessageBox.TYPE_INFO, 5, id="popup")
    first = session.current_dialog

    AddNotification(MessageBox, "second?")
    assert session.current_dialog is first
    assert len(notifications) == 1

    first.close(True)
    # StartEnigma.py 164-180 and 63-73: the close is taken on the next turn of
    # the main loop, and until then the popup is still the current dialog.
    assert session.current_dialog is first
    MainLoop.advance(0)

    second = session.current_dialog
    assert type(second) is MessageBox
    assert second.type == MessageBox.TYPE_YESNO
    assert session.dialog_stack == [(info_bar, False)]
    assert notifications == []


def test_closing_the_popup_gives_the_screen_back_to_the_info_bar():
    session, info_bar = _info_bar()
    AddPopup("hello", MessageBox.TYPE_INFO, 5, id="popup")

    session.current_dialog.close(True)
    MainLoop.advance(0)

    assert session.current_dialog is info_bar
    assert session.dialog_stack == []
    assert current_notifications == []
    # Coming back from under the popup, the info bar stays as it was: hidden.
    assert info_bar.shown is False


def test_an_info_bar_still_showing_is_stacked_as_shown_and_hidden_under_the_popup():
    # StartEnigma.py 132-133 stack the dialog with its `shown` and then hide it
    # (94); popping it back shows it again (137-138, 86-87).
    session = ModalSession()
    info_bar = session.open(NotifiableInfoBar)
    assert info_bar.shown is True

    AddPopup("hello", MessageBox.TYPE_INFO, 5, id="popup")
    assert session.dialog_stack == [(info_bar, True)]
    assert info_bar.shown is False

    session.current_dialog.close(True)
    MainLoop.advance(0)
    assert session.current_dialog is info_bar
    assert info_bar.shown is True


def test_a_new_popup_with_the_same_id_replaces_the_shown_one():
    # Notifications.pyc 69-70 and 62-65: the shown popup is closed, and the new
    # one opens once the info bar executes again.
    session, info_bar = _info_bar()
    AddPopup("one", MessageBox.TYPE_INFO, 5, id="popup")
    first = session.current_dialog

    AddPopup("two", MessageBox.TYPE_WARNING, 5, id="popup")
    MainLoop.advance(0)

    second = session.current_dialog
    assert second is not first
    assert second.text == "two"
    assert second.type == MessageBox.TYPE_WARNING
    assert session.dialog_stack == [(info_bar, False)]
    assert current_notifications == [("popup", second)]


def test_the_plugins_own_popup_arrives_over_the_info_bar_this_way():
    session, info_bar = _info_bar()

    assert osd.show("Dinner is ready", "info", 15) is None

    popup = session.current_dialog
    assert type(popup) is MessageBox
    assert popup.type == MessageBox.TYPE_INFO
    assert popup.timeout == 15
    assert session.dialog_stack == [(info_bar, False)]
