"""The single source of the plugin version.

`tools/build-ipk.sh` reads this file to name and stamp the IPK, the release
workflow checks the tag against it, and the plugin reports it on the `info`
topic. Change it here and nowhere else.
"""

__version__ = "0.1.0"
