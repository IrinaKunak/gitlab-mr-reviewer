"""Notification channels behind the `Notifier` port (application/ports.py).

Adding a channel (e.g. Bitrix24): implement `Notifier` + its formatter in
adapters/notify/<name>/, register the constructor in `bootstrap.NOTIFIERS`,
and add it to the contract tests (tests/test_notify.py)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ...application.ports import Notifier
from ...domain.events import NotificationEvent

logger = logging.getLogger(__name__)


class NullNotifier:
    """Drops every event (tests, dry runs, no channels configured)."""

    name = "null"

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    async def notify(self, event: NotificationEvent) -> None:
        self.events.append(event)


class CompositeNotifier:
    """Fans an event out to every channel; one failing channel never stops the rest."""

    name = "composite"

    def __init__(self, channels: Sequence[Notifier]) -> None:
        self.channels = list(channels)

    async def notify(self, event: NotificationEvent) -> None:
        for channel in self.channels:
            try:
                await channel.notify(event)
            except Exception:  # noqa: BLE001 — a contract breach must not spread
                logger.exception("notifier %s raised on %s", channel.name,
                                 type(event).__name__)
