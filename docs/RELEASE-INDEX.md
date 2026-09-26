# The signed release index

The plugin and the companion Home Assistant integration will only ever install a plugin release
that is named in **one signed list**: `releases.json`, with its signature `releases.json.sig`,
published next to the opkg feed at

    https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/

Why, and what it replaces, is [ADR-0015](adr/0015-signed-self-update.md). This file is the
reference: the format, the rule every reader applies, where each value comes from, and how an index
is built, signed and published. Nothing reads the index yet - the plugin's check and the
integration's reader come in later changes - but the index, its keys and its rule are fixed here,
and the first index is published before anything reads it.

## Format

`releases.json` is UTF-8 JSON, and the signature covers its **exact bytes**. `tools/make-index.py`
writes one member per line and one release per line, newest first:

```json
{
  "schema": 1,
  "package": "enigma2-plugin-extensions-mqttbridge",
  "serial": 7,
  "issued": 1790500000,
  "key_id": "5de3b24c97e88660",
  "floor": "0.2.0",
  "releases": [
    {"version": "0.3.0", "filename": "enigma2-plugin-extensions-mqttbridge_0.3.0_all.ipk", "size": 259730, "sha256": "12d0bcaa...8793", "commit": "26c996bc...c93b", "commit_time": 1790324861, "contract": 1, "min_integration": null, "depends": ["python3-core", "python3-json", "python3-threading", "python3-netclient"], "self_update": false, "withdrawn": null}
  ]
}
```

| Member | Meaning |
|---|---|
| `schema` | 1 |
| `package` | the opkg package name, always the one above |
| `serial` | a whole number from 1, strictly increasing per publication **with one key** |
| `issued` | when the index was built, epoch seconds - shown as its age, never checked: there is no expiry, so a withdrawal reaches a receiver only with a newer index |
| `key_id` | the key that signs this index; must equal the signature file's |
| `floor` | the lowest version anybody may install |
| `version` | a plain `N.N.N` - ASCII digits, no leading zeros, nothing after it - so opkg, PEP 440 and Home Assistant order every pair the same way |
| `filename`, `size`, `sha256` | the package as the release and the feed serve it; a reader checks the bytes it downloads against these before `opkg` sees them |
| `commit`, `commit_time` | the tagged commit and its time |
| `contract` | the topic contract's major ([TOPICS.md](TOPICS.md#contract-version)) |
| `min_integration` | the oldest companion integration the release needs, or `null` |
| `depends` | the package names from the package's own `Depends`, version constraints removed |
| `self_update` | whether the release can update itself from this index |
| `withdrawn` | `null`, or the one-line reason it must no longer be installed |

A reader **tolerates** members it does not know, at the top and in a release: a later index may add
one, and a receiver that refused it could never be updated to understand it. The tools that build
and sign the index know every member and refuse any other, so nothing enters a signed file by
accident. A reader refuses an index over 64 KiB, a member twice, `NaN`, and anything above that is
not what the table says.

`releases.json.sig` is one line:

```json
{"key_id": "5de3b24c97e88660", "algorithm": "ed25519", "signature": "<base64 of 64 bytes>"}
```

## Keys

| Key | `key_id` | Public key (raw, base64) | Rank | Where the private half is |
|---|---|---|---|---|
| main | `5de3b24c97e88660` | `39Ndn8vAkeWAhYIYWvNubezKuF5F/5aY5E1uo+hSnqQ=` | 1 | the `INDEX_SIGNING_KEY` secret of the `release-signing` environment, and one encrypted offline backup |
| spare | `c72fd83e3e514a25` | `1F2ajhsDoTuqAGdV2QOHdRl8hV4B0kNE0xwVGrpHdfs=` | 2 | sealed offline; never on GitHub |

- A `key_id` is the first 16 hex digits of sha256 over the raw 32-byte public key, so an id cannot
  name another key.
- Each key also carries a **baseline**: the serial published when the release carrying the key set
  was built (0 for both today). A release pull request raises it; `index.yml` fails once the
  published serial is within 100 of running out of a baseline's window, so no release is built with
  one that would lock out a receiver installed from it.
- The set's **fingerprint** is sha256 over one `key_id rank public-in-base64` line per key, sorted
  by `key_id`, each line ending in `\n`.
- Both halves embed the same set: `src/MQTTBridge/trust.py` here, `const.py` in the integration;
  a test in each pins it.

**The rules a release's key set keeps**, against the last release that carried one - `index.yml`
reads that release's `trust.py` at its tag and refuses a tree that breaks them:

- a key never changes its rank, and its baseline never goes down;
- a new key ranks above every key the previous release carried (a rotation adds rank 3);
- a release never drops a key while keeping one ranked below it: the one to drop is the lower key
  (after a theft, the main key), never the higher one.

## Accepting an index

Every reader applies the same rule, in this order, and each refusal has a reason code:

1. the index is at most 64 KiB and the signature file at most 1 KiB (`too_large`); the signature
   file is well formed (`malformed_signature`);
2. its `key_id` names a key the reader holds (`unknown_key`);
3. the signature verifies over the exact bytes (`bad_signature`) - before anything in the index is
   believed;
4. the index is well formed (`malformed_index`) and names the same key (`key_mismatch`);
5. the key has not been silenced, and its rank is not below the highest rank the reader has
   accepted **among the keys it holds** (`rank`);
6. the serial is above the last one the reader accepted **from that key** (`replay`) and at most
   1000 above it (`jump`); from a key never accepted before, it lies within 1000 of the key's
   baseline (`first_sight`).

A reader's memory is kept by `key_id` - which names one public key, so it means the same under every
key set - and has two parts:

- **serials**: the last serial accepted from each key;
- **silenced**: the keys it will never accept again. Accepting an index from a key silences every
  key of the reader's own set ranked below it.

The rank floor is the highest rank, in the reader's own key set, of a key it has accepted. So:

- a stolen main key can raise only its own sequence, by at most 1000 per accepted index;
- one index signed by the spare moves every reader that sees it to rank 2, and the main key is
  silenced there for good - **without a release**, and whatever a later release embeds: even a
  release that broke the rules above by dropping the spare and keeping the main key would not bring
  it back (a vector pins this). Using the spare cannot be undone, so it is never used to test
  anything;
- an acceptance build (below) can never raise the rank the release keys are judged by, nor
  silence one: its test keys never include a release key, and it keeps its memory apart (next
  paragraph);
- a release that adds or drops a key keeps everything the reader knew about the keys it still
  holds.

**Where the memory is kept.** A reader stores one state, in one shape both halves write:
`{"schema": 1, "release": {...}, "acceptance": {...}}`. Each part maps the fingerprint of a key set
to what a reader holding that set learned: `{"keys": [key_id], "serials": {key_id: serial},
"silenced": [key_id]}`. A build reads and writes only its own part - an acceptance build the
`acceptance` part, every other build the `release` part - so nothing a test index teaches a
receiver is ever read by the production build that follows it. Within its part, a reader takes the
largest serial for each key it holds and every silenced key from each entry whose key set shares a
key with its own, and nothing from a set that shares none: a release that adds or drops a key keeps
what was known, a downgrade to an older set cannot forget what a newer one learned, and a disjoint
set is never consulted.

**Loading it.** A stored state or memory in any other shape - the bare map of serials an earlier
draft used, a serial that is not a whole number of at least 1, a silenced entry that is not a key
id, a member nobody writes - is refused (`trust.BadMemory`), never read as "nothing remembered":
that would put a reader back at first sight with no key silenced, the one direction a damaged file
must not move it. The reader then judges no index and reports it. **Recovery:** remove the file on
purpose; that is a factory reset of this memory, and the build's baselines apply again. The vectors
carry the shapes that must load and the ones that must be refused.

A reader that finds the exact bytes it already holds re-delivered has nothing new: `replay` is the
verdict, and it is not a failure. This is the spec's "ignored for good" rank rule written per key:
for key sets that keep the rules above it gives the same verdicts as a single stored rank carried
across releases, and where a set breaks them it keeps the safer one.

**The vectors.** [`tests/vectors/release-index.json`](../tests/vectors/release-index.json) is the
definition both halves are tested against: RFC 8032's test vectors and forgeries for the verifier,
the release keys as embedded, throwaway test key sets with their fingerprints, and scenarios - signed
indexes in sequence, each with the key set the reader holds and the verdict (`accept` or a reason
code) it must reach - and the `lineage`, `release` or `acceptance`, whose part of the stored state
it reads and writes. A reader starts each scenario with no state. The file is generated by
`tools/make-index-vectors.py` and never edited by hand; a test regenerates it and compares.

The signature vectors include the forgeries each check exists for: `S + L` and `S = L`, a
non-canonical `R`, `(R, L - S)` and a signature whose two sides share `x` (a comparison of one
coordinate accepts them), and a key with a torsion part that verifies only when `k` is reduced mod
`L`. The scenarios pin the order of the rule: a 64 KiB index is accepted and one byte more refused
before its signature is looked at; an index that is neither signed by the named key nor JSON is
`bad_signature`, because the signature is judged before the index is parsed.

**The verifier.** The receiver verifies with the plugin's own pure-Python Ed25519 verifier
(`src/MQTTBridge/ed25519.py`, about 40 ms per signature on an armv7 receiver), because its image may
not have a cryptography library. For every well-formed key it accepts exactly the signatures
OpenSSL accepts: lengths, `S` below the group order, canonical `R`, and the cofactorless equation
with `k` reduced mod `L` and both coordinates compared. It is stricter than OpenSSL on malformed
public keys (a non-canonical encoding), which no embedded key set contains - `check_keys` refuses
one.

## Where each value comes from

`tools/make-index.py build` reads:

- **the releases API**: every published, non-draft, non-pre-release release tagged `vN.N.N`, and
  its one package asset. The package is downloaded, and its size and sha256 must equal the asset's
  `size` and `digest` - and the **feed's copy** on `gh-pages`, byte for byte, because that is where
  a receiver downloads from;
- **the package itself**: `Package` and `Version` must be this release; `Depends` gives `depends`;
  a package that carries a build id must say it is this commit, at its time, clean, `release`, with
  no test origin or test keys;
- **git**: the tag's commit and its time; `COMPATIBILITY` at the tag, which declares `contract`,
  `min_integration` and `self_update` for the release built from it;
- **`release-index/policy.json` on `main`**: the `floor`, the `withdrawn` reasons by version, and
  `corrections` by version - a declaration found wrong after its release, or one for a release
  older than the `COMPATIBILITY` file (0.1.0 to 0.3.0). Every version it names must be released;
- **the published index**, verified with the embedded keys: the next serial is its serial + 1 when
  the same key signs, and the key's baseline + 1 when another one does. The main key cannot follow
  an index signed by the spare;
- **the history of `gh-pages`**: every index it has ever published, verified. The ruleset refuses
  force pushes and deletion, so that history only grows, while the files at its tip can be changed
  by any ordinary commit. The published pair must be the newest of them - see "When gh-pages was
  rolled back";
- **`release-index/emergency/`**: an emergency index waiting there and not yet published stops the
  build - every reader that sees it would refuse an index built past it.

A release whose declarations are nowhere, a package that is not the release, a feed that does not
serve it: each stops the build, and nothing is signed.

## Building, signing, publishing

Three workflows, and no job can do what the next one does.

- **`index.yml`** runs on every pull request and every push to `main` - not only on the files it is
  about, so it can be a required check. It holds no secret and writes nothing. It runs
  `tools/check-workflows.py` (below), `make-index.py tree` (policy, `COMPATIBILITY`, the published
  index verified, the baselines, an emergency index if one is waiting), and builds the index the
  tree would publish, with the diff against the published one in its summary. A second job runs the
  sign step itself on a real runner with a throwaway key (`tools/rehearse-signing.py`). Both jobs -
  `Release index` and `Sign step rehearsal` - are meant to be required checks of the `main` ruleset:
  the rehearsal is what proves the sign step's text still signs on the runner's OpenSSL before a
  real approval.
- **`publish-index.yml`** runs when `Release` succeeds for a `v*` tag pushed here (the version is
  the triggering commit's one `v*` tag, never a branch name), when `policy.json` changes on `main`,
  or by hand on `main` - never on another ref:
  - **build** (`contents: read`, no key) builds the unsigned index - failing if the triggering
    release's package is not there yet - and puts its **sha256 at the top of the job summary** and
    into a job output;
  - **sign** runs in the `release-signing` environment, which admits the `main` branch only and
    waits for the maintainer's approval, on a fresh hosted `ubuntu-24.04` runner. It has **no
    permissions**, uses only `actions/download-artifact` and `actions/checkout`, and installs
    nothing. Its order is the point: **no code from the repository runs before the key has been
    used.** First the artifact, outside the workspace; then its sha256 against build's output, with
    the runner's own `/usr/bin/sha256sum`; then the signature - `/usr/bin/openssl pkeyutl -sign -rawin
    -keyform DER -inkey /dev/stdin` with fixed arguments, the secret (one line of base64 of the raw
    32-byte seed) expanded once, piped as DER behind the fixed 16-byte PKCS#8 prefix into
    `/usr/bin/base64 -d` under `/usr/bin/env -u INDEX_SIGNING_KEY`, nothing written but the
    signature. Only then the checkout, the full re-check of the file - every member, the main key's
    id, the next serial, the branch's history - and the verification with the embedded main key;
    the signature leaves the runner as the job's output only if both pass;
  - **publish** (`contents: write`, no key) accepts the pair exactly as a reader that has seen the
    published index would, and pushes both files to `gh-pages`.

  The chain runs in one concurrency group, `publish-index`, never cancelled, so two chains never
  both sign "published + 1".
- **`emergency-index.yml`** has a group of its own, `emergency-index`, so a `publish-index` run
  waiting for approval can neither hold it back nor cancel it, and it can be started by hand on
  `main`. See below.

`tools/check-workflows.py` fails a pull request that would weaken this. Its docstring lists every
rule; in short: the sign job's whole text is pinned by sha256, and its hash and sign steps are fixed
texts written in the checker, byte for byte; nothing but the artifact download and the hash check
runs before the key; no step of the job becomes root, runs in the background, touches `/usr`,
`GITHUB_PATH`, `GITHUB_ENV`, `BASH_ENV` or `PATH`, or declares `env` or `defaults`; the job runs on
a hosted `ubuntu-24.04` runner; the `secrets` context appears once in the whole repository; only
that job may name the `release-signing` environment; every `uses:`, flow style included, is pinned
to a full commit SHA with its version beside it; no `pull_request_target`; in the chain and in
`index.yml` every run step is `shell: bash` (pipefail), carries no condition but build's release
step, and runs its guard commands as plain commands - never inside `if`, `!` or an `&&`/`;` list,
never followed by `|` - with no `continue-on-error`, `||`, `set +e` or `always()`; and every job of
the chain is refused off `main` by a condition that nothing can loosen.

**What the maintainer does.** After `Release` succeeds, open the `publish-index.yml` run, read the
sha256 and the diff in build's summary, approve sign, and check that the published serial rose by
one. To withdraw a release or raise the floor: a pull request to `policy.json`, then the same
approval. Never approve a run whose summary says "Nothing is published yet" once an index has been
published, whose serial is not the published one + 1, or whose diff changes an existing release's
size, sha256 or commit. After any change to `publish-index.yml`, never re-run a run from before the
change - re-running executes that run's old workflow with today's secret; start a new one.

**The first run.** The merge that adds `release-index/policy.json` is a push to `main` that touches
it, so it starts `publish-index.yml` by itself: **that run is the first run**. Read its build
summary - "Nothing is published yet: this is the first index", "gh-pages history: no signed index
has ever been published", serial 1, key `5de3b24c97e88660`, 0.1.0 to 0.3.0 with 0.1.0 below the
floor - approve sign, and do not start another run by hand; one
started as well would wait behind it and then build serial 2. Only if no run appears on the merge
commit, start one by hand on `main`, once. The push trigger is kept rather than excluding the first
add: it is the trigger every later withdrawal and floor change uses, and nothing is signed without
the approval either way.

## When the main key is lost or leaked

A deleted secret is not a lost key: it is entered again from the offline backup. When both copies
are gone, or the key has leaked:

1. delete the `INDEX_SIGNING_KEY` secret;
2. on a machine that never held the main key, in a clone with `gh-pages` fetched (the build checks
   every package against the feed's copy and the published index), build the next index naming the
   spare - `tools/make-index.py build --key-id c72fd83e3e514a25 ...` - and sign it with
   `tools/sign-index.py --index releases.json --key-file <spare seed>`, which verifies the signature
   with the embedded spare key before it writes one;
3. reject or cancel any `publish-index.yml` run waiting for approval, then open a pull request adding
   both files under `release-index/emergency/`; `index.yml` accepts them against the published
   index; on merge, `emergency-index.yml` - no environment, no secret - accepts them the same way and
   publishes them. Confirm that run succeeded; if it did not, start it again by hand on `main`. Until
   it is published, no `publish-index.yml` build succeeds;
4. release both halves without the main key and with a new spare: a receiver reset later would
   otherwise still accept the leaked key. The tools sign with the lowest-ranked embedded key, so
   from that release on the `INDEX_SIGNING_KEY` secret holds whichever key that is.

The emergency path needs control of the repository. If the emergency *is* the account, the attacker
controls `gh-pages` too and can withhold the spare-signed index until the account is recovered; for
an installation of one's own, the spare-signed index can be put on the retained
`enigma2mqtt/release_index` topic directly - the signature is what a receiver and Home Assistant
check, not the channel.

## When gh-pages was rolled back

Anything that can push to `gh-pages` - the maintainer, or `release.yml`, which pushes the feed - can
commit an older genuine pair over the newest one, or delete it. That is no forgery, but the next
index built on it would carry a serial every up-to-date reader has already passed, and all of them
would refuse it as `replay`; built on nothing, it would start again at serial 1. The build, the
sign and publish jobs and `index.yml` therefore read the branch's whole history and refuse when
the published pair is older than the newest serial its key published, signed by a key an index
published later silenced, or gone. The message names the commit to restore from: the one that
published **the newest index of the highest-ranked key that no later index silenced** - not merely
the highest serial, which after an emergency may be the silenced main key's. **Recovery:** restore
`feed/releases.json` and `feed/releases.json.sig` from that commit, in a new commit, and run
again.

**Residual.** The history is append-only only while the `gh-pages` ruleset blocks force pushes and
deletion; anyone who can bypass that ruleset can rewrite the history as well, and then this check
sees only what is left. It makes a rollback loud; it does not make gh-pages unwritable, and denying
or delaying an index stays possible for whoever controls the branch - which the signature never
claimed to prevent.

## Acceptance builds

A hardware acceptance run drives the update path with a test index from a test origin, signed with
throwaway keys. Only an `acceptance` build may carry them: `MQTTBRIDGE_BUILD_ORIGIN` and
`MQTTBRIDGE_BUILD_INDEX_KEYS` at build time, written into `buildinfo.py` as `ORIGIN` and
`INDEX_KEYS`. `tools/make-buildinfo.py` refuses either for any other flavour; the plugin honours them
in no other flavour (`trust.configured`); the release workflow reads the package back
(`tools/check-release-package.py`) and refuses one that names either, or whose own modules answer
anything but the published origin, the release keys and the release part of the stored state.

**Test keys never include a release key.** A test key set that contained the main key next to a
higher-ranked test key would let one test-signed index silence the main key for good on the
receiver the drill runs on. `tools/make-buildinfo.py` refuses such a set, and so does the plugin
(`trust.configured` raises `OverlappingKeys` - it never quietly falls back to the release keys, or an
acceptance build could no longer be told from the thing it tests). And whatever keys it carries, an
acceptance build keeps its memory in the `acceptance` part of the stored state, which a release
build never reads.

## Tools

| Tool | What |
|---|---|
| `tools/make-index.py` | `build`, `check`, `wrap`, `verify`, `tag-for`, `tree` - everything but the signature |
| `tools/sign-index.py` | signs offline with OpenSSL, for the spare key only |
| `tools/rehearse-signing.py` | runs the sign step's own text with a throwaway key |
| `tools/check-workflows.py` | the workflow rules above |
| `tools/check-release-package.py` | the release workflow's read-back of the package |
| `tools/make-index-vectors.py` | writes the shared vectors |
