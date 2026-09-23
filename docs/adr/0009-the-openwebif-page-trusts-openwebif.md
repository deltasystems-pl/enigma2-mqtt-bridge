# ADR-0009: The OpenWebif page trusts OpenWebif, and exposes every setting and command

**Status:** accepted 2026-09-23
**Date:** 2026-09-23
**Supersedes:** [ADR-0002](0002-scope-after-m0.md) §5, in part — the clause that the page „is
authenticated by OpenWebif, and it fails closed when OpenWebif authentication is off", and the
consequence that „the status page ties a plugin feature to OpenWebif's authentication being
enabled". The rest of §5 stands: a page registered into OpenWebif, no listener of its own, a
Content-Security-Policy, a one-shot token on every write, a bounded log viewer.
**Amends by reference:** [ADR-0003](0003-control-feedback-and-household-features.md),
[ADR-0004](0004-remote-uninstall.md) §1, [ADR-0005](0005-softcam-restart.md),
[ADR-0007](0007-cec-standby-workaround.md) and [ADR-0008](0008-discreet-toast.md). Wherever those
records call a setting „box-only", „set on the box only", „granted on the setup screen" or not
writable „from the OpenWebif status page", read: **never writable over MQTT; set on the receiver —
the setup screen, the provisioning file, or the OpenWebif page.** None of their decisions is
reversed; the boundary they describe was always the broker, and this record says so.

## Context

**The page nobody could open.** ADR-0002 §5 put a status page into OpenWebif at `/mqttbridge` and
made it refuse every request unless OpenWebif authentication was switched on *and* the request came
from a logged-in session. OpenWebif's authentication is **off by default** for HTTP, and on a
household receiver it usually has to stay off, because the other things that talk to OpenWebif —
Home Assistant's own enigma2 integration among them — poll it without credentials. So on a receiver
at OpenWebif's defaults the page answered 403 to everybody, including its owner, and a page built as
the recovery tool for a broker misconfiguration was a page nobody had seen. It also showed four
settings, so a wrong broker address still needed the television or SSH.

**OpenWebif already guards the page, and it decides first.** Read in the bytecode of OpenWebif
2.2 on OpenViX 6.6, and confirmed by effect on that receiver: OpenWebif builds one resource tree,
mounts every external child — this page — on it, and wraps that same tree in its authentication
resource twice, once for the HTTP site and once for the HTTPS site. The decision is taken on the
**first path segment**, before Twisted ever asks the tree for `mqttbridge`. A request OpenWebif
refuses never constructs anything of this plugin's: a GET of `/mqttbridge` claiming a public
address came back as OpenWebif's own `403.6 IP address rejected` page, with none of the plugin's
headers.

| OpenWebif setting | Who reaches the page |
|---|---|
| Authentication off (the HTTP default) | a client in one of the receiver's own networks; any private address when OpenWebif's VPN access is on; a process on the receiver. Everybody else: OpenWebif's 403 |
| Authentication on (the HTTPS default) | a session logged in with a system user's password, or a process on the receiver. Everybody else: 401 |

🔴 With authentication off, the address OpenWebif judges is the client's own `X-Forwarded-For`
header, whoever sends it, so the „local network" rule is advisory against anyone who can open the
receiver's port 80.

**The box-only rule was never a boundary against OpenWebif.** Read in the same bytecode, and not
called: OpenWebif's `saveconfig` sets **any** `config.*` key by its dotted name — this plugin's
permissions and kill-switches, and OpenWebif's own authentication switch — on a POST that carries no
token and is checked against no origin; its settings listing returns every saved setting verbatim,
**the broker password among them**; its package endpoint accepts `remove`; and its power endpoint
switches the receiver off on a GET. So „a permission can only be granted by somebody in front of the
television" has not been true on any receiver whose web interface admits the client. What *is* true
is the half that mattered: **the broker cannot grant a permission**, because `cmd/config` has an
allowlist and no permission is on it.

**Two defences that were implicit in the login, and are not without it.** Once the page answers
without a login, *DNS rebinding* walks through an origin check: a script served from a name the
attacker controls, later pointed at the receiver, is same-origin with itself, reads the page's token
and posts with `Origin` and `Host` agreeing. And enigma2 writes `/etc/enigma2/settings` as
`key=value` lines with **no escaping**, reading it back line by line, so a line break inside a text
value plants a second setting of the writer's choosing at the next start. Neither mattered while the
page accepted no text and answered nobody.

**Sessions exist without a login.** OpenWebif opens a Twisted session for every request before it
decides anything, so a one-shot token stored in the session works for an anonymous visitor exactly
as it did for a logged one.

## Decision

**1. The plugin enforces no authentication.** The page, its icon and every action answer whatever
OpenWebif lets through, on HTTP and HTTPS alike, and no code path reads an OpenWebif setting. No
replacement check is written: OpenWebif's decision runs first, and a second copy of it would drift
from the first. The page trusts the web interface that mounted it.

**2. What protects a write is four checks, all on every POST, all failing closed:**

- **A `Host` allowlist, on every request, GET included** — the host must be an IP literal (IPv6 in
  brackets), `localhost`, or the receiver's own hostname, bare or with `.local`. Anything else is
  answered `421` with the addresses that work, before the session is read. A browser cannot be made
  to send an IP literal as `Host` for a name somebody else controls, which is what defeats DNS
  rebinding.
- **Same origin**: `Origin` present, its scheme matching the request's, its host equal to `Host`.
- **The session's token**, taken from the POST body only (never the query string) and replaced
  after every write that took effect; a refused request keeps it. A confirmation carries its own
  token, good for one attempt.
- **The exact field set of the form**, no extra, none missing, none repeated.

Headers stay as they were — `no-store`, `nosniff`, `X-Frame-Options: SAMEORIGIN`, `no-referrer`,
the CSP — with `frame-ancestors 'self'` added. The page stays **script-free**, so every confirmation
is a second page rendered by the server, carrying a fresh one-shot token bound to exactly that
action and payload.

**3. Every setting is on the page**, grouped in the setup screen's order, the permissions and
kill-switches in a group of their own. The two passwords are **write-only**: rendered empty, left
unchanged when submitted empty, never shown in any form; clearing one is the setup screen's job. The
OSCam identity salt is not on the page at all. One validator serves every setting, built from what
`config.py` already declares plus a limits table that a test holds to each element's own limits;
**every text value refuses control characters** and is length-capped, and the topic settings refuse
MQTT's wildcards. A change to the node id, the base topic or the discovery prefix is confirmed
first. The save reuses what exists and picks by what changed: a change inside `cmd/config`'s
allowlist goes through `apply_remote_settings`, as the page always did; anything else takes the
setup screen's path — save, write the settings file once, reload — so a permission changed on the
page reaches `info.settings` and discovery exactly as one changed on the television does. 🔴 A
form submits every field, so the page carries the values it rendered, sealed with the session
token, and saves only the fields whose submitted value differs from them: a form left open while
a setting changed elsewhere — over `cmd/config`, or a permission revoked at the television — never
writes its stale copy back.

**4. Every command is a page action, through the same handler.** The page builds the payload a
broker client would have sent and calls the dispatcher with it and an **origin**. The dispatcher
runs the same handler, and publishes or clears `last_error` exactly as for MQTT. The origin answers
one question and nothing else — is the box-side permission granted — and the answer is *yes* for the
page, because anybody OpenWebif admits can grant it anyway, and the setting for everything else. The
origin is **passed down the call and never stored**, so a later command over MQTT cannot inherit the
page's answer, and the softcam's automatic restart, which is nobody's request, stays behind
`softcam_restart_allowed` whatever the page did. The household-safety guards — a recording running,
a timer due, the softcam's rate limits, the key limiter, the bouquet allowlist — live in the handlers
and publishers and apply to the page unchanged. While the bridge is idle the actions are shown
disabled, with the reason.

**5. The rule, restated.** A setting that enables a command is **never writable over MQTT**; it is
set on the receiver — the setup screen, the provisioning file, or the OpenWebif page, which is
exactly as open as the receiver's web interface. `cmd/config`'s allowlist and `info.settings` are
unchanged.

## Consequences

- **The page is exactly as open as OpenWebif**, and `docs/SETUP.md` says so in those words. Its
  checks protect the page, not the receiver: while OpenWebif authentication is off, any web page a
  household member opens can already switch the receiver off or grant a permission through OpenWebif
  itself. The page must not be the weakest link; it cannot be the strongest. The only lever is
  OpenWebif's authentication, or a network that keeps untrusted hosts off port 80.
- **A user who reaches the receiver by a DNS name of their own, or through a reverse proxy, is
  refused.** Accepted as the price of the `Host` check, documented, and deliberately not made a
  setting.
- **A new command needs a page action, and a new setting needs a group on the page.** Tests fail
  when either is missing, so neither can be forgotten quietly.
- **On an image where the original WebInterface is installed**, OpenWebif does not mount its
  own hook loader, and the page would be mounted under that interface's authentication instead. Not
  measured; documented, not handled.
- **The answer to deep standby, reboot or an interface restart is rendered before the command
  runs**, and whether it reaches the browser before the main loop quits is not measured. Nothing
  depends on it: the next request failing is the expected outcome.
- **The setup screen and the page edit the same settings.** If both are open, the television's
  *Cancel* restores what the page saved and its *Save* writes what it shows. Not measured.
- **The authentication-on path rests on the bytecode reading** until it has been exercised on a
  receiver: toggling OpenWebif's authentication is a write to OpenWebif, and is left to the
  on-hardware acceptance.
- **Reversing this** would mean a login of the plugin's own — which ADR-0002 rightly refused to
  invent — or a page that again answers nobody on a default receiver. Neither makes the receiver
  safer while OpenWebif hands out `saveconfig`.
