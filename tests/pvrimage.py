"""The parts of an image the recording library and timeshift read, on a temporary disk.

Shaped on OpenViX 6.6's sources (`Screens/MovieSelection.py`, `Tools/Trashcan.py`,
`Components/Timeshift.py`, `Screens/InfoBar.py`), as far as the plugin asks them:

- `config.movielist.videodirs`, which the movie list defines when it is imported, and
  `config.usage.default_path`, `timer_path`, `instantrec_path` and `movielist_trashcan`;
- `Screens.MovieSelection.moveServiceFiles(ref, dest)`, which moves the recording and the
  files beside it (`.eit`, `.ap`, `.cuts`, `.meta`, `.sc`) - at once, or, as the image
  does with a large file, in a job that finishes later (`deferred`);
- `Tools.Trashcan.createTrashFolder(path)`, the `.Trash` under the location's mount;
- `Screens.InfoBarGenerics.resumePointsInstance.delResumePoint(ref)`;
- `/proc/mounts`, as a file of the test's own.

Installed by `PvrImage.install()`, and taken away again by the monkeypatch.
"""

import os
import struct
import sys
import types

import conftest
from conftest import ConfigLocations, ConfigSelection, ConfigSubsection, ConfigText, ConfigYesNo


class MoveJobs:
    """The image's job manager, as far as a move goes: jobs the test finishes by hand."""

    def __init__(self):
        self.pending = []

    def finish(self):
        pending, self.pending = self.pending, []
        for source, target in pending:
            os.rename(source, target)


class PvrImage:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.disk = tmp_path / "media" / "hdd"
        self.movie = self.disk / "movie"
        self.movie.mkdir(parents=True)
        self.mounts = tmp_path / "mounts"
        self.mount_lines = [
            "rootfs / rootfs rw 0 0",
            "/dev/sda1 " + str(self.disk) + " ext4 rw 0 0",
        ]
        self.write_mounts()
        self.moves = []
        self.move_raises = None
        self.deferred = False
        self.jobs = MoveJobs()
        self.resume_points = []
        self.trash_makes = True
        self.player_opened = []
        self.move_options = []

    # --------------------------------------------------------------- the image --

    def write_mounts(self):
        self.mounts.write_text("\n".join(self.mount_lines) + "\n", encoding="utf-8")

    def install(self, trash=True, trash_function=True, locations=None):
        from MQTTBridge import library

        config = conftest.config
        movielist = ConfigSubsection()
        movielist.videodirs = ConfigLocations(
            default=[str(path) + "/" for path in (locations or [self.movie])]
        )
        usage = ConfigSubsection()
        usage.default_path = ConfigText(default=str(self.movie) + "/")
        usage.timer_path = ConfigSelection(choices=["<default>"], default="<default>")
        usage.instantrec_path = ConfigSelection(choices=["<default>"], default="<default>")
        usage.movielist_trashcan = ConfigYesNo(default=trash)
        usage.panicbutton = ConfigYesNo(default=False)
        self.monkeypatch.setattr(config, "movielist", movielist, raising=False)
        self.monkeypatch.setattr(config, "usage", usage, raising=False)
        self.usage = usage
        self.movielist = movielist

        movie_selection = types.ModuleType("Screens.MovieSelection")
        movie_selection.moveServiceFiles = self._move_service_files
        self.monkeypatch.setitem(sys.modules, "Screens.MovieSelection", movie_selection)

        if trash_function:
            trashcan = types.ModuleType("Tools.Trashcan")
            trashcan.createTrashFolder = self._create_trash_folder
            self.monkeypatch.setitem(sys.modules, "Tools.Trashcan", trashcan)
        else:
            self.monkeypatch.delitem(sys.modules, "Tools.Trashcan", raising=False)

        generics = sys.modules.get("Screens.InfoBarGenerics")
        if generics is None:
            generics = types.ModuleType("Screens.InfoBarGenerics")
            self.monkeypatch.setitem(sys.modules, "Screens.InfoBarGenerics", generics)
        resume = types.SimpleNamespace(delResumePoint=self.resume_points.append)
        self.monkeypatch.setattr(generics, "resumePointsInstance", resume, raising=False)

        self.monkeypatch.setattr(library.LibraryPublisher, "mounts_path", str(self.mounts))
        return self

    def _create_trash_folder(self, path=None):
        if not self.trash_makes:
            return None
        trash = self.movie / ".Trash"
        trash.mkdir(exist_ok=True)
        return str(trash)

    def _move_service_files(self, serviceref, dest, name=None, allowCopy=True):
        if self.move_raises is not None:
            raise self.move_raises
        # `createMoveList`: the file is the reference's own path, never its string.
        source = os.path.normpath(serviceref.getPath())
        self.moves.append((source, dest))
        self.move_options.append((name, allowCopy))
        listed = [(source, os.path.join(dest, os.path.basename(source)))]
        stem = source[: source.rfind(".")]
        if os.path.exists(stem + ".eit"):
            listed.append((stem + ".eit", os.path.join(dest, os.path.basename(stem) + ".eit")))
        for extension in (".ap", ".cuts", ".meta", ".sc"):
            if os.path.exists(source + extension):
                listed.append((source + extension,
                               os.path.join(dest, os.path.basename(source) + extension)))
        for one, target in listed:
            if self.deferred and one == source:
                self.jobs.pending.append((one, target))
            else:
                os.rename(one, target)

    def with_player(self, receiver):
        """The info bar with OpenViX's `openMoviePlayer`, and its movie player."""
        servicelist = receiver.with_channel_list()
        infobar = conftest.InfoBar.instance

        def open_movie_player(ref, LastService=None):
            self.player_opened.append(ref.getPath())
            receiver.session.current_dialog = conftest.MoviePlayer(receiver.session)
            receiver.nav.playService(ref)

        infobar.openMoviePlayer = open_movie_player
        return servicelist

    # ---------------------------------------------------------------- the disk --

    def record(self, name, folder=None, meta=None, size=1024 * 1024, ap=None, eit=False,
               cuts=False):
        """One recording - its `.ts` and the files beside it - and its full path."""
        directory = self.movie if folder is None else folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        with open(path, "wb") as handle:
            handle.truncate(size)
        if meta is not None:
            (directory / (name + ".meta")).write_bytes(
                meta if isinstance(meta, bytes) else meta.encode("utf-8")
            )
        if ap is not None:
            (directory / (name + ".ap")).write_bytes(ap)
        if eit:
            (directory / (name[: name.rfind(".")] + ".eit")).write_bytes(b"\x00" * 16)
        if cuts:
            (directory / (name + ".cuts")).write_bytes(b"\x00" * 12)
        return str(path)


def meta_text(sref="1:0:19:283D:3FB:1:C00000:0:0:0:", name="Wiadomosci",
              description="Serwis", created=1792050000, length=0):
    return "\n".join([
        sref, name, description, str(created), "", str(length), "0", "", "188", "0",
    ]) + "\n"


def access_points(begin_pts, end_pts, entries=4):
    """An `.ap` file: offset and presentation time, 64-bit big-endian each."""
    step = (end_pts - begin_pts) // max(1, entries - 1)
    data = b""
    for index in range(entries):
        pts = begin_pts + step * index if index < entries - 1 else end_pts
        data += struct.pack(">QQ", index * 188 * 1000, pts % (1 << 33))
    return data


class Background:
    """`LibraryPublisher.run_in_background` that runs nothing until the test says so.

    What runs in the worker is then visibly apart from what runs on the main loop:
    the test is the thread.
    """

    def __init__(self):
        self.jobs = []

    def __call__(self, job, name):
        self.jobs.append(job)

    def run(self):
        ran = 0
        while self.jobs:
            self.jobs.pop(0)()
            ran += 1
        return ran


def inline(job, name):
    job()
