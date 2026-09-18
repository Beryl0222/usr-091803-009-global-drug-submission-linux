"""通知记录。

负责人变更、时限调整等动作必须留下可查的通知记录。通知同时进入事件链
（防篡改）与内存索引（便于按接收人查询）。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from .events import EventLog


@dataclass(frozen=True)
class Notification:
    notification_id: str
    recipient: str
    subject: str
    body: str
    category: str
    created_at: str
    related_id: str | None


class NotificationLog:
    def __init__(self, events: EventLog):
        self._events = events
        self._notifications: list[Notification] = []

    def emit(
        self,
        recipient: str,
        subject: str,
        body: str,
        category: str,
        moment: datetime,
        related_id: str | None = None,
        actor: str = "system",
    ) -> Notification:
        notification = Notification(
            notification_id=str(uuid.uuid4()),
            recipient=recipient,
            subject=subject,
            body=body,
            category=category,
            created_at=moment.isoformat(),
            related_id=related_id,
        )
        self._notifications.append(notification)
        self._events.append(
            "notification.emitted",
            {
                "stream_id": notification.notification_id,
                "recipient": recipient,
                "subject": subject,
                "body": body,
                "category": category,
                "related_id": related_id,
            },
            actor=actor,
            timestamp=moment,
        )
        return notification

    def for_recipient(self, recipient: str) -> list[Notification]:
        return [n for n in self._notifications if n.recipient == recipient]

    def for_related(self, related_id: str) -> list[Notification]:
        return [n for n in self._notifications if n.related_id == related_id]

    def all(self) -> list[Notification]:
        return list(self._notifications)
