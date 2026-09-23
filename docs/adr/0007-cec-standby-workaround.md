# ADR-0007: The CEC standby workaround identifies the television's standby when it is queued, and holds the echo across the close

**Status:** accepted 2026-09-22
**Date:** 2026-09-22
**Supersedes:** [ADR-0003](0003-control-feedback-and-household-features.md) §5, in part — it
described this feature, and three parts of it turned out to be wrong when they were read against
the compiled modules a real image runs. Everything else in ADR-0003 stands, including why the
feature is opt-in.

## Context

ADR-0003 §5 described the defect and a fix: when a standby is queued while the channel list is the
current dialog, close the list so the standby proceeds, and when the television reports power-on
while a stale standby is still queued, drop it. The defect it describes is real and was confirmed,
fragment by fragment, in the bytecode of an OpenViX 6.6 image and in current OpenViX and OpenPLi
sources:

- `HdmiCec.standby()` queues `Notifications.AddNotification(Screens.Standby.Standby)`, an entry with
  no identifier, so `RemovePopup` cannot reach it.
- The queue is drained by `InfoBarNotifications`, whose `notificationAdded` callback returns at once
  unless the info bar is executing. With the channel list open, nothing drains it.
- `handlingStandbyFromTV` is set to `True`, the standby is queued, and the flag is set back to
  `False` — around the *queueing* call. It is read much later, by `sendStandbyMessages`, which sends
  `<Standby>` to the television unless the flag is set. When the queue is drained at once the whole
  chain runs inside the bracket; when the standby waited, it does not, and the receiver echoes the
  standby back to the television.

Implementing the fix as ADR-0003 wrote it needed three questions answered that it did not ask, and
each answer changed the design.

**Closing the list does not stop the echo.** `Session.close()` does not pop a dialog. It asserts
that the screen being closed is the current dialog, starts a zero-millisecond timer and returns;
the pop, the info bar resuming, and the drain that finally runs the standby all happen on a later
turn of the main loop, long after `messageReceived` has put the flag back to `False`. However
quickly the list is closed, the standby it releases runs outside the bracket.

**„The television reports power-on" is not an event while the receiver is awake.** `HdmiCec`'s entire
wake-up branch sits inside `if Screens.Standby.inStandby`. A stale standby means the receiver is
awake, so every wake-up message from the television is discarded and nothing is called. The only
route is the raw CEC signal plus a guess at which message a given television sends on power-on —
and the image itself declines to guess: it offers a nine-way setting for it, because it varies.

**The queued standby is indistinguishable from the household's own.** `cmd/power standby` queues
`(None, Standby, (), {}, None)` — the same tuple, equal in every field. Anything that later scans
the queue for „a standby" can cancel one somebody has just asked for. (The remote's power button is
not a second case: on the image read, it opens the standby screen directly and never enters the
queue.)

## Decision

**The hook is the image's own list.** The plugin appends a zero-argument callable to
`Tools.Notifications.notificationAdded` — the list `InfoBarNotifications` registers on — and
removes it when it stops. `__AddNotification` appends the entry and then calls every listener with
no arguments, so the callback reads the last entry itself. Wrapping `AddNotification` is rejected:
replacing a function the whole interface calls is a larger blast radius than joining a list it
already iterates.

**The television's standby is identified at the moment it is queued, and nowhere else.** An entry
counts only when, inside that callback, its screen **is** `Screens.Standby.Standby` and
`Components.HdmiCec.HdmiCec.instance.handlingStandbyFromTV` **is** `True`. The callback runs inside
`HdmiCec.standby()`, which runs inside the bracket, so the flag is `True` if and only if the
television queued the entry — as long as the image is the only thing that writes it. The plugin
writes it too (the hold, below), so the hold never writes `True`: it writes a private marker that is
truthy, which is all the image asks of the flag before it echoes, and fails the identity test. A
household standby queued during a hold is therefore not mistaken for the television's. The entry is
then kept **by identity** — the tuple object itself, not its `id()` — and no other entry is ever
closed on, removed or counted. The callback runs for every notification the image queues, so a
screen other than the standby screen returns after one comparison.

**Strict allowlist: the channel list and nothing else.** Exactly `ChannelSelection`,
`ChannelSelectionRadio` and `PiPZapSelection`, matched against the dialog's class name and the names
in its method resolution order. `ChannelSelectionBase` is **excluded** although both real channel
lists inherit it, because `SimpleChannelSelection` inherits it too, and that is the service picker
embedded in the timer editor and other workflows — closing it answers somebody's half-finished
question with „nothing". Nothing is matched by module either: the list's context menu, the bouquet
selectors and the zap history live in the same module and are left alone. Any screen not on the list
is left alone and the fact is logged at debug.

**Only `session.current_dialog`, once.** It is re-read immediately before acting, `session.in_exec`
is required, the dialog stack is never walked, and each identified standby is acted on at most once
(a flag on its own record). A dialog stacked on the channel list — the context menu, a bouquet
selector, a message box — is therefore the current dialog, is not on the list, and nothing happens.
Every exception from the close is swallowed: a close that did not happen because the user got there
first is the outcome that was wanted anyway. The action runs on the turn of the main loop after the
standby was queued, not inside the television's own message handler — when the info bar is in front
it drains the entry in that same turn, and there is then nothing to do.

**`cancel()` before `close()`.** The dialog's own `cancel()` is what EXIT runs; on the channel list it
restores the mode, the root and the service the user was on before closing. `close()` is the
fallback for a dialog without one. A raw `close()` would leave somebody who was previewing channels
on whichever one the cursor happened to be over.

**The echo is suppressed by holding the flag, not by closing fast.** Immediately before closing, the
plugin records `handlingStandbyFromTV`, sets it to its truthy marker (never `True`, see above), and
restores the recorded value either once
the standby has happened — `config.misc.standbyCounter` moved — or after **five seconds**, whichever
comes first. The deadline exists because a hold that leaked would silently stop „switching the
receiver to standby switches the television off" for the rest of the session. 🔴 The restore never
happens inside the counter's notification: the image reads the flag from its own notifier on the same
counter, so restoring there would make the outcome depend on notifier order. It waits **1.5 seconds**
after the counter moves, which also covers an image set to look for other receivers on the bus —
there the flag is read from a one-second timer the image starts in that notifier. If `HdmiCec` has
become unreachable the list is closed anyway and the log says the echo could not be suppressed; a
late standby is better than none. Stopping the plugin restores a held flag immediately.

**A stale standby is dropped by a deadline, not by a power-on.** An identified standby still in the
queue **30 seconds** after it was queued is removed from the list by identity and counted. An entry
that was drained normally is not touched and its record is discarded. The power-on trigger is
**deferred**: it needs the raw CEC signal and a guess at an opcode the image itself refuses to guess,
in a feature whose failure mode is „a standby somebody asked for was cancelled".

**Once the receiver is in standby, the television's request is done.** When the standby counter
moves, any identified standby still queued is removed by identity, so that waking the receiver does
not put it straight back to sleep. Nothing that was not identified is touched. If one of the
television's standbys ran, the leftovers are repeats (a television that sent `<Standby>` twice,
while the info bar carries out one entry per turn) and their removal is not counted. If none ran —
the receiver went to standby some other way, typically the remote's power button, which opens the
standby screen directly — the plugin has thrown the television's standby away, and that is counted
once as `dropped_stale_standby`.

**One television standby, at most one intervention.** A close is counted when the standby it
released has happened — the counter moved and the entry is gone — or, where the counter cannot be
watched, when the entry is found drained at its deadline (and only if there was a close). A close
that released nothing is not a success: that standby is counted once, as `dropped_stale_standby`,
when its deadline drops it or when the receiver goes to standby some other way first. The kind is
a drop rather than a close because the television's own request is not what put the receiver to
standby.

**Box-only, and not echoed.** `cec_standby_workaround` defaults off, is set on the receiver's setup
screen or in the provisioning file, and is in neither `cmd/config` list: it enables no command, it is
the kill-switch for code that closes a screen, and the capability `cec_workaround` already tells a
consumer whether it is at work. With it off nothing is bound and nothing of the image's is read. The
capability is claimed only when the setting is on, `notificationAdded` is a list, the standby
screen resolved, `HdmiCec.instance` is not `None`, **and** the image's `config.hdmicec.enabled` and
`config.hdmicec.handle_tv_standby` are both on. The singleton alone proves nothing: the image
builds it whether or not CEC is switched on, and `messageReceived` returns at once with CEC off and
only runs the bracket around the queueing call with `handle_tv_standby` on. An image without
HDMI-CEC, with it switched off, set not to follow the television into standby, or without one of
those settings, gets no capability rather than a hook that can never fire. The two settings are
read once, when the workaround starts; changing them takes effect when the plugin next starts.

**The topic.** Retained `cec`: `last_intervention` (epoch seconds or `null`), `kind`
(`closed_channel_list` | `dropped_stale_standby` | `null`), `count` (since the plugin started, at most one per television standby) and
`pending` (an identified standby is queued and has not been carried out). Every key is always
present. `last_intervention` is the time of an event, not a stamp the payload takes when it is
built, so it is **not** volatile under [ADR-0006](0006-volatile-fields-and-publish-on-change.md).
The topic is retracted on every connect on which the capability is absent.

## Consequences

- **The plugin writes an attribute of an image singleton.** That is the cost of suppressing the
  echo, and it is why the write is bounded by a deadline, restores the value it found rather than
  `False`, and is only reachable with the opt-in setting on.
- **Anything that later touches the notification queue identifies by identity.** An equality test —
  including `list.remove`, which removes the first *equal* entry — would remove the household's own
  standby from a queue that holds both. This is now a rule, not a detail.
- **One race is not covered.** When the user closes the channel list in the same instant the
  television's standby arrives, the plugin correctly does nothing, and the standby released by the
  user's own close is echoed exactly as it would have been without the plugin.
- **A second `<Standby>` inside one main-loop turn can end the hold.** If the television repeats
  itself after the close but before the pop, the image's own bracket writes `False` over the hold and
  the standby that follows is echoed. Whether any television does this has not been measured.
- **A second close inside one hold re-asserts the marker and keeps the recorded value.** The
  television's bracket around its second standby writes `False` over the hold before the second
  close; the marker is written again, and the value put back at the end is still the one recorded
  when the hold began — never the marker itself, which would otherwise stay in the flag for the
  rest of the session.
- **The hold also changes what the image sends for a standby from the remote during it.** The
  remote's power button opens the standby screen directly; with the flag held, the image sends
  „source inactive" instead of `<Standby>`. It matters only if the television was switched back on
  within those few seconds.
- **A renamed screen fails safe.** An image that renames its channel-list classes stops matching the
  allowlist, and the workaround quietly does nothing more than drop stale standbys. Nobody should
  „fix" that by widening the match to a base class or a module.
- **The deferred power-on trigger stays deferred** until somebody can measure which message their
  television sends and there is a way to learn it per receiver rather than guess it.
- **The real fix is upstream, and is separate.** Moving `handlingStandbyFromTV = False` out of
  `messageReceived` and onto the standby screen's own lifecycle would cure the echo for every image
  and every plugin. It would not cure the wait, which is what a household notices, so it does not
  replace this record; if it lands, the hold becomes redundant and harmless.
