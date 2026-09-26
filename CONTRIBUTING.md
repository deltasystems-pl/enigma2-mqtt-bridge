# Contributing

Thanks for looking. This project is developed in the open from its first commit: every change -
including the maintainer's - lands through a pull request with CI green, because the pull request
is the public record of *why*.

## The development loop

There is no PC-side Enigma2 sandbox worth the name for Python 3 images, so the loop has two
halves.

**On your machine** - the fast half. The plugin's logic is tested against a stub `enigma`
module, so the parts that build payloads, parse timers, map keys and assemble discovery run under
plain CPython:

```sh
python3 -m pip install -r requirements-dev.txt
ruff check .
pyflakes src tools tests        # skip src/MQTTBridge/_vendor - it is vendored
pytest -q
tools/build-ipk.sh --allow-unreleased
```

**On a box** - the half that decides. Nothing counts as working until it has been seen working on
real hardware:

```sh
BOX_PASSWORD=... tools/deploy-to-box.sh <box-ip> --restart
ssh root@<box-ip> 'tail -f /home/root/mqttbridge.log'
mosquitto_sub -h <broker> -v -t 'enigma2/#'
```

The password comes from `BOX_PASSWORD` or from `--password-file <path>`; it is never echoed and
never put on a command line. `--no-build` reuses the IPK already in `dist/`, and
`--provision <file>` installs a JSON configuration as `/etc/enigma2/mqttbridge.json` so a fresh
box comes up already talking to your broker.

`deploy-to-box.sh` refuses to restart the GUI while a recording runs or a timer is due, for the
same reason the plugin refuses `cmd/restart_gui` - and it refuses just as firmly when it cannot
read the receiver's timer list at all, because not knowing is not the same as knowing it is
safe. Two terminals - the log and the subscription - are the whole debugging apparatus; a change
that cannot be demonstrated as a topic that moves is not demonstrated.

### Translations

`src/MQTTBridge/locale/MQTTBridge.pot` is the template and `pl` and `de` are the catalogues;
`tools/build-ipk.sh` compiles the `.mo` files into the package, so only the `.po` files are
committed. A new household-visible string goes through `_()`, into the template, and into both
catalogues - there is a test that fails if it does not.

Verify by effect, never by a return code. `opkg install` reporting success says the files landed,
not that the plugin loaded; a publish that an ACL denies looks identical to one that succeeded.

## The test matrix

CI runs the unit tests on **Python 3.9, 3.12 and 3.14**:

- **3.9** is the floor. OpenPLi 9 sits there, so no syntax above 3.9 - no `match`, no `X | Y`
  annotations at runtime, no `dict | dict`. Ruff is configured with `target-version = "py39"` and
  will tell you.
- **3.12** is what OpenViX 6.6 and OpenATV 7.x run. This is the one the operator's box exercises.
- **3.14** is OpenBH 6.0, and the reason the vendored MQTT client's threading is retested there.

Lint is ruff plus pyflakes. The redundancy is deliberate: a `NameError` in an unexercised branch
of a sibling project broke every polling cycle for three releases, and pyflakes would have caught
it.

**Discovery templates are rendered, never matched.** `tests/test_discovery_templates.py` renders
every `*_tpl` the discovery payloads carry against the bytes the plugin's own publishers produce,
and fails when a template has no rendering test - so a new templated entity needs one. It renders
with `tests/hatemplate.py`: an environment that starts empty and holds only the filters, tests
and globals listed there, with Home Assistant's own `int`, `round` and `timestamp_utc` copied from
the release the companion integration pins. A template that reaches for anything else fails
before it is rendered. That is on purpose: Home Assistant replaces some of Jinja's filters, and a
test that quietly used Jinja's would be coverage of the wrong engine. Adding a filter means
copying Home Assistant's, saying where it came from, and moving it when that pin moves.

## Becoming an image tester

The maintainer has exactly one box - a Vu+ Uno 4K SE on OpenViX. Every other row of the
compatibility matrix is a guess until somebody runs it.

If you have a box on OpenATV, OpenPLi or OpenBH:

1. Find (or open) the call-for-testers issue for your image.
2. Install the current pre-release, configure it against your broker, and work through the
   by-effect checklist in the issue: zap, standby in and out, a one-minute recording, volume from
   the remote and from Home Assistant, the colour keys short and long, every `cmd/*` read back,
   and a last-will drill with the network cable out for a minute.
3. Paste `enigma2/<node>/info` - its `capabilities` list is what tells us which hooks your image
   actually provides - plus anything the log complained about.

That report is what moves a row from *best effort* to *supported*, and it is credited in the
changelog.

Translations work the same way: the source strings are English, Polish is reviewed, and German is
drafted and marked `# needs-review` until a native speaker confirms it.

## Pull requests

- Branch off `main`, one concern per pull request.
- CI must be green: ruff, pyflakes, the tests on all three Python versions, and the IPK build.
- Add a `CHANGELOG.md` entry under `## [Unreleased]` for anything a user would notice.
- Update the docs in the same pull request. `docs/TOPICS.md` is the contract the companion
  integration is written against - a change to a payload is a change to that file, to
  `docs/contract.json` (the same contract as data; `tools/check-contract.py` fails CI when the two
  disagree, or when a change removes or retypes something the last release promised without a new
  contract major), and to the compatibility table in both READMEs. TOPICS.md's
  "Contract version" section says what a release may change inside a major.
- Describe what you verified and how. „Tested on OpenViX 6.6, zap -> `service` within 1 s" is
  worth more than a paragraph of intent.

## Commit style

Plain, conventional, imperative subject lines under 72 characters; a body when the change needs
one. `fix:`, `feat:`, `docs:`, `refactor:`, `ci:` prefixes are welcome but not enforced.

**No trailers.** No `Co-Authored-By` for tooling, no generated-by footers, no attribution lines
of any kind - in commit messages, pull request bodies, issues, release notes or code comments.

## Decision records

Anything that will be asked about later - a protocol choice, a dependency, a compatibility floor
- goes into `docs/adr/` as an ADR. They are numbered, they have a status, and a decision that is
replaced is marked *superseded* rather than deleted. `docs/adr/README.md` has the template.

## Code of conduct

Participation is covered by the [Contributor Covenant](CODE_OF_CONDUCT.md).
