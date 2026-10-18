# ADR-0020: The receiver menu is drawn on the receiver and decided in Home Assistant

**Status:** accepted 2026-10-15, amended 2026-10-16 (decisions 2, 3 and 5), extended by [ADR-0022](0022-the-menu-creator-page-runs-a-script-every-other-page-stays-script-free.md) (the menu creator) and amended 2026-10-16 (see [the note](#note-2026-10-16---a-see-through-background): the background)
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
   worse than an old one. A payload nested too deeply for the JSON parser, or a slider number too
   large for a float, is invalid the same way and never an exception. Text loses backslashes and
   line breaks - the toast's rule - and the invisible format characters (Unicode Cf: bidirectional
   overrides, zero-width characters), and is cut at the contract's lengths again. An icon name
   outside the contract's list is the neutral icon. A row of a type this plugin does not know is
   shown with its label and state and OK on it sends nothing, because the enumeration is open;
   the log names the type once, not on every publish or redraw (the maintainer's decision).
3. **What is not applied is discarded.** A slider moves a local value until OK (EXIT discards it),
   a selector lists its options and sends only the one chosen, an input field is the image's
   keyboard. Only OK sends: the keys 1-9 move the selection to their row and do nothing else
   (the maintainer's decision), so a digit pressed by mistake switches nothing. A slider moved and
   not applied is discarded when the selection leaves its row. OK on the value the row already
   has sends nothing - the option already set, a slider back at its value, the same text - and the
   list or keyboard just closes, with no pending mark and no notice; while a pending mark
   holds another value the viewer asked for, the row's value picked again is a change back and is
   sent. An option is identified by its value, not its place, so a menu that reorders options
   while they are listed cannot turn an OK into another option.
4. **The pending mark is compared on `value`, never on `rev`.** After OK on a toggle, a slider or
   a selector the row shows a mark until the menu carries a different value for it, or for five
   seconds, after which it shows the real value and the footer says Home Assistant changed
   nothing. `rev` counts publishes, and a publish for another row would end a mark that was
   compared on it.
5. **Greyed, not hidden.** Without a broker connection, or with Home Assistant's status topic
   saying `offline`, the last menu stays on screen greyed, with a notice, and nothing can be acted
   on - the household sees why the lamp will not answer. A status never heard does not grey: Home
   Assistant does not retain its birth message, and a receiver that connects after it would
   otherwise grey a healthy menu. For the same reason the status is forgotten - back to never
   heard - whenever the broker connection goes or comes back: an `online` published while the
   receiver was away would never undo the `offline` from before, and the menu would stay greyed
   for good. A menu that arrives live (retain flag 0, so the integration published it just now)
   is proof that Home Assistant runs and sets the status `online`; a retained replay on
   subscribing can be days old and proves nothing.
6. **An action is never queued.** The receiver's MQTT client keeps every unacknowledged QoS 1
   publish for the next connect; a `menu_action` is taken back out of that queue when the
   socket was already gone as it was published, when the main loop learns the connection went,
   and - the one that does not depend on the main loop being quick - by the client itself on
   paho's network thread as the broker accepts the next connection, in `on_connect`, before paho
   sends its queue again. So an OK from minutes ago never switches a lamp. The integration also
   refuses one older than 120 s.
7. **One screen module, one fallback.** The styled screen (`styledlist.py`) computes its
   geometry from the desktop height, as the toast does, and has a second way for every optional
   drawing call. It is built on trial off the dialog stack before it is opened, because a screen
   that raises inside `session.open` leaves the session with nothing executing; should the real
   open raise all the same, the dialog it took the execution from is given it back first. Anything
   that fails is the reason the image's own `ChoiceBox` is used as it is - same rows and ids, but
   no number keys, because the image's list acts on a digit and cannot be made only to move
   (decision 3) - and `info.menu.screen` says which one the receiver uses. The module knows
   nothing about menus, so the next list (the planned channel list) reuses it.
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

## Note, 2026-10-16 - a see-through background

*Decision 7 stands; this adds one setting to the styled screen.* The setting `menu_transparency` -
0 to 80 %, in steps of 10, 20 % by default - is the alpha of the styled screen's background and of
its list's panel: `percent * 255 / 100` rounded half up, so 30 % is `0x4D` and 80 % is `0xCC` in
enigma2's `#AARRGGBB`.
The bar on the selected row, the text and the icons stay opaque, so what is selected and what is
written can always be read. The skin is built for each opening, so a change applies the next time
the menu opens and a menu on screen keeps its look; at 0 % the screen is as before, but for a
background that had an alpha of `0x08` and is now opaque. The image's own list, the fallback, is
drawn as the image draws it. Like `menu_hotkey`, the setting only tunes a menu the receiver already
offers, so it is writable by `cmd/config` and set on the receiver's settings screen - in its own
"Home Assistant menu" section, last, with the other two - and on the OpenWebif page's Menu subpage. Whether an image's
frame buffer blends the alpha over live television is the hardware's to show; a skin can restyle
the screen by its name, `MQTTBridgeList` (TROUBLESHOOTING.md).
