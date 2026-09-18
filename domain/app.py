"""应用装配门面：把单一事件链与各领域服务接成一套系统。

所有服务共享同一条只增事件日志，因此附件登记、快照冻结、法规换版、
提交发送、问询回复、责任迁移最终都汇入同一条可整体校验的哈希链。
"""

from .artifacts import ArtifactStore
from .audit import AuditService
from .clock import Clock, SystemClock
from .events import EventLog
from .inquiries import InquiryService
from .notifications import NotificationLog
from .requirements import RequirementRegistry
from .snapshots import SnapshotService
from .study import StudyGraph
from .submissions import SubmissionService
from .workflow import WorkflowService


class App:
    def __init__(
        self,
        clock: Clock | None = None,
        approval_levels: dict[str, list[str]] | None = None,
    ):
        self.clock = clock or SystemClock()
        self.events = EventLog()
        self.notifications = NotificationLog(self.events)
        self.artifacts = ArtifactStore(self.events)
        self.graph = StudyGraph(self.events, self.artifacts)
        self.snapshots = SnapshotService(self.events, self.graph, self.artifacts)
        self.requirements = RequirementRegistry(self.events)
        self.submissions = SubmissionService(
            self.events,
            self.snapshots,
            self.artifacts,
            self.requirements,
            self.clock,
            approval_levels=approval_levels,
        )
        self.workflow = WorkflowService(self.events, self.notifications)
        self.inquiries = InquiryService(
            self.events, self.snapshots, self.artifacts, self.workflow
        )
        self.audit = AuditService(self.events)

    def verify_integrity(self) -> None:
        """完整一致性巡检：事件链与附件内容均须通过校验。"""
        self.events.verify()
        self.artifacts.verify()
