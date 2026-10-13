"""What only the receiver knows about streaming its channels.

Every enigma2 image runs a stream server: a client that asks
`http://<receiver>:<port>/<service reference>` is sent that service's transport
stream. This module carries no video and starts no stream. It publishes the
facts a consumer needs to use that server well, and takes two commands:

- `stream` - where the server is and what it wants: the port it listens on,
  the form of the path, whether it asks for a login, and whether the receiver
  has a hardware encoder. Never a credential: whether a login is asked for is
  a boolean, and the name and the password stay on the receiver.
- `stream_clients` - who is being served right now, from the image's own list.
- `cmd/stream_stop` - end one client's stream, the one named by its host and
  its service (ADR-0018).
- `cmd/stream_playable` - whether one service could be played now, beside
  everything the receiver is already doing, answered once on a topic that is
  not retained (ADR-0017).

**Polled, not hooked.** A client that connects or leaves raises no event the
three images share - OpenATV has a signal, the other two have none - so the
image's list is read every five seconds, as the volume is reconciled. The look
is one call that returns a short list; names are resolved and a payload built
only when that list changed (`zaphistory.py` does the same). The timer is this
publisher's own, as every polled area's is: the plugin has no shared tick, and
another area's timer goes when that area does not bind - a receiver without a
volume control would then list no clients.

**A list that cannot be read is taken off the broker.** The topic is retained,
and a consumer offers to stop what it lists. So what is there is the list the
image holds now or nothing: the first publish after every connect is a fresh
read, and when the read fails the topic is emptied rather than left saying
what it said before.

**The image's host, not ours, is what stops a client.** The server compares the
host it was given with the one it holds, character for character, and it may
hold an IPv4 client as `::ffff:192.0.2.20`. The topic shows the address without
that prefix; the command takes it either way, finds the client in the image's
list and hands the image its own spelling back.

**A row's name and bouquet are the channel list's.** They are resolved when the
image's list changes, not on every look - and again when the channel list says
it was walked (`ChannelsPublisher.when_changed`), because that is when they can
differ for the same client: the stream area starts before the first walk, and
a bouquet can be renamed while somebody is watching.

**What the image raises is named by its type.** The text of an exception from
the stream server can hold a client's address, and `last_error` is retained:
the sentence carries the kind of error, the log has it at warning level, and
the text is written at debug level only.
"""

import os
import time

from .channels import is_playable as is_one_service
from .enigma2 import (
    Ticker,
    enigma_attribute,
    same_service,
    service_name,
    service_reference,
)
from .log import get_logger
from .publisher import Publisher, Refusal

LOG = get_logger("stream")

TOPIC = "stream"
CLIENTS_TOPIC = "stream_clients"
PLAYABLE_TOPIC = "stream_playable"

CLIENTS_CAPABILITY = "stream_clients"
STOP_CAPABILITY = "stream_stop"
PLAYABLE_CAPABILITY = "stream_playable"

POLL_MILLISECONDS = 5000

# A stopped client's connection ends a moment after the image said yes
# (measured: about 1.4 s). The list is read again then, so the topic answers
# the command without waiting for the next look.
STOP_REFRESH_MILLISECONDS = 2000

# The port the stream server listens on. It is not a setting: every image
# hard-codes it where the server is constructed, in `lib/dvb/streamserver.cpp` -
# `eStreamServer::eStreamServer() : eServerSocket(8001, eApp)` in OpenViX
# (`Developer`) and OpenPLi (`scarthgap`), and the same call with a second
# member behind it in OpenATV (`master`). `config.OpenWebif.streamport` is
# another thing - the port OpenWebif writes into the links it hands out, which
# may be one forwarded from elsewhere - and is neither read nor published.
LISTEN_PORT = 8001

# What a client asks for: the service reference, as it is published, after the
# slash. A template and not a URL, because the receiver's address is `info.ip`.
PATH_TEMPLATE = "/{sref}"

# Where a hardware encoder shows itself, on the receivers that have one.
ENCODER_PATHS = ("/dev/bcm_enc0", "/proc/stb/encoder/0", "/dev/encoder0")

# More clients than a household has screens. Beyond it the rows are cut and
# `count` still says how many there are.
MAX_CLIENTS = 32

# An address is at most 45 characters; a host the image hands over longer than
# this is not an address.
HOST_LIMIT = 64

# A DVB reference is about thirty characters and one that carries a name or a
# stream's address a few hundred. Anything longer is not a reference this
# plugin could do anything with, and the topic is retained: such a row is
# listed without it.
SREF_LIMIT = 512

MAPPED_PREFIX = "::ffff:"

STOP_NOT_AVAILABLE = "stopping a stream client is not available on this image"
PLAYABLE_NOT_AVAILABLE = "this image cannot say whether a service can be played"
UNREADABLE = "the receiver's list of stream clients cannot be read"
NOT_CONNECTED = ("that stream client is not connected", "not_connected")
NOT_STOPPED = ("the receiver did not stop that stream client", "not_stopped")
STOP_RAISED = "the receiver could not stop that stream client: "
NO_INFORMATION = "the receiver has no information for that service"
NOT_ONE_SERVICE = "cmd/stream_playable takes the reference of one playable service"
NO_ANSWER = "the receiver did not say whether that service can be played"
ANSWER_RAISED = "the receiver could not say whether that service can be played: "


def refusal(pair):
    sentence, reason = pair
    return Refusal(sentence, reason)


# ------------------------------------------------------------------ the image --


def _server():
    """The image's stream server, or None."""
    factory = enigma_attribute("eStreamServer")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        LOG.debug("eStreamServer.getInstance() raised")
        return None


def _service_center():
    factory = enigma_attribute("eServiceCenter")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        return None


def _config(section, name):
    """`config.<section>.<name>.value`, or None where the image has no such element."""
    try:
        from Components.config import config

        found = config
        for part in section.split("."):
            found = getattr(found, part, None)
            if found is None:
                return None
        element = getattr(found, name, None)
        return None if element is None else element.value
    except Exception:
        # An image's settings object may answer a name it does not have with
        # something other than "no such attribute".
        return None


def _port(value):
    """A configured port as a number, or None when it is not one."""
    if isinstance(value, bool):
        return None
    try:
        port = int(str(value))
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def authentication():
    """Whether the stream server asks for a login, or None where the image does not say."""
    value = _config("streaming", "authentication")
    return value if isinstance(value, bool) else None


def has_encoder(paths=None):
    """Whether the receiver has a hardware encoder: one of its device files exists."""
    return any(os.path.exists(path) for path in (ENCODER_PATHS if paths is None else paths))


def encoder_port():
    """The transcoding plugin's port where the image has it as a plain value, else None."""
    return _port(_config("plugins.transcodingsetup", "port"))


def _dotted_quad(text):
    """Whether `text` is an IPv4 address as it is written: four numbers up to 255."""
    parts = text.split(".")
    return len(parts) == 4 and all(
        part.isascii() and part.isdigit() and len(part) <= 3 and int(part) <= 255
        for part in parts
    )


def normalise_host(host):
    """A client's address as the topic shows it: an IPv4 address without its IPv6 wrapping."""
    text = str(host or "").strip()
    if text.lower().startswith(MAPPED_PREFIX) and _dotted_quad(text[len(MAPPED_PREFIX):]):
        return text[len(MAPPED_PREFIX):]
    return text[:HOST_LIMIT]


def read_clients():
    """`[(host, sref, encoder)]` exactly as the image holds them, or None.

    None is "cannot be read", and it is the answer to everything but a list
    of rows: a call that raises, and an answer of another shape - nothing, a
    string, a dictionary, a row that is no `(host, reference, flag)`. Only an
    empty list is "no clients"; reading a surprise as that would publish "no
    clients" for a receiver that is serving some.
    """
    server = _server()
    getter = getattr(server, "getConnectedClients", None) if server is not None else None
    if not callable(getter):
        return None
    try:
        rows = getter()
    except Exception:
        LOG.debug("eStreamServer.getConnectedClients() raised")
        return None
    if not isinstance(rows, (list, tuple)):
        return None
    clients = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            return None
        if not isinstance(row[0], str) or not isinstance(row[1], str):
            return None
        clients.append((row[0], row[1], bool(row[2])))
    return clients


class StreamPublisher(Publisher):
    """`stream` and `stream_clients`, and the two commands about the stream server."""

    name = TOPIC

    def __init__(self, bridge=None, encoder_paths=None):
        Publisher.__init__(self, bridge)
        self._encoder_paths = encoder_paths
        self._encoder = False
        self._clients = False
        self._stop = False
        self._playable = False
        self._key = None
        self._payload = None
        self._ticker = Ticker(self.refresh, "stream clients")
        self._after_stop = Ticker(self.refresh, "stream stop")
        # The last answer `cmd/stream_playable` gave, for the OpenWebif page:
        # the topic it goes out on is not retained, so nothing else holds it.
        self.last_answer = None

    # ----------------------------------------------------------- capabilities --

    def extra_capabilities(self):
        names = []
        if self._clients:
            names.append(CLIENTS_CAPABILITY)
        if self.stop_available():
            names.append(STOP_CAPABILITY)
        if self._playable:
            names.append(PLAYABLE_CAPABILITY)
        return names

    def stop_available(self):
        """A client can only be named from the image's list, so both are needed."""
        return self._clients and self._stop

    def playable_available(self):
        return self._playable

    def lists_clients(self):
        """Whether `stream_clients` is this run's to publish."""
        return self._clients

    def start(self):
        server = _server()
        if server is None:
            return False
        # A device file does not come and go while the receiver runs: looked
        # for once, here, and never on the five-second look.
        self._encoder = has_encoder(self._encoder_paths)
        self._clients = callable(getattr(server, "getConnectedClients", None))
        self._stop = callable(getattr(server, "stopStreamClient", None))
        center = _service_center()
        self._playable = (
            callable(getattr(center, "info", None))
            and enigma_attribute("eServiceReference") is not None
        )
        # Nothing is read here: the channel list has not been walked yet - it
        # starts after this area - and a row built now would have no bouquet.
        # The connect's snapshot is the first read.
        channels = self.bridge.publisher("channels") if self.bridge is not None else None
        listen = getattr(channels, "when_changed", None)
        if listen is not None:
            listen(self._channels_changed)
        self._ticker.start(POLL_MILLISECONDS)
        return True

    def _channels_changed(self):
        """The channel list was walked: what the rows say of it is resolved again.

        At the next look or connect, not here - this runs inside the walk.
        """
        self._key = None

    def stop(self):
        self._ticker.stop()
        self._after_stop.stop()

    # ---------------------------------------------------------------- reading --

    def descriptor(self):
        """The `stream` payload. Settings are read now; nothing here opens a file."""
        return {
            "port": LISTEN_PORT,
            "path": PATH_TEMPLATE,
            "authentication": authentication(),
            "encoder": self._encoder,
            "encoder_port": encoder_port() if self._encoder else None,
        }

    def _bouquet(self, sref):
        channels = self.bridge.publisher("channels") if self.bridge is not None else None
        lookup = getattr(channels, "bouquet_for", None)
        if lookup is None:
            return None
        try:
            return lookup(sref)
        except Exception:
            return None

    def build(self, clients):
        """The `stream_clients` payload for the image's list."""
        rows = []
        for host, sref, encoder in clients[:MAX_CLIENTS]:
            if len(sref) > SREF_LIMIT:
                # Not a reference: the row is listed and counted without it.
                sref = ""
            rows.append({
                "host": normalise_host(host),
                "sref": sref or None,
                "name": (service_name(sref) or None) if sref else None,
                "bouquet": self._bouquet(sref) if sref else None,
                "transcoded": encoder,
            })
        return {"count": len(clients), "clients": rows}

    def _read(self):
        """The `stream_clients` payload for the image's list now, or None when unreadable."""
        clients = read_clients()
        if clients is None:
            self._payload = None
            self._key = None
            return None
        key = tuple(clients)
        if key != self._key or self._payload is None:
            self._payload = self.build(clients)
            self._key = key
        return self._payload

    def _take_back(self):
        """Empty the retained list: what the image holds is not known."""
        if self.bridge is not None:
            self.bridge.retract(self.bridge.topic(CLIENTS_TOPIC))

    def refresh(self):
        self.publish(TOPIC, self.descriptor())
        if not self._clients:
            return None
        known = self._payload is not None
        payload = self._read()
        if payload is not None:
            self.publish(CLIENTS_TOPIC, payload)
        elif known:
            # Once, when the list stops being readable; a connect says it again.
            self._take_back()
        return payload

    def snapshot(self):
        """Both topics for a connect - the list read now, never the one held from before."""
        topics = {TOPIC: self.descriptor()}
        if self._clients:
            payload = self._read()
            if payload is not None:
                topics[CLIENTS_TOPIC] = payload
            else:
                self._take_back()
        return topics

    # --------------------------------------------------------------- commands --

    def stop_client(self, host, sref):
        """End one client's stream. None when the image said it did, else the refusal."""
        if not self.stop_available():
            return STOP_NOT_AVAILABLE
        stopper = getattr(_server(), "stopStreamClient", None)
        clients = read_clients()
        if clients is None:
            return UNREADABLE
        wanted = normalise_host(host).lower()
        for held_host, held_sref, _encoder in clients:
            if len(held_sref) > SREF_LIMIT:
                # Listed without its reference (`build`), so it is not a row
                # anybody can have named - and by its first eleven fields it
                # may be the same service as the row that was.
                continue
            if normalise_host(held_host).lower() != wanted or not same_service(held_sref, sref):
                continue
            try:
                # The image's own spelling of both, of this very row: the
                # image compares them as whole texts and ends the first
                # client that has both. A row left out above is longer than
                # this reference can be, so it is never that client.
                stopped = stopper(held_host, held_sref)
            except Exception as error:
                # Its text can name a client's address: not on a retained topic.
                LOG.warning("eStreamServer.stopStreamClient() raised %s", type(error).__name__)
                LOG.debug("stopStreamClient said: %s", error)
                return Refusal(STOP_RAISED + type(error).__name__, NOT_STOPPED[1])
            if not stopped:
                return refusal(NOT_STOPPED)
            self._after_stop.start(STOP_REFRESH_MILLISECONDS, True)
            return None
        return refusal(NOT_CONNECTED)

    def playable(self, sref, command_id=None):
        """Ask the image whether `sref` could be played now, and publish the answer.

        The second reference handed to `isPlayable` is an EMPTY one
        (ADR-0017). It names a service the image may assume is given up to make
        room; with the playing service there, the image answers yes for every
        channel, because the television could always be switched over. Empty,
        it answers for a stream beside everything that is running - which is
        what a stream is.
        """
        if not self._playable:
            return PLAYABLE_NOT_AVAILABLE
        reference = service_reference(sref)
        valid = getattr(reference, "valid", None)
        try:
            if reference is None or (valid is not None and not valid()):
                return "'" + str(sref) + "' is not a service reference"
        except Exception:
            return "'" + str(sref) + "' is not a service reference"
        if not is_one_service(sref):
            # The channel list's own test: a bouquet and any other directory
            # (alternatives among them), a marker, a numbered marker and a
            # hidden entry are not one playable service, nor is text with
            # fewer fields than a reference has. For a bouquet the image
            # would answer for any one of its members, by walking it.
            return NOT_ONE_SERVICE
        try:
            info = _service_center().info(reference)
        except Exception:
            info = None
        ask = getattr(info, "isPlayable", None) if info else None
        if not callable(ask):
            return NO_INFORMATION
        try:
            answer = ask(reference, enigma_attribute("eServiceReference")())
        except Exception as error:
            LOG.warning("isPlayable raised %s", type(error).__name__)
            LOG.debug("isPlayable said: %s", error)
            return ANSWER_RAISED + type(error).__name__
        if not isinstance(answer, int) or answer < 0:
            # A boolean is an integer here, and either is an answer: zero is
            # no and a positive number is yes. A negative one is the image
            # saying something else, and is not read as yes.
            return NO_ANSWER
        payload = {"sref": str(sref), "playable": answer > 0, "ts": int(time.time())}
        if command_id is not None:
            payload["id"] = command_id
        self.last_answer = payload
        if self.bridge is not None:
            # An event, as `key` is: never retained, so no reconnect replays an
            # answer about tuners that have moved on since.
            self.bridge.publish_state(PLAYABLE_TOPIC, payload, retain=False)
        return None
