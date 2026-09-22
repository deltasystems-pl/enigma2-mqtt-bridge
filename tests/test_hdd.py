"""The recording disk probe never waits on a network mount in the GUI thread."""

from threading import Event

from MQTTBridge import hdd


class DispatchQueue:
    def __init__(self):
        self.calls = []

    def __call__(self, function, *args):
        self.calls.append((function, args))

    def fire(self, index=0):
        function, args = self.calls.pop(index)
        function(*args)


class DropDispatcher:
    def __init__(self):
        self.calls = 0

    def __call__(self, _function, *_args):
        self.calls += 1


class Client:
    def __init__(self, dispatch):
        self._dispatch = dispatch


class Bridge:
    def __init__(self, dispatch):
        self.client = Client(dispatch)
        self.published = []

    def publish_state(self, suffix, payload, raw=False, retain=True, volatile=()):
        self.published.append((suffix, payload, raw, retain))


def publisher(dispatch=None):
    dispatch = dispatch or DispatchQueue()
    bridge = Bridge(dispatch)
    return hdd.HddPublisher(bridge, "/media/network-recordings"), bridge, dispatch


def test_snapshot_omits_unknown_state_without_touching_the_mount(monkeypatch):
    found, _bridge, _dispatch = publisher()
    monkeypatch.setattr(hdd, "read", lambda path: 1 / 0)

    assert found.snapshot() == {}


def test_a_blocked_probe_does_not_block_poll_or_spawn_another(monkeypatch):
    found, _bridge, _dispatch = publisher()
    entered = Event()
    release = Event()
    calls = []

    def blocked(_path):
        calls.append(_path)
        entered.set()
        release.wait(2)
        return {"mounted": True, "path": found.path, "free_mb": 123}

    monkeypatch.setattr(hdd, "read", blocked)
    found.start()
    assert entered.wait(1)

    assert found._poll() is None
    assert found._worker_running is True
    assert calls == [found.path]
    release.set()


def test_probe_completion_is_published_on_the_dispatcher(monkeypatch):
    found, bridge, dispatch = publisher()
    result = {"mounted": True, "path": found.path, "free_mb": 123}
    monkeypatch.setattr(hdd, "read", lambda _path: result)

    found.start()
    for _ in range(100):
        if dispatch.calls:
            break
        Event().wait(0.01)
    assert bridge.published == []
    dispatch.fire()

    assert bridge.published == [("hdd", result, False, True)]
    assert found.snapshot() == {"hdd": result}


def test_a_late_result_after_stop_is_discarded(monkeypatch):
    found, bridge, dispatch = publisher()
    monkeypatch.setattr(
        hdd,
        "read",
        lambda _path: {"mounted": True, "path": found.path, "free_mb": 123},
    )
    found.start()
    for _ in range(100):
        if dispatch.calls:
            break
        Event().wait(0.01)
    found.stop()
    dispatch.fire()

    assert bridge.published == []
    assert found._worker_running is False


def test_a_late_result_after_restart_schedules_a_fresh_probe(monkeypatch):
    found, bridge, dispatch = publisher()
    results = [
        {"mounted": True, "path": found.path, "free_mb": 123},
        {"mounted": True, "path": found.path, "free_mb": 456},
    ]
    monkeypatch.setattr(hdd, "read", lambda _path: results.pop(0))
    found.start()
    for _ in range(100):
        if dispatch.calls:
            break
        Event().wait(0.01)
    found.stop()
    found.start()
    assert found._worker_running is False
    dispatch.fire()
    for _ in range(100):
        if dispatch.calls:
            break
        Event().wait(0.01)
    dispatch.fire()

    assert bridge.published[-1][1]["free_mb"] == 456


def test_a_probe_exception_becomes_unknown_free_space(monkeypatch):
    found, bridge, dispatch = publisher()
    monkeypatch.setattr(hdd, "read", lambda _path: (_ for _ in ()).throw(RuntimeError("lost")))
    found.start()
    for _ in range(100):
        if dispatch.calls:
            break
        Event().wait(0.01)
    dispatch.fire()

    assert bridge.published[-1][1] == {
        "mounted": False,
        "path": found.path,
        "free_mb": None,
    }


def test_a_dispatcher_that_drops_completion_does_not_wedge_restart(monkeypatch):
    dropped = DropDispatcher()
    found, _bridge, _dispatch = publisher(dropped)
    monkeypatch.setattr(
        hdd,
        "read",
        lambda _path: {"mounted": True, "path": found.path, "free_mb": 123},
    )

    found.start()
    for _ in range(100):
        if not found._worker_running:
            break
        Event().wait(0.01)
    found.stop()
    found.start()

    for _ in range(100):
        if dropped.calls >= 2:
            break
        Event().wait(0.01)
    assert dropped.calls == 2


def test_an_older_same_generation_result_cannot_overwrite_a_newer_one(monkeypatch):
    found, bridge, dispatch = publisher()
    results = [
        {"mounted": True, "path": found.path, "free_mb": 123},
        {"mounted": True, "path": found.path, "free_mb": 456},
    ]
    monkeypatch.setattr(hdd, "read", lambda _path: results.pop(0))
    found.start()
    for _ in range(100):
        if dispatch.calls:
            break
        Event().wait(0.01)
    found._poll()
    for _ in range(100):
        if len(dispatch.calls) == 2:
            break
        Event().wait(0.01)

    dispatch.fire(1)
    dispatch.fire(0)

    assert found.snapshot()["hdd"]["free_mb"] == 456
    assert [entry[1]["free_mb"] for entry in bridge.published] == [456]
