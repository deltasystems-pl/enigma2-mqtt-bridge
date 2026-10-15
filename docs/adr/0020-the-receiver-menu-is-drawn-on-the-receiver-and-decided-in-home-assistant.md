# ADR-0020: The receiver menu is drawn on the receiver and decided in Home Assistant

**Status:** accepted 2026-10-15
**Date:** 2026-10-15
**Supersedes:** -

## Context

A household wants to switch a lamp, close a blind or start a scene without putting the remote
down. Home Assistant knows the house; the receiver knows the television and the remote. Something
on the television has to list what Home Assistant offers and let the remote act on it, and the
two ends are written, released and installed separately: this plugin on the receiver, the
companion integration in Home Assistant.

Three things are true of the path between them:

- **The broker is not a security boundary in every set-up.** The Mosquitto add-on most Home
  Assistant users run accepts an ACL file and does not consult it, so any client with a login can
  publish on any topic. Whatever the receiver can ask for, anybody on the broker can ask for.
- **Images differ in what a plugin's screen can draw.** On OpenViX 6.6 a list built in Python
  with icons, states and colours works - the spike measured it - and a drawing call that imports
  can still raise (`MultiContentEntryRectangle`). OpenATV and OpenPLi are read from source, not
  measured.
- **A remote has a few keys, and the image has given most of them a meaning.** The key that opens
  the menu is ADR-0021's subject.

## Decision

1. **The receiver never learns what a row does.** The integration publishes the whole menu,
   retained, on `enigma2mqtt/menu/<node>`: groups (one level) and rows, each a `button`, `toggle`,
   `indicator`, `selector`, `slider` or `input` with the text to show and its current `value`. The
   receiver answers an OK with one `menu_action` - the row's id, `press`, `toggle` or `set`, and
   the value - and Home Assistant performs the action and publishes the menu again. No entity id,
   domain or service crosses to the receiver, so a `menu_action` from anybody can do exactly what
   the menu offers and nothing else; the integration acts only on a row its own configuration
   holds, with the type it has now and a value it validates.
2. **The payload is checked whole, on the receiver too.** Bounds (24 entries, 8 groups, 24 rows,
   24 options, 200 slider steps, 64 KiB), types, ids and the slider's arithmetic. An invalid
   payload is ignored with one log line and the menu before it stays: a menu that half-applies is
   worse than an old one. Text loses backslashes and line breaks - the toast's rule - and is cut
   at the contract's lengths again. An icon name outside the contract's list is the neutral icon;
   an unknown row type is shown and cannot be acted on, because the enumeration is open.
3. **What is not applied is discarded.** A slider moves a local value until OK (EXIT discards it),
   a selector lists its options and sends only the one chosen, an input field is the image's
   keyboard. Only OK sends: the keys 1-9 move the selection to their row and do nothing else
   (maintainer's decision D66), so a digit pressed by mistake switches nothing.
4. **The pending mark is compared on `value`, never on `rev`.** After OK on a toggle, a slider or
   a selector the row shows a mark until the menu carries a different value for it, or for five
   seconds, after which it shows the real value and the footer says Home Assistant changed
   nothing. `rev` counts publishes, and a publish for another row would end a mark that was
   compared on it.
5. **Greyed, not hidden.** Without a broker connection, or with Home Assistant's status topic
   saying `offline`, the last menu stays on screen greyed, with a notice, and nothing can be acted
   on - the household sees why the lamp will not answer. A status never heard does not grey: Home
   Assistant does not retain its birth message, and a receiver that connects after it would
   otherwise grey a healthy menu.
6. **An action is never queued.** The receiver's MQTT client keeps every unacknowledged QoS 1
   publish for the next connect; a `menu_action` is taken back out of that queue when the
   connection goes, and when the socket was already gone as it was published, so an OK from
   minutes ago never switches a lamp. The integration also refuses one older than 120 s.
7. **One screen module, one fallback.** The styled screen (`styledlist.py`) computes its
   geometry from the desktop height, as the toast does, and has a second way for every optional
   drawing call. It is built on trial off the dialog stack before it is opened, because a screen
   that raises inside `session.open` leaves the session with nothing executing. Anything that
   fails is the reason the image's own `ChoiceBox` is used as it is - same rows and ids, but no
   number keys, because the image's list acts on a digit and cannot be made only to move (D66) - and `info.menu.screen` says which one the receiver uses. The module knows nothing about
   menus, so the next list (the planned channel list) reuses it.
8. **On by default, set on the receiver.** `menu_allowed` is a box-only permission, on by default
   because the menu does nothing the user did not put into it in Home Assistant; off, there is no
   entry, no hotkey, no subscription and no `menu` capability. `menu_hotkey` may be written by
   `cmd/config`: it only chooses a key.

## Consequences

- The menu is only as good as the integration that publishes it, and its behaviour - which
  domain is a toggle, what green means, what a slider's step is - is decided there and can change
  without a plugin release.
- Two topics outside the node's tree are read and never written: the menu and Home Assistant's
  status. A broker client can publish a menu too; what it can make the receiver send is a
  `menu_action` for a row id, which the integration refuses unless its own configuration has it.
- The styled screen's numbers and the fallback's look are measured on one image and one skin;
  the other images and skins are a tester's to confirm (H10/H11).
- Taking a message out of paho's queue uses paho's own data structures, because paho offers no
  public call for it. The vendored paho is pinned and the code leaves a paho that looks different
  alone; a paho update has to keep the test that runs it against the vendored client green.
