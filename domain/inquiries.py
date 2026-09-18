"""审评问询与回复。

铁律：回复只能引用可验证的快照。每条回复引用的每份证据文件哈希，都
必须属于某个当场通过清单哈希与字节哈希双重校验的冻结快照——任何共享
盘上的"最新版"或游离文件都不能写进回复。问询到达即自动开立待办；
提交回复后待办完成，补充问询再开新待办，全过程在事件链内。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from .artifacts import ArtifactStore
from .errors import RuleViolation, WorkflowError
from .events import EventLog
from .snapshots import SnapshotService
from .workflow import WorkflowService

RECEIVED = "RECEIVED"
RESPONDED = "RESPONDED"
CLOSED = "CLOSED"


@dataclass(frozen=True)
class Inquiry:
    inquiry_id: str
    jurisdiction: str
    submission_package_id: str | None
    question_ref: str
    subject: str
    body: str
    received_at: str
    status: str
    item_id: str
    owner: str


@dataclass(frozen=True)
class InquiryResponse:
    response_id: str
    inquiry_id: str
    snapshot_refs: tuple[dict, ...]
    evidence_hashes: tuple[str, ...]
    message: str
    responded_by: str
    responded_at: str


class InquiryService:
    def __init__(
        self,
        events: EventLog,
        snapshots: SnapshotService,
        artifacts: ArtifactStore,
        workflow: WorkflowService,
    ):
        self._events = events
        self._snapshots = snapshots
        self._artifacts = artifacts
        self._workflow = workflow
        self._inquiries: dict[str, Inquiry] = {}
        self._responses: dict[str, list[InquiryResponse]] = {}

    def receive(
        self,
        jurisdiction: str,
        question_ref: str,
        subject: str,
        body: str,
        owner: str,
        moment: datetime,
        submission_package_id: str | None = None,
    ) -> tuple[Inquiry, "object"]:
        """登记审评问题到达，并开立对应的回复待办。"""
        inquiry_id = str(uuid.uuid4())
        item = self._workflow.create_item(
            title=f"[{jurisdiction}] {subject}",
            category="inquiry_response",
            owner=owner,
            moment=moment,
            related_inquiry_id=inquiry_id,
            created_by="regulator",
        )
        inquiry = Inquiry(
            inquiry_id=inquiry_id,
            jurisdiction=jurisdiction,
            submission_package_id=submission_package_id,
            question_ref=question_ref,
            subject=subject,
            body=body,
            received_at=moment.isoformat(),
            status=RECEIVED,
            item_id=item.item_id,
            owner=owner,
        )
        self._inquiries[inquiry_id] = inquiry
        self._responses[inquiry_id] = []
        self._events.append(
            "inquiry.received",
            {
                "stream_id": inquiry_id,
                "jurisdiction": jurisdiction,
                "submission_package_id": submission_package_id,
                "question_ref": question_ref,
                "subject": subject,
                "body": body,
                "owner": owner,
                "item_id": item.item_id,
            },
            actor="regulator",
            timestamp=moment,
        )
        return inquiry, item

    def respond(
        self,
        inquiry_id: str,
        snapshot_refs: list[dict],
        evidence_hashes: list[str],
        message: str,
        responded_by: str,
        moment: datetime,
    ) -> InquiryResponse:
        """提交回复；只允许引用通过验证的冻结快照内的证据。"""
        inquiry = self._require(inquiry_id)
        if inquiry.status == CLOSED:
            raise WorkflowError("问询已关闭，不再接受回复")

        if not snapshot_refs:
            raise RuleViolation("回复必须至少引用一份冻结证据快照")

        # 1) 每份快照当场解析并重新验证清单哈希与全部附件字节。
        snapshots = []
        allowed_hashes: set[str] = set()
        for ref in snapshot_refs:
            snapshot = self._snapshots.resolve(ref)
            self._snapshots.verify(snapshot)
            snapshots.append(snapshot)
            allowed_hashes.update(a["sha256"] for a in snapshot.attachment_refs)

        # 2) 回复引用的每个文件哈希都必须落在已验证快照内，并可回取字节。
        cited: list[str] = []
        for digest in evidence_hashes:
            if digest not in allowed_hashes:
                raise RuleViolation(
                    f"证据 {digest[:12]}… 不属于任何已验证的冻结快照，"
                    "回复只能引用可验证的快照"
                )
            self._artifacts.get_bytes(digest)
            cited.append(digest)
        if not cited:
            raise RuleViolation("回复必须引用至少一个证据文件哈希")

        response = InquiryResponse(
            response_id=str(uuid.uuid4()),
            inquiry_id=inquiry_id,
            snapshot_refs=tuple(dict(ref) for ref in snapshot_refs),
            evidence_hashes=tuple(cited),
            message=message,
            responded_by=responded_by,
            responded_at=moment.isoformat(),
        )
        self._responses[inquiry_id].append(response)
        self._events.append(
            "inquiry.responded",
            {
                "stream_id": inquiry_id,
                "snapshot_refs": [dict(ref) for ref in snapshot_refs],
                "evidence_hashes": cited,
                "message": message,
            },
            actor=responded_by,
            timestamp=moment,
        )
        # 待办随回复完成；如审评方再追问，receive() 会另开新待办。
        self._workflow.complete(inquiry.item_id, responded_by, moment)
        updated = Inquiry(
            inquiry_id=inquiry.inquiry_id,
            jurisdiction=inquiry.jurisdiction,
            submission_package_id=inquiry.submission_package_id,
            question_ref=inquiry.question_ref,
            subject=inquiry.subject,
            body=inquiry.body,
            received_at=inquiry.received_at,
            status=RESPONDED,
            item_id=inquiry.item_id,
            owner=inquiry.owner,
        )
        self._inquiries[inquiry_id] = updated
        return response

    def close(self, inquiry_id: str, actor: str, moment: datetime) -> Inquiry:
        inquiry = self._require(inquiry_id)
        closed = Inquiry(
            inquiry_id=inquiry.inquiry_id,
            jurisdiction=inquiry.jurisdiction,
            submission_package_id=inquiry.submission_package_id,
            question_ref=inquiry.question_ref,
            subject=inquiry.subject,
            body=inquiry.body,
            received_at=inquiry.received_at,
            status=CLOSED,
            item_id=inquiry.item_id,
            owner=inquiry.owner,
        )
        self._inquiries[inquiry_id] = closed
        self._events.append(
            "inquiry.closed", {"stream_id": inquiry_id}, actor=actor, timestamp=moment
        )
        return closed

    def get(self, inquiry_id: str) -> Inquiry:
        return self._require(inquiry_id)

    def responses_of(self, inquiry_id: str) -> list[InquiryResponse]:
        self._require(inquiry_id)
        return list(self._responses[inquiry_id])

    def _require(self, inquiry_id: str) -> Inquiry:
        from .errors import NotFoundError

        inquiry = self._inquiries.get(inquiry_id)
        if inquiry is None:
            raise NotFoundError(f"问询不存在: {inquiry_id}")
        return inquiry
