# ADR-0025: A press combo takes its key from the image in live TV and the players

**Status:** accepted 2026-10-22
**Date:** 2026-10-22
**Amends:** [ADR-0021](0021-the-menu-hotkey-is-offered-the-key-after-the-image.md), for the
combo's key only

## Context

The receiver menu (ADR-0020) opens from the Extensions menu and from a hotkey: a long HELP by
default, or a long RED, F3 or F4 (ADR-0021). Many households drive the receiver with the
**television's own remote over HDMI-CEC** instead of the receiver's. Measured on OpenViX 6.6 with
the plugin's own `key` topic, a television remote over CEC delivers:

- its **Back** key as **`KEY_PREVIOUS`, a short press** (other televisions send `KEY_EXIT` for
  Back); its play/pause, stop, OK, down and left keys too;
- **no long press**: holding Back for about two seconds arrived as two short presses about 0.84 s
  apart;
- three quick presses about 0.8 to 0.9 s apart.

Such a remote has no HELP, no colour keys and no F keys, and cannot hold a key: no choice of the
hotkey can be pressed from it.

`KEY_PREVIOUS` is not free. OpenViX 6.6 maps it in live TV to `historyBack` in
`InfobarChannelSelection` (on make and repeat) - a zap back in the zap history - and in other
contexts to `prevSubservice`, `prevEvent`, `jumpPreviousFile` and, in lists and text fields, to
`deleteBackward` or `backspace`. Under ADR-0021 a binding sits after every binding of the image,
so it would never even see this key in live TV.

## Decision

1. **A second way to open the menu, the press combo**: a key pressed N times, each press within a
   settable maximum gap of the one before, opens the menu. It sits beside the hotkey, which keeps
   working. Three settings - the key (`menu_combo_key`: `off`, `previous`, `exit`, `playpause`,
   `stop`; **off by default**), the count (`menu_combo_presses`, 2 to 5, 3 by default) and the gap
   (`menu_combo_gap_ms`, 300 to 3000 ms in steps of 100, 1200 by default) - are on the television's
   setup screen, the OpenWebif page's Menu subpage and writable through `cmd/config`, like the
   hotkey. No arrow, OK or number key is offered: live TV needs them.
2. **While a combo key is set, that key only counts toward the menu where it counts.**
   `previous` counts in live TV and the players - exactly the screens the hotkey opens the menu
   over (`menu.hotkey_screen`): the info bar, the image's movie player and the players built on
   it, and the image's media player. `exit`, `stop` and `playpause` count **over the info bar
   alone**, live TV: in a player they are how the viewer leaves, stops and pauses it, so there
   they reach the player untouched - not consumed, not counted. The image's own function on the
   key where it counts - on OpenViX the history zap - is switched off, so no
   press of the combo key does anything else: not the first N-1, not a lone press. **In every other
   screen** - menus, lists, the EPG, this plugin's own menu and list - the key reaches the image as
   before. This departs from ADR-0021 ("take nothing from the image") for the combo's key only:
   a key that only opened the menu on the N-th press would zap twice on the way, and open the menu
   over another channel.
3. **Everything of the key is consumed where it counts**: the make, the break, the repeats and
   the long press. Consuming the makes alone would let a break or a long press reach an image
   binding that acts on it.
4. **How it is bound.** At run time, into a context of the plugin's own
   (`MQTTBridgeMenuComboActions`) under a keymap domain of its own, one action per flag, and taken
   back with `unbindKeyDomain`. The context is bound at `-sys.maxsize`: after the key publisher,
   which binds the wildcard at `-sys.maxsize - 1` and always answers no - so the `key` topic still
   reports every press of the combo key - and before every binding of the image (around 0) and its
   user-assignable button setup (-10). The combo is bound while the hotkey could be: the menu
   running and `menu_allowed` on. With the plugin idle or the menu switched off, the key is the
   image's again everywhere.
5. **The count.** A press is a make; repeats of a held key are not presses, and a CEC hold that
   arrives as several makes counts as several presses. The gap is measured make to make on a
   monotonic clock, its bound included; a later make starts a new count at one. On the N-th make
   the menu opens, by the hotkey's own path, and the count starts again. It also starts again when
   the key arrives over any other screen, over a different live-TV or player screen than the
   press before, when the menu opens any other way, and on every rebind. No timer runs: a count
   that is given up did nothing.
6. **The image's button setup is overridden, not yielded to.** A function the household put on
   the combo key in the image's user-assignable button setup (`config.misc.ButtonSetup`,
   `config.misc.hotkey`) does not run where the key counts while the combo is set - the
   difference from the hotkey, which yields to it.
7. **`info.menu.combo.replaces` says what the key no longer reaches** where it counts:
   `"<context>.<action>"` for every mapping of the key in the image's keymap (`config.usage.keymap`,
   else `/usr/share/enigma2/keymap.xml`) in a context the info bar binds - and, for `previous`, a
   player - recognised by name: one that carries `Infobar` in any case, or, for `previous`, begins
   with `MoviePlayer` or `MediaPlayer` -
   read once when the key is bound; and `"ButtonSetup.<name>"` for each assignment in the button
   setup, read whenever `info` is built. `[]` when it found none, `null` when the keymap could not
   be read. Best effort: it never stops the key from being bound.

## Consequences

- On OpenViX with the combo on `previous`, Back no longer zaps back in the history in live TV. It
  is reported in `replaces` as `InfobarChannelSelection.historyBack`, beside the other info-bar and
  player functions of the key; the history is still reachable from the image's own menus and from
  Home Assistant (`cmd/zap_history`).
- `replaces` is drawn from names, not from the screens that run: it can list a context the info bar
  binds only in some states (the subservice quick zap) and misses a mapping that a plugin's own
  keymap file adds. It describes what is taken; it is never used to decide anything.
- A count abandoned over another screen and resumed in live TV within the gap is caught only when
  a press of the combo key lands on the other screen or on another live-TV screen; leaving and
  returning without one is not seen.
- With the combo set to `exit`, EXIT no longer hides the info bar in live TV; with `stop`, STOP no
  longer stops timeshift there; with `playpause`, PLAY/PAUSE no longer starts or pauses
  timeshift there. In the players all three keep working, which is why they count in live TV
  alone. The setting is off by default and the household chooses which key to give up.
- A press sent with `cmd/key` is a press like the remote's: it counts toward the combo and is
  taken where the remote's would be. A remote card in Home Assistant that sends Back three times
  opens the menu as the television's remote does.
- When the menu cannot open - no menu screen can be built, a menu is still listed as open, both
  screens refused - the key is still taken, by the rule above: no press of it does the image's
  work while the combo is set. The log says why, once for each cause, so a dead key in live TV
  has a line that explains it.

## Amendment 2026-10-22 - PREVIOUS on the plugin's own screens

On OpenViX `KEY_PREVIOUS` is no cancel: `OkCancelActions` maps only EXIT, and `DirectionActions`
maps `KEY_PREVIOUS` to `shiftUp`. A television's remote whose Back arrives as `KEY_PREVIOUS` could
open the receiver menu with the combo and then not leave it.

1. **On the screens the plugin opens - the receiver menu and the list of choices, the plugin's own
   styled screens and the image's `ChoiceBox` in their place - `KEY_PREVIOUS` does what EXIT does
   there**: back one level, or close. Whatever `menu_combo_key` is, `off` included. The image's own
   screens are unchanged, among them the keyboard a menu row opens.
2. **Bound per screen, from its opening to its close** (`backkey.py`): the key is mapped at run
   time, on its make only, into a context of the plugin's own while any such screen is open, and
   each screen binds it at `-sys.maxsize + 1` - after the combo, before the image - answering only
   while it is the executing dialog.
3. **The press that opened a screen is never its EXIT.** Its binding is made while that press is
   handed out, and enigma2's dispatcher hands a press's repeats and break only to a binding that
   saw its make; only the make is mapped anyway. The menu the third Back of a combo opened stays
   open, and the next Back leaves it.
