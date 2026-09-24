"""What a feature area is.

A publisher owns one area of the contract: it binds the enigma2 hooks that area
needs, it names itself in `info.capabilities` when they bound, and it produces a
snapshot of everything it knows for `on_connect`.

It lives in its own module rather than in `bridge.py` so that the publishers and
the command handlers can import it without either importing the bridge - a
command needs to reach a publisher (to read a state back), and a publisher needs
to reach the bridge (to publish), and one shared base class is what keeps that
from becoming a circular import.
"""


class Refusal(str):
    """A handler's refusal that also carries a stable reason code.

    Still the sentence - every caller that treats a refusal as text goes on
    doing so - with `reason` beside it, which the dispatcher puts on
    `last_error` as the optional `reason` field. The sentence is for the person
    reading the topic; the code is for a consumer that says the same thing in
    the household's language. Only handlers that define codes return one.
    """

    def __new__(cls, sentence, reason):
        refusal = str.__new__(cls, sentence)
        refusal.reason = str(reason)
        return refusal


class Publisher:
    """One feature area's state: its hooks, its snapshot, and its capability name."""

    name = ""

    # Suffixes whose payload goes out verbatim rather than as JSON. The contract
    # has three of them - `availability`, `power` and `screen` - because a
    # string and a JPEG are not improved by being wrapped in quotes.
    raw = ()

    # Fields this area's payload stamps from the clock rather than reads from
    # the receiver - `generated`, and anything else that moves on its own. They
    # go out with every publish and take no part in the change comparison,
    # because a field that moves by itself turns „publish when it changed" into
    # „publish every time it was built".
    #
    # A tuple of names, never a bare string: membership is tested with `in`, so
    # `volatile = "generated"` would quietly exclude every field whose name is a
    # substring of it.
    volatile = ()

    # Set by a publisher that returns False from `start()` because its feature
    # is switched off in the settings, rather than because this image could not
    # give it the hooks. The two look identical from outside and read very
    # differently in a log: one is a choice somebody made, the other is a
    # limitation of the receiver.
    switched_off = False

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
        yes by default. They come apart when a hook can only bind later - an
        InfoBar enigma2 has not created yet - and the publisher has to stay
        registered while it waits. Claiming a capability that produces no topic
        is exactly the dead entity `capabilities` exists to prevent, so the wait
        is unclaimed and the bridge republishes `info` when it ends.
        """
        return True

    def extra_capabilities(self):
        """Capability names this area claims besides its own name, when it can.

        For an area whose commands an image can offer only in part: the zap
        history can be read on an image that has no way to clear it.
        """
        return []

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
        """Publish this feature area's state - but only when it has changed."""
        if self.bridge is None:
            return None
        return self.bridge.publish_state(
            suffix, payload, raw=suffix in self.raw, volatile=self.volatile
        )

    def report(self, command, message):
        """Say why something a publisher was asked to do could not be done."""
        if self.bridge is not None:
            self.bridge.publish_last_error(command, message)
