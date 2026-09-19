"""创新药跨境申报的领域对象。

药品、试验、受试者分组、分析结果与附件之间建立明确引用；
证据快照冻结某一时刻的引用与内容哈希，提交包、审评问询、
事项与通知都围绕快照展开，保证已递交资料不随共享文件更新而失真。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class RequirementKind(str, Enum):
    """法域要求的类别。"""

    POPULATION = "population"  # 试验人群
    STAT_TABLE = "stat_table"  # 统计表
    TRANSLATION_SEAL = "translation_seal"  # 翻译签章
    INQUIRY_RESPONSE = "inquiry_response"  # 补充问询


@dataclass(frozen=True)
class Drug:
    drug_id: str
    name: str
    sponsor: str


@dataclass(frozen=True)
class Trial:
    trial_id: str
    drug_id: str
    phase: str
    title: str


@dataclass(frozen=True)
class SubjectGroup:
    group_id: str
    trial_id: str
    arm: str
    population: str
    size: int


@dataclass(frozen=True)
class AnalysisResult:
    """分析结果一经登记即不可变；数据更新只能登记新结果并指明取代关系。"""

    result_id: str
    trial_id: str
    subject_group_ids: tuple[str, ...]
    endpoint: str
    sha256: str
    supersedes: Optional[str] = None


@dataclass(frozen=True)
class AttachmentVersion:
    attachment_id: str
    version: int
    name: str
    sha256: str
    uploaded_by: str
    uploaded_at: datetime


@dataclass
class Attachment:
    """同名文件的版本线：替换与重试只追加版本，不改写历史。"""

    attachment_id: str
    name: str
    versions: list[AttachmentVersion] = field(default_factory=list)

    @property
    def latest(self) -> AttachmentVersion:
        return self.versions[-1]


@dataclass(frozen=True)
class AttachmentRef:
    """快照中对附件某一版本的冻结引用。"""

    attachment_id: str
    name: str
    version: int
    sha256: str


@dataclass(frozen=True)
class EvidenceSnapshot:
    """冻结证据：某一时刻分析结果与附件版本的引用及整体摘要。"""

    snapshot_id: str
    trial_id: str
    created_by: str
    created_at: datetime
    result_ids: tuple[str, ...]
    attachment_refs: tuple[AttachmentRef, ...]
    digest: str


@dataclass(frozen=True)
class RequirementVersion:
    """某法域某类要求的一个生效版本，随时间生效并携带截止日期。"""

    requirement_id: str
    jurisdiction: str
    kind: RequirementKind
    version: int
    effective_from: date
    effective_to: Optional[date]  # None 表示持续有效
    detail: str
    deadline: Optional[date] = None
    response_window_days: Optional[int] = None  # 问询类要求的答复时限

    def in_force(self, on: date) -> bool:
        if on < self.effective_from:
            return False
        return self.effective_to is None or on <= self.effective_to


@dataclass(frozen=True)
class SealRecord:
    sealed_by: str
    sealed_at: datetime
    seal_no: str


@dataclass(frozen=True)
class Rendition:
    """由冻结证据派生的某语言提交文件；翻译件单独登记，不改动原始结果。"""

    rendition_id: str
    package_id: str
    name: str
    language: str
    sha256: str
    source_snapshot_id: str
    seal: Optional[SealRecord] = None


class PackageStatus(str, Enum):
    DRAFT = "draft"
    IN_APPROVAL = "in_approval"
    APPROVED = "approved"
    SENT = "sent"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class Approval:
    """逐级批准链中的一级。"""

    level: int
    approver: str
    decided_at: datetime
    comment: str = ""


@dataclass
class SubmissionPackage:
    """面向某法域、某语言的提交包，派生自同一份冻结证据。"""

    package_id: str
    jurisdiction: str
    language: str
    snapshot_id: str
    created_by: str
    created_at: datetime
    required_approval_levels: int = 2
    status: PackageStatus = PackageStatus.DRAFT
    approvals: list[Approval] = field(default_factory=list)
    rendition_ids: list[str] = field(default_factory=list)
    send_ids: list[str] = field(default_factory=list)
    withdrawn_at: Optional[datetime] = None
    withdraw_reason: Optional[str] = None


@dataclass(frozen=True)
class FileDigest:
    """递交当日某一文件的哈希留痕。"""

    name: str
    sha256: str
    origin: str  # "evidence" 证据附件 | "rendition" 语言版本
    version: Optional[int] = None
    language: Optional[str] = None


@dataclass(frozen=True)
class SealStatus:
    """递交当日某语言版本的签章状态留痕。"""

    name: str
    language: str
    sealed: bool
    sealed_by: Optional[str] = None
    sealed_at: Optional[datetime] = None
    seal_no: Optional[str] = None


@dataclass(frozen=True)
class SendRecord:
    """一次递交的不可变记录：当天发送的哈希、法规版本、签章与批准链。"""

    send_id: str
    package_id: str
    sent_by: str
    sent_at: datetime
    snapshot_id: str
    snapshot_digest: str
    files: tuple[FileDigest, ...]
    requirement_ids: tuple[str, ...]
    seals: tuple[SealStatus, ...]
    approvals: tuple[Approval, ...]


class InquiryStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"


@dataclass
class Inquiry:
    inquiry_id: str
    package_id: str
    jurisdiction: str
    question: str
    received_at: datetime
    response_due_at: Optional[datetime]
    status: InquiryStatus = InquiryStatus.OPEN
    response_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InquiryResponse:
    """审评问询的答复，只能引用可验证的快照。"""

    response_id: str
    inquiry_id: str
    body: str
    snapshot_ids: tuple[str, ...]
    created_by: str
    created_at: datetime


class TaskStatus(str, Enum):
    OPEN = "open"
    DONE = "done"


@dataclass
class Task:
    task_id: str
    title: str
    owner: str
    due_on: Optional[date] = None
    status: TaskStatus = TaskStatus.OPEN
    related_ref: Optional[str] = None


@dataclass(frozen=True)
class Notification:
    notification_id: str
    recipient: str
    kind: str  # "reassignment" 负责人变更 | "deadline_change" 时限调整
    message: str
    related_task_id: str
    created_at: datetime


@dataclass(frozen=True)
class Event:
    """追加式事件：所有状态变化的历史留痕。"""

    seq: int
    at: datetime
    type: str
    actor: str
    detail: dict


@dataclass(frozen=True)
class AuditReport:
    """对某次递交的审计结果：精确还原发送当日状态。"""

    send_id: str
    package_id: str
    jurisdiction: str
    language: str
    sent_at: datetime
    snapshot_id: str
    snapshot_digest: str
    files: tuple[FileDigest, ...]
    requirements: tuple[RequirementVersion, ...]
    seals: tuple[SealStatus, ...]
    approvals: tuple[Approval, ...]
    verified: bool
