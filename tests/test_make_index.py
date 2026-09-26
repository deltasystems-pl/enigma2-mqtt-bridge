"""`tools/make-index.py`: the unsigned index is built only from what is really published.

Every test builds against a made-up repository (`indexlab.Lab`): tagged releases, their packages as
the releases API lists them, and a `gh-pages` branch serving the feed - and spoils one of them at a
time. The index is what a receiver will install from, so each spoilt input must stop the build
rather than end up signed.
"""

import base64
import hashlib
import json
from pathlib import Path

import indexlab
import pytest
from indexlab import Lab

from MQTTBridge import trust
from MQTTBridge.version import CONTRACT

make_index = indexlab.tool("make-index")
Failed = make_index.IndexError_
REPO_ROOT = Path(__file__).resolve().parents[1]

needs_openssl = pytest.mark.skipif(not indexlab.openssl_can_sign(),
                                   reason="signing needs OpenSSL 3 (pkeyutl -rawin)")

KEYS = trust.keys_from_data(indexlab.keyset_of(("t1", 1), ("t2", 2)))
T1, T2 = KEYS
ISSUED = 1790500000


def _build(lab, policy=None, published=(None, None), key=T1, **kwargs):
    raw, _old = make_index.build(
        lab.source(), policy or lab.policy(), KEYS, key.key_id, root=lab.repo,
        published=published, feed_ref="gh-pages", issued=kwargs.pop("issued", ISSUED), **kwargs)
    return raw


def _signed(raw, name="t1"):
    key = T1 if name == "t1" else T2
    return raw, trust.signature_file(key.key_id, indexlab.sign(indexlab.seed_of(name), raw))


@pytest.fixture()
def lab(tmp_path):
    lab = Lab(tmp_path)
    lab.release("0.1.0", compatibility=None)
    lab.release("0.2.0", with_buildinfo=True)
    return lab


POLICY = {"floor": "0.2.0", "corrections": {"0.1.0": {"contract": 0, "min_integration": None,
                                                        "self_update": False}}}


# ------------------------------------------------------------------ the tree --


def test_the_repositorys_policy_and_compatibility_are_well_formed():
    policy = make_index.parse_policy((REPO_ROOT / "release-index" / "policy.json").read_bytes())
    assert policy["floor"] == "0.2.0"
    declared = make_index.parse_compatibility((REPO_ROOT / "COMPATIBILITY").read_bytes())
    # The contract a release declares is the one its info.contract publishes.
    assert declared["contract"] == CONTRACT
    # Every release before this file existed is declared in the policy instead.
    assert set(policy["corrections"]) == {"0.1.0", "0.2.0", "0.3.0"}
    assert policy["corrections"]["0.1.0"]["contract"] == 0


@pytest.mark.parametrize("change, words", [
    ({"schema": 2}, "schema must be 1"),
    ({"floor": "0.2"}, "floor"),
    ({"withdrawn": {"0.2.0": ""}}, "one line"),
    ({"withdrawn": {"0.2.0": "a\nb"}}, "one line"),
    ({"withdrawn": {"0.2.0": "x" * 301}}, "one line"),
    ({"withdrawn": {"0.2": "bad"}}, "not a plain"),
    ({"corrections": {"0.2.0": {"contract": -1}}}, "contract"),
    ({"corrections": {"0.2.0": {"contract": True}}}, "contract"),
    ({"corrections": {"0.2.0": {"self_update": "yes"}}}, "self_update"),
    ({"corrections": {"0.2.0": {"min_integration": "0.4"}}}, "min_integration"),
    ({"corrections": {"0.2.0": {"floor": "0.1.0"}}}, "unknown"),
    ({"extra": 1}, "exactly"),
])
def test_a_policy_that_is_not_well_formed_is_refused(change, words):
    policy = {"schema": 1, "floor": "0.2.0", "withdrawn": {}, "corrections": {}}
    policy.update(change)
    with pytest.raises(Failed, match=words):
        make_index.parse_policy(json.dumps(policy).encode())


def test_a_policy_with_a_member_twice_is_refused():
    with pytest.raises(Failed, match="duplicate"):
        make_index.parse_policy(b'{"schema": 1, "floor": "0.2.0", "floor": "0.3.0", '
                                b'"withdrawn": {}, "corrections": {}}')


@pytest.mark.parametrize("text, words", [
    ("contract = 1\nmin_integration = none\n", "needs all"),
    ("contract = 1\ncontract = 2\nmin_integration = none\nself_update = false\n", "one `name"),
    ("contract = one\nmin_integration = none\nself_update = false\n", "whole number"),
    ("contract = 1\nmin_integration = 0.4\nself_update = false\n", "min_integration"),
    ("contract = 1\nmin_integration = none\nself_update = yes\n", "true or false"),
    ("contract = 1\nmin_integration = none\nself_update = false\nfloor = 0.2.0\n", "unknown"),
    ("contract 1\nmin_integration = none\nself_update = false\n", "one `name"),
])
def test_a_compatibility_file_that_is_not_well_formed_is_refused(text, words):
    with pytest.raises(Failed, match=words):
        make_index.parse_compatibility(text.encode())


# ------------------------------------------------------------------- build --


def test_the_index_lists_every_release_newest_first_from_what_is_published(lab):
    raw = _build(lab, lab.policy(**POLICY, withdrawn={"0.1.0": "replaced by 0.2.0"}))
    index = trust.parse_index(raw, strict=True)
    assert index["serial"] == 1 and index["key_id"] == T1.key_id and index["floor"] == "0.2.0"
    assert [entry["version"] for entry in index["releases"]] == ["0.2.0", "0.1.0"]
    newest, oldest = index["releases"]
    blob = (lab.assets / newest["filename"]).read_bytes()
    assert newest["size"] == len(blob) and newest["sha256"] == hashlib.sha256(blob).hexdigest()
    assert newest["commit"] == lab.git("rev-parse", "v0.2.0^{commit}")
    assert newest["commit_time"] == 1790000000
    assert newest["depends"] == ["python3-core", "python3-json"]
    assert (newest["contract"], newest["min_integration"], newest["self_update"]) == (1, None,
                                                                                     False)
    assert newest["withdrawn"] is None and oldest["withdrawn"] == "replaced by 0.2.0"
    assert oldest["contract"] == 0
    assert raw == make_index.render(index)


def test_a_correction_in_the_policy_wins_over_compatibility_at_the_tag(lab):
    policy = lab.policy(corrections={**POLICY["corrections"], "0.2.0": {"min_integration":
                                                                      "0.4.0"}})
    index = trust.parse_index(_build(lab, policy))
    assert index["releases"][0]["min_integration"] == "0.4.0"
    assert index["releases"][0]["contract"] == 1


def test_a_release_whose_declarations_are_nowhere_is_refused(lab):
    with pytest.raises(Failed, match=r"v0\.1\.0: no contract, min_integration, self_update"):
        _build(lab, lab.policy())


def test_the_policy_may_name_only_released_versions(lab):
    policy = lab.policy(**POLICY, withdrawn={"0.9.0": "never released"})
    with pytest.raises(Failed, match="0.9.0, which is not released"):
        _build(lab, policy)


@pytest.mark.parametrize("spoil, words", [
    ({"digest": "sha256:" + "0" * 64}, "not the release asset's digest"),
    ({"digest": "md5:0"}, "not the release asset's digest"),
    ({"size": 1}, "the release says 1"),
    ({"feed_blob": b"another file"}, "does not serve the release asset's bytes"),
    ({"publish": False}, "does not serve the release asset's bytes"),
    ({"blob": indexlab.ipk("9.9.9")}, "the package says"),
    ({"blob": indexlab.ipk("0.3.0", package="other")}, "the package says"),
    ({"blob": b"not an ipk"}, "not a readable package"),
])
def test_a_package_that_is_not_the_release_stops_the_build(lab, spoil, words):
    lab.release("0.3.0", **spoil)
    with pytest.raises(Failed, match=words):
        _build(lab, lab.policy(**POLICY))


def test_a_package_that_says_it_is_another_build_stops_the_build(lab):
    info = ('COMMIT = "' + "1" * 40 + '"\nCOMMIT_TIME = 1790000000\nDIRTY = False\n'
            'FLAVOUR = "release"\n')
    lab.release("0.3.0", blob=indexlab.ipk("0.3.0", buildinfo=info))
    with pytest.raises(Failed, match="the package's build id is"):
        _build(lab, lab.policy(**POLICY))


def test_a_package_carrying_test_keys_stops_the_build(lab):
    # Otherwise exactly the release: its commit, its time, clean, release flavour.
    lab.release("0.3.0", with_buildinfo=True,
                buildinfo_extra='ORIGIN = "https://lab.example/feed/"\n')
    with pytest.raises(Failed, match="test origin or test keys"):
        _build(lab, lab.policy(**POLICY))


def test_a_release_without_exactly_one_package_stops_the_build(lab):
    lab.releases[0]["assets"] = []
    with pytest.raises(Failed, match="exactly one"):
        _build(lab, lab.policy(**POLICY))


def test_drafts_pre_releases_and_other_tags_are_not_releases(lab):
    lab.releases.insert(0, dict(lab.releases[0], tag_name="v0.9.0", draft=True))
    lab.releases.insert(0, dict(lab.releases[0], tag_name="v1.0.0", draft=False,
                                prerelease=True))
    lab.releases.insert(0, dict(lab.releases[0], tag_name="nightly", prerelease=False))
    index = trust.parse_index(_build(lab, lab.policy(**POLICY)))
    assert [entry["version"] for entry in index["releases"]] == ["0.2.0", "0.1.0"]


def test_a_release_run_waits_for_its_package(lab):
    # The ordering guard: a run for v0.3.0 whose release has no package yet builds nothing.
    with pytest.raises(Failed, match="v0.3.0 has no published package yet"):
        _build(lab, lab.policy(**POLICY), require="0.3.0")
    assert trust.parse_index(_build(lab, lab.policy(**POLICY), require="0.2.0"))


@needs_openssl
def test_the_serial_follows_the_published_index(lab):
    first = _build(lab, lab.policy(**POLICY))
    published = _signed(first)
    second = trust.parse_index(_build(lab, lab.policy(**POLICY), published=published))
    assert second["serial"] == 2


@needs_openssl
def test_the_main_key_cannot_follow_an_index_signed_by_the_spare(lab):
    published = _signed(_build(lab, lab.policy(**POLICY), key=T2), "t2")
    with pytest.raises(Failed, match="refuses key .* for good"):
        _build(lab, lab.policy(**POLICY), published=published, key=T1)


@needs_openssl
def test_the_spare_starts_at_its_own_baseline_after_the_main_key(lab):
    published = _signed(_build(lab, lab.policy(**POLICY), serial=40))
    index = trust.parse_index(_build(lab, lab.policy(**POLICY), published=published, key=T2))
    assert index["serial"] == T2.baseline + 1 and index["key_id"] == T2.key_id


@needs_openssl
def test_a_published_index_that_does_not_verify_stops_the_build(lab):
    raw, sig = _signed(_build(lab, lab.policy(**POLICY)))
    with pytest.raises(Failed, match="published index does not verify"):
        _build(lab, lab.policy(**POLICY), published=(raw.replace(b'"serial": 1', b'"serial": 9'),
                                                     sig))
    with pytest.raises(Failed, match="both be there"):
        _build(lab, lab.policy(**POLICY), published=(raw, None))


# ---------------------------------------------------------- check, wrap, verify --


def test_check_accepts_what_build_wrote(lab):
    raw = _build(lab, lab.policy(**POLICY))
    assert make_index.check_unsigned(raw, KEYS, T1.key_id, (None, None))["serial"] == 1


def _rewritten(raw, **changes):
    index = json.loads(raw)
    index.update(changes)
    return make_index.render(index)


@pytest.mark.parametrize("change, words", [
    (lambda raw: json.dumps(json.loads(raw), indent=2).encode(), "not in the form"),
    (lambda raw: _rewritten(raw, key_id=T2.key_id), "only .* signs here"),
    (lambda raw: _rewritten(raw, serial=2), "not the next one"),
    (lambda raw: _rewritten(raw, serial=1001), "not the next one"),
    (lambda raw: _rewritten(raw, mirror="x"), "unknown members"),
    (lambda raw: _rewritten(raw, releases=list(reversed(json.loads(raw)["releases"]))),
     "newest first"),
    (lambda raw: _rewritten(raw, schema=2), "not well formed"),
])
def test_check_refuses_anything_but_the_next_consistent_index(lab, change, words):
    raw = change(_build(lab, lab.policy(**POLICY)))
    with pytest.raises(Failed, match=words):
        make_index.check_unsigned(raw, KEYS, T1.key_id, (None, None))


@needs_openssl
def test_check_refuses_an_index_issued_before_the_one_it_follows(lab):
    published = _signed(_build(lab, lab.policy(**POLICY)))
    with pytest.raises(Failed, match="issued before"):
        _build(lab, lab.policy(**POLICY), published=published, issued=ISSUED - 1)
    raw = _build(lab, lab.policy(**POLICY), issued=ISSUED - 1, serial=2)
    with pytest.raises(Failed, match="issued before"):
        make_index.check_unsigned(raw, KEYS, T1.key_id, published)


@needs_openssl
def test_wrap_writes_only_a_signature_that_verifies_with_that_key(lab):
    raw = _build(lab, lab.policy(**POLICY))
    good = indexlab.sign(indexlab.seed_of("t1"), raw)
    assert trust.parse_signature(make_index.wrap(raw, good, KEYS, T1.key_id)) == (T1.key_id, good)
    wrong = indexlab.sign(indexlab.seed_of("t2"), raw)
    with pytest.raises(Failed, match="the secret is not that key"):
        make_index.wrap(raw, wrong, KEYS, T1.key_id)
    with pytest.raises(Failed, match="not one of the keys"):
        make_index.wrap(raw, good, KEYS, "0" * 16)


@needs_openssl
def test_verify_judges_as_a_reader_that_has_seen_the_published_index(lab):
    first = _signed(_build(lab, lab.policy(**POLICY)))
    assert make_index.verify(*first, KEYS, (None, None)).index["serial"] == 1
    with pytest.raises(Failed, match="replay"):
        make_index.verify(*first, KEYS, first)
    second = _signed(_build(lab, lab.policy(**POLICY), published=first))
    assert make_index.verify(*second, KEYS, first).index["serial"] == 2
    jumped = _signed(_build(lab, lab.policy(**POLICY), serial=1002))
    with pytest.raises(Failed, match="jump"):
        make_index.verify(*jumped, KEYS, first)


# --------------------------------------------------------------------- tag-for --


def test_a_release_commit_is_named_by_its_one_version_tag(lab):
    assert make_index.tag_for(lab.git("rev-parse", "v0.2.0^{commit}"), root=lab.repo) == "0.2.0"
    lab.release("0.3.0", annotated=False)
    assert make_index.tag_for(lab.git("rev-parse", "v0.3.0"), root=lab.repo) == "0.3.0"


def test_a_commit_with_no_or_two_version_tags_names_no_release(lab):
    commit = lab.git("rev-parse", "v0.2.0^{commit}")
    lab.git("tag", "nightly", commit)
    assert make_index.tag_for(commit, root=lab.repo) == "0.2.0"
    lab.git("tag", "v0.2.1", commit)
    with pytest.raises(Failed, match="2 release tags"):
        make_index.tag_for(commit, root=lab.repo)
    with pytest.raises(Failed, match="0 release tags"):
        make_index.tag_for(lab.git("rev-parse", "main~2"), root=lab.repo)
    with pytest.raises(Failed, match="not a 40-digit"):
        make_index.tag_for("v0.2.0", root=lab.repo)


# ------------------------------------------------------------------------ tree --


def _tree(tmp_path, published, emergency=None):
    root = tmp_path / "tree"
    (root / "release-index" / "emergency").mkdir(parents=True)
    (root / "release-index" / "policy.json").write_text(json.dumps(
        {"schema": 1, "floor": "0.2.0", "withdrawn": {}, "corrections": {}}))
    (root / "COMPATIBILITY").write_text("contract = 1\nmin_integration = none\n"
                                        "self_update = false\n")
    if emergency:
        (root / "release-index" / "emergency" / "releases.json").write_bytes(emergency[0])
        (root / "release-index" / "emergency" / "releases.json.sig").write_bytes(emergency[1])
    lines = []
    make_index.tree(root, KEYS, published, log=lines.append)
    return lines


@needs_openssl
def test_the_tree_check_verifies_the_published_index(lab, tmp_path):
    published = _signed(_build(lab, lab.policy(**POLICY)))
    assert "published: serial 1, key " in _tree(tmp_path, published)[1]
    assert _tree(tmp_path / "none", (None, None))[1] == "nothing is published yet"


@needs_openssl
def test_the_tree_check_fails_before_a_baseline_falls_too_far_behind(lab, tmp_path):
    margin = trust.MAX_JUMP - make_index.BASELINE_MARGIN
    near = _signed(_build(lab, lab.policy(**POLICY), serial=margin))
    assert "verified" in _tree(tmp_path / "near", near)[1]
    far = _signed(_build(lab, lab.policy(**POLICY), serial=margin + 1))
    with pytest.raises(Failed, match="raise it in src/MQTTBridge/trust.py"):
        _tree(tmp_path / "far", far)


@needs_openssl
def test_the_tree_check_judges_an_emergency_index(lab, tmp_path):
    published = _signed(_build(lab, lab.policy(**POLICY), serial=5))
    spare = _signed(_build(lab, lab.policy(**POLICY), key=T2, published=published), "t2")
    assert "accepted against the published one" in _tree(tmp_path / "a", published, spare)[-1]
    assert "it is the published one" in _tree(tmp_path / "b", spare, spare)[-1]
    later = _signed(_build(lab, lab.policy(**POLICY), key=T2, serial=2), "t2")
    assert "already superseded" in _tree(tmp_path / "c", later, spare)[-1]
    forged = (spare[0].replace(b'"floor": "0.2.0"', b'"floor": "0.1.0"'), spare[1])
    with pytest.raises(Failed, match="does not verify"):
        _tree(tmp_path / "d", published, forged)
    main_after_spare = _signed(_build(lab, lab.policy(**POLICY), serial=6))
    with pytest.raises(Failed, match="would be refused .rank."):
        _tree(tmp_path / "e", spare, main_after_spare)


# ------------------------------------------------------------------------ misc --


def test_the_diff_says_what_changed(lab):
    first = trust.parse_index(_build(lab, lab.policy(**POLICY)))
    lines = make_index.diff(None, first)
    assert lines[0].startswith("Nothing is published yet")
    assert any(line.startswith("- **added 0.2.0**") for line in lines)
    second = dict(first, serial=2, floor="0.1.0",
                  releases=[dict(first["releases"][0], withdrawn="broken")])
    lines = make_index.diff(first, second)
    assert "- serial: 1 -> 2" in lines
    assert "- **floor: 0.2.0 -> 0.1.0**" in lines
    assert "- **0.2.0 withdrawn: None -> 'broken'**" in lines
    assert "- **removed 0.1.0**" in lines


def test_one_release_per_line():
    raw = make_index.render({"schema": 1, "package": trust.PACKAGE, "serial": 1, "issued": 0,
                             "key_id": T1.key_id, "floor": "0.2.0", "releases": []})
    assert raw.endswith(b'  "releases": []\n}\n')
    assert json.loads(raw)["releases"] == []


# ------------------------------------------------------ gh-pages history guard --


def _history(lab):
    return make_index.published_history("gh-pages", KEYS, lab.repo)


def _publish(lab, pair):
    lab.publish(*pair)
    return make_index.published_pair("gh-pages", root=lab.repo)


@needs_openssl
def test_a_rolled_back_or_deleted_published_index_stops_the_build(lab):
    first = _publish(lab, _signed(_build(lab, lab.policy(**POLICY))))
    second = _publish(lab, _signed(_build(lab, lab.policy(**POLICY), published=first,
                                          history=_history(lab))))
    assert json.loads(second[0])["serial"] == 2
    history = _history(lab)
    assert history.count == 2 and history.memory["serials"] == {T1.key_id: 2}
    assert "the highest serial 2 by key" in history.describe()
    # An ordinary commit putting serial 1 back: the next build would be 2 again - a replay.
    rolled_back = _publish(lab, first)
    with pytest.raises(Failed, match="below serial 2, which gh-pages published before"):
        _build(lab, lab.policy(**POLICY), published=rolled_back, history=_history(lab))
    with pytest.raises(Failed, match="rolled back"):
        make_index.verify(*second, KEYS, rolled_back, _history(lab))
    # Deleted: the build would start again at serial 1.
    with pytest.raises(Failed, match="the files were deleted"):
        _build(lab, lab.policy(**POLICY), published=(None, None), history=_history(lab))
    # The recovery the message names: the newest pair back, in a new commit.
    restored = _publish(lab, second)
    index = trust.parse_index(_build(lab, lab.policy(**POLICY), published=restored,
                                     history=_history(lab)))
    assert index["serial"] == 3


@needs_openssl
def test_a_rollback_past_the_spare_stops_the_build(lab):
    main = _publish(lab, _signed(_build(lab, lab.policy(**POLICY))))
    _publish(lab, _signed(_build(lab, lab.policy(**POLICY), key=T2, published=main,
                                 history=_history(lab)), "t2"))
    back = _publish(lab, main)
    with pytest.raises(Failed, match="silenced"):
        _build(lab, lab.policy(**POLICY), published=back, history=_history(lab))


@needs_openssl
def test_history_ignores_what_does_not_verify(lab):
    raw, sig = _signed(_build(lab, lab.policy(**POLICY)))
    lab.publish(raw.replace(b'"floor": "0.2.0"', b'"floor": "0.1.0"'), sig)
    history = _history(lab)
    assert history.count == 0 and history.unverified >= 1
    assert "no signed index has ever been published" in history.describe()


# ------------------------------------------------------- a waiting emergency index --


@needs_openssl
def test_no_index_is_built_past_an_emergency_index_waiting_on_main(lab):
    published = _publish(lab, _signed(_build(lab, lab.policy(**POLICY))))
    spare = _signed(_build(lab, lab.policy(**POLICY), key=T2, published=published), "t2")
    folder = lab.repo / "release-index" / "emergency"
    folder.mkdir(parents=True)
    (folder / "releases.json").write_bytes(spare[0])
    (folder / "releases.json.sig").write_bytes(spare[1])
    with pytest.raises(Failed, match="emergency index .* not published yet"):
        _build(lab, lab.policy(**POLICY), published=published)
    # Once published it is history, and the spare goes on from it.
    published = _publish(lab, spare)
    index = trust.parse_index(_build(lab, lab.policy(**POLICY), key=T2, published=published))
    assert index["serial"] == 2


# ------------------------------------------------------------ the key set's rules --


def _key(name, rank, baseline=0):
    key = indexlab.VECTORS["test_keys"][name]
    return trust.Key(key["key_id"], rank, base64.b64decode(key["public"]), baseline)


@pytest.mark.parametrize("current, words", [
    ((("t1", 1, 5), ("t2", 2)), None),
    ((("t1", 1, 5), ("t2", 2), ("t3", 3)), None),
    ((("t2", 2), ("t3", 3)), None),
    ((("t1", 1, 5), ("t2", 3)), "changed rank 2 -> 3"),
    ((("t1", 1, 0), ("t2", 2)), "lowered its baseline 5 -> 0"),
    ((("t1", 1, 5), ("t3", 3)), "is dropped while"),
    ((("t3", 2),), "not above 2"),
])
def test_a_release_keeps_ranks_adds_higher_keys_and_drops_from_the_bottom(current, words):
    previous = trust.check_keys((_key("t1", 1, 5), _key("t2", 2)))
    problems = make_index.key_evolution_problems(
        previous, trust.check_keys(_key(*item) for item in current))
    if words is None:
        assert problems == []
    else:
        assert any(words in problem for problem in problems), problems


def test_a_new_key_must_rank_above_every_key_before_it():
    previous = trust.check_keys((_key("t1", 1), _key("t3", 3)))
    problems = make_index.key_evolution_problems(previous, trust.check_keys(
        (_key("t1", 1), _key("t3", 3), _key("t2", 2))))
    assert any("new key" in problem and "not above 3" in problem for problem in problems)


def test_the_key_set_is_read_from_trust_py_as_literals():
    text = (REPO_ROOT / "src" / "MQTTBridge" / "trust.py").read_text(encoding="utf-8")
    assert make_index.keys_in_source(text) == trust.EMBEDDED


def test_the_tree_check_holds_the_keys_to_the_last_release_that_carried_them(lab):
    assert make_index.previous_release_keys(lab.repo) == (None, None)
    source = lab.repo / "src" / "MQTTBridge"
    source.mkdir(parents=True)
    (source / "trust.py").write_text(
        (REPO_ROOT / "src" / "MQTTBridge" / "trust.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    lab.release("0.4.0")
    assert make_index.previous_release_keys(lab.repo) == ("v0.4.0", trust.EMBEDDED)
    (lab.repo / "release-index").mkdir()
    (lab.repo / "release-index" / "policy.json").write_text(json.dumps(
        {"schema": 1, "floor": "0.2.0", "withdrawn": {}, "corrections": {}}))
    lines = []
    make_index.tree(lab.repo, trust.EMBEDDED, (None, None), log=lines.append)
    assert "embedded keys: consistent with v0.4.0" in lines
    # A tree that drops the spare but keeps the main key is refused.
    with pytest.raises(Failed, match="is dropped while"):
        make_index.tree(lab.repo, (trust.MAIN,), (None, None), log=lines.append)


# ------------------------------------------------------------------------ misc --


def test_a_release_commit_with_a_non_release_tag_beside_its_tag(lab):
    commit = lab.git("rev-parse", "v0.2.0^{commit}")
    lab.git("tag", "v0.2.0-rc1", commit)
    lab.git("tag", "v0.2", commit)
    assert make_index.tag_for(commit, root=lab.repo) == "0.2.0"


def test_a_missing_file_is_an_answer_not_a_traceback(tmp_path, capsys):
    status = make_index.main(["--published-dir", str(tmp_path), "verify", "--index",
                              str(tmp_path / "gone.json"), "--sig", str(tmp_path / "gone.sig")])
    assert status == 1
    assert "No such file or directory" in capsys.readouterr().err
