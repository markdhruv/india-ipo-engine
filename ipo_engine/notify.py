"""Alert delivery.

Deliberately a thin seam. Desktop toasts only reach you while this machine is
awake, which is the one guarantee an IPO cutoff does not give you - so adding
Telegram or email later should mean writing one ``send`` method, not touching
the engine. Register a new channel and it joins the fan-out.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class Channel:
    name = "base"

    def send(self, title: str, body: str, *, urgent: bool = False) -> bool:
        raise NotImplementedError


class ConsoleChannel(Channel):
    """Prints the alert, and must never lose one to the terminal's encoding.

    Alert bodies contain the rupee sign. A console left on cp1252 - which is
    what a redirected stream, a pythonw.exe run or a Task Scheduler job can
    hand us - raises UnicodeEncodeError on it. Losing an alert to a codepage
    is not acceptable, so the text degrades to ASCII rather than failing.
    """
    name = "console"

    _FALLBACKS = {"₹": "Rs ", "→": "->", "–": "-", "—": "-"}

    @classmethod
    def _asciify(cls, text: str) -> str:
        for char, replacement in cls._FALLBACKS.items():
            text = text.replace(char, replacement)
        return text.encode("ascii", "replace").decode("ascii")

    def send(self, title: str, body: str, *, urgent: bool = False) -> bool:
        marker = "!! URGENT" if urgent else "--"
        message = f"\n{marker} {title}\n{body}\n"
        try:
            print(message)
        except UnicodeEncodeError:
            print(self._asciify(message))
        except (ValueError, OSError):
            return False          # no usable stdout (pythonw, closed stream)
        return True


class WindowsToastChannel(Channel):
    """Windows 10/11 notification-centre toast.

    win11toast needs a desktop session; it fails inside a service or over a
    bare SSH shell, so failure is logged and never raised.
    """
    name = "windows-toast"

    def __init__(self) -> None:
        self._toast = None
        try:
            # notify() returns immediately; toast() blocks until the toast is
            # dismissed or times out, which would stall a run that has several
            # alerts to deliver.
            from win11toast import notify
            self._toast = notify
        except Exception as exc:                       # pragma: no cover
            log.warning("Windows toasts unavailable (%s); falling back to console", exc)

    @property
    def available(self) -> bool:
        return self._toast is not None

    def send(self, title: str, body: str, *, urgent: bool = False) -> bool:
        if not self._toast:
            return False
        try:
            self._toast(
                title,
                body,
                duration="long" if urgent else "short",
                audio={"silent": "false"} if urgent else None,
            )
            return True
        except Exception as exc:                       # pragma: no cover
            log.warning("toast failed: %s", exc)
            return False


class Notifier:
    """Fan one alert out to every configured channel."""

    def __init__(self, cfg: dict):
        # Cheap insurance: alerts carry the rupee sign, and the engine may be
        # started by something that never went through the CLI entry point.
        from .util import force_utf8
        force_utf8()

        settings = cfg.get("notify", {})
        self.channels: list[Channel] = []
        if settings.get("windows_toast", True):
            toast_channel = WindowsToastChannel()
            if toast_channel.available:
                self.channels.append(toast_channel)
        # On by default, so it is also the safety net when toasts are
        # unavailable; an explicit "console": false genuinely silences output.
        if settings.get("console") is not False:
            self.channels.append(ConsoleChannel())

    def send(self, title: str, body: str, *, urgent: bool = False) -> int:
        delivered = 0
        for channel in self.channels:
            try:
                if channel.send(title, body, urgent=urgent):
                    delivered += 1
            except Exception as exc:                   # pragma: no cover
                log.warning("channel %s failed: %s", channel.name, exc)
        return delivered
