"""待办事项与责任迁移。

审评问询的回复任务、补料任务等都是 WorkItem。负责人变更时把未完成
事项整体迁移给新负责人，逐项产生通知；截止时限调整保留调整轨迹
（原期限、新期限、原因），同样通知责任人。已完成事项不迁移。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from .errors import RuleViolation, WorkflowError
from .events import EventLog
from .notifications import NotificationLog

OPEN = "OPEN"
DONE = "DONE"


@dataclass(frozen=True)
class DeadlineChange:
    previous_due_at: str
    new_due_at: str
    reason: str
    changed_by: str
    changed_at: str


@dataclass(frozen=True)
class WorkItem:
    item_id: str
    title: str
    category: str
    owner: str
    created_at: str
    due_at: str | None
    status: str
    related_inquiry_id: str | None
    deadline_history: tuple[DeadlineChange, ...] = ()


class WorkflowService:
    def __init__(self, events: EventLog, notifications: NotificationLog):
        self._events = events
        self._notifications = notifications
        self._items: dict[str, WorkItem] = {}

    def create_item(
        self,
        title: str,
        category: str,
        owner: str,
        moment: datetime,
        due_at: str | None = None,
        related_inquiry_id: str | None = None,
        created_by: str | None = None,
    ) -> WorkItem:
        item = WorkItem(
            item_id=str(uuid.uuid4()),
            title=title,
            category=category,
            owner=owner,
            created_at=moment.isoformat(),
            due_at=due_at,
            status=OPEN,
            related_inquiry_id=related_inquiry_id,
        )
        self._items[item.item_id] = item
        self._events.append(
            "workitem.created",
            {
                "stream_id": item.item_id,
                "title": title,
                "category": category,
                "owner": owner,
                "due_at": due_at,
                "related_inquiry_id": related_inquiry_id,
            },
            actor=created_by or owner,
            timestamp=moment,
        )
        return item

    def complete(self, item_id: str, actor: str, moment: datetime) -> WorkItem:
        item = self._require(item_id)
        if item.status == DONE:
            raise WorkflowError("事项已完成")
        done = WorkItem(
            item_id=item.item_id,
            title=item.title,
            category=item.category,
            owner=item.owner,
            created_at=item.created_at,
            due_at=item.due_at,
            status=DONE,
            related_inquiry_id=item.related_inquiry_id,
            deadline_history=item.deadline_history,
        )
        self._items[item_id] = done
        self._events.append(
            "workitem.completed",
            {"stream_id": item_id, "owner": item.owner},
            actor=actor,
            timestamp=moment,
        )
        return done

    def transfer_ownership(
        self,
        old_owner: str,
        new_owner: str,
        actor: str,
        moment: datetime,
        reason: str = "",
    ) -> list[WorkItem]:
        """把某人全部未完成事项迁移给新负责人，并逐项通知双方。"""
        if old_owner == new_owner:
            raise RuleViolation("新旧负责人不能相同")
        moved: list[WorkItem] = []
        for item in list(self._items.values()):
            if item.owner != old_owner or item.status != OPEN:
                continue
            transferred = WorkItem(
                item_id=item.item_id,
                title=item.title,
                category=item.category,
                owner=new_owner,
                created_at=item.created_at,
                due_at=item.due_at,
                status=item.status,
                related_inquiry_id=item.related_inquiry_id,
                deadline_history=item.deadline_history,
            )
            self._items[item.item_id] = transferred
            moved.append(transferred)
            self._events.append(
                "workitem.ownership_transferred",
                {
                    "stream_id": item.item_id,
                    "old_owner": old_owner,
                    "new_owner": new_owner,
                    "reason": reason,
                },
                actor=actor,
                timestamp=moment,
            )
            self._notifications.emit(
                recipient=new_owner,
                subject=f"待办已转交：{item.title}",
                body=(
                    f"原负责人 {old_owner} 的未完成事项已迁移给您。"
                    f"原因：{reason or '（未填写）'}；"
                    f"截止：{item.due_at or '无'}"
                ),
                category="ownership_transfer",
                moment=moment,
                related_id=item.item_id,
                actor=actor,
            )
        # 给原负责人一条汇总通知，留下"谁被移走了什么"的记录。
        self._notifications.emit(
            recipient=old_owner,
            subject=f"已移交 {len(moved)} 项未完成事项给 {new_owner}",
            body="；".join(item.title for item in moved) or "无未完成事项",
            category="ownership_transfer_summary",
            moment=moment,
            actor=actor,
        )
        return moved

    def adjust_deadline(
        self,
        item_id: str,
        new_due_at: str,
        reason: str,
        actor: str,
        moment: datetime,
    ) -> WorkItem:
        item = self._require(item_id)
        if item.status == DONE:
            raise WorkflowError("已完成事项不再调整时限")
        if item.due_at == new_due_at:
            raise RuleViolation("新时限与当前时限相同")
        change = DeadlineChange(
            previous_due_at=item.due_at or "",
            new_due_at=new_due_at,
            reason=reason,
            changed_by=actor,
            changed_at=moment.isoformat(),
        )
        updated = WorkItem(
            item_id=item.item_id,
            title=item.title,
            category=item.category,
            owner=item.owner,
            created_at=item.created_at,
            due_at=new_due_at,
            status=item.status,
            related_inquiry_id=item.related_inquiry_id,
            deadline_history=item.deadline_history + (change,),
        )
        self._items[item_id] = updated
        self._events.append(
            "workitem.deadline_adjusted",
            {
                "stream_id": item_id,
                "previous_due_at": item.due_at,
                "new_due_at": new_due_at,
                "reason": reason,
            },
            actor=actor,
            timestamp=moment,
        )
        self._notifications.emit(
            recipient=item.owner,
            subject=f"时限调整：{item.title}",
            body=(
                f"截止时间由 {item.due_at or '无'} 调整为 {new_due_at}。"
                f"原因：{reason}"
            ),
            category="deadline_change",
            moment=moment,
            related_id=item_id,
            actor=actor,
        )
        return updated

    def open_items_for(self, owner: str) -> list[WorkItem]:
        return [
            item for item in self._items.values()
            if item.owner == owner and item.status == OPEN
        ]

    def get(self, item_id: str) -> WorkItem:
        return self._require(item_id)

    def _require(self, item_id: str) -> WorkItem:
        item = self._items.get(item_id)
        if item is None:
            from .errors import NotFoundError

            raise NotFoundError(f"事项不存在: {item_id}")
        return item
