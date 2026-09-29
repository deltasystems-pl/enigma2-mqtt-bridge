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
   docs/INSTALL.md therefore tells users to change the root password; the plugin cannot do it for them.
2. **The LAN is the trust boundary.** The plugin connects to the broker the user configures and,
   since 0.4.0, for updates of itself only, to two more places:
   - its release origin, `https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/`, over HTTPS
     with a verified certificate and no redirects: once a day when the receiver-only setting
     `update_check` is on (off by default), on `cmd/update_check` over MQTT (refused unless
     `update_check` is on), when a person presses *check* on the television or the OpenWebif page,
     and for an install - asked for there, or over MQTT with the receiver-only permission
     `update_allowed` - that does not go through Home Assistant's relay;
   - for an install Home Assistant starts, or one on a receiver without internet, the Home
     Assistant relay address the companion integration gives it over MQTT: a fixed path, `/api/enigma2_mqtt/relay/<token>`,
     fetched over plain HTTP or over HTTPS without checking the certificate. That is enough
     because what it serves is checked against the signed index (below) before anything is
     installed.

   There is no telemetry and no cloud service. TLS to the broker is supported; client
   certificates are not in v1.

   **What an update trusts** ([ADR-0015](docs/adr/0015-signed-self-update.md),
   [docs/RELEASE-INDEX.md](docs/RELEASE-INDEX.md)). The plugin installs only a release named in the
   signed release index, and checks the package's size and SHA-256 against that entry before the
   package manager sees it. The index is verified with Ed25519 public keys built into the plugin: a
   main key, which only this repository's CI uses, in a signing job the maintainer approves by
   hand, and a spare key of higher rank, kept offline. A receiver refuses an index whose serial is
   not above the last one it accepted from that key, and one from a key ranked below a key it has
   accepted. The origin, and Home Assistant when it relays the index and the package to a receiver
   without internet (above), are couriers, not authorities. Installing over MQTT needs
   the receiver-only permission `update_allowed` (off by default) and never downgrades; a downgrade
   needs a confirmation on the television or the OpenWebif page.

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
   ```

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

## Supply chain

paho-mqtt is vendored at a pinned version with the source archive's SHA-256 recorded in
[NOTICE](NOTICE) and no local modifications. Release IPKs are built by CI from the tag, each
release carries the IPK's SHA-256 next to it, and the signed release index lists each release's
size and SHA-256. A vendored-dependency bump is always a changelog
entry.
