"""What a feature area is.

A publisher owns one area of the contract: it binds the enigma2 hooks that area
needs, it names itself in `info.capabilities` when they bound, and it produces a
snapshot of everything it knows for `on_connect`.

It lives in its own module rather than in `bridge.py` so that the publishers and
the command handlers can import it without either importing the bridge — a
command needs to reach a publisher (to read a state back), and a publisher needs
to reach the bridge (to publish), and one shared base class is what keeps that
from becoming a circular import.
"""


class Publisher:
    """One feature area's state: its hooks, its snapshot, and its capability name."""

    name = ""

    # Suffixes whose payload goes out verbatim rather than as JSON. The contract
    # has three of them — `availability`, `power` and `screen` — because a
    # string and a JPEG are not improved by being wrapped in quotes.
    raw = ()

    def __init__(self, bridge=None):
        self.bridge = bridge

    def snapshot(self):
        """{topic suffix: payload} for everything this publisher owns."""
        return {}

    def start(self):
        """Bind enigma2 hooks. Returns True when the image provided them."""
        return True

    def claimed(self):
        """Whether this area's capability is true *now*.

        Starting and working are usually the same thing, which is why this says
        yes by default. They come apart when a hook can only bind later — an
        InfoBar enigma2 has not created yet — and the publisher has to stay
        registered while it waits. Claiming a capability that produces no topic
        is exactly the dead entity `capabilities` exists to prevent, so the wait
        is unclaimed and the bridge republishes `info` when it ends.
        """
        return True

    def stop(self):
        pass

    # ----------------------------------------------------- what a subclass uses --

    @property
    def session(self):
        return None if self.bridge is None else self.bridge.session

    def value(self, name):
        """One of the plugin's settings, read now rather than cached at start."""
        return None if self.bridge is None else self.bridge.value(name)

    def publish(self, suffix, payload):
        """Publish this feature area's state — but only when it has changed."""
        if self.bridge is None:
            return None
        return self.bridge.publish_state(suffix, payload, raw=suffix in self.raw)

    def report(self, command, message):
        """Say why something a publisher was asked to do could not be done."""
        if self.bridge is not None:
            self.bridge.publish_last_error(command, message)
