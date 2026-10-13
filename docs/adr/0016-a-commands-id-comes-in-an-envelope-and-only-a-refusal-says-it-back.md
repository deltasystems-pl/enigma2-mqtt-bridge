# ADR-0016: A command's id comes in an envelope around its payload, and only a refusal says it back

**Status:** accepted 2026-10-13
**Date:** 2026-10-13
**Supersedes:** - . It replaces one sentence of [TOPICS.md](../TOPICS.md) section 2, "no
correlation id in v1", with an optional id inside contract 1.

## Context

A command has no acknowledgement: its answer is its state topic changing, and a refusal is the
retained `last_error`, which names the command in `cmd` and nothing more. That is enough for a
person with one remote, and not for a consumer that sends more than one command.

**`cmd` does not say which command.** The companion integration fails every command it is waiting
on whose name equals `last_error.cmd`. Two zaps sent close together are one name; so are a volume
slider's steps.

**A refusal can come late.** A zap is verified on `service` five seconds after it was made. A zap
from standby waits for the wake first. A screenshot fails when `grab` exits, a softcam restart on a
later turn of its sequence, a removal after the broker has acknowledged the retraction, and an
update minutes later - sometimes from the plugin process that starts after the restart. By then
the consumer has sent other commands, and has stopped waiting for this one.

**Payloads are not uniform.** Some commands take a bare string (`PRESS`, `standby`, a service
reference, the node id), some a JSON object, several either, and a dozen ignore the payload. A
handler decides what its payload is; nothing in front of the handlers parsed anything.

**The contract is in its first major**, and what may change inside one is written down
([TOPICS.md](../TOPICS.md), Contract version). The list names a payload member and a capability
name as additions, which covers `last_error.id` and `command_id`. It does not name a new form of
an existing command's payload. For that this record rests on what the history table of the same
section already classified as additions inside contract 1: `cmd/message`'s `style` in 0.3.0 and
`cmd/timer` `delete` accepting a finished timer in 0.4.0 - both a payload an older plugin
refused or read differently, taken by a newer one. A command that used to mean one thing and
now means another is not an addition, and nothing here is one: every payload a command took
before, it takes unchanged.

## Decision

- **The id travels in an envelope around the command's own payload**: a JSON object with exactly
  the members `id` and `payload`. The dispatcher opens it before any handler runs and hands the
  handler what `payload` stands for - a string as that text, an object as its JSON text. No
  handler knows there was an envelope, so there is one mechanism for every command, the present
  ones and the next.
- **`payload` is a string or an object, and nothing else.** Those are the two shapes a command's
  payload has on the broker. A number, a boolean, an array or `null` there makes the message no
  envelope: a bare `40` and a bare `true` are text, so `"payload": "40"` already says them, and
  a second spelling would be one more thing whose reading - `1e2`, `-0`, `null` for "nothing" -
  has to be promised. The empty string is the empty payload.
- **Anything that is not exactly that object is the command's own payload**, passed on as the same
  text it arrived as. A command without an id is not parsed differently, and its refusal is the
  same bytes as before, because the member is absent and not `null`.
- **Only a refusal says the id back**, as `last_error.id`. There is no acknowledgement of success
  and no result topic: a command that worked is answered by its state topic, as it always was.
- **An id is 1 to 64 characters of letters, digits, `.`, `_`, `:` and `-`.** It ends up on a
  retained topic, in the log and on the OpenWebif page, and nothing in that set needs escaping in
  any of them. It is not trimmed, cut or cleaned: a changed id is no longer the one its sender
  waits for.
- **An id the plugin cannot use is dropped and the command runs.** That covers a wrong type, a
  wrong shape, and an id that holds a registered secret - the broker password or the OSCam web
  interface's password. The log says why, without the id, once for each reason in a run of the
  plugin and at debug level after that.
- **The capability `command_id` is claimed by every plugin that opens the envelope.** It is the
  one name in `info.capabilities` that says nothing about the receiver: it says the envelope is
  safe to send.
- **Every refusal is given the id of its own command explicitly, and nothing that publishes a
  refusal reads "the command being handled".** The dispatcher passes the id for the refusals it
  publishes itself. Work that outlives its handler takes the id when it is accepted - the
  dispatcher holds it for exactly as long as the handler runs - keeps it with that work, and hands
  it over when the work fails. `Bridge.publish_last_error` takes the id as an argument and has no
  other source for it.
- **Across a restart the id is kept with the update's request file**, the one file of a transaction
  that the plugin writes and both plugin processes read. The helper is not changed: it reads the
  members it knows. What a starting plugin reads from that file is held to the id's rule again
  before it is published.
- **Only the broker's commands have ids.** The OpenWebif page and the receiver's own screen call
  the same handlers with no envelope; `cmd/relay`, which is an answer and not a command, is not
  opened either.

## Alternatives that were not taken

- **A member inside the command's payload** (`{"sref": "...", "id": "..."}`). It has no place in a
  bare-string payload, so `PRESS`, `standby` and a service reference would each need a JSON form,
  and every handler would have to know the member. Several payloads already own the name:
  `cmd/relay`'s `id` is the relay request's, and `cmd/timer` identifies an event by `event_id`.
- **The id in the topic** (`cmd/<name>/<id>`). No parsing at all, but an older plugin treats the
  longer topic as an unknown command name, the id would be bound by topic rules instead of ours,
  and an access-control list written for `cmd/+` stops matching.
- **MQTT 5 correlation data.** The plugin's session is MQTT 3.1.1, which has no properties, and a
  refusal would have to carry the id on a retained topic all the same.
- **A prefix on a string payload** (`id=...;PRESS`). It changes what an existing payload means for
  any sender whose text happens to start that way, and `cmd/message` shows free text.
- **Any JSON value as `payload`.** Built first, and withdrawn before release: a number was
  written again the encoder's way and `null` stood for the empty payload, which is two rules a
  sender has to know for nothing a string does not already carry.
- **Refusing a command whose id cannot be used.** The id is an aid to the sender, not part of the
  request. A random id that happens to contain a short broker password would turn into a command
  that fails for no reason its sender could find, on that one household's receiver.
- **Looking for a piece of a secret in the id.** A plugin that drops every id found inside a
  password it holds answers "is this text part of the password" for anyone who can publish a command and
  read `last_error`, one character at a time. Only an id that holds a whole registered secret is
  dropped, which tells a sender nothing it did not already hold.
- **An acknowledgement of success.** It would be a second answer next to the state topic, and the
  two would disagree whenever the receiver did something other than what it said - which is the
  case "verified by effect" exists for.

## Consequences

- **A late refusal cannot carry another command's id**, and this is by construction rather than by
  care: the only way an id reaches `last_error` is as an argument, so a path that forgets to carry
  one publishes a refusal without an id. The failure mode of a mistake is a missing id, never a
  wrong one. Each path that carries one is tested against a command with another id handled in
  between.
- **A consumer must not depend on the id coming back.** A dropped id, a refusal from before the
  consumer learned the capability, and an install started at the television all give a refusal
  without one. A refusal without `id` is read by `cmd`, as before.
- **A consumer sends the envelope only after reading `command_id` in `info.capabilities`.** A
  plugin up to 0.4.0 takes the envelope for the payload: a command that reads its payload refuses
  it, and one that ignores its payload runs - with no id on anything it says afterwards.
- **The shape `{"id", "payload"}` is taken.** No command may ever be given a payload of exactly
  those two members, and a later extension of the envelope needs a capability of its own - a
  plugin that knows only this one hands an envelope with a third member to the handler.
- **An object in `payload` reaches its handler as JSON text written again**, not as the bytes the
  sender wrote: key order is kept, there are no spaces, a number is spelled the encoder's way and
  of two members with one name the last is kept. A handler that echoes a rejected payload on
  `last_error` echoes that text. A sender that needs its exact bytes sends them as a string.
- **A payload UTF-8 cannot carry makes the message no envelope.** JSON can spell half a surrogate
  pair; opened, it would reach a handler as text no refusal could be published with. Unopened it
  is the raw payload, refused or not as it always was, without an id. The same hole existed for
  the commands that read JSON themselves, and is closed where `last_error` is built: the sentence
  carries the escape as text.
- **A short secret can cost a household its ids.** An id that holds a two-letter password is
  dropped every time, so a consumer uses random ids without a fixed alphabetic prefix.
- **The 4096-byte limit includes the envelope**, so the largest command that fits is a little
  smaller with an id than without.
- **`info.capabilities` is no longer `[]` on a plugin that has bound nothing.** The name is an
  addition, and a consumer ignores names it does not know; a test that compared the list with `[]`
  is the only kind of reader that notices.
- **The update's request file has an optional member the helper ignores.** A request without an id
  is the file it always was. A plugin older than this one that starts after a downgrade does not
  read the member, and its `last_error` has no id.
- **If this is ever reversed** - the envelope withdrawn - that is a removal, and needs a new
  contract major.
