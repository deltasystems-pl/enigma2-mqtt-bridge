# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.4.x | yes - the current development line |
| < 0.4.0 | no |

Security fixes are released on the newest minor line. There are no long-term support branches
before v1.0.

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report it privately through GitHub: *Security -> Advisories -> Report a vulnerability* on
[this repository](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/security/advisories/new).
That opens a private thread with the maintainer.

What to expect:

- **Acknowledgement within 7 days** of the report.
- A fix, or a documented mitigation, **before the advisory is published**.
- Credit in the advisory and the changelog unless you would rather stay anonymous.

Please include the image and version of the receiver, the plugin version, what you observed and
how to reproduce it. Logs are welcome - strip the broker password first; the plugin never writes
it, but a manually edited provisioning file might be in your paste.

## Threat model

The plugin runs on a set-top box and holds a credential for your broker. Three facts shape
everything else:

1. **The box is weakly protected.** Enigma2 images commonly ship a well-known root password and
   an open telnet or SSH port. Anyone with LAN access to such a box can read the plugin's
   configuration, and with it the broker credential. The first install step in the README and in
   docs/INSTALL.md therefore tells users to change the root password; the plugin cannot do it for
   them.
2. **The LAN is the trust boundary.** The plugin connects to the broker the user configures and,
   since 0.4.0, for updates of itself only, to three more places:
   - its release origin, `https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/`, on GitHub
     Pages, over HTTPS with a verified certificate and no redirects. How often:
     - **a check** asks for two small files, the index and its signature, and for both once more
       when the pair does not verify. With the receiver-only setting `update_check` on (off by
       default) there is one when the last check of any kind is a day old - the plugin looks
       once an hour whether it is, and not before the receiver's clock has been set - and one on
       `cmd/update_check` over MQTT (refused unless `update_check` is on). A person pressing
       *check* on the television or the OpenWebif page gets one whatever the setting says. A
       check asked for within ten minutes after the last one, by the receiver's clock and the
       time of the last check it keeps in a file, is answered from that check's result and
       connects to nothing. Three things let one through sooner: a clock that was set back to
       before the last check; the first moments after a start, until that file has been read;
       and a file that is missing or cannot be read;
     - **an install asked for at the television or on the page** first asks for the signature
       file alone, once, to learn whether the receiver can fetch the release itself - unless a
       check or a probe of the last ten minutes has already said, or an install ended in the
       last twenty minutes because the origin gave no answer, which is taken as the origin's
       word for that long;
     - **an install that does not go through Home Assistant's relay** - asked for there, or over
       MQTT with the receiver-only permission `update_allowed` - asks for the index and its
       signature again, and for the package;
   - for that last kind of install only, GitHub's API, once:
     `https://api.github.com/repos/deltasystems-pl/enigma2-mqtt-bridge/releases/tags/v<version>`,
     with the same certificate check and no redirects, to compare the digest GitHub publishes for
     the release's package with the signed one. Nothing of the receiver's is sent to either
     address but the request itself, with the user agent `enigma2-mqtt-bridge`;
   - for an install Home Assistant starts, or one on a receiver without internet, the Home
     Assistant relay address the companion integration gives it over MQTT: a fixed path,
     `/api/enigma2_mqtt/relay/<token>`, fetched over plain HTTP or over HTTPS without checking
     the certificate. That is enough because what it serves is checked against the signed index
     (below) before anything is installed.

   One command makes the image connect, not the plugin: `cmd/epg_import` (since 0.3.0, refused
   over MQTT unless the receiver-only permission `epg_import_allowed` is on; off by default)
   starts the image's own EPG importer, which downloads from the sources selected in it.

   There is no telemetry and no cloud service. TLS to the broker is supported; client
   certificates are not in v1.

   **What an update trusts** ([ADR-0015](docs/adr/0015-signed-self-update.md),
   [docs/RELEASE-INDEX.md](docs/RELEASE-INDEX.md)). The plugin installs only a release named in the
   signed release index, and checks the package's size and SHA-256 against that entry before the
   package manager sees it. The index is verified with Ed25519 public keys built into the plugin: a
   main key, which only this repository's CI uses, in a signing job the maintainer approves by
   hand, and a spare key of higher rank, kept offline. A receiver refuses an index whose serial is
   not above the last one it accepted from that key, and one from a key ranked below a key it has
   accepted. The origin, and Home Assistant when it relays the index and the package (above), are
   couriers, not authorities. Installing over MQTT needs the receiver-only permission
   `update_allowed` (off by default) and never downgrades; a downgrade needs a confirmation on the
   television or the OpenWebif page.

   **What that does not cover.** Whoever controls the origin, the relay or the broker can delay or
   withhold an index or an install, but cannot get anything installed that the index does not
   name. The index has no expiry, so a withdrawn release is refused only once a newer index has
   reached the receiver. A compromise of the repository's account is detectable and recoverable
   with the spare key, not prevented. The image's own package manager reading the opkg feed
   (`opkg upgrade`, the image's software update screens) and any install by hand check no
   signature: they trust the feed's HTTPS, as before 0.4.0. What happens when a key is lost or
   leaked is in [docs/RELEASE-INDEX.md](docs/RELEASE-INDEX.md#when-the-main-key-is-lost-or-leaked).
3. **The broker credential must be scoped.** A box compromise must not become a Home Assistant
   compromise. The documented setup is a **dedicated broker login per box** with an ACL that
   limits it to that node's own topics:

   ```
   user enigma2box
   topic readwrite enigma2/<node_id>/#
   topic write enigma2mqtt/discovery/<node_id>/#
   topic write homeassistant/device/<node_id>/#
   topic write homeassistant/device_automation/<node_id>/#
   topic read enigma2mqtt/release_index
   topic read enigma2mqtt/integration/<node_id>
   topic read enigma2mqtt/menu/<node_id>
   topic read enigma2mqtt/menu_catalogue/<node_id>
   topic read enigma2mqtt/menu_edit_result/<node_id>
   # the default discovery prefix, homeassistant; with another, use yours
   topic read homeassistant/status
   ```

   The last four are the receiver menu's, read while `menu_allowed` is on: the menu, the menu
   creator's catalogue and its answers, and Home Assistant's own status under the discovery prefix
   (`homeassistant` by default; use yours). The node's own `menu_action` and `menu_edit` are in
   `enigma2/<node_id>/#` already.

   Verify the ACL by subscribing elsewhere and trying to publish there as that user - Mosquitto
   drops an ACL-denied publish silently and the publisher sees success either way. The Mosquitto
   add-on in Home Assistant accepts an ACL file and never consults it - reported against 7.1.0
   ([home-assistant/addons#4721](https://github.com/home-assistant/addons/issues/4721)), still
   the case in 7.1.1 - so on
   that broker the dedicated login is the only separation; see
   [docs/SETUP.md](docs/SETUP.md#broker-access).

Consequences the design accepts: the password is stored in enigma2's settings file in clear, as
every enigma2 plugin's credentials are; the provisioning file holds it too, which is why it is
deleted immediately after import; and the `key`, `epg` and `screen` topics are privacy-sensitive
by nature, which [docs/SETUP.md](docs/SETUP.md#privacy) covers and the settings can switch off.

## The OpenWebif page and the receiver menu

The plugin's OpenWebif page is exactly as open as OpenWebif
([ADR-0009](docs/adr/0009-the-openwebif-page-trusts-openwebif.md)): it enforces no login of its
own, and with OpenWebif's authentication off - its default - anyone who can reach the receiver's
web interface can use it. Its checks keep **other web sites** out (the `Host` allowlist,
same-origin requests, a session token), not people on the LAN.

The receiver menu ([ADR-0020](docs/adr/0020-the-receiver-menu-is-drawn-on-the-receiver-and-decided-in-home-assistant.md))
is on by default. **Editing it from the receiver**, with the menu creator
([ADR-0022](docs/adr/0022-the-menu-creator-page-runs-a-script-every-other-page-stays-script-free.md)),
**is off by default**, and switched on in Home Assistant, in the companion integration's receiver
menu options ("Allow editing from the receiver"). While it is off, Home Assistant publishes no
catalogue of its entities and refuses every edit; the page says editing is switched off there.

**What switching it on gives away**, and why it is off until you decide:

- **Anyone who can reach the receiver's web interface, or publish on the broker, can choose which
  entities the television controls - and then operate them from the menu**: any usable entity, a
  lock, a garage door or a cover, a script or an automation included. The menu creator's page saves
  a new menu without any Home Assistant login, and so does a client that publishes `menu_edit` on
  the broker - the broker's ACL is no protection where the broker does not enforce one (the
  Mosquitto add-on, above), and anyone on the broker can then send the `menu_action` that runs the
  row. Home Assistant still checks every edit with its own menu options' rules, so an edit can bind
  only a usable entity with a control Home Assistant allows for it. Switch it on only where
  everything on the broker and the LAN is something you would hand the remote control - or leave it
  off and build the menu in Home Assistant.
- **The catalogue tells anyone on the broker every entity's name and area, while editing is on.** It
  is published, retained, for the menu creator, and holds every entity a menu row can be bound to -
  hidden and diagnostic ones included - with its name and area. Anyone who can read
  `enigma2mqtt/menu_catalogue/<node_id>`, or open the Menu page, can read that list. With editing
  off it says only that editing is off.
- **An answer can be forged by whoever can read the edit.** Anyone subscribed to
  `<base>/<node_id>/menu_edit` sees a save's id, and can publish an answer to it on
  `enigma2mqtt/menu_edit_result/<node_id>` before Home Assistant does: the page then says what
  that client wrote. It changes nothing Home Assistant stores, and it is no more than editing,
  once switched on, already lets such a client do.
- **No Home Assistant token is on the receiver.** The creator goes through the broker and Home
  Assistant's own validation; a receiver taken over gains no credential to Home Assistant from it.
- **The page's one script reads the catalogue as untrusted text**: every string goes on screen as
  text, never as markup, under a policy that admits no other script and no other origin.

## Supply chain

paho-mqtt is vendored at a pinned version with the source archive's SHA-256 recorded in
[NOTICE](NOTICE) and no local modifications. Release IPKs are built by CI from the tag, each
release carries the IPK's SHA-256 next to it, and the signed release index lists each release's
size and SHA-256. A vendored-dependency bump is always a changelog
entry.
