"""The announcement, the Home Assistant discovery payloads, and retraction.

A retained topic belongs to the broker and outlives whatever created it. Rename
a node, drop a component, uninstall the plugin — and the old payload sits there
forever while a consumer keeps an entity nothing will ever update again. The
only cure is to publish an empty retained payload to the exact topic, which
means the plugin has to remember every topic it has ever published, across
restarts. That is what the state file is for.

The discovery payloads are written against what Home Assistant 2026.9 actually
accepts, which was read out of its source rather than out of its documentation.
Four things it does that are worth knowing before changing anything here:

* **An unknown key at the top level of the device payload drops every component
  in it.** The top level is validated strictly; inside a component, unknown keys
  are quietly discarded instead. So a typo in one place costs an entity and in
  the other costs the whole device.
* **`object_id` is gone.** The key that decides an entity's id is
  `default_entity_id` (`def_ent_id`), it wants a full `domain.object` string
  even though the domain half is thrown away, and it is read **once**, when the
  entity is created. Changing it later renames nothing.
* **Leaving a component out of a republished payload does not remove it.** The
  documented removal is the component reduced to nothing but its platform key,
  `{"p": "sensor"}`, which is why this module is told what it announced last
  time.
* **The null state is the literal string `None`.** A `value_template` over a
  JSON `null` renders exactly that, so a field that is null on the wire becomes
  an unknown state for free — but only if the key exists. An *absent* key
  renders an empty string, which for a numeric sensor means „ignore this
  message" and leaves the old value on screen. Hence `| default(none)`.
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

REPOSITORY = "https://github.com/deltasystems-pl/enigma2-mqtt-bridge"
ORIGIN_NAME = "enigma2-mqtt-bridge"

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

# Who made a box, worked out from the machine name. Wrong is worse than absent
# only if it is confidently wrong, so anything not on this list is „Enigma2"
# rather than a guess at a brand.
MANUFACTURERS = (
    ("vu", "Vu+"),
    ("gb", "GigaBlue"),
    ("dm", "Dream Multimedia"),
    ("et", "Xtrend"),
    ("zgemma", "Zgemma"),
    ("h9", "Zgemma"),
    ("formuler", "Formuler"),
    ("osmio", "Edision"),
    ("osmini", "Edision"),
    ("osnino", "Edision"),
    ("axas", "AX"),
    ("sf8", "Octagon"),
    ("sf4", "Octagon"),
    ("mutant", "Mutant"),
    ("xtrend", "Xtrend"),
)
DEFAULT_MANUFACTURER = "Enigma2"


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


def manufacturer(boxtype):
    name = str(boxtype or "").strip().lower()
    for prefix, brand in MANUFACTURERS:
        if name.startswith(prefix):
            return brand
    return DEFAULT_MANUFACTURER


def device_topic(prefix, node_id):
    return str(prefix).strip("/") + "/device/" + str(node_id) + "/config"


def trigger_topic(prefix, node_id, name):
    return str(prefix).strip("/") + "/device_automation/" + str(node_id) + "/" + name + "/config"


def component_platforms(payload):
    """{component key: platform} out of a device payload, for the state file.

    A component consisting of nothing but its platform is a *removal* rather
    than a component, so it is not recorded: remembering it would mean
    publishing the same removal on every connect for the life of the box.
    """
    components = (payload or {}).get("cmps") or {}
    found = {}
    for key, component in components.items():
        component = component or {}
        platform = component.get("p")
        if platform and len(component) > 1:
            found[key] = platform
    return found


def _entity_id(platform, slug, key):
    """The `default_entity_id` value: a full entity id, whose domain is ignored.

    The domain half is thrown away by Home Assistant, which keeps only what
    follows the dot — but a value with **no** dot leaves nothing to keep and the
    entity is created as `<platform>.unnamed_device`. So the domain is always
    written out even though nothing reads it.
    """
    return platform + "." + slug + "_" + key


def _kibibytes(field):
    """A kB field rendered as MiB, and rendered as unknown when it is not there.

    The arithmetic has to be guarded. A `value_template` over a JSON `null`
    renders the literal string `None` for free — but only where the template
    does nothing to the value first; `null / 1024` is a template *error*, and a
    template error is not an unknown state, it is the previous reading staying
    on screen for ever with a line in the log nobody reads.

    `| default(none)` before the test so that an absent key and an explicit null
    take the same branch: an absent key would otherwise be Undefined, which is
    not `none` and would go down the arithmetic path after all.
    """
    return (
        "{% set kb = value_json." + field + " | default(none) %}"
        "{{ (kb / 1024) | round(1) if kb is not none else none }}"
    )


def build_discovery_components(node_id, friendly_name, base_topic, info, prefix="homeassistant",
                               channel_options=(), deep_standby_allowed=False, previous=None):
    """Home Assistant discovery payloads, as {topic: payload}.

    One device-based payload carrying every component, and eight device
    triggers, which are separate topics because that is what the contract in
    `docs/TOPICS.md` promises the companion integration. (They would in fact
    ride inside the device payload on this version of Home Assistant — that was
    measured, not assumed — but the contract is what the other half of this
    project is written against, and one retained topic saved is not worth
    breaking it.)

    Every component is gated on a capability, so a box whose image did not
    provide the volume hooks gets no volume entity rather than one that never
    moves.
    """
    from .channels import slugify

    capabilities = set(info.get("capabilities") or ())
    slug = slugify(friendly_name or node_id) or str(node_id)
    root = str(base_topic).strip("/") + "/" + str(node_id)

    builder = _Components(node_id, slug, root, capabilities)
    builder.add_all(channel_options, deep_standby_allowed)
    components = builder.finish(previous)

    payload = {
        "dev": _device_block(node_id, friendly_name, info),
        "o": {"name": ORIGIN_NAME, "sw": info.get("plugin"), "url": REPOSITORY},
        # Shared with every component that has a use for them; a component may
        # override either. Commands are QoS 1 by the contract, and this is the
        # only place a consumer can be told so.
        "avty_t": root + "/availability",
        "qos": 1,
        "cmps": components,
    }

    topics = {device_topic(prefix, node_id): payload}
    topics.update(_triggers(prefix, node_id, friendly_name, root, capabilities))
    return topics


def _device_block(node_id, friendly_name, info):
    block = {
        "ids": [str(node_id)],
        "name": friendly_name or str(node_id),
        "mf": manufacturer(info.get("boxtype")),
        "mdl": info.get("boxtype") or "Enigma2",
        "sw": str(info.get("image") or "") + " / MQTT Bridge " + str(info.get("plugin") or ""),
    }
    address = str(info.get("ip") or "").strip()
    if address:
        # Only with an address: Home Assistant validates this as a URL and
        # `http:///` would be rejected — taking the whole device with it.
        block["cu"] = "http://" + address + "/"
    return block


class _Components:
    """The `cmps` block, assembled one entity at a time."""

    def __init__(self, node_id, slug, root, capabilities):
        self.node_id = node_id
        self.slug = slug
        self.root = root
        self.capabilities = capabilities
        self.components = {}

    def topic(self, suffix):
        return self.root + "/" + suffix

    def add(self, key, platform, needs, **fields):
        if needs is not None and needs not in self.capabilities:
            return None
        component = {"p": platform, "uniq_id": str(self.node_id) + "_" + key,
                     "def_ent_id": _entity_id(platform, self.slug, key)}
        component.update(fields)
        self.components[key] = component
        return component

    def finish(self, previous):
        """Add the removals: what was announced last time and is not here now.

        A component simply left out of a republished payload is *kept* by Home
        Assistant, not removed — so turning the screenshots off, or moving a box
        to an image without the volume hooks, would leave an entity behind
        forever. The documented removal is the component cut down to nothing but
        its platform.
        """
        for key, platform in sorted((previous or {}).items()):
            if key not in self.components:
                LOG.info("retracting the %s component; it is not announced any more", key)
                self.components[key] = {"p": platform}
        return self.components

    # --------------------------------------------------------------- the entities --

    def add_all(self, channel_options, deep_standby_allowed):
        self.add(
            "power", "switch", "power",
            name="Power",
            stat_t=self.topic("power"),
            cmd_t=self.topic("cmd/power"),
            pl_on="on",
            pl_off="standby",
            ic="mdi:television",
        )
        self.add(
            "channel", "sensor", "service",
            name="Channel",
            stat_t=self.topic("service"),
            val_tpl="{{ value_json.name | default(none) }}",
            json_attr_t=self.topic("service"),
            json_attr_tpl=(
                "{{ {'sref': value_json.sref, 'bouquet': value_json.bouquet, "
                "'provider': value_json.provider, 'width': value_json.width, "
                "'height': value_json.height} | tojson }}"
            ),
            ic="mdi:television-classic",
        )
        self.add(
            "program", "sensor", "epg",
            name="Programme",
            stat_t=self.topic("epg"),
            # `value_json.now.title` on a null `now` is a template error, and a
            # template error leaves the previous programme on screen forever.
            val_tpl="{{ value_json.now.title if value_json.now else none }}",
            json_attr_t=self.topic("epg"),
            json_attr_tpl=(
                "{% set n = value_json.now %}{% set x = value_json.next %}"
                "{{ {'begin': n.begin if n else none, 'end': n.end if n else none, "
                "'event_id': n.event_id if n else none, 'short': n.short if n else none, "
                "'long': n.long if n else none, 'next_title': x.title if x else none, "
                "'next_begin': x.begin if x else none, 'next_end': x.end if x else none} "
                "| tojson }}"
            ),
            ic="mdi:play-box",
        )
        self.add(
            "next_program", "sensor", "epg",
            name="Next programme",
            stat_t=self.topic("epg"),
            val_tpl="{{ value_json.next.title if value_json.next else none }}",
            ic="mdi:skip-next",
        )
        self.add(
            "recording", "binary_sensor", "recording",
            name="Recording",
            stat_t=self.topic("recording"),
            val_tpl="{{ 'ON' if value_json.active else 'OFF' }}",
            dev_cla="running",
        )
        self.add(
            "active_recordings", "sensor", "recording",
            name="Active recordings",
            stat_t=self.topic("recording"),
            val_tpl="{{ value_json.active | count }}",
            ic="mdi:record-rec",
        )
        self.add(
            "next_timer", "sensor", "recording",
            name="Next timer",
            stat_t=self.topic("recording"),
            # A timestamp sensor will not take epoch seconds and will not take a
            # time without a zone; this renders the one shape it accepts.
            val_tpl=(
                "{% if value_json.next %}"
                "{{ value_json.next.begin | int | timestamp_utc }}+00:00"
                "{% else %}None{% endif %}"
            ),
            dev_cla="timestamp",
        )
        self.add(
            "volume", "number", "volume",
            name="Volume",
            stat_t=self.topic("volume"),
            cmd_t=self.topic("cmd/volume"),
            val_tpl="{{ value_json.level }}",
            min=0,
            max=100,
            step=1,
            mode="slider",
            ic="mdi:volume-high",
        )
        self.add(
            "mute", "switch", "volume",
            name="Mute",
            stat_t=self.topic("volume"),
            cmd_t=self.topic("cmd/mute"),
            val_tpl="{{ 'ON' if value_json.muted else 'OFF' }}",
            ic="mdi:volume-mute",
        )
        if channel_options:
            self.add(
                "channel_select", "select", "channels",
                name="Channel list",
                stat_t=self.topic("service"),
                cmd_t=self.topic("cmd/zap"),
                val_tpl="{{ value_json.name | default(none) }}",
                # `tojson` supplies the quotes as well as the escaping: a
                # channel called 4" News would otherwise publish broken JSON.
                cmd_tpl='{"name": {{ value | tojson }}}',
                ops=list(channel_options),
            )
        self.add(
            "screen", "image", "screenshot",
            name="Screen",
            img_t=self.topic("screen"),
            cont_type="image/jpeg",
        )
        self.add(
            "screenshot", "button", "screenshot",
            name="Take a screenshot",
            cmd_t=self.topic("cmd/screenshot"),
            ic="mdi:camera",
        )
        self.add(
            "restart_gui", "button", None,
            name="Restart the user interface",
            cmd_t=self.topic("cmd/restart_gui"),
            ent_cat="config",
            ic="mdi:restart",
        )
        self.add(
            "refresh_discovery", "button", None,
            name="Refresh discovery",
            cmd_t=self.topic("cmd/discovery"),
            ent_cat="config",
            ic="mdi:refresh",
        )
        if deep_standby_allowed:
            # Only when the box has been told it may: a button that is always
            # refused is a button somebody will press twice and then report.
            self.add(
                "deep_standby", "button", None,
                name="Deep standby",
                cmd_t=self.topic("cmd/deep_standby"),
                ent_cat="config",
                ic="mdi:power-off",
            )
            self.add(
                "reboot", "button", None,
                name="Reboot",
                cmd_t=self.topic("cmd/reboot"),
                ent_cat="config",
                ic="mdi:restart-alert",
            )
        for key, name, unit in (
            ("snr", "Signal quality", "%"),
            ("agc", "Signal strength", "%"),
            ("ber", "Bit error rate", None),
        ):
            fields = {
                "name": name,
                "stat_t": self.topic("tuner"),
                "val_tpl": "{{ value_json." + key + " | default(none) }}",
                "ent_cat": "diagnostic",
                "stat_cla": "measurement",
                # A signal reading is for the evening somebody rings up about
                # rain fade, not for every dashboard.
                "en": False,
            }
            if unit:
                fields["unit_of_meas"] = unit
            self.add(key, "sensor", "tuner", **fields)
        self.add(
            "recording_disk", "binary_sensor", "hdd",
            name="Recording disk",
            stat_t=self.topic("hdd"),
            val_tpl="{{ 'ON' if value_json.mounted else 'OFF' }}",
            dev_cla="connectivity",
            ent_cat="diagnostic",
        )
        # What the enigma2 process costs. Diagnostic, and all but the resident
        # set disabled by default: the one a dashboard ever wants is the curve
        # of how much memory the box is using, and the other four are what
        # somebody enables for a fortnight when that curve turns upwards.
        self.add(
            "process_memory", "sensor", "process",
            name="Process memory",
            stat_t=self.topic("process"),
            # kB on the wire, MiB on screen — and the division is guarded,
            # because dividing a JSON `null` is a template error and a template
            # error leaves the last reading on screen for ever. `| default(none)`
            # first, so an absent key and an explicit null take the same branch.
            val_tpl=_kibibytes("rss_kb"),
            unit_of_meas="MiB",
            dev_cla="data_size",
            stat_cla="measurement",
            ent_cat="diagnostic",
            ic="mdi:memory",
        )
        self.add(
            "process_memory_peak", "sensor", "process",
            name="Process memory peak",
            stat_t=self.topic("process"),
            val_tpl=_kibibytes("hwm_kb"),
            unit_of_meas="MiB",
            dev_cla="data_size",
            stat_cla="measurement",
            ent_cat="diagnostic",
            ic="mdi:memory",
            en=False,
        )
        self.add(
            "process_threads", "sensor", "process",
            name="Process threads",
            stat_t=self.topic("process"),
            val_tpl="{{ value_json.threads | default(none) }}",
            stat_cla="measurement",
            ent_cat="diagnostic",
            ic="mdi:cog-outline",
            en=False,
        )
        self.add(
            "process_open_files", "sensor", "process",
            name="Process open files",
            stat_t=self.topic("process"),
            val_tpl="{{ value_json.fds | default(none) }}",
            stat_cla="measurement",
            ent_cat="diagnostic",
            ic="mdi:file-multiple",
            en=False,
        )
        self.add(
            "process_started", "sensor", "process",
            name="Process started",
            stat_t=self.topic("process"),
            # Same shape as `next_timer`: a timestamp sensor takes neither epoch
            # seconds nor a time without a zone.
            val_tpl=(
                "{% if value_json.started %}"
                "{{ value_json.started | int | timestamp_utc }}+00:00"
                "{% else %}None{% endif %}"
            ),
            dev_cla="timestamp",
            ent_cat="diagnostic",
            en=False,
        )
        self.add(
            "uptime", "sensor", None,
            name="Uptime",
            stat_t=self.topic("info"),
            val_tpl="{{ value_json.uptime | default(none) }}",
            dev_cla="duration",
            unit_of_meas="s",
            ent_cat="diagnostic",
        )


def _triggers(prefix, node_id, friendly_name, root, capabilities):
    """The colour keys, as device triggers in the automation editor.

    Eight topics, because a trigger is identified by its own topic and because
    the contract says eight. Availability is not offered: the trigger schema has
    no concept of it and would drop the key silently.
    """
    from .keys import COLOUR_KEYS, PRESS_LONG, PRESS_SHORT

    if "keys" not in capabilities:
        return {}
    device = {"ids": [str(node_id)], "name": friendly_name or str(node_id)}
    topics = {}
    for key in COLOUR_KEYS:
        colour = key.replace("KEY_", "").lower()
        for press in (PRESS_SHORT, PRESS_LONG):
            name = colour + "_" + press
            topics[trigger_topic(prefix, node_id, name)] = {
                "atype": "trigger",
                "type": "button_" + press + "_press",
                "stype": colour,
                "t": root + "/key",
                "val_tpl": "{{ value_json.key }}_{{ value_json.press }}",
                "pl": key + "_" + press,
                "dev": device,
            }
    return topics


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
        self._component_keys = {}
        self._grid_slugs = []
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
        slugs = raw.get("epg_grid_slugs") or []
        keys = raw.get("component_platforms") or {}
        self._topics = {t for t in topics if isinstance(t, str)}
        self._components = [c for c in components if isinstance(c, str)]
        self._grid_slugs = [s for s in slugs if isinstance(s, str)]
        self._component_keys = {
            k: v for k, v in keys.items() if isinstance(k, str) and isinstance(v, str)
        } if isinstance(keys, dict) else {}
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
            "component_platforms": dict(self._component_keys),
            "epg_grid_slugs": list(self._grid_slugs),
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
        if self._topics or self._components or self._grid_slugs or self._component_keys:
            self._topics.clear()
            self._components = []
            self._component_keys = {}
            self._grid_slugs = []
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

    @property
    def component_keys(self):
        """{component key: platform} as last announced.

        Retracting a *topic* is not enough for device-based discovery: the
        components all live in one payload, and one left out of a republished
        payload is kept rather than removed. Removing it means naming it, which
        means remembering it.
        """
        return dict(self._component_keys)

    def set_component_keys(self, mapping):
        new = {k: v for k, v in (mapping or {}).items() if isinstance(k, str)}
        if new != self._component_keys:
            self._component_keys = new
            self._dirty = True

    # --------------------------------------------------------------- grid slugs --

    @property
    def grid_slugs(self):
        """Which `epg_grid/<slug>` topics this node has published.

        The second shape of the retained-ghost trap: drop a bouquet from the
        configuration, or rename one, and the topic it used to own would go on
        being served by the broker forever. Remembering the slugs is what makes
        that retractable from a process that has been restarted since.
        """
        return list(self._grid_slugs)

    def set_grid_slugs(self, slugs):
        new = [s for s in slugs if isinstance(s, str)]
        if new != self._grid_slugs:
            self._grid_slugs = new
            self._dirty = True
