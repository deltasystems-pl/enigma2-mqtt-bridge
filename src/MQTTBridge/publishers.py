"""Which feature areas a running bridge has, and in what order.

The order is not cosmetic. `channels` is registered before `service` and
`epg_grid` because both ask it questions — which bouquet a service came from,
which bouquets to build a grid for — and a publisher that failed to start is
removed from the registry, so „ask the channel list if there is one" is a
lookup that has to happen after it either started or did not.

It is also the order `info.capabilities` lists, which is the order `docs/TOPICS.md`
describes the topics in. A reader who compares the two should not have to sort
anything.
"""

from .cam import CamPublisher
from .channels import ChannelsPublisher
from .epggrid import EpgGridPublisher
from .hdd import HddPublisher
from .power import PowerPublisher
from .recording import RecordingPublisher, TimersPublisher
from .remote import KeyPublisher
from .screen import ScreenPublisher
from .service import EpgPublisher, ServicePublisher, TunerPublisher
from .volume import VolumePublisher

PUBLISHER_CLASSES = (
    PowerPublisher,
    ServicePublisher,
    EpgPublisher,
    TunerPublisher,
    CamPublisher,
    RecordingPublisher,
    TimersPublisher,
    VolumePublisher,
    HddPublisher,
    ChannelsPublisher,
    EpgGridPublisher,
    KeyPublisher,
    ScreenPublisher,
)


def default_publishers(bridge):
    """One of each, bound to this bridge. None of them has started yet."""
    return [publisher(bridge) for publisher in PUBLISHER_CLASSES]
