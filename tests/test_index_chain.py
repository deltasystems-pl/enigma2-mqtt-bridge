"""The build, sign and publish chain, rehearsed end to end with throwaway keys.

`publish-index.yml` cannot be run from a pull request - its sign job needs the maintainer's approval
and the real key - so this runs what its three jobs run, in their order, against a made-up
repository (`indexlab.Lab`):

- **build**: `make-index.py build` from the releases, the feed and the published index, writing the
  sha256 to the job output file and the summary;
- **precheck**: `make-index.py check` against that sha256, with no key;
- **sign**: **the hash, sign and emit steps' own texts, taken out of publish-index.yml**, run by
  `bash -eo pipefail` with a throwaway seed as `INDEX_SIGNING_KEY` - no repository code;
- **publish**: `make-index.py wrap` on the emitted signature, then `verify`, then the files onto
  the lab's `gh-pages`.

A second round follows the first, and the chain refuses what it must: a replayed or tampered pair,
a wrong key, and the main key after the spare's emergency index. The keys are the vectors' test
keys, never the release keys.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess

import indexlab
import pytest
from indexlab import Lab

pytestmark = pytest.mark.skipif(
    not indexlab.openssl_can_sign() or shutil.which("bash") is None,
    reason="the chain needs bash and OpenSSL 3 (pkeyutl -rawin)",
)

make_index = indexlab.tool("make-index")
rehearse = indexlab.tool("rehearse-signing")
sign_index = indexlab.tool("sign-index")


@pytest.fixture()
def chain(tmp_path):
    lab = Lab(tmp_path / "lab")
    lab.release("0.2.0")
    lab.release("0.3.0", with_buildinfo=True)
    (lab.repo / "release-index").mkdir()
    (lab.repo / "release-index" / "policy.json").write_text(json.dumps(lab.policy("0.2.0")))
    lab.git("add", "-A")
    lab.git("commit", "-q", "-m", "the policy")
    keyset = tmp_path / "keys.json"
    keyset.write_text(json.dumps(indexlab.keyset_of(("t1", 1), ("t2", 2))))
    return lab, keyset


def _cli(lab, keyset, *args):
    common = ["--root", str(lab.repo), "--keyset", str(keyset), "--published-ref", "gh-pages"]
    return make_index.main([*common, *args])


def _round(lab, keyset, work, seed_name="t1"):
    """build -> check -> sign step -> wrap -> verify -> push, as the three jobs do."""
    work.mkdir()
    (work / "index").mkdir()
    output, summary = work / "output", work / "summary.md"
    assert _cli(lab, keyset, "build", "--releases-json", str(lab.releases_json()),
                "--assets-dir", str(lab.assets), "--out", str(work / "index" / "releases.json"),
                "--summary", str(summary), "--github-output", str(output)) == 0
    sha256 = output.read_text().strip().split("=", 1)[1]
    assert f"**sha256 of the unsigned index: `{sha256}`**" in summary.read_text()
    index_path = work / "index" / "releases.json"
    assert hashlib.sha256(index_path.read_bytes()).hexdigest() == sha256

    assert _cli(lab, keyset, "check", "--index", str(index_path), "--sha256", sha256) == 0
    # sign: the hash, sign and emit steps' own texts, as the key-holding job runs them - no
    # repository code there.
    code, out = rehearse.run_hash_step(work, sha256)
    assert code == 0, out
    code, out = rehearse.run_sign_step(work, indexlab.seed_of(seed_name))
    assert code == 0, out
    assert sorted(path.name for path in (work / "index").iterdir()) == [
        "releases.json", "releases.json.raw-sig"]
    code, out = rehearse.run_emit_step(work, work / "github-output")
    assert code == 0, out
    # publish: decode what sign handed on, verify it with the embedded key, accept the pair.
    handed_on = work / "handed-on.raw-sig"
    handed_on.write_bytes(rehearse.emitted_signature(work / "github-output"))
    assert _cli(lab, keyset, "wrap", "--index", str(index_path), "--raw-signature",
                str(handed_on), "--out", str(work / "index" / "releases.json.sig")) == 0

    assert _cli(lab, keyset, "verify", "--index", str(index_path), "--sig",
                str(work / "index" / "releases.json.sig"), "--sha256", sha256) == 0
    lab.publish(index_path.read_bytes(), (work / "index" / "releases.json.sig").read_bytes())
    return json.loads(index_path.read_bytes())


def test_two_rounds_publish_serials_1_and_2(chain, tmp_path):
    lab, keyset = chain
    first = _round(lab, keyset, tmp_path / "one")
    assert first["serial"] == 1
    assert [entry["version"] for entry in first["releases"]] == ["0.3.0", "0.2.0"]
    lab.release("0.4.0", with_buildinfo=True)
    second = _round(lab, keyset, tmp_path / "two")
    assert second["serial"] == 2
    assert [entry["version"] for entry in second["releases"]] == ["0.4.0", "0.3.0", "0.2.0"]
    # What gh-pages serves now is what a reader accepts.
    published = (lab.git("show", "gh-pages:feed/releases.json"),
                 lab.git("show", "gh-pages:feed/releases.json.sig"))
    assert json.loads(published[0])["serial"] == 2
    assert json.loads(published[1])["key_id"] == first["key_id"]


def test_the_sign_job_refuses_a_file_that_is_not_the_one_build_hashed(chain, tmp_path, capsys):
    lab, keyset = chain
    _round(lab, keyset, tmp_path / "one")
    work = tmp_path / "two"
    work.mkdir()
    out, github = work / "releases.json", work / "output"
    assert _cli(lab, keyset, "build", "--releases-json", str(lab.releases_json()),
                "--assets-dir", str(lab.assets), "--out", str(out),
                "--github-output", str(github)) == 0
    sha256 = github.read_text().strip().split("=", 1)[1]
    # Swapped between the jobs: the same shape, one byte different.
    out.write_bytes(out.read_bytes().replace(b'"floor": "0.2.0"', b'"floor": "0.3.0"'))
    assert _cli(lab, keyset, "check", "--index", str(out), "--sha256", sha256) == 1
    assert "not " + sha256 + ", the one build reported" in capsys.readouterr().err


def test_publish_refuses_a_replay_a_tampered_pair_and_a_wrong_secret(chain, tmp_path):
    lab, keyset = chain
    first_dir = tmp_path / "one"
    _round(lab, keyset, first_dir)
    index, sig = first_dir / "index" / "releases.json", first_dir / "index" / "releases.json.sig"
    # The published pair again: a replay.
    assert _cli(lab, keyset, "verify", "--index", str(index), "--sig", str(sig)) == 1
    # A wrong secret: the sign step signs, wrap refuses.
    work = tmp_path / "wrong"
    work.mkdir()
    (work / "index").mkdir()
    lab.release("0.4.0")
    assert _cli(lab, keyset, "build", "--releases-json", str(lab.releases_json()),
                "--assets-dir", str(lab.assets), "--out", str(work / "index" / "releases.json")
                ) == 0
    code, _out = rehearse.run_sign_step(work, indexlab.seed_of("t3"))
    assert code == 0
    assert _cli(lab, keyset, "wrap", "--index", str(work / "index" / "releases.json"),
                "--raw-signature", str(work / "index" / "releases.json.raw-sig"),
                "--out", str(work / "index" / "releases.json.sig")) == 1
    assert not (work / "index" / "releases.json.sig").exists()


def test_an_empty_secret_stops_the_sign_step(tmp_path):
    (tmp_path / "index").mkdir()
    (tmp_path / "index" / "releases.json").write_bytes(b"{}\n")
    code, out = rehearse.run_sign_step(tmp_path, b"")
    assert code == 1
    assert b"has no INDEX_SIGNING_KEY" in out
    assert not (tmp_path / "index" / "releases.json.raw-sig").exists()


def test_a_broken_pipe_fails_the_step_instead_of_signing_with_half_a_key(tmp_path):
    # `shell: bash` means -eo pipefail: a seed that does not decode must fail the step, not hand
    # OpenSSL whatever arrived.
    (tmp_path / "index").mkdir()
    (tmp_path / "index" / "releases.json").write_bytes(b"{}\n")
    step = rehearse._tool("check-workflows").sign_step_script()
    work = tmp_path
    script = work / "s.sh"
    script.write_text(step)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=work,
        env={"PATH": os.environ["PATH"], "INDEX_SIGNING_KEY": "not base64 at all!"},
        capture_output=True, check=False)
    assert result.returncode != 0


def test_the_emergency_path_publishes_a_spare_signed_index_once(chain, tmp_path):
    lab, keyset = chain
    first_dir = tmp_path / "one"
    _round(lab, keyset, first_dir)
    # Offline, with the spare: build naming the spare, sign with sign-index.py.
    emergency = tmp_path / "emergency"
    emergency.mkdir()
    spare_id = indexlab.VECTORS["test_keys"]["t2"]["key_id"]
    index = emergency / "releases.json"
    assert _cli(lab, keyset, "build", "--releases-json", str(lab.releases_json()),
                "--assets-dir", str(lab.assets), "--key-id", spare_id, "--out", str(index)) == 0
    seed_file = emergency / "seed"
    seed_file.write_text(base64.b64encode(indexlab.seed_of("t2")).decode() + "\n")
    assert sign_index.main(["--index", str(index), "--key-file", str(seed_file),
                            "--keyset", str(keyset)]) == 0
    sig = emergency / "releases.json.sig"
    assert json.loads(sig.read_bytes())["key_id"] == spare_id
    # What emergency-index.yml runs, then the push.
    assert _cli(lab, keyset, "verify", "--index", str(index), "--sig", str(sig)) == 0
    lab.publish(index.read_bytes(), sig.read_bytes())
    assert _cli(lab, keyset, "verify", "--index", str(index), "--sig", str(sig)) == 1
    # And from now on the main key cannot publish.
    with pytest.raises(make_index.IndexError_, match="for good"):
        make_index.build(lab.source(), lab.policy("0.2.0"),
                         make_index.load_keys(str(keyset)),
                         indexlab.VECTORS["test_keys"]["t1"]["key_id"], root=lab.repo,
                         published=make_index.published_pair("gh-pages", root=lab.repo),
                         feed_ref="gh-pages")


def test_the_offline_signer_refuses_a_seed_that_is_not_the_named_key(chain, tmp_path, capsys):
    lab, keyset = chain
    index = tmp_path / "releases.json"
    assert _cli(lab, keyset, "build", "--releases-json", str(lab.releases_json()),
                "--assets-dir", str(lab.assets), "--out", str(index)) == 0
    seed_file = tmp_path / "seed"
    seed_file.write_text(base64.b64encode(indexlab.seed_of("t2")).decode())
    assert sign_index.main(["--index", str(index), "--key-file", str(seed_file),
                            "--keyset", str(keyset)]) == 1
    assert "the secret is not that key" in capsys.readouterr().err
    assert not (tmp_path / "releases.json.sig").exists()
    seed_file.write_text("short")
    assert sign_index.main(["--index", str(index), "--key-file", str(seed_file),
                            "--keyset", str(keyset)]) == 1


def test_the_rehearsal_passes_here():
    lines = []
    rehearse.rehearse(log=lines.append)
    assert lines[-1].startswith("rehearsal passed with OpenSSL")
    assert any("by another key: refused" in line for line in lines)
