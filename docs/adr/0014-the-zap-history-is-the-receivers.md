# ADR-0014: The zap history is the receiver's, and the plugin's zaps are in it

**Status:** accepted 2026-09-24, amended 2026-09-25
**Date:** 2026-09-24
**Supersedes:** - (changes the behaviour of `cmd/zap`, documented in `docs/TOPICS.md`)

## Context

An enigma2 receiver keeps a list of the channels zapped to. Its "History Zap" screen shows it when
KEY_NEXT or KEY_PREVIOUS is pressed, newest first, and a household uses it to go back to what it
was watching. Nothing outside the receiver could see that list, jump into it or clear it, and the
0 key - which on the images read here clears it and lands on channel 1 - had no counterpart over
MQTT.

Reading the bytecode of one image (OpenViX 6.6, `Screens/ChannelSelection`,
`Screens/InfoBarGenerics`, `Components/Timeshift`, `Screens/Standby`, `Navigation`, `keymap.xml`)
settled what the list is and how it moves:

- **It is one Python list on the info bar's channel selection**, `InfoBar.instance.servicelist
  .history`, oldest first. Each entry is the service path at the time of the zap (root, bouquet)
  with the service last; `history_pos` is the current one. `HISTORYSIZE` (20) bounds it, and the
  same service never appears twice. Radio and television share it while the image's "e1-like"
  radio mode is on, which is its default.
- **Only zaps through the channel selection enter it.** `addToHistory` is called from the channel
  selection's zap and its timeshift callback, and never from `Navigation.playService`. The plugin's
  `cmd/zap` used `playService` whenever the target was not in the list being browsed, and always
  from standby, so those zaps were missing from the list the household sees.
- **The remote's own "zap to a channel of a bouquet"** is
  `InfoBar.instance.selectAndStartService(service, bouquet)`: enter the bouquet, select, zap,
  correct the channel number. It records `[root, bouquet, service]`. Its zap asks
  `checkTimeshiftRunning` first, which during timeshift opens a question with no timeout, and in
  picture-in-picture zap mode it zaps the small picture.
- **Waking is deferred.** `inStandby.Power()` closes the standby screen on the next turn of the
  main loop, and the screen's own close handler then plays the channel the box slept on. A zap made
  in the same turn as the wake is overwritten.
- **The 0 key is one handler**, `keyNumberGlobal(0)`, on key-down only; a long press is the same
  press. With `config.usage.panicbutton` on (the default) and more than one history entry it runs
  `checkTimeshiftRunning(recallPrevService)`, and the panic branch **replaces** the history with a
  new empty list and zaps to channel 1 - the first channel of the first bouquet - which records
  that one channel. It does nothing with fewer than two entries, asks the timeshift question in
  timeshift, does nothing while `pts_blockZap_timer` runs, and acts on picture-in-picture when that
  is showing and `pip_zero_button` is not "standard". There is no "panic channel" setting.

## Decision

**The plugin publishes the receiver's list and keeps none of its own.** A new retained topic
`zap_history` carries the entries the receiver's own screen would show, newest first - service
reference, the name the `service` topic uses, the bouquet of the path and its published name -
with `current`, `limit` and `panic_button`. It is read fresh from `InfoBar.instance.servicelist` on
every look, never through a held reference, because the 0 key replaces the list object. It is
polled every two seconds, the same look `bouquet_context` takes, and published only when a cheap
key of the list changes. A user-interface restart empties the receiver's list, and the topic then
says so.

**Two capabilities, because an image can offer one without the other.** `zap_history` is claimed on
the first successful read of the list with the history screen's own calls present; it carries the
topic and `cmd/zap_history`. `history_clear` additionally needs the 0 key's own path on the info bar
and `config.usage.panicbutton`; it carries `cmd/history_clear`.

**A zap into the history is the history screen's own.** `cmd/zap_history` takes exactly
`{"sref": ...}` - by reference, never by position or name, because the list reorders on every zap
and names repeat - finds the entry by identity and calls `historyMenuClosed` with the entry's own
reference object, which moves it to the front and plays it. It has no permission and no recording
or timeshift guard, because the receiver's screen has none. It is refused while a recording is
played back.

**Clearing is the 0 key's own handler, after every case in which 0 would not clear.**
`cmd/history_clear` refuses, in order: standby, `panic_button` not on, fewer than two entries,
timeshift, the post-timeshift zap block, picture-in-picture taking the 0 key, and the playback of a
recording, and any other screen open over the info bar. Then it calls `InfoBar.instance.keyNumberGlobal(0)` - the handler the key reaches when
nothing is open - rather than injecting a key, which would go to whatever screen has focus. It then
reads the list back and says so when more than one entry is left. It has no permission, because
`cmd/key KEY_0` already does the same with none.

**Each refusal carries a stable reason code.** `last_error` gains an optional `reason` field, set
only by handlers that define codes (`standby`, `panic_off`, `too_short`, `timeshift`,
`zap_blocked`, `pip`, `playback`, `screen_open`, `not_cleared`), so a consumer can say the refusal in the
household's language. The English sentence stays the contract's human text, and every other
`last_error` is byte for byte what it was.

**Every zap the plugin makes goes through the channel list, so it is recorded.** `cmd/zap` calls
`selectAndStartService` in the bouquet the channel list is browsing when that is a published
bouquet holding the service, otherwise in the first published bouquet that does - moving the channel
list there, as a number zap on the remote does. From standby it hooks the standby screen's close
before waking it and zaps on the turn after the receiver's own restore; any later zap replaces one
still waiting. Six cases play the service directly and are not recorded, because the recorded path
would be worse: a screen open over the info bar (the channel list, the EPG, a menu - the zap would
leave the remote on a list opened out of sight), timeshift (the no-timeout question; any active
timeshift blocks, whatever the image's "check timeshift" setting says, and so does a timeshift state
that cannot be read), picture-in-picture zap mode (the small picture), a channel list in radio mode
(a television bouquet would be saved under the radio root), a service in no published bouquet
(nothing to enter), and a selection that did not take - the published bouquets are read once a
minute, and a stale one leaves the channel list re-zapping what is playing - unless the service is
protected by parental control and the PIN is what it waits for. `cmd/bouquet` is refused during
timeshift for the same question, and its zap is played directly while a screen is open.

**Amended 2026-09-25 (review, before merge).** "A screen open over the info bar" was not in the
first draft: the executing dialog is `session.current_dialog` (`StartEnigma.Session`), the info bar
is the session's first dialog, and the channel list becomes the executing dialog through
`execDialog` - the image's own skin reloader asks "is anything open?" the same way. It adds the
direct-play case above, the `screen_open` refusal of `cmd/history_clear`, and the direct play of
`cmd/zap_history`. The same review added the fallback for a selection that did not take, the
replacement of a waiting zap by any later one, and `cmd/zap_history` checking the entry before it
wakes the receiver.

**Amended again 2026-09-25 (after 0.3.0).** An information popup directly over the info bar is not
a screen open: a queued popup is opened with `session.open`, so while it shows it is the executing
dialog, and a zap made then - under a `cmd/message` popup, or under the image's own "Zapped to
timer service" - was played directly and left out of the history. The exception is narrow: exactly
`Screens.MessageBox.MessageBox`, of type information, warning or error (`TYPE_MESSAGE`, which only
a type the image does not know becomes, is not included), with an empty answer list, executing,
and with `dialog_stack` holding the info bar alone. A question - what a message box queued without
a type is - never qualifies. The popup is left to its own timeout, as the image's channel-list zap
leaves it. `cmd/zap`, `cmd/zap_history` and the zap of `cmd/bouquet` share the rule;
`cmd/history_clear` does not, because it is the 0 key and a key goes to the popup.

**Discovery mode announces the clear button and no history select.** A core MQTT select carries its
options inside the discovery payload, so a list that changes on every zap would mean republishing
discovery on every zap. The companion integration builds the select from the topic.

## Consequences

- **Home Assistant zaps now move the receiver's channel list** when the channel is outside the
  bouquet being browsed: channel up and down walk the new bouquet and `bouquet` names it. This is
  what the remote does, and a visible change for anybody used to the old behaviour.
- **Names travel.** Every entry is on the broker, retained, whatever bouquet it came from; the
  broker's access control is the only thing between it and any other login, exactly as for
  `channels`. Hiding a bouquet is the consumer's business and hides nothing on the broker. The
  OpenWebif page shows the list unfiltered to whoever OpenWebif admits.
- **The list is only as complete as the receiver's.** Zaps made by OpenWebif's own zap, by a zap
  timer or from the EPG are in it only if the image routes them through the channel list, which was
  not read. The direct-play cases above are not in it.
- **The clear lands on channel 1**, which is the first channel of the first bouquet and moves when
  the bouquet order does. The settings that change what 0 does - `panicbutton`, `multibouquet`,
  `pip_zero_button` - are the image's; the plugin reads `panicbutton`, and asks the image's own
  `pipHandles0Action` for the picture-in-picture case. It does not read `check_timeshift`: any
  active timeshift refuses the clear.
- Reversing the zap change means going back to `playService` and to zaps the receiver's list does
  not show; reversing the history means only removing a topic and two commands.
