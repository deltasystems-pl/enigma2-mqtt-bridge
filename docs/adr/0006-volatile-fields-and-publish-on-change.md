# ADR-0006: A field a payload stamps from the wall clock takes no part in publish-on-change

**Status:** accepted 2026-09-22
**Date:** 2026-09-22
**Supersedes:** —

## Context

Every retained state topic is published on change. The bridge keeps the last payload it sent for
each topic, encodes the next one the same way, and drops the publish when the two are equal. The
rule exists because a receiver that republishes the same volume every five seconds writes a row
into somebody's recorder database every five seconds, and because a retained topic is delivered to
every subscriber the moment it is written.

The comparison is made on the encoded JSON, and the code that does it already explains why the
encoding is canonical: sorting the keys is *"what makes the comparison meaningful: two
dictionaries built in a different order encode to the same bytes, so „changed" means the box
changed, not that the code walked it differently."*

The EPG grid violated exactly that principle from the other direction. Its payload carries
`generated`, the time the grid was built, taken from the wall clock at the start of the build. The
grid is rebuilt every fifteen minutes, so no two builds ever share a second: whenever a pass found
exactly the television the previous pass had found, the stamp was the one field that differed and
„changed" meant „the code ran again".

How often that happens is worth stating precisely, because it is easy to overclaim. Measured on a
live receiver with a persistent subscriber across one refresh cycle, all eleven bouquets had
genuine programme changes — between 2 of 9 and 47 of 210 channels moved — so in that window every
republish was justified and the defect cost nothing. A quarter of an hour against a live guide is
long enough that content usually does move. What the defect costs is the cases where it does not:
a bouquet whose channels carry no EPG at all, an overnight window, a receiver whose EPG import has
failed. There it is unbounded — the largest payload in the contract, rewritten four times an hour
for as long as the box is switched on, delivered to every consumer and recorded by every recorder.

The decision does not rest on the size of that waste. `docs/TOPICS.md` has stated since M2 that a
grid whose content has not changed is not republished. A consumer may have built on that sentence,
and a sentence in a published contract is either true or it is not.

The symptom that surfaced it was the test asserting exactly that sentence. It fails only when its
two builds straddle a second boundary, which for two sub-millisecond builds is rare — an
independent reviewer could not reproduce it in twenty-five consecutive runs — and so it was read as
a flaky test rather than as the product defect it was reporting faithfully. A test whose verdict
depends on a coin toss reports nothing reliably in either direction.

Two facts narrowed what the fix could cost. Nothing consumes `generated`: across both halves of
this product the only reader is the companion integration's diagnostics dump. And the contract had
never promised what the code was doing — `docs/TOPICS.md` already stated that a grid whose content
has not changed is not republished, so the behaviour was a defect against the published contract
rather than a documented feature.

The alternative was to weaken the contract: stop promising that an unchanged grid is quiet, and
keep `generated` meaning „when this was last built". That trades recorder churn on every
installation, for ever, against a field nothing reads. It is rejected.

## Decision

A field whose value a payload stamps from the wall clock, rather than reads from the receiver,
**takes no part in the change comparison**. It is still published, unchanged, in every payload
that goes out.

The publisher that owns a topic names such fields, in a `volatile` tuple beside the existing `raw`
tuple, so that a reader sees at the topic which fields it tolerates rather than finding a list of
exceptions in the bridge. `EpgGridPublisher` declares `("generated",)`; every other publisher
declares nothing and is compared exactly as before.

What is recorded as „last sent", for the next comparison, is the **comparison form** and not the
bytes that went out. A connect snapshot and an ordinary state publish reach the same topic by
different routes, and if the two recorded it differently the first rebuild after every reconnect
would look like a change — which is the same defect once per connection instead of every quarter
of an hour.

The connect snapshot remains the deliberate exception to publish-on-change: it forgets everything
and sends the lot, because a broker that lost its retained store has to be able to converge.

## Consequences

- **The meaning of a stamped field changes, and it is a contract change.** The retained
  `epg_grid/<bouquet_slug>.generated` is now when that grid's content last *changed*, not when it
  was last *built*. `docs/TOPICS.md` says so in the field table and in prose. Nothing reports the
  time of the last build any more; a consumer that wants to know the plugin is alive reads
  `availability`, which is what it is for.
- **The rule generalises, and is meant to.** Any future payload that stamps itself — a build time,
  a poll time, a sequence number that only counts passes — declares the field `volatile` or it
  will republish its topic for ever. A field that moves because the *receiver* moved is not
  volatile and must stay in the comparison; the test is whether the value would differ on a second
  reading with nothing having happened.
- **It is not a licence to hide real movement.** A monotonically increasing value read from
  something outside the plugin — a service's own uptime, for instance — has the same effect on
  publish-on-change and is not covered by this record: it reports something true about the world,
  and silencing it is a decision about that topic's cadence, taken separately.
- **A test may no longer depend on two operations landing in the same second.** The tests that
  cover this move a clock they own, and assert both halves: a rebuild a second later publishes
  nothing, and a rebuild whose content did change still carries a fresh stamp.
- **Reversing it** means either accepting the republish or removing the field, and removing a
  documented field from a retained payload is a breaking change to the contract.
