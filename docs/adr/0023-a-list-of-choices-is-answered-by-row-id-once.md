# ADR-0023: A list of choices is answered by row id, once

**Status:** accepted 2026-10-18
**Date:** 2026-10-18
**Supersedes:** - . It extends
[ADR-0017](0017-whether-a-service-can-be-played-is-asked-with-an-empty-second-reference.md)
in one place: a second command is answered on a topic of its own.

## Context

Home Assistant sometimes has a question for whoever is in front of the television: someone is at
the gate - open it or not? The television goes off in a minute - fifteen minutes more, or not? The
viewer has the remote in hand; Home Assistant knows what each answer means. A toast can say that
something happened; it cannot ask.

Three things are true of the path between them, as they are for the receiver menu
([ADR-0020](0020-the-receiver-menu-is-drawn-on-the-receiver-and-decided-in-home-assistant.md)):

- **The broker is not a security boundary in every set-up.** The Mosquitto add-on most Home
  Assistant users run never consults its ACL file, so any client with a login can publish on any
  topic - a question for the television, and an answer to it.
- **An answer that arrives late is a wrong answer.** "Open the gate" five minutes after the
  doorbell, delivered from a queue after a reconnect, opens the gate for whoever is there now.
- **Images differ in what a plugin's screen can draw**, and the receiver menu already has a styled
  screen built on trial and the image's own `ChoiceBox` as its fallback.

## Decision

1. **The answer is a row id, and nothing else.** `cmd/list` carries a title, a text and 1 to 24
   rows, each an `id` and a `label`, with an optional key and icon. The receiver shows them and
   says, on `list_answer`, which row id was picked. It never learns what a row does, holds no
   entity id, domain or service, and calls nothing. A yes/no question is a list of two rows.
   Whoever sent the list decides what the id means - so a list can do exactly what its sender
   does with the answer, and the receiver adds no power of its own to it.
2. **Exactly one answer for every list accepted, or one late refusal.** It ends `picked` (with the
   row), `timeout` (with the `default` row, or `null`), `dismissed` (EXIT), `replaced` (a newer
   list) or `closed` (standby, the plugin stopping, the screen closed by something else). Every way
   out goes through one function that answers once; the first way out wins, and a key, a timer or a
   callback that comes after it finds the list answered. A timeout names the default row but says
   `timeout`, so a sender can always tell a press from silence. A refused list - invalid, in
   standby, without the capability, while an update holds the receiver - has a refusal on
   `last_error` and no answer; so does an accepted one that could not be put on the screen after
   all, whose refusal comes later, with its id. While an update keeps the plugin silent the doors'
   refusal of a list is published all the same, not retained: its caller is waiting.
3. **The list's id is the command's.** It comes in the envelope every command takes
   ([ADR-0016](0016-a-commands-id-comes-in-an-envelope-and-only-a-refusal-says-it-back.md)), and
   the answer says it back, as `stream_playable` does. A list without an id is shown and answered
   all the same. The receiver compares ids with nothing; a sender keeps its own apart.
4. **Never queued.** The answer is an event at QoS 1, never retained, published on the open
   session only, and taken out of the MQTT client's queue when the broker had not acknowledged it
   as the connection went - by the main loop when it hears of it, and by the client itself as the
   broker accepts the next connection, exactly as a `menu_action`. A pick made without a broker is
   lost, and its sender's wait ends with no answer. `closed` is said while the session is still
   up: before `offline` on a stop, on the old session on a settings save that reconnects, before
   the uninstall closes its doors - and never later. A save applied under the open session
   leaves the list alone.
5. **A list always has a timeout**: 60 seconds unless the sender says 5 to 600. A question left on
   the television until somebody happens to press EXIT would answer, much later, a question
   nobody is waiting for any more.
6. **One list at a time; the newer one wins.** A list arriving while one is shown answers the open
   one `replaced` first - its sender is not left waiting - and is shown once the old screen has
   gone. An invalid list refuses itself and leaves the open one alone.
7. **Over whatever is on screen, and never like the receiver's own dialog.** The list is a modal
   screen opened over the dialog that is executing - the info bar, a player, the receiver menu, the
   image's keyboard - which keeps its place and gets the keys back. The screen always carries a
   header line saying the question comes from Home Assistant through this plugin, and no text in the
   payload can change it. One text rule, which the companion integration applies too: every
   backslash removed (the toast's rule), the invisible format characters and any lone surrogate
   dropped - JSON can spell one, and the image's renderer would be handed it unchecked - control
   characters made spaces, cut at the limit, trimmed; a title and a label are one line. The title
   and the text are shown whole: the screen is as tall as they need. With a default row the
   countdown names it, so the viewer sees what waiting answers. The power key does on the list what
   it does on the screen under it. In standby, or while the receiver asks whether to shut down, a
   list is refused, not kept for later: a late question is a wrong question.
8. **The receiver menu's look, and the image's list where it cannot be had.** The styled screen
   reuses the menu's machinery - computed geometry, the icon set, the see-through background of
   `menu_transparency`, a second way for every optional drawing call, a trial build off the dialog
   stack before the real open. A row's key is drawn, never written: a filled rounded button in the
   key's colour, or the digit in a small box. Where the styled screen cannot be built the image's
   own `ChoiceBox` shows the same rows, ids and keys, and keeps the header line where the image puts
   the question's first line in its window title, with the countdown after it. `info.list.screen`
   says which; the capability `list` is claimed only once one of them has been built, and taken back
   when a skin reload can build neither, as the toast's is.
9. **No permission and no setting**, as a message has none. A list changes nothing on the
   receiver; what it can do is decided by its sender, who can already publish anything on the
   broker. SECURITY.md says what follows: anyone on the broker can put a list on the television or
   forge its answer, so an answer is never proof of who pressed what.

## Alternatives that were not taken

- **The action in the payload** - a service to call, or an MQTT message to publish, per row. The
  receiver would then hold, and could be made to perform, whatever a broker client wrote into a
  row; with ids only it holds nothing worth taking.
- **An answer that may wait for the broker.** The client's queue would deliver a pick minutes after
  the question, which is the one thing a doorbell answer must never do. Losing it is the better
  failure: the sender knows it got none.
- **A permission on the receiver.** It would protect nothing the broker does not already give away,
  and a household would switch it on once and forget it; the protection that matters is in the
  sender, which treats an answer as a wish, not a proof.
- **Queueing a second list behind the first.** The questions of a house go stale quickly; the
  newest is the one that matters, and the older one's sender gets its `replaced` at once.
- **The image's `MessageBox`** for a yes/no question. It looks exactly like the receiver's own
  dialog, has no countdown of the plugin's own and cannot carry more than its own two answers.

## Consequences

- A sender has to handle every ending, `closed` and `replaced` included, and treat no answer at all
  as possible: a pick without a broker is lost on purpose.
- Anyone who can publish on the broker can put a question on the television. The header line says
  where it came from; it cannot say who sent it.
- The look on images other than the one the receiver menu was measured on - the styled screen's
  numbers, the fallback's countdown in its title, the power key over a list - is a tester's to
  confirm.
