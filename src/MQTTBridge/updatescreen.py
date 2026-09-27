"""The television's "Plugin updates" screen, opened with the blue key of the setup screen.

It shows what the OpenWebif page shows (`updateview.py`): the running version and build, the
signed list of versions the receiver holds and how old it is, every version that list offers -
the running one marked, the ones that cannot be installed with the reason - and the update in
progress with who started it. Yellow asks for the list again; OK or green installs the chosen
version.

**The person at the television is the consent** (spec ae.4). Neither `update_check` nor
`update_allowed` is asked: whoever holds the remote control can open the setup screen and switch
either on, so refusing them here would protect nothing. Both requests go through the dispatcher
with the origin `screen`, so every guard that protects the household - a recording, a running EPG
import, standby, an update already running, the ten-minute limits - applies exactly as it does
over MQTT, and `last_error` says what it says for any other origin.

**Every install is asked first, and a downgrade is asked in its own words.** An install ends in
a restart of the user interface, so OK alone never starts one; the question defaults to "no". A
version below the running one is a downgrade, which only this screen and the page may start
(spec ae.8), and its question names what it takes away: the newer features, until the next
update. Only a "yes" to *that* question carries the downgrade consent down the call - the
question asked is recorded when it is asked, not guessed again from the answer.

**The doors.** From the helper's `installing` on, the plugin's files are being replaced under
this process (`selfupdate.py`). The screen may stay open - the image's restart question opens on
top of it (spec ae.6a) - but it then shows only the sentence the setup screen and the page show,
and its keys do nothing but close it. It refreshes itself every second from what is loaded: this
module and `updateview.py` import nothing lazily, because a first import now could read a
half-written file of the next release.

Nothing a key or the timer does may raise into enigma2's main loop: each entry point logs what it
catches and leaves the screen as it was.
"""

import json

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.MenuList import MenuList
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen

from . import plugin as plugin_module
from . import updateview
from .enigma2 import Ticker
from .i18n import _
from .log import get_logger
from .origin import SCREEN

LOG = get_logger("updatescreen")

REFRESH_MILLISECONDS = 1000


class MQTTBridgeUpdates(Screen):
    skin = """
        <screen name="MQTTBridgeUpdates" position="center,center" size="900,600"
                title="Plugin updates">
            <widget name="info" position="20,20" size="860,110" font="Regular;22" />
            <widget name="list" position="20,140" size="860,300" scrollbarMode="showOnDemand" />
            <widget name="status" position="20,450" size="860,90" font="Regular;22" />
            <widget name="key_red" position="20,550" size="200,30" font="Regular;22"
                    foregroundColor="red" />
            <widget name="key_green" position="230,550" size="200,30" font="Regular;22"
                    foregroundColor="green" />
            <widget name="key_yellow" position="440,550" size="200,30" font="Regular;22"
                    foregroundColor="yellow" />
        </screen>
    """

    def __init__(self, session, bridge=None):
        Screen.__init__(self, session)
        # Resolved once: the bridge outlives every session it opens, and asking the plugin
        # module later would be a lookup behind closed doors.
        self._bridge = bridge if bridge is not None else plugin_module.get_bridge()
        self._rows = None
        self._asked = None
        self._said = ""
        self["info"] = Label("")
        self["list"] = MenuList([])
        self["status"] = Label("")
        self["key_red"] = Label(_("Close"))
        self["key_green"] = Label(_("Install"))
        self["key_yellow"] = Label(_("Check now"))
        self["mqttbridgeUpdateActions"] = ActionMap(
            ["OkCancelActions", "ColorActions"],
            {
                "ok": self.keyInstall,
                "green": self.keyInstall,
                "yellow": self.keyCheck,
                "cancel": self.keyClose,
                "red": self.keyClose,
            },
            -2,
        )
        try:
            self.setTitle(_("Plugin updates"))
        except Exception:
            LOG.debug("this image's Screen has no usable setTitle")
        self._ticker = Ticker(self.refresh, "plugin updates screen")
        self.onClose.append(self._ticker.stop)
        self.refresh()
        self._ticker.start(REFRESH_MILLISECONDS)

    # ------------------------------------------------------------------ drawing --

    def refresh(self):
        """Redraw from the `update` topic's content; every second while the screen is open."""
        try:
            self._draw()
        except Exception:
            LOG.exception("the plugin updates screen could not be refreshed")

    def _draw(self):
        bridge = self._bridge
        closed = updateview.doors(bridge)
        if closed is not None:
            self._rows = []
            self._said = ""
            self["info"].setText(closed)
            self["list"].setList([])
            self["status"].setText("")
            self["key_green"].setText("")
            self["key_yellow"].setText("")
            return
        self["key_green"].setText(_("Install"))
        self["key_yellow"].setText(_("Check now"))
        lines = updateview.header(bridge)
        idle = self._idle()
        if idle:
            lines.append(idle)
        self["info"].setText("\n".join(lines))
        rows = updateview.rows(bridge)
        if rows != self._rows:
            # Only when the list changed: setting it again every second would move the
            # selection back to the top under the person choosing.
            self._rows = rows
            self["list"].setList([(updateview.row_label(row), row) for row in rows])
        status = [line for line in (updateview.transaction_line(bridge), self._said) if line]
        self["status"].setText("\n".join(status))

    def _idle(self):
        bridge = self._bridge
        if bridge is None:
            reason = _("the plugin did not start")
        elif not bridge.running:
            reason = bridge.idle_reason or _("the bridge is idle")
        else:
            return None
        return _("Commands need a running bridge: %s") % reason

    def _say(self, text):
        self._said = text or ""
        self.refresh()

    # --------------------------------------------------------------------- keys --

    def keyInstall(self):
        try:
            self._install()
        except Exception:
            LOG.exception("the install key could not be handled")

    def _install(self):
        if updateview.doors(self._bridge) is not None:
            return
        chosen = self["list"].getCurrent()
        row = chosen[1] if isinstance(chosen, tuple) and len(chosen) > 1 else None
        if row is None:
            return
        version, _relation, reason = row
        if reason is not None:
            self._say(updateview.row_label(row))
            return
        idle = self._idle()
        if idle:
            self._say(idle)
            return
        # What is asked is what the answer consents to: a "yes" to the plain question is
        # never a downgrade, whatever the version turns out to be.
        self._asked = (version, updateview.older(version))
        self.session.openWithCallback(
            self._answered,
            MessageBox,
            updateview.question(version),
            getattr(MessageBox, "TYPE_YESNO", 0),
            default=False,
        )

    def _answered(self, answer=None, *_rest):
        try:
            asked, self._asked = self._asked, None
            if answer is not True or asked is None:
                return
            self._request(*asked)
        except Exception:
            LOG.exception("the answer to the install question could not be handled")

    def _request(self, version, downgrade):
        bridge = self._bridge
        idle = self._idle()
        if idle:
            self._say(idle)
            return
        consents = {"downgrade": True} if downgrade else {}
        refusal = bridge.run_command("update", json.dumps({"version": version}), SCREEN,
                                     **consents)
        if refusal:
            self._say(updateview.doors(bridge)
                      or updateview.household_refusal(refusal, version))
            return
        self._say(_("The update to version %s has started.") % version)

    def keyCheck(self):
        try:
            self._check()
        except Exception:
            LOG.exception("the check key could not be handled")

    def _check(self):
        bridge = self._bridge
        if updateview.doors(bridge) is not None:
            return
        idle = self._idle()
        if idle:
            self._say(idle)
            return
        refusal = bridge.run_command("update_check", "", SCREEN)
        if refusal:
            self._say(updateview.doors(bridge) or updateview.household_refusal(refusal))
            return
        self._say(_("Asked for the list of versions; it is shown here as soon as it arrives."))

    def keyClose(self):
        try:
            self._ticker.stop()
        except Exception:
            LOG.debug("the refresh timer could not be stopped")
        self.close()
