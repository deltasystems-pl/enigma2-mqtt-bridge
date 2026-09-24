"""Where a command came from, and what that changes: one permission question.

A command reaches the dispatcher from the broker or from the OpenWebif page, and
it runs the same handler either way. The only thing the two disagree about is
whether a box-side permission - `deep_standby_allowed`, `softcam_restart_allowed`
- has to be granted first.

Over MQTT it does, always: `cmd/config` is writable by anything with publish
rights on the command topic, so a permission is the one thing the broker must
never be able to hand itself. On the page it does not. The page is served by the
receiver's own web interface, and anybody that interface admits can already set
every `config.*` key through OpenWebif's own `saveconfig` - the permission
included - so refusing them here would protect nothing and only make them take
the long way round.

The origin travels **down the call**, as an argument, and is never stored. A
flag on the bridge would outlive the request that set it, and the next command
over MQTT - or the softcam's own automatic restart, which is no request at all -
would inherit a bypass it was never given. Everything that is not the page is
treated as the broker, so a caller that forgets to say where it came from is
gated.

The household-safety guards - a recording running, a timer about to start, the
softcam's one-a-minute limit - are not permissions and do not ask this module:
they apply to the page exactly as they apply to the broker.
"""

MQTT = "mqtt"
PAGE = "page"


def granted(read, permission, origin):
    """Whether `permission` is granted to a command from `origin`.

    `read` looks a setting up by name - the bridge's or a publisher's `value`.
    The page is always granted; anything else is granted only when the setting
    is on, with anything falsy (an element that did not build on this image
    included) read as a refusal.
    """
    if origin == PAGE:
        return True
    return bool(read(permission))
