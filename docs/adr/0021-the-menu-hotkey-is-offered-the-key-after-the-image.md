# ADR-0021: The menu hotkey is offered the key after the image, and takes it only when it opened the menu

**Status:** accepted 2026-10-15
**Date:** 2026-10-15
**Supersedes:** -

## Context

The receiver menu (ADR-0020) opens from the Extensions menu and from a hotkey, a long HELP by
default; the setting offers a long HELP, a long RED, F3, F4 or none. The discreet toast taught
that images give keys functions of their own, and that a plugin which binds a key the image
uses takes something from the household.

How a key reaches anything, read in `lib/actions/action.cpp` and `lib/python/keymapparser.py` of
OpenViX (`Developer`), OpenATV (`master`) and OpenPLi (`scarthgap`), all 2026-10-14, which agree:

- One dispatcher, `eActionMap`, holds bindings ordered by **priority, lowest first**, and within
  one priority by age. A Python binding names a context and is called with the context and an
  action name, for a key that a keymap mapped into that context with the flag of this event
  (make, break, repeat, long); the first binding that answers non-zero ends the press.
- A long press sets a mark on every binding, and the break that follows is then delivered to
  none of them: the short-press function of the same key does not run after a long press.
- A keymap can be read from a file (`keymap.xml` in a plugin's directory is loaded by the plugin
  loader of all three images) or bound at run time with `eActionMap.bindKey(domain, device, key,
  flags, context, action)`, and taken back with `unbindKeyDomain(domain)`.
- The info bar's own maps bind at about priority 0; its user-assignable button setup -
  `InfoBarButtonSetup` on OpenViX and OpenATV, `InfoBarHotkey` on OpenPLi - at -10, with entries
  for `displayHelp_long`, `red_long`, `f3` and `f4` whose functions are in `config.misc.ButtonSetup`
  or `config.misc.hotkey`: empty by default for HELP long, F3 and F4 on all three, and for RED long
  on OpenATV and OpenPLi; OpenViX gives RED long to HbbTV by default. An assigned function consumes
  the key; an empty one returns 0 and the key goes on. The info bar's "unhandled key" marker binds
  the wildcard at `sys.maxsize`, the very end.
- `HelpActions` maps a long HELP to `displayHelpLong` (OpenViX, OpenATV) or `toggleConfig`
  (OpenPLi), which only the help screen itself handles. In live TV a long HELP does nothing on
  OpenViX 6.6 - measured.

The spike on OpenViX 6.6 opened its screen on a long HELP injected through OpenWebif with a
`keymap.xml` and an action map attached to the running info bar, while a short HELP still opened
the image's help. A long press on the physical remote was not measured.

## Decision

1. **The key is bound at run time into a context of the plugin's own**, `MQTTBridgeMenuActions`,
   with `bindKey` under a domain of its own, so the setting decides the key, a change rebinds it
   without a restart, and `unbindKeyDomain` takes it back. No `keymap.xml` ships: a file would map
   every choice at once, and the image's keymaps are never touched.
2. **The context is bound at `sys.maxsize - 1000`**: after every binding of the image and of
   other plugins at ordinary priorities, and before the unhandled-key marker. Whatever the image
   or the user put on the key runs first and the plugin never hears of the press - including a
   function the user assigns in the image's button setup later.
3. **It answers yes only when it opened the menu**: when the info bar is the executing dialog
   (live TV) and no menu is open. Over any other screen, in a movie, or with the menu already
   open, it answers no and the key goes on as if the plugin were not there.
4. **HELP and RED are mapped on the long press only**, so a short press reaches no new binding.
   F3 and F4, keyboard keys, are mapped on their break, as the images' button setups map them.
5. **`info.menu.hotkey` says only what can open the menu.** The bound choice, or `null` with the
   reason: switched off, an image without `bindKey`, or a key the image's button setup gives a
   function - read from `config.misc.ButtonSetup` and `config.misc.hotkey` whenever `info` is
   built, never written. Any other binding of the image that consumes the key cannot be known in
   advance; the Extensions entry opens the menu whatever happens to the key, and appears in the
   image's own button setup as a function a household can put on any key.

## Consequences

- On OpenViX a long RED opens HbbTV by default, so `red_long` reports no hotkey there until the
  user clears that assignment; nothing is taken from HbbTV.
- OpenViX's and OpenATV's button setups swallow the next button-setup key once after a long-press
  function ran
  without the info bar losing focus (`longkeyPressed`); a long HELP right after such a press may
  need pressing again. That is the image's behaviour and is left alone.
- Whether a physical long press arrives with the long flag on a given remote and image is a
  hardware question (H11), as is any keymap translation an image applies to a remote's keys.
