"""The single source of the plugin version, and of the contract major it implements.

`tools/build-ipk.sh` reads this file to name and stamp the IPK, the release
workflow checks the tag against it, and the plugin reports it on the `info`
topic. Change it here and nowhere else.

`CONTRACT` is the topic contract's major number (docs/TOPICS.md, "Contract
version"), published as `info.contract`. It changes only in a release that
declares a new major, and a test holds it to docs/contract.json.
"""

__version__ = "0.3.0"

CONTRACT = 1
