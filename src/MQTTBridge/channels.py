"""The bouquets and what is in them.

This is the only part of the plugin that reads a list rather than an event, and
it is read for three different consumers: the `channels` topic, the `bouquet`
field of `service`, and `cmd/zap` by name. One cache serves all three, because
walking every bouquet costs real time on a receiver and doing it three times
would cost three times as much.

**The list changes without anything telling you.** Bouquets are files, edited by
the user, by an editor over the network, or by a channel-scan - this box's list
went from 36 entries to 11 in half an hour once. There is no event. So the
modification times of `bouquets.tv` and every `userbouquet.*` are compared once
a minute, which is cheap, and the walk only happens when one of them moved.

**Markers are not channels.** A bouquet holds separators and headings as well as
services, and they come back from the same call. A marker published as a channel
becomes an option in a select box that cannot be tuned.
"""

import os
import re
import time
import unicodedata

from .enigma2 import (
    Ticker,
    enigma_attribute,
    identity,
    missing,
    service_name,
    service_reference,
)
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("channels")

# The service-type filter in front of a bouquet reference. The list of numbers
# differs between images and between releases of the same image - high-definition,
# ultra-high-definition and several operator-specific types were each added to it
# at some point - so it is imported from the receiver's own channel selection
# screen and this literal is only the fallback.
SERVICE_TYPES_TV = "1:7:1:0:0:0:0:0:0:0:(type == 1) || (type == 17) || (type == 22)" \
                   " || (type == 25) || (type == 31) || (type == 134) || (type == 195)"

BOUQUET_SOURCE = 'FROM BOUQUET "bouquets.tv" ORDER BY bouquet'

# A box with „multiple bouquets" switched off has no bouquet list at all - it
# has one favourites list, and asking for `bouquets.tv` answers with nothing.
FAVOURITES_SOURCE = 'FROM BOUQUET "userbouquet.favourites.tv" ORDER BY bouquet'
FAVOURITES_NAME = "Favourites (TV)"

BOUQUET_DIRECTORY = "/etc/enigma2"
BOUQUET_FILES = ("bouquets.tv",)
BOUQUET_PREFIX = "userbouquet."

POLL_MILLISECONDS = 60000

# One retained topic for each bouquet, beside `channels`: its size follows the
# largest bouquet where `channels` follows the sum of all of them.
LIST_PREFIX = "channels/"

# The index of those topics: every bouquet with the slug its topic has and how
# many services it holds. Small whatever the receiver, so it is there when
# `channels` is too big to send.
INDEX = "bouquets"

# Claimed with `channels`: the index and the per-bouquet topics are published.
LISTS_CAPABILITY = "channel_topics"


# eServiceReference flags, by value, because the names are only importable on a
# receiver and the numbers are part of the reference's string form anyway: they
# are the second colon-separated field, which is why `1:64:` is the spelling of
# a marker line everybody recognises.
FLAG_IS_DIRECTORY = 1
FLAG_MUST_DESCENT = 2
FLAG_IS_MARKER = 64
FLAG_IS_NUMBERED_MARKER = 256
FLAG_IS_INVISIBLE = 512

NOT_A_CHANNEL = (
    FLAG_IS_DIRECTORY | FLAG_MUST_DESCENT | FLAG_IS_MARKER
    | FLAG_IS_NUMBERED_MARKER | FLAG_IS_INVISIBLE
)

_NOT_ALLOWED = re.compile(r"[^a-z0-9]+")

# What decomposition will not take apart. Polish `ł` has no combining form, and
# the German sharp s expands to two letters rather than losing an accent.
TRANSLITERATIONS = {
    "ł": "l",
    "Ł": "L",
    "ß": "ss",
    "đ": "d",
    "Đ": "D",
    "ø": "o",
    "Ø": "O",
    "æ": "ae",
    "Æ": "AE",
}


def slugify(name):
    """A bouquet name as an MQTT topic segment: „Ulubione TV" -> `ulubione_tv`.

    A slug is an address, never a label - the payload carries the original name
    and that is what a person should be shown. Two bouquets whose names differ
    only in punctuation would slug the same; the second one to be published wins
    the topic, which is visible in the payload's `bouquet` field rather than
    silent.
    """
    text = "".join(TRANSLITERATIONS.get(character, character) for character in str(name or ""))
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NOT_ALLOWED.sub("_", stripped.lower()).strip("_")


def topic_slugs(names):
    """The slug of each bouquet's own channel topic, in bouquet order.

    The slug of the name, and "" where the name leaves none. Where two names
    slug the same, the first bouquet keeps the slug and each later one gets
    `_2`, `_3` and so on behind it - the first number from 2 that no bouquet's
    own slug and no number already given out has taken, so a bouquet that is
    really called "Sport HD 2" keeps `sport_hd_2` wherever it stands in the
    list. Sharing one topic would leave the list of all but one of them
    nowhere, once `channels` has had to drop its lists.

    The EPG grid does not use this: it slugs each name by itself.
    """
    natural = [slugify(name) for name in names]
    taken = {slug for slug in natural if slug}
    seen = set()
    slugs = []
    for slug in natural:
        if slug and slug in seen:
            number = 2
            while slug + "_" + str(number) in taken:
                number += 1
            slug = slug + "_" + str(number)
            taken.add(slug)
        seen.add(slug)
        slugs.append(slug)
    return slugs


# The fewest colon-separated fields anything resembling a service reference has.
MINIMUM_FIELDS = 4


def _flags(sref):
    """The flags field of a service reference, or None when there is not one."""
    parts = str(sref or "").split(":")
    if len(parts) < MINIMUM_FIELDS:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def is_playable(sref):
    """A real service, rather than a marker, a separator, a sub-bouquet or a hidden entry.

    Every one of these comes back from the same call as the channels do. A
    marker published as a channel becomes an option in a select box that cannot
    be tuned, and a hidden service is hidden because somebody chose to hide it.
    """
    flags = _flags(sref)
    if flags is None:
        return False
    return not flags & NOT_A_CHANNEL


def _service_center():
    factory = enigma_attribute("eServiceCenter")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        LOG.exception("eServiceCenter.getInstance() raised")
        return None


def _list_entries(handler, sref):
    """[(reference, name), ...] for a bouquet or for the bouquet list itself.

    `getContent("SN", True)` is enigma2's own way of asking for the string form
    of each reference and the name beside it, sorted as the user ordered them.
    """
    reference = service_reference(sref)
    if reference is None or handler is None:
        return []
    try:
        listing = handler.list(reference)
    except Exception:
        LOG.exception("could not list %s", sref)
        return []
    if listing is None:
        return []
    try:
        content = listing.getContent("SN", True)
    except Exception:
        LOG.exception("could not read the content of %s", sref)
        return []
    entries = []
    for row in content or []:
        try:
            entries.append((str(row[0]), str(row[1] or "")))
        except (IndexError, TypeError):
            continue
    return entries


def service_types_tv():
    """The receiver's own television service-type filter, or this plugin's copy."""
    try:
        from Screens.ChannelSelection import service_types_tv as types

        if types:
            return str(types)
    except Exception:
        LOG.debug("this image does not export service_types_tv; using the built-in list")
    return SERVICE_TYPES_TV


def bouquet_roots():
    """Where to look for bouquets, in order of preference."""
    prefix = service_types_tv() + " "
    return (prefix + BOUQUET_SOURCE, prefix + FAVOURITES_SOURCE)


def read_bouquets(wanted=None):
    """`(root, bouquets)` - which root answered, and the bouquets it holds.

    `wanted` is the `bouquets_for_select` setting already split into names. An
    empty selection means every bouquet - which is what a box that has never
    been configured should publish, rather than nothing.

    The root is returned rather than assumed, because the one that answered is
    not always the first one asked: a box with „multiple bouquets" switched off
    has no `bouquets.tv` at all, and everything published here then came out of
    the favourites list. Anything that later wants to *enter* one of these
    bouquets has to enter it under the root it was read from.
    """
    handler = _service_center()
    if handler is None:
        missing("eServiceCenter")
        return None, []
    selection = [name.strip().lower() for name in (wanted or []) if name.strip()]
    slugs = {slugify(name) for name in selection}

    root, favourites = bouquet_roots()
    listed = _list_entries(handler, root)
    if not listed:
        # No bouquet list: this box keeps one favourites list instead, which is
        # what „multiple bouquets" being switched off means.
        LOG.info("no bouquet list on this box; reading the favourites list instead")
        listed = [(favourites, FAVOURITES_NAME)]
        root = favourites

    bouquets = []
    for sref, name in listed:
        if selection and name.strip().lower() not in selection and slugify(name) not in slugs:
            continue
        channels = [
            {"sref": service, "name": label or service_name(service)}
            for service, label in _list_entries(handler, sref)
            if is_playable(service)
        ]
        bouquets.append({"name": name, "sref": sref, "channels": channels})
    return root, bouquets


def selection_from(setting):
    """The `bouquets_for_select` setting as a list of names.

    Comma-separated, because a bouquet name can contain almost anything else -
    including spaces, brackets and a colon.
    """
    return [part.strip() for part in str(setting or "").split(",") if part.strip()]


def bouquet_mtimes(directory=BOUQUET_DIRECTORY):
    """What the bouquet files look like right now, as one comparable value."""
    stamps = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return ()
    for name in names:
        if name not in BOUQUET_FILES and not name.startswith(BOUQUET_PREFIX):
            continue
        try:
            stamps.append((name, os.path.getmtime(os.path.join(directory, name))))
        except OSError:
            continue
    return tuple(stamps)


class ChannelsPublisher(Publisher):
    """`channels` - the bouquets a consumer may offer, and their services.

    Picons are not published. A picon is a file on the box, it is tens of
    kilobytes each, and a consumer that wants them has OpenWebif on the same
    address.
    """

    name = "channels"

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._bouquets = []
        self._root = None
        self._generated = 0
        self._mtimes = ()
        self._index = {}
        # The size of the packet `channels` would need, when the last walk
        # found that over the bound, else 0. Kept until the next walk, so a
        # connect does not build megabytes of JSON to learn it again.
        self._too_big = 0
        # (body, stamp): the JSON of the index's `bouquets`, and when it last
        # differed from the walk before.
        self._index_part = None
        # The slug of each bouquet's own topic, beside `_bouquets`, and what
        # was last logged about the ones that had to be numbered.
        self._slugs = []
        self._collisions = ()
        # slug -> (head, tail, stamp): the JSON of that bouquet's topic in two
        # parts, between which `generated` goes, and when the two last differed
        # from the walk before (`_encode_lists`).
        self._parts = {}
        self._ticker = Ticker(self._poll, "bouquets")
        self._on_change = []

    # ------------------------------------------------------------------ hooks --

    def start(self):
        if _service_center() is None:
            return False
        self.refresh()
        self._ticker.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._ticker.stop()

    def when_changed(self, callback):
        """Tell somebody - the EPG grid - that the bouquets moved."""
        self._on_change.append(callback)

    def extra_capabilities(self):
        """`channel_topics`: the index, and each bouquet's list on a topic of its own."""
        return [LISTS_CAPABILITY]

    def prepared(self, suffix, payload):
        """The JSON of a bouquet's own topic, and the key that says whether it differs.

        Both come from the parts the walk encoded, so neither a publish nor a
        connect encodes a list again. The key is the payload without its
        stamp: on `channels/<bouquet_slug>` `generated` takes no part in the
        comparison, or a change to one bouquet would rewrite the retained list
        of every other. The index, `bouquets`, is answered the same way. On
        `channels` the stamp does count, as it always has.
        """
        if suffix == INDEX and self._index_part is not None:
            body, stamp = self._index_part
            return '{"bouquets":' + body + ',"generated":' + str(stamp) + "}", body
        if not suffix.startswith(LIST_PREFIX):
            return None, None
        part = self._parts.get(suffix[len(LIST_PREFIX):])
        if part is None:
            return None, None
        head, tail, stamp = part
        return head + ',"generated":' + str(stamp) + tail, (head, tail)

    # ------------------------------------------------------------------ reading --

    def _selection(self):
        return selection_from(self.value("bouquets_for_select"))

    def refresh(self, force=True):
        """Walk the bouquets. Returns the payload, or None when nothing changed."""
        mtimes = bouquet_mtimes()
        if not force and mtimes == self._mtimes:
            return None
        self._mtimes = mtimes
        self._root, self._bouquets = read_bouquets(self._selection())
        self._generated = int(time.time())
        self._index = {}
        for bouquet in self._bouquets:
            for channel in bouquet["channels"]:
                self._index.setdefault(identity(channel["sref"]), bouquet["name"])
        self._slugs = topic_slugs([bouquet["name"] for bouquet in self._bouquets])
        self._log_collisions()
        payload = self._publish_channels()
        self._encode_lists()
        self.publish(INDEX, self.index())
        for suffix, one in self._lists().items():
            self.publish(suffix, one)
        if self.bridge is not None:
            # A bouquet that was renamed, removed or deselected has a list of
            # its own on the broker that nothing above replaced.
            self.bridge.sync_channel_slugs()
        for callback in list(self._on_change):
            try:
                callback()
            except Exception:
                LOG.exception("a bouquet-change listener raised")
        return payload

    def _poll(self):
        if self.refresh(force=False) is not None:
            LOG.info("the bouquets changed; the channel list was republished")

    def _publish_channels(self):
        """Publish `channels` - every bouquet with its list, as it has always been.

        The bridge measures it first, and the JSON it measured is the JSON it
        publishes, so a list that fits is encoded once. One that does not is
        withheld by the bridge like any payload over the bound; what is kept
        here is the size it measured, so that a connect can say "still too
        big" without encoding megabytes to find out (`too_big`). The lists are
        on `channels/<bouquet_slug>` either way, and `bouquets` says where.
        """
        payload = self.payload()
        if self.bridge is None:
            return payload
        encoded, self._too_big = self.bridge.measure("channels", payload)
        self.publish("channels", payload, encoded)
        return payload

    def payload(self):
        return {"generated": self._generated, "bouquets": self._bouquets}

    def too_big(self):
        """{suffix: packet bytes} for what the last walk measured over the bound.

        The snapshot leaves such a topic out and the bridge withholds it from
        this, exactly as it would from a measurement of its own - the same
        payload on the same topic has the same size.
        """
        return {"channels": self._too_big} if self._too_big else {}

    def index(self):
        """What `bouquets` carries: each bouquet's name, reference, topic slug and size."""
        stamp = self._generated if self._index_part is None else self._index_part[1]
        return {
            "generated": stamp,
            "bouquets": [
                {
                    "name": bouquet["name"],
                    "sref": bouquet["sref"],
                    "slug": slug,
                    "count": len(bouquet["channels"]),
                }
                for slug, bouquet in zip(self._slugs, self._bouquets)
            ],
        }

    def _log_collisions(self):
        """Say which bouquets had their slug numbered - when that changes, not at every walk."""
        collisions = tuple(
            (bouquet["name"], slug)
            for slug, bouquet in zip(self._slugs, self._bouquets)
            if slug != slugify(bouquet["name"])
        )
        if collisions != self._collisions:
            for name, slug in collisions:
                LOG.warning(
                    "the bouquet %s slugs the same as an earlier one; its channel list is"
                    " on channels/%s", name, slug,
                )
        self._collisions = collisions

    def _encode_lists(self):
        """Encode each bouquet's list once, and stamp the ones that differ.

        The JSON of `channels/<bouquet_slug>` is put together here by hand, in
        the key order and with the separators of the bridge's own encoder, so
        that the megabyte in it - the list - is encoded once a walk and never
        on a connect: `prepared` joins the parts around `generated`, and the
        parts without it are the change key. A test holds the result equal to
        what the encoder makes of the whole payload.

        A bouquet whose parts are what the walk before made of it keeps its
        stamp, so `generated` says when that bouquet's list last differed -
        within the life of this publisher, which a start of the plugin or a
        settings save begins anew. The index's `bouquets` is encoded here as
        well and stamped by the same rule.
        """
        parts = {}
        if self.bridge is not None:
            encode = self.bridge.encode
            body = encode(self.index()["bouquets"])
            if self._index_part is None or self._index_part[0] != body:
                self._index_part = (body, self._generated)
            for slug, bouquet in zip(self._slugs, self._bouquets):
                if not slug:
                    continue
                head = (
                    '{"bouquet":' + encode(bouquet["name"])
                    + ',"channels":' + encode(bouquet["channels"])
                )
                tail = ',"sref":' + encode(bouquet["sref"]) + "}"
                before = self._parts.get(slug)
                if before is not None and before[:2] == (head, tail):
                    parts[slug] = before
                else:
                    parts[slug] = (head, tail, self._generated)
        self._parts = parts

    def _lists(self):
        """{suffix: payload} - `channels/<bouquet_slug>` for each bouquet.

        A bouquet whose name leaves no slug has no topic, as it has no grid.
        `generated` is the bouquet's own stamp (`_encode_lists`).
        """
        topics = {}
        for slug, bouquet in zip(self._slugs, self._bouquets):
            if not slug:
                continue
            part = self._parts.get(slug)
            topics[LIST_PREFIX + slug] = {
                "bouquet": bouquet["name"],
                "sref": bouquet["sref"],
                "generated": self._generated if part is None else part[2],
                "channels": bouquet["channels"],
            }
        return topics

    @property
    def published_slugs(self):
        """The slugs of the `channels/<bouquet_slug>` topics, in bouquet order."""
        return [slug for slug in self._slugs if slug]

    @property
    def bouquets(self):
        return list(self._bouquets)

    @property
    def root(self):
        """The service-list root these bouquets were read from, or None.

        Whoever activates one of them has to enter it under this root and not
        under „the first one we would have tried": on a box with no bouquet
        list the two are different, and entering the wrong one builds a path
        the receiver then persists.
        """
        return self._root

    def snapshot(self):
        if not self._bouquets:
            self.refresh()
        topics = {} if self._too_big else {"channels": self.payload()}
        topics[INDEX] = self.index()
        topics.update(self._lists())
        return topics

    # ---------------------------------------------------------------- lookups --

    def bouquet_for(self, sref):
        """Which of the configured bouquets a service came from, or None."""
        return self._index.get(identity(sref))

    def find_by_name(self, name):
        """(sref, error) for `cmd/zap` by name.

        Refused unless exactly one service matches. A household has „Sport" four
        times over, on four satellites, and tuning to whichever one came first
        would be a coin toss nobody asked for. The error names the count so the
        person reading `last_error` knows to be more specific rather than
        thinking the channel is missing.
        """
        wanted = str(name or "").strip().lower()
        if not wanted:
            return None, "no channel name given"
        matches = []
        for bouquet in self._bouquets:
            for channel in bouquet["channels"]:
                if str(channel.get("name") or "").strip().lower() == wanted:
                    if not any(identity(channel["sref"]) == identity(found) for found in matches):
                        matches.append(channel["sref"])
        if not matches:
            return None, "no channel called '" + str(name) + "' in the configured bouquets"
        if len(matches) > 1:
            return None, (
                "service name '" + str(name) + "' is not unique (" + str(len(matches)) + " matches)"
            )
        return matches[0], None
