# ADR-0008: The discreet toast is made of widgets that bind no keys, refuses a timeout it cannot honour, and is deleted rather than closed

**Status:** accepted 2026-09-23, amended 2026-09-23 (twice: the backslash rule, then the popup
under it), amended by [ADR-0009](0009-the-openwebif-page-trusts-openwebif.md) (the box-only setting)
**Date:** 2026-09-23
**Supersedes:** [ADR-0003](0003-control-feedback-and-household-features.md) §2, in part - it
described this feature, and three parts of it were right for the wrong reason or contradicted
themselves when they were read against the compiled modules and the C++ a real image runs.
Everything else in ADR-0003 §2 stands: a plugin-owned screen instantiated as a dialog it never
executes, top right, auto-hide only, newest replaces current, capability `toast` claimed only once
the screen exists, and the box-only setting `osd_toast`, on by default.

## Context

ADR-0003 §2 decided a second message style: a non-modal screen the plugin owns, created with
`session.instantiateDialog()` and never executed, shown and hidden by a timer. That mechanism was
confirmed on an OpenViX 6.6 image, whose own volume bar and unhandled-key symbol are built exactly
this way: `instantiateDialog` constructs, skins and lays the screen out and never touches the dialog
stack, `current_dialog` or `in_exec`; a Python `ActionMap` binds only between `execBegin` and
`execEnd`; and `Screen.show()` calls neither. There is no toast screen on the image to borrow, and
`MessageBox` cannot be borrowed non-modally, because its own timeout calls `close()`.

Three things in §2 did not survive that reading.

**„It binds no action map, so it cannot swallow a key press" is right for the wrong reason.** Every
key reaches the interface through one dispatcher, ordered by priority and, within a priority, by the
age of the binding. Python action maps are one kind of binding there. **Native widget bindings are the
other, and they need neither an action map nor exec**: enigma2's list widget, `eListbox`, binds the
list-navigation keys at priority 0 in its own constructor, and while it is visible it consumes
UP/DOWN/LEFT/RIGHT, the channel keys and the skip keys and stops dispatch. The channel list moves on
exactly those bindings. A toast containing any list-type widget - `MenuList`, a `List` source,
`ChoiceList`, `ConfigList`, `SelectionList`, `ScrollLabel` - or an `Input`, created when the session
starts and therefore older than the channel list, would be asked first and eat the DOWN key while it
is on screen. After a skin reload the order reverses and the defect hides, which would make it
intermittent in the field.

**„Clamped to 1-30, and `0` is refused" contradicts itself** - clamping makes `0` into `1` - and it
misses the negatives: to the popup, **every** timeout of `0` or less means „until dismissed",
because `MessageBox` starts its timer only when the timeout is positive. It also hid an ordering
defect: the command handler fills in the popup's default of 10 seconds before it reads anything
else, so implemented as written, every toast without a `timeout` would last ten seconds, not five.

**„Torn down on standby and on shutdown" said what, not how, and the obvious how is wrong.**
`Screen.close()` on a screen that is not executing only records a value for an `execBegin` that
will never come; it hides nothing. The image's own volume control closes its dialogs this way on a
skin reload and leaks its windows. And teardown on shutdown is not cosmetic: enigma2 repaints the
desktop once more **after** the plugins are shut down, and the next process clears the frame buffer
only when it initialises, so a toast still visible at that moment is the frame a restarting receiver
leaves on the television for the whole restart.

Two further facts were not in §2 at all. The standby screen is a full-screen black window at z 0, so
a toast - which has to be above the channel list, also at 0 - is drawn over it on a television that
is still on. And enigma2's text renderer reads a backslash, a `c` and the next eight characters as a
colour change, so a payload could recolour its own text, which is not „one fixed appearance".

## Decision

**The widget rule.** The toast screen holds `Label` and `Pixmap` components only - in practice two
labels, a fixed header and the message - and no `ActionMap` of any kind. A test inspects the
instantiated screen and fails on any other `GUIComponent` and on any action map, and it is shown
able to fail by instantiating the screen with a `MenuList` added. „Cannot take a key press" is a
property of the widgets, not of the action maps. A skin may still define a screen called
`MQTTBridgeToast` and restyle it; that is the box owner's choice, not a broker client's.

**Z-position 10**, the tier the image uses for its own transient overlays. The toast has to be
strictly above the channel list without relying on creation order, because within one z the older
window wins and a skin reload recreates the toast.

**The geometry is computed, and written into the skin as plain integers.** Numbers in a plugin's
embedded skin are desktop pixels. The toast reads the desktop's size, takes `f = height / 720`, and
formats integers into its skin string per instance before `Screen.__init__`: width `420·f`, never
wider than the desktop; equal margins of `12·f` from the top and the right edge; fonts of `16·f` and
`20·f`; and a height that follows the text, measured after it is set and capped at `220·f`. It uses
none of the skin-expression features, which are not on every image.

**One fixed appearance.** A dark, slightly transparent box, light text, and a header that always
reads the plugin's name in the receiver's language - not the device name, which is provisioned from
outside. **No backslash reaches the screen: every backslash is removed, and nothing else.** The
renderer reads its escapes **after** right-to-left reordering: text that mixes a right-to-left script
with `cFFFF0000\` is an escape on screen and none in the string, and a receiver showed exactly that -
the escape consumed by the renderer and never drawn. Every escape the renderer knows starts with a
backslash, so without one there is none, in any drawing order. The characters after it stay:
`\cFFFF0000Alarm` shows as `cFFFF0000Alarm`, `C:\config.txt` as `C:config.txt`, a literal `\n` as
`n`; a real newline character stays a line break. The text is capped at 200 characters **after**
this, so the cap counts what is displayed. (Amended 2026-09-23: this record first removed a `\c`
escape together with its eight characters and then the remaining backslashes, and before that only
the escape. The measurement above ended the first rule; the operator then chose the plainer second.)

**The rule covers both styles** (amended 2026-09-23, by operator decision, while this record is
still unreleased). The popup's text is drawn by the same renderer, so everything above about
escapes is as true of a popup as of a toast; a popup that could still recolour its text or be broken
into lines by a payload would leave the escape problem open on the default style. The popup removes
every backslash and nothing else through the toast's own function - one rule in one place - then
applies its own cap of **500** to what is left, and refuses a text with nothing left as empty, as it
always refused a blank one.

**The contract.** `cmd/message` gains an optional `style`. **The handler decides the style before it
applies any default.** Absent or `null` - or a payload that is not a JSON object - is the popup,
unchanged except that its text follows the rule above. `"toast"`, trimmed and case-insensitive, is
the toast; any other value is refused. For a toast:

| Field | Rule |
|---|---|
| `text` | required; empty refused; every backslash removed and nothing else, then truncated at 200 |
| `timeout` | parsed as the popup parses it, with `int()`; default **5**; **`0` or less refused** („a toast hides itself; timeout must be 1&ndash;30 seconds"); more than 30 becomes 30, with a note in the log |
| `type` | validated exactly as for a popup - an unknown value is refused, so a payload is valid or invalid whatever its style - and then ignored |

**Standby refuses; it does not queue.** On the increment of the standby counter the toast is hidden
and its timer stopped. A toast that arrives while the receiver is in standby, or while the image's
„really shut down / restart?" question is on screen, is refused on `last_error` with „the receiver is
in standby". A late toast is a wrong toast, and a refusal is something a sender can see where a
silent drop is not. The second condition is the image's `inTryQuitMainloop`, and it is narrower than
its name: `TryQuitMainloop` sets it when that question is shown and clears it when it is hidden,
which happens before the main loop is told to quit, and the question appears only when there is a
reason to ask - a recording, a running job, timeshift, a stream. An ordinary shutdown or interface
restart never sets it.

**Deleted, never closed.** The toast is torn down by stopping its timer, then calling
`session.deleteDialog()`, then dropping the reference - in that order, because the delete sets every
attribute of the screen to `None`. It is never passed to `close()`, `open()`, `openWithCallback()` or
`execDialog()`. The teardown runs on every path that stops the bridge - a settings save, removal from
the plugin browser, the shutdown notification - and on a skin reload, after which it is created
again for the new desktop. If that fails, the capability is taken back and `info` is republished.

**Nothing raises into the interface.** Every entry point - showing, the timer, the standby hook, the
skin-reload callback, stopping - is wrapped, and a failure is a line in the log.

## Consequences

- **A toast can never carry a list, a scroll area or an input.** Anything that needs one - a longer
  message, a choice - is a different feature with a different screen, and it would have to solve the
  key-binding problem this record describes before it could be non-modal.
- **A timeout of `0` means different things in the two styles**, and a consumer has to know which it
  is sending: „until dismissed" to a popup, refused by a toast. The companion integration is to
  refuse a toast timeout outside 1-30 itself, so that a user sees the reason before anything is
  published.
- **The toast depends on the image's standby flags** (`Screens.Standby.inStandby` and
  `inTryQuitMainloop`). An image without them gets a toast that is not refused in standby - it is
  still hidden when the standby counter moves - rather than no toast at all.
- **The geometry is a starting point, not a measurement.** Legibility and size on a real television,
  and how the translucent box reads over bright video, need a person looking at the screen.
- **Neither style can show a backslash at all**, even one a sender meant literally - a path, a
  regular expression. That is the price of an appearance the payload cannot change.
- ~~**The popup still passes colour escapes through.** Whether it should get the same treatment is a
  separate decision; this record does not change the popup in any way.~~ Decided the same day: it
  gets the same treatment. **This changes the popup for existing senders**: a sender that relied on a
  literal `\n` for a line break in a popup now shows an `n` and has to send a real newline.
