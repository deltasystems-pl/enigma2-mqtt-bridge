# ADR-0019: Whether the volume acts on the audio is true, false or unknown, read from the image's setting for the active track's codec

**Status:** accepted 2026-10-13, amended 2026-10-13 (see [the amendment](#amendment-2026-10-13---aac-that-is-being-transcoded-and-how-much-is-read))
and 2026-10-14 (see [the second amendment](#amendment-2026-10-14---the-picture-comes-from-the-decoder))
**Date:** 2026-10-13
**Supersedes:** -

## Context

A receiver can hand encoded audio - AC-3, AAC, DTS - to the television or an amplifier as a
bitstream instead of decoding it. Its own volume then does nothing anybody can hear: the bar
moves, the `volume` topic follows, and the sound stays where it was. A consumer that shows a
volume slider for the receiver shows a control that does not work, on some channels and not on
others, and the household reports it as broken.

Whether that is the case is known only on the receiver, and from two things: which audio track is
being played, and what the image is set to do with that track's codec.

**The track.** `service.audioTracks()` lists the tracks and names the one being played. A track
has a description, a language and a PID; on the measured image (OpenViX 6.6) it has no type. The
description is the word the image shows in its audio menu, and the three images use different
words for the same codec.

**The setting.** It lives in `config.av`, one element for each codec, and neither the names nor
the types are the same everywhere (read from each image's `lib/python/Components/AVSwitch.py` on
2026-10-13):

| Codec | OpenViX `Developer` | OpenATV `master` | OpenPLi `scarthgap` |
|---|---|---|---|
| AC-3 | `downmix_ac3`: `downmix` / `passthrough` | `downmix_ac3`: a yes/no (yes is downmix); a word with more choices on some Dreambox models | `downmix_ac3`: `downmix` / `passthrough` |
| E-AC-3 | `downmix_ac3plus`: `downmix` / `passthrough` | `transcodeac3plus`: the receiver's own choices | `transcodeac3plus`: the receiver's own choices |
| AAC | `downmix_aac`: `downmix` / `passthrough` | `downmix_aac`: a yes/no, or a word on some models | `downmix_aac`: `downmix` / `passthrough` |
| HE-AAC | `downmix_aacplus`, on receivers that have it | `downmix_aacplus`, on receivers that have it | `downmix_aacplus`, on receivers that have it |
| DTS | `downmix_dts`: `downmix` / `passthrough` | `downmix_dts`: a yes/no | `downmix_dts`: `downmix` / `passthrough` |
| DTS-HD | `dtshd` | `dtshd` | `dtshd` |

The elements for the newer codecs offer more than two choices - `multichannel`, `hdmi_best`,
`use_hdmi_caps`, `force_ac3`, `force_dts` - read from the receiver's driver, so the list differs
by receiver.

## Decision

- **`volume.passthrough` has three values**: `true`, `false`, and `null` for "cannot be told".
  It is about the track being played now.
- **A track's codec is read off its description**, through one table of the words the three
  images write for a DVB track. A word that is not in the table is an unknown codec. The same
  name is published as `service.audio_tracks[].codec`.
- **The setting asked is the one for that codec**, the first of the names above the image has.
  An element whose value is the empty string - the placeholder an image puts where the receiver
  cannot do it - is not a setting.
- **Only two answers are taken from a setting**: the word `passthrough` is `true`; the word
  `downmix`, and the boolean yes that stands for it, is `false`; the boolean no is `true`.
  **Every other choice is `null`.**
- **`mp2` and `lpcm` are `false` without asking**: no image offers to pass them on.
- **`null` for everything else**: nothing playing, no active track, an unknown codec, `ac4` and
  `dra` (no image has such a setting), an image without the element, and `aac_he` on a receiver
  with no setting of its own for it.
- **It is read from memory, with the volume.** The settings are attributes of objects the image
  holds; no file under `/proc` is read. The reconciliation the volume already runs every five
  seconds reads it too, and it is read at once on the service events `evStart`, `evUpdatedInfo`
  and `evEnd`.
- **The capability `volume_passthrough`** is claimed where the image has at least one of the
  settings. The member is published either way.

## Alternatives that were not taken

- **A boolean.** It would have to say `false` for "the image has no such setting" and for "the
  setting says `hdmi_best`", and a consumer would re-enable a slider that does nothing. A wrong
  `false` is the defect this member exists to remove.
- **Guessing the other choices.** `multichannel` sends PCM, on which a volume could act;
  `force_ac3` sends a bitstream, on which it could not; `hdmi_best` depends on what the
  television says it supports. Each is a guess about a driver this project has not measured,
  and the answer for the last one is not on the receiver at all.
- **Reading `/proc/stb/audio/ac3` and its neighbours.** On the measured receiver the files say
  what the settings say, and reading them is a file read on a path that runs every five seconds
  on the thread that draws the television, for the same answer.
- **A notifier on each setting.** It would catch a changed setting at once instead of within
  five seconds, at the price of hooks into six of the image's elements that have to be taken
  off again, on images where the way to take one off differs. The reconciliation is already
  there.
- **HE-AAC judged by the setting for AAC.** On a receiver without `downmix_aacplus` one driver
  file may well govern both; it has not been measured, so it is `null`.
- **Asking the driver what it is doing.** No image exposes it.

## Consequences

- **`true` means the receiver is set to send the bitstream**, not that the television plays it.
  What a television does with a codec it cannot take is not known here.
- **`null` is common on receivers with the newer settings**: an E-AC-3 track with
  `transcodeac3plus` on `use_hdmi_caps` or `force_ac3` cannot be told. A consumer treats
  `null` as it treats a plugin without the member.
- **A track picked on the remote and a setting changed in the menu are followed within five
  seconds**, not at once: neither raises an event.
- **A new word from an image is `null` until the table knows it.** The images do not agree on
  the words - "AC3" on two of them is "Dolby Digital" on the third - and an image that changes
  one costs the codec name and the answer on that image until a release adds the word. The
  table is one dictionary in `audio.py`, with a test for every word.
- **Measured on one image.** The words and the two-choice settings were seen on OpenViX 6.6; the
  boolean form and the other names are read from OpenATV's and OpenPLi's source and have not
  been seen on a receiver.
- **If this is ever reversed** - the member withdrawn - that is a removal, and needs a new
  contract major. Giving a `null` case a `true` or `false` later is a fix, not a change of
  meaning: the member already says what it means.

## Amendment 2026-10-13 - AAC that is being transcoded, and how much is read

Found in review; the Decision's own rule applied where the first reading missed it.

- **AAC has a second switch.** Beside `downmix_aac` all three images have
  `config.av.transcodeaac` - `off`, `ac3` or `dts`, and `off` or `force_ac3` on two OpenATV
  models. With it on anything but `off` the receiver sends the television a transcode, which by
  this record's rule is neither of the two certain answers: `aac` and `aac_he` are then `null`,
  whatever the downmix setting says. The placeholder an image puts there when the receiver
  cannot transcode is no switch. No other codec has such an element in the three images.
- **Two `null` cases the list did not name.** OpenATV's `downmix_ac3` on dreamone and dreamtwo
  is `0`, `1` or `2`, which are not the two words, so `ac3` is `null` there. And
  `service.frame_rate`, which this record does not otherwise concern, is published without an
  upper bound, as the image gave it.
- **One track is read, not all of them.** "Read with the volume" meant every track of the
  service on every reconciliation and every volume key press, and on OpenViX describing the
  active track starts a detector in the decoder. The volume now asks which track is playing and
  reads that one's description only when the track or the service is not the one it last looked
  at; a service event makes it read again. `volume` and `service` each read what they need and
  share nothing: `service` may not have bound on a receiver where `volume` has, and the other
  way round.

- **Read once is not read for ever.** With one read for each track of each service, three things
  could leave an answer standing with nothing to end it: a track that became another codec under
  the same index with no service event, a description that could not be read the one time it
  was asked for, and a receiver whose session announces no service events. So the active
  track's description is read again every sixth reconciliation of the volume - thirty seconds,
  for one read in that time, because the read is the call that must stay rare; a read that
  failed is tried again at the next reconciliation; and where the service listener did not bind
  the track is read at every reconciliation, five seconds, as it was before this amendment but
  for one track. A volume key press never reads the track and does not count towards the six.

## Amendment 2026-10-14 - the picture comes from the decoder

The first amendment says `service.frame_rate` is published "as the image gave it". That is no
longer the whole truth, and the record is read with this in mind; the rest of it stands, the
missing upper bound included.

- **What changed.** `service.frame_rate`, `width`, `height` and `interlaced` are read from the
  first video decoder's own files, `/proc/stb/vmpeg/0/framerate`, `xres`, `yres` and
  `progressive`, where the image has them. The frame rate is still thousandths divided by a
  thousand and still published without an upper bound.
- **Value by value.** A file that is missing, unreadable or not a value the decoder can have - a
  `progressive` that is not 0 or 1, a frame rate of 0 - leaves that one member to the image's
  service information, as before. A receiver without the directory publishes what it always did.
- **No picture is null.** A size of 0 is the decoder with no picture yet, and its scan and frame
  rate are then meaningless: all four members are `null`, never 0 and never the image's answer.
- **Why.** Measured on OpenViX 6.6: after enigma2 restarts the decoder goes on showing the same
  picture, no size event comes, and the image's service information reports another picture - a
  720p channel as 1920 x 1080 - until the next zap, while the decoder's files have the truth.
  Because no event comes, `service` is also read again three and eight seconds after the plugin
  starts and after every service start; from a service start until the decoder raises one of its
  own events or the first re-read comes, its files, which still hold the last channel's picture
  for about a second, are not read ([TOPICS.md](../TOPICS.md)).
