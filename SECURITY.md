# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes - the current development line |
| < 0.1.0 | no |

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
2. **The LAN is the trust boundary.** The plugin makes no outbound connection other than to the
   broker the user configures. There is no telemetry, no cloud service, and the release check is
   the integration's business and is off by default. TLS to the broker is supported; client
   certificates are not in v1.

   > **Superseded in part by [ADR-0015](docs/adr/0015-signed-self-update.md) (proposed):** a
   > plugin that updates itself fetches a signed release index and its own packages from one fixed
   > HTTPS address - only when a person asks, when an install needs it, or when the receiver-only
   > setting `update_check` is on. That is not built: every released plugin makes no connection
   > but the broker, exactly as this item says. The policy here - what is trusted, how keys are
   > kept and rotated, what an unsigned path still allows - is rewritten when the first release
   > that implements ADR-0015 ships, not before.
3. **The broker credential must be scoped.** A box compromise must not become a Home Assistant
   compromise. The documented setup is a **dedicated broker login per box** with an ACL that
   limits it to that node's own topics:

   ```
   user enigma2box
   topic readwrite enigma2/<node_id>/#
   topic write enigma2mqtt/discovery/<node_id>/#
   topic write homeassistant/device/<node_id>/#
   topic write homeassistant/device_automation/<node_id>/#
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
[NOTICE](NOTICE) and no local modifications. Release IPKs are built by CI from the tag, and each
release carries the IPK's SHA-256 next to it. A vendored-dependency bump is always a changelog
entry.
