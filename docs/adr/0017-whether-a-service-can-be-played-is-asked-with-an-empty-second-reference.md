# ADR-0017: Whether a service can be played is asked with an empty second reference, and answered once on a topic that is not retained

**Status:** proposed
**Date:** 2026-10-13
**Supersedes:** - . It amends [ADR-0016](0016-a-commands-id-comes-in-an-envelope-and-only-a-refusal-says-it-back.md)
in one place: "only a refusal says the id back" gains one exception, the answer to a question.

## Context

A consumer that opens a stream of a channel beside the television wants to know first whether
the receiver can serve it. The receiver may have two tuners and three things to do with them: the
television, a recording, a stream somebody else opened. A stream that cannot get a tuner is
refused by the image only after the client has connected, and the client sees a connection that
delivers nothing.

**The image can be asked.** `eServiceCenter.getInstance().info(ref).isPlayable(ref, ignore)` is
what the channel list calls to grey out the services that cannot be tuned. Its second argument
names a service the image may assume is given up to make room.

**What that argument is decides the answer.** Measured on OpenViX 6.6, with live television and
one stream holding both inputs:

- with the playing service as the second reference - what the channel list passes - the answer
  was "yes" for every channel asked. The image assumes the television is switched over, which
  for the channel list is exactly the plan;
- with an empty reference the answer was 0 for a channel on another satellite, and a stream of
  that channel requested next was refused.

So the same call is two different questions, and the one a stream needs is the second.

**Nothing in the contract answers a question.** A command is answered by its state topic changing
and refused on `last_error`; ADR-0016 added an id that only a refusal says back, and says in so
many words that there is "no result topic". That is right for a command that changes something.
A question changes nothing, so there is no state topic for the answer to appear on.

## Decision

- **The question is `cmd/stream_playable`, for one service**, by reference: a bare reference or
  `{"sref": ...}`, the two forms `cmd/zap` takes.
- **The image is asked with an empty second reference**, always. The plugin does not offer the
  other question: the channel list already answers it on the television.
- **The answer is one message on `stream_playable`**: `{"sref", "playable", "ts"}`, and `id`
  when the command carried one in its envelope. QoS 0, never retained.
- **The id is in the payload, not in the topic.** One fixed topic, as `last_error` is one: a
  consumer subscribes once, and an access-control list written for the node's tree covers it.
  ADR-0016 rejected an id in a command's topic for the reasons that apply here unchanged.
- **Absent, not `null`, without an id**, as on `last_error`. A sender without ids reads its
  answer by `sref`.
- **A question that cannot be asked is a refusal on `last_error`**, with the command's id, and
  nothing on `stream_playable`: no reference, text that is none, a service the image has no
  information for, an image that raises or answers with something that is no number.
- **Any number but 0 is yes.** The image answers with an integer, and the images differ in what
  a positive one means beyond "playable".
- **The capability `stream_playable`** is claimed where the image's service centre gives
  information for a reference and a reference can be built - with the rest of the stream area,
  so not on an image without a stream server.
- **One service a command, and no list.** Playability is not published for the channel list.

## Alternatives that were not taken

- **Playability for every channel, as a retained topic.** It is a second copy of the channel
  list that is wrong after every zap, every recording that starts and every stream that opens,
  on a receiver whose list can run to thousands of services. Asking for the one channel about
  to be streamed costs one call.
- **The answer on `last_error`, as a "refusal" with a reason code.** "No" is not a failure of the
  command - the command worked - and a consumer that raises `last_error` to the household would
  show an error for a question that was answered.
- **A retained answer.** It would be read after a reconnect as the state of the tuners now, and
  it is the state of the tuners then.
- **QoS 1.** A message queued while the session is down is delivered when it is back, late, with
  the same defect. An answer is only worth having at once; a consumer that gets none asks again.
- **The id in the topic** (`stream_playable/<id>`). No parsing for the consumer, but the id
  would be bound by topic rules instead of the envelope's, a wildcard subscription would be
  needed to hear one's own answer, and an access-control list for exact topics stops matching.
- **Refusing a question without an id.** A person with `mosquitto_pub` has none and can read the
  answer by its reference.
- **Passing the playing service as the second reference.** It answers what the channel list
  asks. Measured: everything is playable then.

## Consequences

- **The contract has its first answer that is not a state topic**, and the sentences that said
  there is none are amended in TOPICS.md: section 2's introduction, the paragraph on a command's
  id, and "Worth knowing". Everything else is answered as it was.
- **The answer is true for the moment it was asked.** A timer that fires a second later takes
  the tuner it counted on. A consumer asks just before it opens the stream and still has to
  handle a stream that delivers nothing.
- **"No" does not say why.** The image does not: a tuner in use, a service on a satellite the
  dish cannot see with the other input in use, and a reference the receiver does not know all
  come back as 0.
- **A consumer must not wait for an answer for ever.** QoS 0 and no retain mean an answer can be
  lost; no answer within a few seconds is "unknown", not "no".
- **Rests on one measurement**, on one image. OpenATV and OpenPLi have the same signature in
  `lib/service/iservice.h` and the same call sites in their channel lists; what an empty second
  reference answers there has not been measured.
- **If the image's meaning of the second argument changes**, the answer changes with it and
  nothing here would notice. The test stub records what was asked, so a change to the call is
  caught; a change in the image is not.
- **If this is ever reversed** - the topic or the command withdrawn - that is a removal, and
  needs a new contract major.
