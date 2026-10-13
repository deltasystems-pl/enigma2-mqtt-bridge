# ADR-0018: Stopping a stream client needs no permission, and names exactly one client

**Status:** accepted 2026-10-13
**Date:** 2026-10-13
**Supersedes:** -

## Context

The image's stream server can end one client's stream: `eStreamServer.stopStreamClient(host,
service reference)`. Measured on OpenViX 6.6, it returns True and the connection ends about
1.4 s later. With the list of clients on `stream_clients`, a consumer can offer "stop this one".

Every command that takes something away from the household sits behind a permission set on the
receiver: deep standby and reboot, a softcam restart, an EPG import, an update, the removal of
the plugin. The rule ([TOPICS.md](../TOPICS.md), `info.settings`) is that a setting which enables
a command is never writable over MQTT. A permission is a setting, a line on the setup screen and
on the OpenWebif page, three strings in each language, and a refusal a consumer has to explain.

The commands that have none are the ones the remote control does without asking: a zap, the
volume, standby, a key press. A zap takes the picture away from whoever is watching the
television, and nobody is asked.

## Decision

- **`cmd/stream_stop` is always allowed.** No setting permits it and none forbids it. It is in
  the family of the zap and standby, not of the reboot: it ends one stream, the client may open
  it again the next second, and nothing on the receiver is changed or lost.
- **It names exactly one client**, by the two things `stream_clients` shows for it: `host` and
  `sref`. Both members, each a non-empty string, and no other member.
- **There is no "all".** An empty payload, a bare word, one member alone and an object with a
  third member are refused before the image is asked. A consumer that wants every client stopped
  sends one command for each row it read.
- **A client that is not connected is a refusal**, with the reason code `not_connected`; an image
  that answers "no" for a client it listed is `not_stopped`. Both say the command's id back.
- **The host is matched as the topic shows it, and the image is handed its own spelling.** The
  image compares the host as text and holds an IPv4 client as `::ffff:192.0.2.20`; the topic
  shows `192.0.2.20`. The plugin finds the row in the image's list and passes on what the image
  holds, for the host and for the reference.
- **The list is read again two seconds after a stop**, so `stream_clients` answers the command
  without waiting for the next five-second look. Nothing is verified beyond that: a row still
  there may be the same client or the one that reconnected, and the topic cannot tell them apart.

## Alternatives that were not taken

- **A permission, `stream_stop_allowed`.** It would guard the one command of the stream area
  that does no lasting harm, on a receiver where the same broker client may zap, mute and put
  the receiver in standby. Whoever may publish to the node's command topics is already trusted
  with the television.
- **Stopping by host alone.** One host can hold two streams, and the one meant is the one on
  the row.
- **Stopping by a row number.** The list reorders as clients come and go; a number read a second
  ago names another client.
- **"Stop all" as an empty payload or a keyword.** It is the payload a mistake produces.
- **Reporting "still connected" when the row is there after the stop.** A player that reconnects
  by itself - most of them - is a new connection with the same host and the same service, so the
  report would say the stop failed whenever it worked on such a player.

## Consequences

- **Anybody who can publish a command can end anybody's stream**, as they can change the channel
  on the television. A household that wants that separated does it with the broker's
  access-control list, as for every other command.
- **Stopping is not blocking.** A client that reconnects is served again. There is no command
  that keeps one out, and this one must not be offered as if it did.
- **Two streams of one service to one host are one row to the command**: the image ends the
  first it finds, and a second command ends the other.
- **OpenATV lists the clients of its RTSP server in the same list** (read from its source, not
  measured) and `stopStreamClient` does not look there, so such a row can be read and not
  stopped; the refusal is `not_stopped`.
- **No setup-screen string and no setting were added**, so nothing here needs a translation of a
  setting; the OpenWebif page's action is translated as every action is.
- **If a permission is ever wanted**, it is a new read-only setting and a new refusal, both of
  which the contract allows inside a major.
