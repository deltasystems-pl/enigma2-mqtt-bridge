# Architecture decision records

Anything that will be asked about later — why the topic tree looks like this, why paho is
vendored instead of depended on, why Python 3.9 is the floor — is written down here as an ADR:
the context, the decision, and what it costs. The point is that a decision keeps its reasons. A
commit message explains a change; an ADR explains a constraint that is still binding a year
later.

## Numbering

`NNNN-short-title.md`, four digits, allocated in order and never reused. `0000-prd.md` is the
product requirements document that started the project; everything after it is a decision taken
against that background.

## Statuses

Every record carries one, in its header:

- **proposed** — written down, not yet agreed. Fine to argue with.
- **accepted** — in force. The code is expected to match it.
- **superseded by ADR-NNNN** — replaced. **The record stays**, with the pointer added; it is not
  edited to say something else and it is never deleted. Reading why a decision was reversed is
  usually more useful than the decision itself.
- **partly superseded by ADR-NNNN** — still in force except where the later record says
  otherwise. The index says so, because a reader who takes such a record at face value will be
  wrong about exactly the part somebody already argued over.
- **amended `<date>`** — a later paragraph appended to a record that is otherwise unchanged, for
  an ambiguity rather than a reversal. Same rule as above: nothing already agreed is rewritten.
- **extended by ADR-NNNN** — still in force, and a later record adds scope it never mentioned.
  Nothing in it is wrong; it is simply no longer the whole picture, and the index says so because a
  reader who takes it as the current scope will be missing something rather than mistaken.

A record also names what it supersedes, so the chain reads in both directions.

## Template

```markdown
# ADR-NNNN: <the decision, as a statement>

**Status:** proposed | accepted <date> | superseded by ADR-NNNN
**Date:** YYYY-MM-DD
**Supersedes:** — (or ADR-NNNN)

## Context
What is true that forces a choice: the constraint, the measurement, the thing that broke.

## Decision
What we are doing, in the present tense.

## Consequences
What this costs, what it rules out, and what has to change if it is ever reversed.
```

## The records

| # | Title | Status |
|---|---|---|
| [0000](0000-prd.md) | Product requirements (PRD) | accepted 2026-09-16, partly superseded by [ADR-0001](0001-m0-decisions.md), extended by [ADR-0002](0002-scope-after-m0.md) and [ADR-0003](0003-control-feedback-and-household-features.md) |
| [0001](0001-m0-decisions.md) | M0 sign-off — the three open questions | accepted 2026-09-16, amended 2026-09-16 |
| [0002](0002-scope-after-m0.md) | Scope added and changed after M0 | accepted 2026-09-21, extended by [ADR-0003](0003-control-feedback-and-household-features.md) |
| [0003](0003-control-feedback-and-household-features.md) | Control feedback and household features — the 0.2.0 and 0.3.0 plan | accepted 2026-09-21, amended 2026-09-22, extended by [ADR-0004](0004-remote-uninstall.md), §3 partly superseded by [ADR-0005](0005-softcam-restart.md), §5 partly superseded by [ADR-0007](0007-cec-standby-workaround.md) |
| [0004](0004-remote-uninstall.md) | Remote uninstall behind a box-side permission | accepted 2026-09-22, implementation pending |
| [0005](0005-softcam-restart.md) | The softcam is restarted by collapsing its instances, counted by process roots | accepted 2026-09-22 |
| [0006](0006-volatile-fields-and-publish-on-change.md) | A self-stamped field takes no part in publish-on-change | accepted 2026-09-22 |
| [0007](0007-cec-standby-workaround.md) | The CEC standby workaround identifies the television's standby when it is queued, and holds the echo across the close | accepted 2026-09-22 |
