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

## Accepting an index

Every reader applies the same rule, in this order, and each refusal has a reason code:

1. the index is at most 64 KiB and the signature file at most 1 KiB (`too_large`); the signature
   file is well formed (`malformed_signature`);
2. its `key_id` names a key the reader holds (`unknown_key`);
3. the signature verifies over the exact bytes (`bad_signature`) - before anything in the index is
   believed;
4. the index is well formed (`malformed_index`) and names the same key (`key_mismatch`);
5. the key's rank is not below the highest rank the reader has accepted **among the keys it
   holds** (`rank`);
6. the serial is above the last one the reader accepted **from that key** (`replay`) and at most
   1000 above it (`jump`); from a key never accepted before, it lies within 1000 of the key's
   baseline (`first_sight`).

A reader's memory is one number per key - the last serial it accepted - kept by `key_id`. That is
all it needs: the highest accepted rank is the highest rank, in the reader's own key set, of a key it
has accepted. So:

- a stolen main key can raise only its own sequence, by at most 1000 per accepted index;
- one index signed by the spare moves every reader that sees it to rank 2, and the main key is
  refused there for good - **without a release**; this cannot be undone, so the spare is never used
  to test anything;
- a build that carries other keys (an acceptance build, below) remembers only those keys and can
  never raise the rank the release keys are judged by;
- a release that adds or drops a key keeps everything the reader knew about the keys it still
  holds.

**The vectors.** [`tests/vectors/release-index.json`](../tests/vectors/release-index.json) is the
definition both halves are tested against: RFC 8032's test vectors and forgeries for the verifier,
the release keys as embedded, throwaway test key sets with their fingerprints, and scenarios - signed
indexes in sequence, each with the key set the reader holds and the verdict (`accept` or a reason
code) it must reach. A reader starts each scenario with no memory. The file is generated by
`tools/make-index-vectors.py` and never edited by hand; a test regenerates it and compares.

**The verifier.** The receiver verifies with the plugin's own pure-Python Ed25519 verifier
(`src/MQTTBridge/ed25519.py`, about 40 ms per signature on an armv7 receiver), because its image may
not have a cryptography library. It checks what OpenSSL checks: lengths, `S` below the group order,
canonical point encodings, and the cofactorless equation with both coordinates compared.

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
  an index signed by the spare.

A release whose declarations are nowhere, a package that is not the release, a feed that does not
serve it: each stops the build, and nothing is signed.

## Building, signing, publishing

Three workflows, and no job can do what the next one does.

- **`index.yml`** runs on every pull request and every push to `main` - not only on the files it is
  about, so it can be a required check. It holds no secret and writes nothing. It runs
  `tools/check-workflows.py` (below), `make-index.py tree` (policy, `COMPATIBILITY`, the published
  index verified, the baselines, an emergency index if one is waiting), and builds the index the
  tree would publish, with the diff against the published one in its summary. A second job runs the
  sign step itself on a real runner with a throwaway key (`tools/rehearse-signing.py`).
- **`publish-index.yml`** runs when `Release` succeeds for a `v*` tag pushed here (the version is
  the triggering commit's one `v*` tag, never a branch name), when `policy.json` changes on `main`,
  or by hand on `main` - never on another ref:
  - **build** (`contents: read`, no key) builds the unsigned index - failing if the triggering
    release's package is not there yet - and puts its **sha256 at the top of the job summary** and
    into a job output;
  - **sign** runs in the `release-signing` environment, which admits the `main` branch only and
    waits for the maintainer's approval. It has **no permissions**, uses only `actions/checkout`
    and `actions/download-artifact`, installs nothing, and runs the runner's own `python3` and
    OpenSSL. It checks the file again - the sha256 against build's output, every member, the main
    key's id, the next serial - signs it with `openssl pkeyutl -sign -rawin -keyform DER -inkey
    /dev/stdin`, the secret (one line of base64 of the raw 32-byte seed) piped in as DER behind the
    fixed 16-byte PKCS#8 prefix, `base64` and `openssl` run without the secret in their environment,
    and nothing written to disk but the signature - then verifies the signature with the embedded
    main key before handing it on;
  - **publish** (`contents: write`, no key) accepts the pair exactly as a reader that has seen the
    published index would, and pushes both files to `gh-pages`.

  The chain runs in one concurrency group, `publish-index`, never cancelled, so two chains never
  both sign "published + 1".
- **`emergency-index.yml`**: see below.

`tools/check-workflows.py` fails a pull request that would weaken this: a `pull_request_target`
trigger; an action not pinned to a full commit SHA with its version in a comment; the key referenced
anywhere but the sign step; a sign job outside the environment, with permissions, with any other
action or installing anything; a sign step without `shell: bash`, with tracing, with `xxd`, `od`,
`hexdump`, `tee`, `printenv` or an encoding `base64`, with the key expanded anywhere but the one
`printf` that pipes it into `base64 -d`, or with `base64` or `openssl` still holding the key in
their environment; a `--keyset` anywhere; a chain workflow outside the concurrency group or runnable
off `main`.

**What the maintainer does.** After `Release` succeeds, open the `publish-index.yml` run, read the
sha256 and the diff in build's summary, approve sign, and check that the published serial rose by
one. To withdraw a release or raise the floor: a pull request to `policy.json`, then the same
approval. The first run is started by hand after this is merged, and publishes serial 1 with 0.1.0
to 0.3.0 (0.1.0 below the floor, listed for completeness).

## When the main key is lost or leaked

A deleted secret is not a lost key: it is entered again from the offline backup. When both copies
are gone, or the key has leaked:

1. delete the `INDEX_SIGNING_KEY` secret;
2. on a machine that never held the main key, build the next index naming the spare - `tools/make-index.py
   build --key-id c72fd83e3e514a25 ...` - and sign it with `tools/sign-index.py --index releases.json
   --key-file <spare seed>`, which verifies the signature with the embedded spare key before it
   writes one;
3. open a pull request adding both files under `release-index/emergency/`; `index.yml` accepts them
   against the published index; on merge, `emergency-index.yml` - no environment, no secret -
   accepts them the same way and publishes them;
4. release both halves without the main key and with a new spare: a receiver reset later would
   otherwise still accept the leaked key. The tools sign with the lowest-ranked embedded key, so
   from that release on the `INDEX_SIGNING_KEY` secret holds whichever key that is.

The emergency path needs control of the repository. If the emergency *is* the account, the attacker
controls `gh-pages` too and can withhold the spare-signed index until the account is recovered; for
an installation of one's own, the spare-signed index can be put on the retained
`enigma2mqtt/release_index` topic directly - the signature is what a receiver and Home Assistant
check, not the channel.

## Acceptance builds

A hardware acceptance run drives the update path with a test index from a test origin, signed with
throwaway keys. Only an `acceptance` build may carry them: `MQTTBRIDGE_BUILD_ORIGIN` and
`MQTTBRIDGE_BUILD_INDEX_KEYS` at build time, written into `buildinfo.py` as `ORIGIN` and
`INDEX_KEYS`. `tools/make-buildinfo.py` refuses either for any other flavour; the plugin honours them
in no other flavour (`trust.configured`); the release workflow reads the package back
(`tools/check-release-package.py`) and refuses one that names either, or whose own modules answer
anything but the published origin and the release keys.

## Tools

| Tool | What |
|---|---|
| `tools/make-index.py` | `build`, `check`, `wrap`, `verify`, `tag-for`, `tree` - everything but the signature |
| `tools/sign-index.py` | signs offline with OpenSSL, for the spare key only |
| `tools/rehearse-signing.py` | runs the sign step's own text with a throwaway key |
| `tools/check-workflows.py` | the workflow rules above |
| `tools/check-release-package.py` | the release workflow's read-back of the package |
| `tools/make-index-vectors.py` | writes the shared vectors |
