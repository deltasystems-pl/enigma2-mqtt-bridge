"""The plugin's own log file.

Enigma2's debug log is off on most boxes and `/tmp` is a tmpfs, so a plugin that
wants to be diagnosable after a reboot keeps its own file on the persistent
rootfs. It is capped, because filling the flash of a receiver is a way to brick
an evening.

Two rules hold everywhere in this package:

* the broker password is never written, at any level. `register_secret` teaches
  this module a value to scrub and a filter on the handler catches anything the
  call sites miss;
* logging never raises. A logging failure must not be the reason the television
  stops working.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "MQTTBridge"

PRIMARY_LOG_PATH = "/home/root/mqttbridge.log"
FALLBACK_LOG_PATH = "/tmp/mqttbridge.log"

MAX_BYTES = 1000000
BACKUP_COUNT = 2
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

REDACTED = "***"

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_secrets = set()
_handler = None
_active_path = None


def register_secret(value):
    """Remember a value that must never reach the log."""
    if isinstance(value, str) and value.strip():
        _secrets.add(value)


def forget_secrets():
    _secrets.clear()


def redact(text):
    """Replace every registered secret in `text`."""
    if not isinstance(text, str):
        text = str(text)
    for secret in _secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    return text


class _RedactingFilter(logging.Filter):
    """Last line of defence: scrub the formatted message, whatever produced it."""

    def filter(self, record):
        if not _secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:
            record.msg = "<unformattable log record>"
            record.args = ()
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


class _RedactingFormatter(logging.Formatter):
    """Scrub the complete rendered record, including exception and stack text."""

    def format(self, record):
        try:
            return redact(logging.Formatter.format(self, record))
        except Exception:
            # Never let malformed third-party logging arguments reach logging's
            # stderr fallback, which prints the raw record and its arguments.
            safe = logging.LogRecord(
                record.name,
                record.levelno,
                record.pathname,
                record.lineno,
                "<unformattable log record>",
                (),
                None,
            )
            safe.created = record.created
            safe.msecs = record.msecs
            return logging.Formatter.format(self, safe)


def level_value(name):
    return LEVELS.get(str(name or "").strip().lower(), logging.INFO)


def get_logger(name=None):
    if not name:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(LOGGER_NAME + "." + name)


def close():
    """Detach and close the file handler. Safe to call when there is none."""
    global _handler, _active_path
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    _handler = None
    _active_path = None


def configure(level="info", path=None):
    """Point the package logger at its file. Returns the path in use, or None.

    Called again with the same path it only adjusts the level, so a log-level
    change from the setup screen does not rotate or reopen anything.
    """
    global _handler, _active_path

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level_value(level))
    logger.propagate = False

    if _handler is not None and (path is None or path == _active_path):
        return _active_path

    close()

    candidates = [path] if path else [PRIMARY_LOG_PATH, FALLBACK_LOG_PATH]
    rejected = []
    for candidate in candidates:
        try:
            handler = RotatingFileHandler(
                candidate, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
            )
        except OSError as error:
            rejected.append((candidate, error))
            continue
        handler.setFormatter(_RedactingFormatter(LOG_FORMAT))
        handler.addFilter(_RedactingFilter())
        logger.addHandler(handler)
        _handler = handler
        _active_path = candidate
        break
    else:
        # No file anywhere. Stay quiet rather than let logging print to stderr,
        # which on a receiver means into enigma2's own console.
        logger.addHandler(logging.NullHandler())
        return None

    for candidate, error in rejected:
        logger.warning(
            "cannot write %s (%s); logging to %s instead", candidate, error, _active_path
        )
    return _active_path


def active_path():
    return _active_path


def log_directory_is_writable(path):
    directory = os.path.dirname(path) or "."
    return os.path.isdir(directory) and os.access(directory, os.W_OK)
