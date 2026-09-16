"""The announcement, the record of what is retained, and retraction.

A retained topic belongs to the broker and outlives whatever created it. Rename
a node, drop a component, uninstall the plugin — and the old payload sits there
forever while a consumer keeps an entity nothing will ever update again. The
only cure is to publish an empty retained payload to the exact topic, which
means the plugin has to remember every topic it has ever published, across
restarts. That is what the state file is for, and it is why it is written before
it is needed rather than after.

The Home Assistant discovery payloads themselves arrive in the next milestone.
`build_discovery_components` is here now, returning nothing, so that the code
that retracts stale components is exercised from the first release rather than
written at the moment it is first needed.
"""

import json
import os
import tempfile

from .log import get_logger

LOG = get_logger("discovery")

ANNOUNCE_PREFIX = "enigma2mqtt/discovery"

STATE_FILE_NAME = "mqttbridge-state.json"
STATE_DIRECTORY = "/etc/enigma2"
STATE_VERSION = 1

ANNOUNCEMENT_FIELDS = (
    "image",
    "enigma",
    "plugin",
    "boxtype",
    "mac",
    "ip",
    "capabilities",
    "ha_mode",
)


def announcement_topic(node_id):
    return ANNOUNCE_PREFIX + "/" + str(node_id) + "/config"


def build_announcement(info, node_id, friendly_name, base_topic):
    """What a consumer needs to find this box without being told where to look."""
    payload = {
        "node_id": node_id,
        "name": friendly_name or node_id,
        "base_topic": base_topic,
    }
    for field in ANNOUNCEMENT_FIELDS:
        payload[field] = info.get(field)
    return payload


def build_discovery_components(node_id, friendly_name, base_topic, info):
    """Home Assistant discovery payloads, as {topic: payload}.

    Empty in this release: the plugin publishes no entity state yet, and an
    announced entity that nothing updates is worse than no entity at all.
    """
    return {}


def default_state_path(plugin_directory=None):
    """Beside enigma2's own settings, or beside the plugin when that is read-only."""
    if os.path.isdir(STATE_DIRECTORY) and os.access(STATE_DIRECTORY, os.W_OK):
        return os.path.join(STATE_DIRECTORY, STATE_FILE_NAME)
    directory = plugin_directory or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(directory, STATE_FILE_NAME)


class StateStore:
    """What this node has published retained, and what it has announced."""

    def __init__(self, path=None):
        self.path = path or default_state_path()
        self._topics = set()
        self._components = []
        self._dirty = False
        self.load()

    # ------------------------------------------------------------- persistence --

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except OSError:
            return False
        except ValueError as error:
            LOG.warning("%s is not readable JSON (%s); starting from empty", self.path, error)
            return False
        if not isinstance(raw, dict):
            return False
        topics = raw.get("retained_topics") or []
        components = raw.get("discovery_components") or []
        self._topics = {t for t in topics if isinstance(t, str)}
        self._components = [c for c in components if isinstance(c, str)]
        self._dirty = False
        LOG.debug("state: %d retained topic(s) remembered from %s", len(self._topics), self.path)
        return True

    def save(self, force=False):
        """Atomic: a half-written state file would be worse than a missing one."""
        if not self._dirty and not force:
            return True
        payload = {
            "version": STATE_VERSION,
            "retained_topics": sorted(self._topics),
            "discovery_components": list(self._components),
        }
        directory = os.path.dirname(self.path) or "."
        handle = None
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".mqttbridge-state.", dir=directory)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(temporary, self.path)
            temporary = None
        except OSError as error:
            LOG.warning("could not write %s (%s)", self.path, error)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            if temporary is not None:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
            return False
        self._dirty = False
        return True

    # ------------------------------------------------------------------ topics --

    @property
    def retained_topics(self):
        return sorted(self._topics)

    def knows(self, topic):
        return topic in self._topics

    def remember(self, topic):
        if not topic or topic in self._topics:
            return False
        self._topics.add(topic)
        self._dirty = True
        return True

    def forget(self, topic):
        if topic not in self._topics:
            return False
        self._topics.discard(topic)
        self._dirty = True
        return True

    def forget_all(self):
        if self._topics or self._components:
            self._topics.clear()
            self._components = []
            self._dirty = True

    # -------------------------------------------------------------- components --

    @property
    def components(self):
        return list(self._components)

    def set_components(self, topics):
        new = [t for t in topics if isinstance(t, str)]
        if new != self._components:
            self._components = new
            self._dirty = True
