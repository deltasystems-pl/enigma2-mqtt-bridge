"""What the television's "Plugin updates" screen and the OpenWebif page say about updates.

One reading of the `update` topic for both places a person can install a release from the
receiver itself: which versions the signed index offers and why some cannot be installed, what
runs now and who started it, how the last update ended, and - for the television - what a
refusal means in the household's language. The page and the screen then differ only in how they
draw it, so the two never tell the same household two different things.

**Nothing here decides anything.** The versions are the `available` list the update check
publishes, judged by the one rule (`updatecheck.offer`); an install is `cmd/update` through the
dispatcher with the origin `screen` or `page` (`selfupdate.py` does every guard). The only
judgement made here is which question to ask first: a version below the running one is a
downgrade, and the question says what it takes away (spec ae.6, ae.8). What counts as older,
and whether the running build is the release it is listed beside, `selfupdate.py` says - the
dispatcher judges a downgrade by the same function, so the question and the answer cannot
disagree.

**A wait is said as a wait.** An install at the television or on the page is accepted before it
starts when the receiver first looks at the release origin, or asks Home Assistant for the
package because the origin does not answer (`selfupdate.py`, the relay handshake). Both places
then say what it waits for (`relay_line`) and, once it ended without an update, why
(`relay_outcome`) - never that an update started. The page reads that outcome whenever it is
loaded, so it says it only for an hour after the wait ended (`RELAY_OUTCOME_SECONDS`); the
screen that asked reads it once, when its wait ends, and keeps it while it is open.

**One line per version on the television.** A list row does not wrap, so a version that cannot
be installed says only that (`row_label`); the reason is `row_detail`, shown when the row is
chosen and listed whole on the page.

**Every string is built when it is asked for**, never at import, so the language is the one the
receiver shows now. The refusal sentences are keyed by the reason codes `cmd/update` and
`cmd/update_check` put on `last_error`: the code is stable, the English sentence there is the
contract's, and a reason this table does not know is shown as that English sentence rather than
hidden.

**Imported with the screen and the page, never later.** While an update has closed the plugin's
doors the files of this directory may be the next release's, so this module imports nothing
lazily: a screen left open under the restart question refreshes from what is already loaded.
"""

import time

from . import buildid, trust
from .i18n import _
from .log import get_logger
from .origin import PAGE, SCREEN
from .selfupdate import household_doors, older
from .version import __version__

LOG = get_logger("updateview")

INSTALLED = "installed"
# The release of the running number while a development build of it runs (spec ae.5, v5.5).
SAME_NUMBER = "same_number"
NEWER = "newer"
OLDER = "older"

# How long after a wait ended without an update `relay_outcome` still says how. The outcome is
# kept until the next install is asked for, which may be days later, and "Home Assistant did
# not answer" read then would be taken for news about now. An hour covers a person who asked
# and comes back to look; after it the page says nothing about that wait, and `last_error`
# still carries the refusal until a command next succeeds (the page's Status shows it).
RELAY_OUTCOME_SECONDS = 60 * 60

# `check_error` codes (`updatecheck.py`, TOPICS.md) by what they mean to the household. The
# rule's own verdicts - `trust.REASONS`, and a relayed payload that is not an index - all say
# "this list is not to be believed", which is spec ae.10's sentence.
_UNREACHABLE = ("unreachable",)
_NOT_DOWNLOADED = ("redirect", "http_error")
_NOT_SAVED_FULL = ("write_failed",)
_NOT_SAVED_BUSY = ("trust_busy",)
_UNREADABLE = ("bad_memory", "bad_keys")
_INTERNAL = ("internal_error",)
_REFUSED = trust.REASONS + ("malformed_relay", "too_large")


def _phase(phase):
    return {
        "downloading": _("downloading"),
        "verifying": _("checking the download"),
        "snapshot": _("saving the current version"),
        "installing": _("installing"),
        "restarting": _("restarting the user interface"),
        "proving": _("checking the new version"),
        "rolling_back": _("putting the previous version back"),
    }.get(phase, phase or "-")


def _started_by(origin):
    return {
        "mqtt": _("over MQTT"),
        "home_assistant": _("from Home Assistant"),
        SCREEN: _("on the television"),
        PAGE: _("on the OpenWebif page"),
        "ssh": _("over SSH"),
    }.get(origin, origin or "-")


def _result(result):
    return {
        "installed": _("installed"),
        "withdrawn_before_restart": _("withdrawn before the restart; the previous version runs"),
        "rolled_back": _("the new version did not start; the previous one is back"),
        "failed": _("failed; the previous version runs"),
        "interrupted": _("interrupted"),
    }.get(result, result or "-")


def _reason(reason):
    return {
        "incompatible": _("does not work with this plugin or with the Home Assistant "
                          "integration"),
        "depends": _("needs a package that is not installed on this receiver"),
    }.get(reason, reason or "-")


def _check_problem(code):
    """The last check's `check_error` in the household's words; an unknown code is named."""
    for codes, sentence in (
        (_UNREACHABLE, lambda: _("The last check could not reach the list of versions on the "
                                 "internet.")),
        (_NOT_DOWNLOADED, lambda: _("The last check could not download the list of versions.")),
        (_REFUSED, lambda: _("The plugin's list of versions has an invalid signature or is "
                             "older than the one already known. Nothing was changed.")),
        (_NOT_SAVED_FULL, lambda: _("The last check could not save the list of versions; the "
                                    "receiver's memory may be full.")),
        (_NOT_SAVED_BUSY, lambda: _("The last check could not save the list of versions. Try "
                                    "again in a moment.")),
        (_UNREADABLE, lambda: _("The receiver's record of the list of versions cannot be read, "
                                "so no list is checked.")),
        (_INTERNAL, lambda: _("The last check failed; the plugin log says why.")),
    ):
        if code in codes:
            return sentence()
    return _("The last check failed (%s).") % code


def _payload(bridge):
    updates = getattr(bridge, "updates", None)
    if updates is None:
        return {}
    try:
        return updates.payload() or {}
    except Exception:
        LOG.exception("the update topic could not be read")
        return {}


def rows(bridge):
    """`(version, relation, reason)` for every version the signed index offers, newest first.

    `relation` is `installed`, `newer` or `older` than the running plugin - or `same_number`, the
    release of the running number while a development build of it runs; `reason` is None for a
    version that can be installed and the rule's reason code for one that cannot.
    """
    found = []
    for entry in _payload(bridge).get("available") or []:
        version = entry.get("version")
        if not trust.is_version(version):
            continue
        if version == __version__:
            relation = INSTALLED if _runs_release(bridge, version) else SAME_NUMBER
        elif older(version):
            relation = OLDER
        else:
            relation = NEWER
        found.append((version, relation, None if entry.get("compatible") else
                      entry.get("reason") or "incompatible"))
    return found


def _runs_release(bridge, version):
    updater = getattr(bridge, "self_update", None)
    if updater is None:
        return True
    try:
        return updater.runs_release(version)
    except Exception:
        LOG.exception("the running build could not be compared with the release")
        return False


def row_label(row):
    """The row as the television's list shows it: one line, which never wraps.

    A version that cannot be installed says only that; why is `row_detail`, which the screen
    shows when the row is chosen and the page lists in full (review round 2: the reasons ran
    past the list's width in every language).
    """
    version, relation, reason = row
    if reason is not None:
        return _("%s - cannot be installed") % version
    if relation == INSTALLED:
        return _("%s - installed") % version
    if relation == SAME_NUMBER:
        return _("%s - release; a development build of it runs") % version
    if relation == OLDER:
        return _("%s - older version") % version
    return _("%s - newer version") % version


def row_detail(row):
    """The row with the whole reason a version cannot be installed, for a box that wraps."""
    version, _relation, reason = row
    if reason is None:
        return row_label(row)
    return _("%(version)s - cannot be installed: %(reason)s") % {
        "version": version, "reason": _reason(reason)}


def installable(bridge):
    """`(version, label)` of every version that can be chosen for an install."""
    return [(row[0], row_label(row)) for row in rows(bridge) if row[2] is None]


def header(bridge):
    """The lines above the list: what runs, which list of versions is known, the last check."""
    build = getattr(bridge, "build", None) if bridge is not None else None
    lines = [_("Installed version: %s") % buildid.display_version(
        __version__, build if build is not None else buildid.LOADED)]
    payload = _payload(bridge)
    index = payload.get("index")
    if isinstance(index, dict) and isinstance(index.get("issued"), int):
        # "via", never "from": that is who started an update, and a language may need two words.
        source = _("via Home Assistant") if index.get("source") == "relay" \
            else _("from the internet")
        lines.append(_("List of versions no. %(serial)s of %(date)s, %(source)s") % {
            "serial": index.get("serial"),
            "date": time.strftime("%Y-%m-%d", time.localtime(index["issued"])),
            "source": source,
        })
    else:
        lines.append(_("No list of versions is known yet. Check for updates first."))
    problem = payload.get("check_error")
    if problem:
        lines.append(_check_problem(problem))
    return lines


def transaction_line(bridge):
    """What runs now and who started it, or how the last update ended; None when neither."""
    updater = getattr(bridge, "self_update", None)
    try:
        record = updater.transaction_payload() if updater is not None else None
    except Exception:
        LOG.exception("the update in progress could not be read")
        record = None
    if not isinstance(record, dict) or not record.get("target"):
        return None
    values = {"target": record["target"], "who": _started_by(record.get("started_by"))}
    if record.get("phase") == "finished":
        values["result"] = _result(record.get("result"))
        return _("Last update to %(target)s, started %(who)s: %(result)s") % values
    values["phase"] = _phase(record.get("phase"))
    return _("Update to %(target)s, started %(who)s: %(phase)s") % values


def doors(bridge):
    """The only sentence the screen and the page may show while an update has closed the doors."""
    updater = getattr(bridge, "self_update", None)
    try:
        closed = bool(updater.closed) if updater is not None else False
    except Exception:
        closed = False
    return household_doors(updater) if closed else None


def relay_line(bridge):
    """What an install at the television or on the page waits for, while it waits; else None.

    The relay handshake (`selfupdate.py`): first a look at whether the release origin answers,
    then - when it does not - Home Assistant's answer, counted down. Both are said as the
    household sees them: the internet, and Home Assistant.
    """
    updater = getattr(bridge, "self_update", None)
    try:
        wait = updater.relay_wait() if updater is not None else None
    except Exception:
        LOG.exception("the install's wait could not be read")
        wait = None
    if not isinstance(wait, dict) or not wait.get("version"):
        return None
    if wait.get("phase") == "probe":
        return _("Checking whether the receiver can reach the internet to download version "
                 "%s.") % wait["version"]
    return _("The receiver has no access to the internet, so it has asked Home Assistant for "
             "version %(version)s. Waiting for the answer: %(seconds)s s left.") % {
        "version": wait["version"], "seconds": wait.get("seconds_left", "-")}


def relay_outcome(bridge):
    """How the last such wait ended when it did not start an update, in the household's words.

    None when it started one, when nothing waited, or when it ended `RELAY_OUTCOME_SECONDS` ago
    or more. The refusal is the dispatcher's own (`SelfUpdater.relay_refusal`): no answer, an
    answer this clock calls expired, or a refusal of the table asked again when the wait ended.
    """
    updater = getattr(bridge, "self_update", None)
    refusal = getattr(updater, "relay_refusal", None) if updater is not None else None
    if not refusal:
        return None
    ended = getattr(updater, "relay_ended", None)
    try:
        if ended is not None and updater.monotonic() - ended >= RELAY_OUTCOME_SECONDS:
            return None
    except Exception:
        # Its age unreadable, the outcome is said: it is still the last wait's, and true.
        LOG.exception("the age of the install's last wait could not be read")
    return household_refusal(refusal, getattr(updater, "relay_version", None))


def question(version):
    """What is asked before an install of `version`: the downgrade question names the loss."""
    if older(version):
        return (_("Install the older version %s? The plugin's newer features will disappear "
                  "until it is updated again.") % version
                + " " + _restart_note())
    return _("Install version %s of the plugin?") % version + " " + _restart_note()


def _restart_note():
    # No duration and no promise about the channel: neither is measured on a receiver yet.
    return _("When the new version is in place the receiver's user interface restarts, and the "
             "picture stops while it does.")


def household_refusal(refusal, version=None):
    """A refusal of `cmd/update` or `cmd/update_check` in the household's language.

    By the reason code the dispatcher carries beside the contract's English sentence; a
    refusal this table does not know - or one with no code at all - is its own sentence.
    """
    reason = getattr(refusal, "reason", None)
    shown = version or "-"
    sentences = {
        "no_capability": _("This copy of the plugin was not installed by the package manager, "
                           "so it cannot update itself."),
        "busy": _("An installation or update of the plugin is already running on the "
                  "receiver."),
        "opkg_busy": _("The receiver's package manager is busy. Try again in a moment."),
        "standby": _("The receiver is in standby. An update restarts the receiver's user "
                     "interface, which wakes it and may switch the television on. Switch the "
                     "receiver on and try again."),
        "recording": _("The receiver is recording or is about to start a recording. Try again "
                       "when the recording has finished."),
        "epg_import": _("An EPG import is running. Try again when it has finished."),
        "cannot_restart": _("The receiver cannot restart its user interface right now. Try "
                            "again in a moment."),
        "unknown_version": _("Version %s is not on the plugin's signed list of versions. Check "
                             "for updates and try again.") % shown,
        "withdrawn": _("Version %s has been withdrawn by the plugin's authors.") % shown,
        "below_floor": _("Version %s is older than the oldest version this plugin can "
                         "install.") % shown,
        "incompatible": _("Version %s does not work with this plugin or with the Home "
                          "Assistant integration.") % shown,
        "depends": _("Version %s needs a package that is not installed on this "
                     "receiver.") % shown,
        "current": _("Version %s is already installed and running.") % shown,
        "no_space": _("There is not enough free space on the receiver."),
        "rate_limited": _("An update ran less than ten minutes ago. Try again later."),
        "internal_error": _("The update could not be started; the plugin log says why."),
        # The relay handshake's two ends without an update (spec ae.6).
        "no_relay": _("The receiver has no access to the internet, and Home Assistant did not "
                      "answer. The installation is not possible."),
        # Only what the receiver knows: an answer came - from whom, nothing on the broker says -
        # with an address its own clock calls expired.
        "clock_skew": _("An answer arrived, but its download address had already expired by "
                        "the receiver's clock. If the receiver's clock is wrong, set it and try "
                        "again."),
    }
    for code in ("recording_due", "recording_unknown"):
        sentences[code] = sentences["recording"]
    return sentences.get(reason, str(refusal))
