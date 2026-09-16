# ADR-0001: M0 sign-off — the three open questions

**Status:** accepted 2026-09-16
**Date:** 2026-09-16
**Supersedes:** the proposed answers in [ADR-0000](0000-prd.md) §12

## Context

The product requirements document closed with three questions marked „to close at M0". Sign-off
cannot happen while they are open, because two of them change what the plugin publishes and the
third changes what the installer has to support. Answered below, in the order they were asked.

## Decision

### 1. The EPG grid ships in v1

**This reverses the PRD's proposal.** §12 suggested leaving grids on OpenWebif on the grounds
that a grid is pull-shaped. That reasoning holds for *EPG search* and for browsing arbitrary time
windows, and those do stay on OpenWebif. It does not hold for the one grid a household actually
looks at — what is on now and next across the channels it watches — which is small, changes
slowly, and is exactly what a retained topic is for. Fetching it over HTTP means a consumer needs
a second transport, credentials for it, and a reason to poll; the plugin already has the EPG
cache open.

Scope:

- A compact grid for the **configured bouquets**, the next N events per channel, where N is the
  `epg_grid_events` setting, default **4**, and **`0` turns the feature off** and drops
  `epg_grid` from the `capabilities` list.
- Retained JSON on `enigma2/<node>/epg_grid`, shaped
  `{bouquet, generated, channels: [{sref, name, events: [{title, begin, end, event_id}]}]}`.
- Refreshed when a bouquet changes, every 15 minutes, and on demand via `cmd/epg_grid`.
- Full EPG search and browsing timers the plugin did not create stay on OpenWebif. This is a
  grid, not a database.

Delivery: the plugin side lands in **M2**, with the rest of the state topics. The integration
consumes it in **M3** through an **action that returns a response** — deliberately *not* as a
state attribute. An 80 KB attribute is rewritten into the recorder database on every update; a
response is fetched when something asks for it and stored nowhere.

### 2. Both project documents stay in the `integrations` book

The operator's wiki gives applications developed in this lab their own book on the *Applications*
shelf. This project does not get one: the PRD and the design document stay in the **`integrations`
book**, where the reader looking for „how does the receiver talk to Home Assistant" will be. A
book of two documents would be a shelf entry that exists to be tidy rather than to be found.

Revisit if the project grows operator-facing runbooks of its own — which is a different kind of
document from these two.

### 3. Telnet-only boxes: document „enable SSH first" in v1

A few OpenATV builds ship with SSH off and only telnet available. v1 does not carry a telnet
transport. `docs/INSTALL.md` tells those users to enable SSH first, which is two screens in the
receiver's own network menu.

A telnet installer transport is a **v1.1** item, tracked as an issue once M4 exists and the
installer is real enough to have a transport worth adding one to.

## Consequences

- The EPG grid adds a state topic, a setting, a command and a capability name to the v1 contract
  — all of which are in `docs/TOPICS.md` from today, marked „since M2", so the integration is
  written against a contract that will not move under it.
- It also adds the project's only payload that can reach tens of kilobytes. The consumer-side
  rule (an action's response, never a state attribute) is part of the decision, not an
  implementation detail, and belongs in the integration's own review.
- Turning the grid off has to be a real off: no topic, and the capability absent, so a consumer
  hides the feature rather than showing an empty one.
- Keeping the documents in `integrations` means the wiki taxonomy script needs no new book, and
  the cross-links in both documents keep working unchanged.
- Telnet-only users are turned away at the install step in v1. That is a real gap; it is written
  down as one rather than discovered by somebody whose box cannot run the installer.
