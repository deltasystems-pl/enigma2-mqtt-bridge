# ADR-0022: The menu creator page runs a script; every other page stays script-free

**Status:** accepted 2026-10-16
**Date:** 2026-10-16
**Supersedes:** - . Amends [ADR-0009](0009-the-openwebif-page-trusts-openwebif.md) (where the
menu's settings are on the page) and extends [ADR-0020](0020-the-receiver-menu-is-drawn-on-the-receiver-and-decided-in-home-assistant.md)
(who edits the menu, and how it looks).

## Context

The receiver menu (ADR-0020) is Home Assistant's: the companion integration keeps it in its
options and publishes it, and its options flow is where it is built. That flow is a series of
forms in Home Assistant's settings, a long way from the television the menu is for, and a
household wants to build the menu where they look at it. The receiver has a web interface -
OpenWebif, with the plugin's own page in it (ADR-0009, ADR-0010) - and the plugin's page is the
natural place for an editor. Four things are true of it:

- **Picking an entity is a search.** A house has hundreds of entities. Home Assistant's own entity
  picker filters as one types; a page that is a form posted to the server for every keystroke is
  not an editor anybody would use. That needs a script in the browser.
- **Every page of this plugin is script-free** (`default-src 'none'`), and its confirmations are
  second pages rendered by the server for that reason. OpenWebif's panel loads the main page with
  jQuery, which runs any script in what it injects, so the main page and its fragment must stay
  without one.
- **The receiver must not hold a Home Assistant token.** A token on the receiver is a credential
  to the whole house on the box that publishes its web interface on the LAN with no login by
  default.
- **What the page shows comes from the broker.** Anyone who can publish on the broker can write
  what the editor reads, and the broker is not a security boundary in every set-up (ADR-0020).

## Decision

1. **A subpage, `<mount>/menu`, holds the menu's settings and the creator.** `menu_allowed`,
   `menu_hotkey` and `menu_transparency` are set there and only there on the page: the main page's
   settings form no longer carries them, and shows what they are set to with a link to the
   subpage, so there is one place to change each. Its settings form is the main form's rules on
   three fields - `Host`, same-origin `Origin`, the session's token, exactly the form's fields, the
   rendered values sealed - and works without script.
2. **The creator is the one script, a static file.** `creator.js` is shipped in the package and
   served as it is from `<mount>/menu/creator.js` with `text/javascript` and `nosniff`: nothing is
   built into it per request, so no request can put anything into it. No inline script, no event
   handler attribute, no `eval` or code built from text, no address outside this origin, no
   library. The page hands it what it needs - its addresses, the session's token, the icon names,
   the bounds and every sentence in the household's language - as `data-` attributes, escaped like
   every attribute on the page.
3. **The subpage alone has a policy that admits it.** `default-src 'none'; script-src 'self';
   connect-src 'self'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self';
   frame-ancestors 'self'; base-uri 'none'`, with the usual `X-Frame-Options: SAMEORIGIN`,
   `Referrer-Policy: same-origin`, `no-store` and `nosniff`. The main page keeps its policy byte
   for byte, and it and its fragment stay script-free; the tests hold both.
4. **Every string from the catalogue is untrusted text.** The script puts it on the page with
   `textContent` and form values only: never as markup, never into an attribute by string, never
   into an address. A static test refuses `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
   `document.write`, `eval`, `Function(` and a string `setTimeout`, and the script's own tests run
   it against a fake document that throws on each of them.
5. **No Home Assistant token on the receiver.** The integration publishes a **catalogue** - every
   entity a row can be bound to, with its name, area and allowed controls, and the menu as it is
   stored with a revision, a hash of it - on `enigma2mqtt/menu_catalogue/<node>`. The page edits a
   copy and posts it to `<mount>/menu/save`; the receiver publishes it once on
   `<base>/<node>/menu_edit` with the revision it started from; the integration validates it with
   its options flow's own rules, stores it or refuses it, and answers on
   `enigma2mqtt/menu_edit_result/<node>` (docs/TOPICS.md, "The menu creator"). The receiver checks
   only envelopes: the catalogue's (at most 1 MiB, its own bound) and the edit's (at most 64 KiB).
   Its exact bytes are what `catalogue.json` serves, so the page reads what Home Assistant wrote.
6. **A save waits for its answer, at most ten seconds, and changes no token.** The save request is
   held open until the answer with its id arrives, ten seconds pass, or the broker connection goes
   - then an edit the broker had not acknowledged is taken back out of the client's queue, as a
   `menu_action` is. The session's token is not replaced by a save, unlike by every form post: the
   page saves again without a reload, and what keeps a stale copy from being written back is the
   revision the edit carries, which Home Assistant compares. A token replaced by another tab still
   makes the save `out_of_date`.
7. **Only the main page tells a panel load from a navigation.** The creator's `fetch` calls send
   `Sec-Fetch-Dest: empty`, which is the main page's signal for OpenWebif's panel (ADR-0010); they
   go to resources of their own, which never answer with the fragment. `catalogue.json` and `save`
   also refuse a `Sec-Fetch-Site` other than `same-origin` where the browser sends one.
8. **Editing is always on**, while the menu is: there is no switch for the creator. The capability
   `menu_editor` says the receiver offers it - `menu` is claimed and OpenWebif has mounted the
   page.

## Consequences

- **Anyone who can reach the receiver's web interface, or publish `menu_edit` on the broker, can
  choose which entities the television controls**, and anyone on the broker can read the catalogue
  - every entity name and area Home Assistant has. Chosen knowingly by the maintainer, and said
  plainly in SECURITY.md. The integration still acts only on a row its stored menu holds, with the
  control it allows: an edit can put a lamp into the menu, it cannot make the menu do what Home
  Assistant would not let its own options flow do.
- **One page of this plugin runs a script.** A future page that wants one gets its own record;
  the main page's rule is unchanged, and the policy that admits a script is set on the subpage's
  responses alone.
- **The browsers that run it are the household's.** Plain ES2017 was chosen for them; an image's
  own browser, or a very old one, gets the settings form, which works without the script, and a
  line saying the creator needs JavaScript.
- **A save from Home Assistant's own options flow made from an older copy replaces a save made
  here**, and the other way round only by revision: Home Assistant is the owner. Documented, not
  prevented.
- **Reversing this** means building the menu in Home Assistant again, or a form round trip per
  keystroke; the topics and the integration's validation can stay as they are.
