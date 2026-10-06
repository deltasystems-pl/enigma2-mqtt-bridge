"""What is on, across a bouquet - one retained topic per bouquet.

The grid is the one payload in this project that can reach tens of kilobytes, so
everything here is about not doing it all at once and not doing it often.

**One bouquet per turn of the main loop.** A bouquet of two hundred channels is
two hundred EPG lookups, and doing every bouquet in one call would hold the
thread that draws the television for as long as that takes. Each bouquet is
generated on its own timer tick and the time it took is logged, so the cost is
measurable on any box rather than assumed from this one.

**A slug is an address.** `enigma2/<node>/epg_grid/ulubione_tv` is derived from
the bouquet's name so that a consumer can subscribe to one bouquet, and the
payload carries the real name because that is what a person should read. Rename
a bouquet and the old slug is retracted; that is the same retained-ghost trap
the discovery payloads have, answered the same way.

**A rebuild that finds the same television publishes nothing.** `generated` is
stamped afresh on every pass and is declared `volatile` so that it cannot be the
only thing that differs. Against a live guide a quarter of an hour usually does
move something, so this is not a claim about most passes; it matters where the
guide is static - a bouquet whose channels carry no EPG, an overnight window, an
import that has failed - and there it is unbounded, because a grid is the
largest payload here and nothing would ever stop it being rewritten.
"""

import time

from .channels import slugify
from .enigma2 import Ticker, enigma_attribute, identity
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("epggrid")

REFRESH_MILLISECONDS = 15 * 60 * 1000

# One bouquet per tick, with a gap long enough for the user interface to draw a
# frame in between.
STEP_MILLISECONDS = 20
CHANNELS_PER_STEP = 4

# How far ahead to ask for. Enough that a channel showing long films still fills
# its quota, bounded so that a query cannot come back with a week of television.
MINUTES_PER_EVENT = 90
EXTRA_MINUTES = 60

# The order of these characters is the order of the fields in every row that
# comes back: service reference, event id, begin, duration, title.
QUERY_FIELDS = "RIBDT"

# enigma2's query type for „events in a time window".
QUERY_BY_TIME = 0

# Log a warning when one bouquet takes longer than this to build.
SLOW_MILLISECONDS = 200


def epg_cache():
    factory = enigma_attribute("eEPGCache")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        LOG.exception("eEPGCache.getInstance() raised")
        return None


def _rows(cache, srefs, minutes, begin=-1):
    """One `lookupEvent` for a whole bouquet, or one per channel if that fails.

    The multi-service form is a single call into the EPG cache for every channel
    in the bouquet, which is why it is worth trying first. Not every image has
    answered it the same way, so a `None` - enigma2's way of saying „I did not
    understand the query" - falls back to asking per channel.
    """
    query = [QUERY_FIELDS]
    for sref in srefs:
        query.append((sref, QUERY_BY_TIME, begin, minutes))
    try:
        rows = cache.lookupEvent(query)
    except Exception:
        LOG.debug("a multi-service EPG query was refused; asking per channel")
        rows = None
    if rows is not None:
        return rows

    rows = []
    for sref in srefs:
        try:
            found = cache.lookupEvent([QUERY_FIELDS, (sref, QUERY_BY_TIME, begin, minutes)])
        except Exception:
            LOG.debug("no EPG for %s", sref)
            continue
        rows.extend(found or [])
    return rows


def _event(row):
    """One row of a lookup as (sref, event), or (None, None) when it is not usable."""
    try:
        sref = str(row[0] or "")
        begin = int(row[2] or 0)
        duration = int(row[3] or 0)
        return sref, {
            "title": str(row[4] or ""),
            "begin": begin,
            "end": begin + duration,
            "event_id": int(row[1] or 0),
        }
    except (IndexError, TypeError, ValueError):
        return None, None


def build(bouquet, count):
    """The payload for one bouquet: every channel, up to `count` events each."""
    channels = bouquet.get("channels") or []
    payload = {
        "bouquet": bouquet.get("name"),
        "generated": int(time.time()),
        "events_per_channel": count,
        "channels": [
            {"sref": channel["sref"], "name": channel.get("name"), "events": []}
            for channel in channels
        ],
    }
    if not channels or count <= 0:
        return payload

    cache = epg_cache()
    if cache is None:
        return payload

    by_sref = {}
    for entry in payload["channels"]:
        by_sref.setdefault(entry["sref"], entry)

    minutes = count * MINUTES_PER_EVENT + EXTRA_MINUTES
    for row in _rows(cache, [entry["sref"] for entry in payload["channels"]], minutes) or []:
        sref, event = _event(row)
        if event is None:
            continue
        entry = by_sref.get(sref)
        if entry is None:
            # enigma2 may spell the reference back differently from the way the
            # bouquet spelled it; fall back to the identity comparison rather
            # than dropping the row.
            wanted = identity(sref)
            for candidate in payload["channels"]:
                if identity(candidate["sref"]) == wanted:
                    entry = candidate
                    break
        if entry is None or len(entry["events"]) >= count:
            continue
        entry["events"].append(event)

    for entry in payload["channels"]:
        entry["events"].sort(key=lambda event: event["begin"])
    return payload


def _build_state(bouquet, count):
    """A complete payload shell and the cursor used by the live, yielding build."""
    channels = bouquet.get("channels") or []
    payload = {
        "bouquet": bouquet.get("name"),
        "generated": int(time.time()),
        # How many events a channel may carry here. The setting, until the grid
        # has to be cut to fit one packet (`EpgGridPublisher._fitted`).
        "events_per_channel": count,
        "channels": [
            {"sref": channel["sref"], "name": channel.get("name"), "events": []}
            for channel in channels
        ],
    }
    by_sref = {}
    by_identity = {}
    for entry in payload["channels"]:
        # Preserve build()'s first-match behaviour for duplicate bouquet rows.
        by_sref.setdefault(entry["sref"], entry)
        by_identity.setdefault(identity(entry["sref"]), entry)
    return {
        "bouquet": bouquet,
        "count": count,
        "payload": payload,
        "by_sref": by_sref,
        "by_identity": by_identity,
        "cursor": 0,
        "began": time.time(),
    }


def _add_rows(state, rows):
    for row in rows or []:
        sref, event = _event(row)
        if event is None:
            continue
        entry = state["by_sref"].get(sref) or state["by_identity"].get(identity(sref))
        if entry is None or len(entry["events"]) >= state["count"]:
            continue
        entry["events"].append(event)


def _finish_state(state):
    for entry in state["payload"]["channels"]:
        entry["events"].sort(key=lambda event: event["begin"])
    return state["payload"]


# What an event takes in a grid's JSON besides its title and the digits of its
# three numbers: the punctuation and the keys, and the comma in front of it.
EVENT_LEAST = len(',{"begin":,"end":,"event_id":,"title":""}')


def _least_added(payload, count):
    """The least a grid's JSON grows by when a channel may carry one event more than `count`.

    Counted, not encoded: for each channel that has another event, the title's
    characters, the digits of its numbers and the fixed text around them. The
    encoded form can only be longer - a character is at least a byte, and an
    escape adds to it - so this is a floor, good for saying that something
    cannot fit and for nothing else.
    """
    added = 0
    for entry in payload["channels"]:
        events = entry["events"]
        if len(events) > count:
            event = events[count]
            added += (
                EVENT_LEAST + len(event["title"]) + len(str(event["begin"]))
                + len(str(event["end"])) + len(str(event["event_id"]))
            )
    return added


def _cut(payload, count):
    """A finished grid with at most `count` events on each channel - the earliest."""
    cut = dict(payload)
    cut["events_per_channel"] = count
    cut["channels"] = [
        dict(entry, events=entry["events"][:count]) for entry in payload["channels"]
    ]
    return cut


class EpgGridPublisher(Publisher):
    """`epg_grid/<bouquet_slug>` - one topic for each configured bouquet."""

    name = "epg_grid"

    # When the grid was built is not whether the grid changed, and the builder
    # runs on a timer. Comparing this would republish every bouquet on every
    # pass with nothing on television having moved.
    volatile = ("generated",)

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._slugs = []
        self._payloads = {}
        # slug -> the events per channel a grid was last cut to, which is where
        # the next pass starts looking (`_fitted`).
        self._cuts = {}
        self._queue = []
        self._current = None
        self._generation = 0
        self._completed_slugs = []
        self._active = False
        self._started_at = 0.0
        self._refresh = Ticker(self.regenerate, "epg grid refresh")
        self._step = Ticker(self._next_bouquet, "epg grid step")

    # ------------------------------------------------------------------ hooks --

    def start(self):
        if self.events_per_channel <= 0:
            self.switched_off = True
            LOG.info("epg_grid_events is 0; the EPG grid is switched off")
            return False
        if epg_cache() is None:
            return False
        if self._channels() is None:
            LOG.info("the EPG grid needs the channel list, which did not start")
            return False
        self._active = True
        self._channels().when_changed(self.regenerate)
        self.regenerate()
        self._refresh.start(REFRESH_MILLISECONDS)
        return True

    def stop(self):
        self._active = False
        self._generation += 1
        self._queue = []
        self._current = None
        self._refresh.stop()
        self._step.stop()

    @property
    def events_per_channel(self):
        try:
            return int(self.value("epg_grid_events") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def published_slugs(self):
        return list(self._slugs)

    def _channels(self):
        if self.bridge is None:
            return None
        return self.bridge.publisher("channels")

    # ------------------------------------------------------------- generation --

    def regenerate(self):
        """Rebuild every configured bouquet in small, yielding channel batches."""
        channels = self._channels()
        if channels is None:
            return 0
        self._generation += 1
        self._queue = list(channels.bouquets)
        self._current = None
        self._completed_slugs = []
        self._started_at = time.time()
        if not self._queue:
            self._finished()
            return 0
        self._step.start(STEP_MILLISECONDS, True)
        return len(self._queue)

    def _next_bouquet(self):
        if not self._active:
            return
        generation = self._generation
        if self._current is None and not self._queue:
            self._finished()
            return
        if self._current is None:
            self._current = _build_state(self._queue.pop(0), self.events_per_channel)
        state = self._current
        payload_channels = state["payload"]["channels"]
        start = state["cursor"]
        stop = min(start + CHANNELS_PER_STEP, len(payload_channels))
        if start < stop and state["count"] > 0:
            srefs = [entry["sref"] for entry in payload_channels[start:stop]]
            minutes = state["count"] * MINUTES_PER_EVENT + EXTRA_MINUTES
            try:
                rows = _rows(epg_cache(), srefs, minutes)
            except Exception:
                LOG.exception("could not build an EPG grid batch")
                rows = []
            if generation != self._generation or state is not self._current:
                return
            _add_rows(state, rows)
        state["cursor"] = stop
        if stop < len(payload_channels):
            self._step.start(STEP_MILLISECONDS, True)
            return

        payload = _finish_state(state)
        self._current = None
        bouquet = state["bouquet"]
        slug = slugify(bouquet.get("name"))
        if slug:
            payload, encoded = self._fitted(slug, payload)
            # The fitted payload is the one kept, so a connect republishes it
            # as it is and nothing is fitted again there.
            self._payloads[slug] = payload
            if slug not in self._completed_slugs:
                self._completed_slugs.append(slug)
            self.publish("epg_grid/" + slug, payload, encoded)
        # After the fitting, so that what it cost is in the line below.
        elapsed = int((time.time() - state["began"]) * 1000)
        events = sum(len(entry["events"]) for entry in payload["channels"])
        line = LOG.warning if elapsed >= SLOW_MILLISECONDS else LOG.info
        line(
            "epg grid: %s took %d ms for %d channel(s), %d event(s)",
            bouquet.get("name"), elapsed, len(payload["channels"]), events,
        )
        if self._queue:
            self._step.start(STEP_MILLISECONDS, True)
            return
        self._finished()

    def _fitted(self, slug, payload):
        """`(payload, encoded)`: this bouquet's grid with as many events as one packet takes.

        A grid whose packet would be over the bridge's bound is cut to fewer
        events per channel - the earliest of each channel's list, which is
        already built and in order, so the EPG cache is not asked again - and
        the largest count that fits is what is published, with
        `events_per_channel` saying which. Only a grid that does not fit with
        one event on each channel is handed over too big, for the bridge to
        withhold as it withholds anything else.

        Measuring is encoding, and a grid near the bound is about a megabyte of
        JSON, so the encodings are counted. A grid that fits is encoded once,
        and that JSON is the one published. The first time one does not, the
        count is searched by halves - the size only falls as events are cut -
        which is at most five more with the setting at its highest, 20, and
        two with the default of 4.

        After that the search starts from the count the bouquet was last cut
        to, because a guide a quarter of an hour older is nearly the same
        guide. That count is tried first and usually fits: one encoding, the
        same as for a grid that was never too big, and the whole grid is not
        encoded at all. Whether one event more would fit is first asked
        without encoding - the JSON just measured, plus the least the next
        event of each channel can add (`_least_added`) - and only when that
        leaves room is the next count encoded to find out, which is when the
        guide has thinned out or the titles got shorter, not at every pass.
        If it fits, the counts above it are searched by halves; if the last
        count itself no longer fits, the ones below it are. Either way what is
        published is the largest count that fits.

        This runs in the pass's own timer step, every quarter of an hour for
        one bouquet at a time, and never on a connect, which republishes the
        payload fitted here.
        """
        if self.bridge is None:
            return payload, None
        suffix = "epg_grid/" + slug
        wanted = payload["events_per_channel"]
        measure = self.bridge.measure
        seed = self._cuts.get(slug)
        best = smallest = None
        if seed is None or seed >= wanted:
            encoded, size = measure(suffix, payload)
            if not size:
                self._cuts.pop(slug, None)
                return payload, encoded
            smallest = (payload, encoded)
            low, high = 1, wanted - 1
        else:
            trial = _cut(payload, seed)
            encoded, size = measure(suffix, trial)
            if size:
                smallest = (trial, encoded)
                low, high = 1, seed - 1
            else:
                best = (trial, encoded)
                low, high = seed + 1, wanted
                floor = len(encoded) + _least_added(payload, seed)
                if self.bridge.exceeds(suffix, floor):
                    # One event more cannot fit, so nothing above it can.
                    high = seed
                else:
                    trial = _cut(payload, seed + 1)
                    encoded, size = measure(suffix, trial)
                    if size:
                        high = seed
                    else:
                        best = (trial, encoded)
                        low = seed + 2
        while low <= high:
            middle = (low + high) // 2
            trial = _cut(payload, middle)
            encoded, size = measure(suffix, trial)
            if size:
                smallest = (trial, encoded)
                high = middle - 1
            else:
                best = (trial, encoded)
                low = middle + 1
        if best is None:
            # Too big with one event on each channel. The smallest one measured
            # goes to the bridge, which keeps it off the wire and says so. The
            # cut is forgotten with it: fitting again later is a new cut.
            self._cuts.pop(slug, None)
            return smallest
        count = best[0]["events_per_channel"]
        if count >= wanted:
            # The guide has thinned out and the whole grid fits again.
            self._cuts.pop(slug, None)
            return best
        if self._cuts.get(slug) != count:
            # When the cut starts or moves, not at every pass.
            LOG.warning(
                "epg grid: %s is cut from %d to %d event(s) per channel to fit one packet",
                payload.get("bouquet"), wanted, count,
            )
        self._cuts[slug] = count
        return best

    def _finished(self):
        if self._current is not None or self._queue:
            return
        completed = set(self._completed_slugs)
        self._payloads = {
            slug: payload for slug, payload in self._payloads.items() if slug in completed
        }
        self._slugs = list(self._completed_slugs)
        # A cut belongs to a bouquet; one that is gone takes its own with it.
        self._cuts = {slug: count for slug, count in self._cuts.items() if slug in completed}
        total = int((time.time() - self._started_at) * 1000)
        LOG.info("epg grid: %d bouquet(s) in %d ms", len(self._slugs), total)
        if self.bridge is not None:
            # Whatever was published under a slug that is no longer configured
            # is retracted here - a renamed bouquet is a new slug and an old one.
            self.bridge.sync_grid_slugs()

    def snapshot(self):
        return {"epg_grid/" + slug: payload for slug, payload in self._payloads.items()}
