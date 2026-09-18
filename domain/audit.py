"""递交审计：精确还原"当天发送出去的东西"。

审计不信任当前内存状态，而是以哈希链事件日志为唯一事实来源，
重放并核验后给出某次递交的发送回执：逐文件哈希、适用的法规版本、
签章状态（含签章页哈希与签署凭证）、逐级批准人及时间。
任何历史改动都会在 verify() 阶段被识破。
"""

from dataclasses import dataclass
from datetime import datetime

from .errors import IntegrityError, NotFoundError
from .events import EventLog


@dataclass(frozen=True)
class SubmissionAudit:
    package_id: str
    jurisdiction: str
    language: str
    sent_at: str
    sent_by: str
    snapshot_id: str
    snapshot_manifest_sha256: str
    package_manifest_sha256: str
    files: tuple[dict, ...]
    requirement_versions: tuple[dict, ...]
    signatures: tuple[dict, ...]
    approvers: tuple[dict, ...]
    due_at: str | None
    withdrawn: dict | None
    chain_head_at_send: str

    def to_dict(self) -> dict:
        return {
            "package_id": self.package_id,
            "jurisdiction": self.jurisdiction,
            "language": self.language,
            "sent_at": self.sent_at,
            "sent_by": self.sent_by,
            "snapshot_id": self.snapshot_id,
            "snapshot_manifest_sha256": self.snapshot_manifest_sha256,
            "package_manifest_sha256": self.package_manifest_sha256,
            "files": list(self.files),
            "requirement_versions": list(self.requirement_versions),
            "signatures": list(self.signatures),
            "approvers": list(self.approvers),
            "due_at": self.due_at,
            "withdrawn": self.withdrawn,
            "chain_head_at_send": self.chain_head_at_send,
        }


class AuditService:
    def __init__(self, events: EventLog):
        self._events = events

    def audit_submission(self, package_id: str) -> SubmissionAudit:
        """按包 ID 重放事件链，还原最终发送回执（含撤回信息，若有）。"""
        self._events.verify()
        sent_event = None
        withdrawn_event = None
        for event in self._events.filter("submission.sent", package_id):
            sent_event = event  # 一个包正常只发送一次；取最后一次亦如实呈现
        if sent_event is None:
            raise NotFoundError(f"未找到 {package_id} 的发送记录")
        withdrawn = list(self._events.filter("submission.withdrawn", package_id))
        if withdrawn:
            withdrawn_event = withdrawn[-1]

        payload = sent_event.payload
        return SubmissionAudit(
            package_id=package_id,
            jurisdiction=payload["jurisdiction"],
            language=payload["language"],
            sent_at=payload["sent_at"],
            sent_by=sent_event.actor,
            snapshot_id=payload["snapshot"]["snapshot_id"],
            snapshot_manifest_sha256=payload["snapshot"]["manifest_sha256"],
            package_manifest_sha256=payload["package_manifest_sha256"],
            files=tuple(dict(f) for f in payload["files"]),
            requirement_versions=tuple(
                dict(r) for r in payload["requirements"]
            ),
            signatures=tuple(dict(s) for s in payload["signatures"]),
            approvers=tuple(dict(a) for a in payload["approvers"]),
            due_at=payload.get("due_at"),
            withdrawn=(
                {
                    "withdrawn_at": withdrawn_event.timestamp,
                    "withdrawn_by": withdrawn_event.actor,
                    "reason": withdrawn_event.payload.get("reason", ""),
                    "event_hash": withdrawn_event.hash,
                }
                if withdrawn_event
                else None
            ),
            chain_head_at_send=sent_event.hash,
        )

    def submissions_sent_on(
        self, day: datetime | str, jurisdiction: str | None = None
    ) -> list[SubmissionAudit]:
        """返回某一自然日（UTC 日期）发送的全部递交回执。"""
        day_str = day if isinstance(day, str) else day.date().isoformat()
        self._events.verify()
        package_ids: list[str] = []
        for event in self._events.filter("submission.sent"):
            sent_date = event.timestamp[:10]
            if sent_date != day_str:
                continue
            if (
                jurisdiction is not None
                and event.payload.get("jurisdiction") != jurisdiction
            ):
                continue
            package_ids.append(event.payload["stream_id"])
        return [self.audit_submission(pid) for pid in package_ids]

    def verify_chain(self) -> None:
        """对外暴露的完整性校验入口。"""
        self._events.verify()
