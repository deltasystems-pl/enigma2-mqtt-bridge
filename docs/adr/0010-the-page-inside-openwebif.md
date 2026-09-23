# ADR-0010: OpenWebif's panel load gets a fragment framing the page, and the page shows the last screenshot

**Status:** accepted 2026-09-23
**Date:** 2026-09-23
**Supersedes:** — (it amends [ADR-0009](0009-the-openwebif-page-trusts-openwebif.md) on how
OpenWebif opens the page; nothing ADR-0009 decides about who reaches the page or what guards its
writes changes)

## Context

**How OpenWebif opens an external page.** OpenWebif's `prepareMainTemplate` turns an external child
registered with a GUI and the target `"_self"` into a menu entry that calls `load_maincontent`,
which on the receiver is `$("#content_container").load(url)`: a jQuery request whose answer is
injected into OpenWebif's own document, inline scripts executed in OpenWebif's origin. Any other
target becomes a plain `target='_blank'` link. Earlier in 0.3.0 the page was registered with
`"_blank"` for exactly that reason: a whole page injected into OpenWebif leaks its CSS into it, and
its forms navigate the whole window. The cost was that the one menu entry that opened in a new tab
was this one, and the household wanted it inside OpenWebif like every other entry.

**The panel load can be recognised.** Measured in Chromium against the page under Twisted and
upstream's jQuery: the panel load arrives with `X-Requested-With: XMLHttpRequest` and
`Sec-Fetch-Dest: empty`; every navigation — a tab, a bookmark, a frame — carries neither
(`Sec-Fetch-Dest` is `document` or `iframe`). And a frame of the page inside an OpenWebif page of
the same origin renders under the page's existing headers (`X-Frame-Options: SAMEORIGIN`,
`frame-ancestors 'self'`), and its forms post with the frame's own origin and the session cookie,
so ADR-0009's checks accept them unchanged and each answer replaces the frame, never OpenWebif.

**The screenshot did not survive a save.** The last picture lived only on the screenshot
publisher, which every settings save replaces, while the retained copy on the broker outlived it.
There was also no record of when a picture was taken, only of when a capture started.

## Decision

**1. The hook registers `"_self"` again, and the page answers a panel load with a fragment.** A
`GET` of the page that carries `X-Requested-With: XMLHttpRequest`, or `Sec-Fetch-Dest: empty` for a
theme that uses `fetch()`, is answered — after the `Host` allowlist and before anything else — with
one `<div>` holding an `<iframe>` of the page and a link to open it in a new tab. The fragment has
**no script, no style element and no class OpenWebif styles**; its presentation is the frame's
inline `style`, a fixed height that scrolls, because sizing the frame to its content would need
script. It carries no token and no data. Every navigation gets the full page as before, and every
answer of the page's `GET` says `Vary: X-Requested-With, Sec-Fetch-Dest`. `POST` is never answered
with a fragment. What gets injected into OpenWebif is the fragment, not the page, which is why
ADR-0009's earlier reason for `"_blank"` no longer applies.

**2. The bridge holds the last picture it put on `screen`, with the time the capture finished.**
It is recorded where it is published, is the same bytes object, is cleared when `screen` is
retracted or the retained topics are reset, and is set again by the snapshot. It survives a
publisher replacement, a remote save and a reload, as the retained topic does, and is lost when the
process restarts.

**3. `<mount>/screen.jpg` serves it**, behind the same `Host` allowlist, with the page's headers and
a closed CSP, and answers 404 when nothing is recorded, when screenshots are off or when the bridge
is not running. It never runs `grab`. Whoever reaches it already reaches OpenWebif's own `/grab`,
which takes a fresh picture on every request, so it adds no exposure.

**4. The page shows the picture beside *Take a screenshot*, and waits without script.** While a
capture started no more than 20 seconds ago is still running, the page carries a meta refresh to
itself every two seconds. A refresh is a `GET`, which never mutates, so it cannot repeat the
command, and the bound stops a hung `grab` from refreshing the page for ever. CSP has no directive
for a meta refresh, so `default-src 'none'` is unchanged.

## Consequences

- The page stays script-free, and OpenWebif's document never receives anything but the fragment.
- A theme that loaded panels with neither header would get the whole page injected. Considered and
  kept as the fallback: a second, GUI-only external child registered only for the panel.
- The frame's height is a fixed allowance for OpenWebif's header, measured on one layout; it is a
  number to tune, not a guarantee.
- Under a DNS name or a reverse proxy the frame shows the page's `421`, as ADR-0009 accepted.
- The picture on the page is what this process last sent, not what the broker holds; after a restart
  the page has none until the next capture.
