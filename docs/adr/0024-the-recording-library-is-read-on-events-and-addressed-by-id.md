# ADR-0024: The recording library is read on events and addressed by id

**Status:** accepted 2026-10-18
**Date:** 2026-10-18
**Supersedes:** -

## Context

Home Assistant knew whether the receiver was recording, whether its recording disk was mounted,
how many recordings ran and when the next timer was. It did not know what the receiver had
recorded, could not play a recording on the receiver, and could not delete one. A household
wants all three, from the media browser and from an automation. Five things are true of the
receivers this plugin runs on:

- **The recordings are files** in the receiver's movie locations - its default movie location and its
  movie list's locations - with subfolders, a
  trash (`.Trash`) and, beside each recording, the files the image writes for it: `.meta` (the
  service, the title, the description, the time, the length), `.ap` (access points), `.cuts`,
  `.eit` and `.sc`. The image's movie list reads them through `eServiceCenter`, which opens the
  recording itself to work out a length its `.meta` does not have. A location may be on a network
  share, and an automounted share is mounted only when something walks into it.
- **A recording disk sleeps.** Reading its folders and the files beside every recording wakes it.
  A read on a timer is a disk that never sleeps; `statvfs` is answered from the superblock and
  `/proc/mounts` is in memory, and neither wakes anything.
- **There is no event for a change to the library**, beyond a recording starting and ending
  (`session.nav.record_event`) and a disk being added or removed. A recording deleted, moved or
  renamed in the movie list, through OpenWebif or over FTP says nothing to anybody.
- **The image's own delete moves into its trash.** `Screens.MovieSelection.moveServiceFiles`
  moves the recording and the files beside it into `.Trash` - a large file in a background job
  that finishes later - and the image's trash setting (`config.usage.movielist_trashcan`, on by
  default on OpenViX, OpenATV and OpenPLi) decides whether there is a trash at all. When a move
  fails, the movie list offers to delete for good.
- **What the broker carries is public to whoever may read it**, and anyone who may publish on a
  node's command topic can send it any payload.

## Decision

1. **The library is read once at start, then only on events.** A recording starting or ending
   (after three seconds without another), a delete this plugin made, a movie location becoming
   mounted - and `cmd/recordings_refresh`, which Home Assistant offers as a button and which is
   the only read that may wake a disk on purpose. **Never on a timer.** Whether a location is
   mounted is read from `/proc/mounts`, and its free space with `statvfs` only where it is mounted,
   on the recording disk's existing minute timer, in its thread. A location that is not mounted
   keeps the list it had when it was last read; only the refresh walks it, which mounts an
   automounted share. Changes made on the receiver itself show after the next event or a refresh,
   and the documentation says so.
2. **Files are read off the main loop, the image is asked on it.** The walk, the `.meta` and `.ap`
   reads and the encoding of each folder's payload run in a worker thread; a channel's name and
   its bouquet are asked on the main loop, in slices that end after 30 ms. The length comes from
   `.meta`, otherwise from the first and last entry of `.ap` - two reads of sixteen bytes, where
   the image's own reading opens the recording. One read runs at a time; an event during a read is
   read again after it, and a refresh during a read is answered by that read.
3. **One retained topic for each folder, and an index.** `recordings` lists each location - its
   free space, whether it is mounted, its folders - and `recordings/<folder_slug>` each folder's
   recordings, newest first. A folder over the packet bound is cut, its oldest left out, and the
   index says how many - the EPG grid's pattern. The topic set follows the index: a folder that
   goes is retracted, also one that went while the plugin was not running, found in the state
   file.
4. **A recording is addressed by an id, never by a path.** The first 16 hex digits of the sha256
   of its full path's bytes. Commands take the id; the plugin acts only on a recording its own
   current library lists. Nothing that arrives from the broker names a file, so no payload can
   point the plugin at one the library did not find - a path outside the movie locations, the
   trash, a file of the image.
5. **Play is the receiver's.** `cmd/play_recording` hands the recording to the image's own movie
   player, as its movie list does, with the image's own resume rule; a receiver in standby is
   woken first, as for a zap.
6. **Delete only ever moves into the image's trash.** `cmd/delete_recording` is behind the
   box-only permission `recording_delete_allowed`, off by default and never writable over MQTT. It
   is the image's own move with the image's own companion files, verified when the file has gone
   from where it was. It is refused while the image's trash is switched off, on an image without
   one, for a recording being recorded or played, and when the move fails - the image's offer to
   delete for good is never taken. `recording_delete` is claimed only while the permission is on
   and the trash is on, and `info.recording_trash` says which of the two is missing.
7. **The receiver lists everything.** Which recordings to hide - those of a bouquet the household
   hides - is Home Assistant's, as it is for the channel lists and the zap history: each entry
   carries the recorded service and the first published bouquet that holds it. The receiver has
   no rule of its own for hiding.

## Consequences

- A disk that is asleep stays asleep, except at the plugin's start, after a recording, after a
  delete, when a location is mounted, and on a refresh.
- The library can be stale: a change made on the receiver itself is seen at the next event or
  refresh, and an automounted share that was unmounted shows what it held when last read.
- An id is stable for as long as the file's path is. A recording renamed or moved on the receiver
  has a new id after the next read; a command with the old one is refused as unknown.
- A folder of more than about three thousand recordings is published without its oldest, and
  those cannot be played or deleted from Home Assistant until the folder is split on the receiver.
- The whole library - titles, descriptions, channels, folder names - is on the broker for whoever
  may read the node's topics.
- With the permission on, whoever may publish on the node's command topic can move every
  recording into the trash; the receiver's trash settings decide how long they stay there, and
  nothing this plugin does deletes one for good.
