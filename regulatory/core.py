"""跨境申报的领域服务：证据冻结、提交包生成、问询答复与责任追溯。

所有状态变化都追加事件，历史只可追加不可改写；内容按哈希寻址，
上传重试、同名替换、撤回重提都不会打断既有历史。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from .model import (
    AnalysisResult,
    Approval,
    Attachment,
    AttachmentRef,
    AttachmentVersion,
    AuditReport,
    Drug,
    Event,
    EvidenceSnapshot,
    FileDigest,
    Inquiry,
    InquiryResponse,
    InquiryStatus,
    Notification,
    PackageStatus,
    Rendition,
    RequirementKind,
    RequirementVersion,
    SealRecord,
    SealStatus,
    SendRecord,
    SubjectGroup,
    SubmissionPackage,
    Task,
    TaskStatus,
    Trial,
)


class DomainError(Exception):
    """领域规则被违反。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


class UnverifiableSnapshotError(DomainError):
    """快照不存在或校验失败，不可引用。"""


class SealRequiredError(DomainError):
    """法域要求翻译签章，存在未签章文件。"""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class RegulatoryService:
    """内存版领域服务；时钟可注入，便于审计场景复现。"""

    def __init__(self, clock: Optional[Callable[[], datetime]] = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._seq = 0
        self._counters: dict[str, int] = {}
        self.drugs: dict[str, Drug] = {}
        self.trials: dict[str, Trial] = {}
        self.groups: dict[str, SubjectGroup] = {}
        self.results: dict[str, AnalysisResult] = {}
        self.attachments: dict[str, Attachment] = {}
        self._attachment_by_name: dict[str, str] = {}
        self.content_store: dict[str, bytes] = {}
        self.snapshots: dict[str, EvidenceSnapshot] = {}
        self.requirements: dict[str, RequirementVersion] = {}
        self.packages: dict[str, SubmissionPackage] = {}
        self.renditions: dict[str, Rendition] = {}
        self.sends: dict[str, SendRecord] = {}
        self.inquiries: dict[str, Inquiry] = {}
        self.responses: dict[str, InquiryResponse] = {}
        self.tasks: dict[str, Task] = {}
        self.notifications: dict[str, Notification] = {}
        self.events: list[Event] = []

    # ---- 基础设施 ----

    def _now(self) -> datetime:
        return self._clock()

    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]}"

    def _emit(self, type_: str, actor: str, **detail) -> None:
        self._seq += 1
        self.events.append(Event(seq=self._seq, at=self._now(), type=type_, actor=actor, detail=detail))

    def history(self) -> tuple[Event, ...]:
        return tuple(self.events)

    def _content_intact(self, sha256: str) -> bool:
        content = self.content_store.get(sha256)
        return content is not None and _sha256(content) == sha256

    # ---- 基础档案：药品 → 试验 → 受试者分组 → 分析结果 → 附件 ----

    def register_drug(self, name: str, sponsor: str, actor: str = "system") -> Drug:
        drug = Drug(self._next_id("drug"), name, sponsor)
        self.drugs[drug.drug_id] = drug
        self._emit("drug_registered", actor, drug_id=drug.drug_id, name=name)
        return drug

    def register_trial(self, drug_id: str, phase: str, title: str, actor: str = "system") -> Trial:
        if drug_id not in self.drugs:
            raise NotFoundError(f"药品不存在：{drug_id}")
        trial = Trial(self._next_id("trial"), drug_id, phase, title)
        self.trials[trial.trial_id] = trial
        self._emit("trial_registered", actor, trial_id=trial.trial_id, drug_id=drug_id)
        return trial

    def register_subject_group(
        self, trial_id: str, arm: str, population: str, size: int, actor: str = "system"
    ) -> SubjectGroup:
        if trial_id not in self.trials:
            raise NotFoundError(f"试验不存在：{trial_id}")
        group = SubjectGroup(self._next_id("grp"), trial_id, arm, population, size)
        self.groups[group.group_id] = group
        self._emit("subject_group_registered", actor, group_id=group.group_id, trial_id=trial_id)
        return group

    def register_analysis_result(
        self,
        trial_id: str,
        subject_group_ids: tuple[str, ...],
        endpoint: str,
        content: bytes,
        supersedes: Optional[str] = None,
        actor: str = "system",
    ) -> AnalysisResult:
        if trial_id not in self.trials:
            raise NotFoundError(f"试验不存在：{trial_id}")
        for gid in subject_group_ids:
            group = self.groups.get(gid)
            if group is None or group.trial_id != trial_id:
                raise StateError(f"受试者分组不属于该试验：{gid}")
        if supersedes is not None and supersedes not in self.results:
            raise NotFoundError(f"被取代的分析结果不存在：{supersedes}")
        sha = _sha256(content)
        self.content_store[sha] = content
        result = AnalysisResult(
            self._next_id("res"), trial_id, tuple(sorted(subject_group_ids)), endpoint, sha, supersedes
        )
        self.results[result.result_id] = result
        self._emit("analysis_result_registered", actor, result_id=result.result_id, trial_id=trial_id, sha256=sha)
        return result

    def upload_attachment(self, name: str, content: bytes, uploaded_by: str) -> tuple[AttachmentVersion, bool]:
        """上传附件。返回 (版本, 是否新建版本)。

        重试（同名同内容）幂等返回既有版本；同名不同内容追加新版本，历史保留。
        """
        sha = _sha256(content)
        self.content_store[sha] = content
        existing_id = self._attachment_by_name.get(name)
        if existing_id is None:
            attachment = Attachment(self._next_id("att"), name)
            version = AttachmentVersion(attachment.attachment_id, 1, name, sha, uploaded_by, self._now())
            attachment.versions.append(version)
            self.attachments[attachment.attachment_id] = attachment
            self._attachment_by_name[name] = attachment.attachment_id
            self._emit("attachment_created", uploaded_by, attachment_id=attachment.attachment_id, name=name, sha256=sha)
            return version, True
        attachment = self.attachments[existing_id]
        latest = attachment.latest
        if latest.sha256 == sha:
            self._emit("attachment_upload_replayed", uploaded_by, attachment_id=attachment.attachment_id, version=latest.version)
            return latest, False
        version = AttachmentVersion(attachment.attachment_id, latest.version + 1, name, sha, uploaded_by, self._now())
        attachment.versions.append(version)
        self._emit(
            "attachment_replaced", uploaded_by, attachment_id=attachment.attachment_id, name=name,
            version=version.version, sha256=sha,
        )
        return version, True

    # ---- 证据快照：冻结后可验证，后续文件更新不影响 ----

    def freeze_snapshot(
        self,
        trial_id: str,
        result_ids: tuple[str, ...],
        attachment_ids: tuple[str, ...],
        created_by: str,
    ) -> EvidenceSnapshot:
        if trial_id not in self.trials:
            raise NotFoundError(f"试验不存在：{trial_id}")
        if not result_ids and not attachment_ids:
            raise StateError("快照至少需要一项分析结果或附件")
        results = []
        for rid in result_ids:
            result = self.results.get(rid)
            if result is None:
                raise NotFoundError(f"分析结果不存在：{rid}")
            if result.trial_id != trial_id:
                raise StateError(f"分析结果不属于该试验：{rid}")
            results.append(result)
        refs = []
        for aid in attachment_ids:
            attachment = self.attachments.get(aid)
            if attachment is None:
                raise NotFoundError(f"附件不存在：{aid}")
            latest = attachment.latest
            refs.append(AttachmentRef(aid, attachment.name, latest.version, latest.sha256))
        refs.sort(key=lambda ref: ref.attachment_id)
        results.sort(key=lambda r: r.result_id)
        snapshot = EvidenceSnapshot(
            snapshot_id=self._next_id("snap"),
            trial_id=trial_id,
            created_by=created_by,
            created_at=self._now(),
            result_ids=tuple(r.result_id for r in results),
            attachment_refs=tuple(refs),
            digest=self._snapshot_digest(trial_id, results, refs),
        )
        self.snapshots[snapshot.snapshot_id] = snapshot
        self._emit("snapshot_frozen", created_by, snapshot_id=snapshot.snapshot_id, digest=snapshot.digest)
        return snapshot

    def _snapshot_digest(self, trial_id: str, results: list[AnalysisResult], refs: list[AttachmentRef]) -> str:
        payload = {
            "trial_id": trial_id,
            "results": [{"result_id": r.result_id, "sha256": r.sha256} for r in sorted(results, key=lambda r: r.result_id)],
            "attachments": [
                {"attachment_id": ref.attachment_id, "version": ref.version, "sha256": ref.sha256}
                for ref in sorted(refs, key=lambda ref: ref.attachment_id)
            ],
        }
        return _sha256(_canonical(payload))

    def verify_snapshot(self, snapshot_id: str) -> bool:
        """重新计算摘要并核对每个引用，证明快照仍可验证。"""
        snapshot = self.snapshots.get(snapshot_id)
        if snapshot is None:
            return False
        try:
            results = [self.results[rid] for rid in snapshot.result_ids]
            for ref in snapshot.attachment_refs:
                version = self.attachments[ref.attachment_id].versions[ref.version - 1]
                if version.sha256 != ref.sha256 or not self._content_intact(ref.sha256):
                    return False
            if any(not self._content_intact(r.sha256) for r in results):
                return False
            digest = self._snapshot_digest(snapshot.trial_id, results, list(snapshot.attachment_refs))
            return digest == snapshot.digest
        except (KeyError, IndexError):
            return False

    # ---- 法域要求：随时间生效的版本与截止日期 ----

    def add_requirement(
        self,
        jurisdiction: str,
        kind,
        effective_from: date,
        detail: str,
        *,
        effective_to: Optional[date] = None,
        deadline: Optional[date] = None,
        response_window_days: Optional[int] = None,
        actor: str = "system",
    ) -> RequirementVersion:
        kind = RequirementKind(kind)
        version = 1 + sum(
            1 for r in self.requirements.values() if r.jurisdiction == jurisdiction and r.kind is kind
        )
        requirement = RequirementVersion(
            self._next_id("req"), jurisdiction, kind, version,
            effective_from, effective_to, detail, deadline, response_window_days,
        )
        self.requirements[requirement.requirement_id] = requirement
        self._emit(
            "requirement_registered", actor, requirement_id=requirement.requirement_id,
            jurisdiction=jurisdiction, kind=kind.value, version=version,
        )
        return requirement

    def requirements_in_force(self, jurisdiction: str, on: date) -> tuple[RequirementVersion, ...]:
        """某法域在某日实际生效的要求（每类取生效中的最新版本）。"""
        best: dict[RequirementKind, RequirementVersion] = {}
        for requirement in self.requirements.values():
            if requirement.jurisdiction != jurisdiction or not requirement.in_force(on):
                continue
            current = best.get(requirement.kind)
            if current is None or requirement.version > current.version:
                best[requirement.kind] = requirement
        return tuple(sorted(best.values(), key=lambda r: r.kind.value))

    # ---- 提交包：同一冻结证据派生多语言版本 ----

    def generate_package(
        self,
        jurisdiction: str,
        language: str,
        snapshot_id: str,
        documents: list[tuple[str, bytes]],
        created_by: str,
        required_approval_levels: int = 2,
    ) -> SubmissionPackage:
        if not self.verify_snapshot(snapshot_id):
            raise UnverifiableSnapshotError(f"快照不存在或校验失败：{snapshot_id}")
        package = SubmissionPackage(
            self._next_id("pkg"), jurisdiction, language, snapshot_id, created_by, self._now(),
            required_approval_levels,
        )
        self.packages[package.package_id] = package
        for name, content in documents:
            sha = _sha256(content)
            self.content_store[sha] = content
            rendition = Rendition(self._next_id("rend"), package.package_id, name, language, sha, snapshot_id)
            self.renditions[rendition.rendition_id] = rendition
            package.rendition_ids.append(rendition.rendition_id)
        self._emit(
            "package_generated", created_by, package_id=package.package_id,
            jurisdiction=jurisdiction, language=language, snapshot_id=snapshot_id,
        )
        return package

    def seal_rendition(self, rendition_id: str, sealed_by: str, seal_no: str) -> Rendition:
        rendition = self.renditions.get(rendition_id)
        if rendition is None:
            raise NotFoundError(f"语言版本不存在：{rendition_id}")
        sealed = replace(rendition, seal=SealRecord(sealed_by, self._now(), seal_no))
        self.renditions[rendition_id] = sealed
        self._emit("rendition_sealed", sealed_by, rendition_id=rendition_id, seal_no=seal_no)
        return sealed

    def approve(self, package_id: str, approver: str, comment: str = "") -> SubmissionPackage:
        """逐级批准：级别必须依次推进，同一批准人不可重复批准。"""
        package = self._package(package_id)
        if package.status not in (PackageStatus.DRAFT, PackageStatus.IN_APPROVAL):
            raise StateError(f"当前状态不可批准：{package.status.value}")
        if any(a.approver == approver for a in package.approvals):
            raise StateError(f"批准人已在链中：{approver}")
        level = len(package.approvals) + 1
        package.approvals.append(Approval(level, approver, self._now(), comment))
        package.status = (
            PackageStatus.APPROVED if level >= package.required_approval_levels else PackageStatus.IN_APPROVAL
        )
        self._emit("package_approved", approver, package_id=package_id, level=level, status=package.status.value)
        return package

    def send_package(self, package_id: str, sent_by: str) -> SendRecord:
        """递交：固化当日的文件哈希、生效法规版本、签章状态与批准链。"""
        package = self._package(package_id)
        if package.status is not PackageStatus.APPROVED:
            raise StateError("提交包尚未完成逐级批准")
        now = self._now()
        in_force = self.requirements_in_force(package.jurisdiction, now.date())
        renditions = [self.renditions[rid] for rid in package.rendition_ids]
        if any(r.kind is RequirementKind.TRANSLATION_SEAL for r in in_force):
            missing = [r.name for r in renditions if r.seal is None]
            if missing:
                raise SealRequiredError(f"法域要求翻译签章，未签章文件：{missing}")
        snapshot = self.snapshots[package.snapshot_id]
        files = [
            FileDigest(ref.name, ref.sha256, "evidence", version=ref.version)
            for ref in snapshot.attachment_refs
        ]
        seals = []
        for rendition in renditions:
            files.append(FileDigest(rendition.name, rendition.sha256, "rendition", language=rendition.language))
            seal = rendition.seal
            seals.append(SealStatus(
                rendition.name, rendition.language, seal is not None,
                seal.sealed_by if seal else None,
                seal.sealed_at if seal else None,
                seal.seal_no if seal else None,
            ))
        record = SendRecord(
            send_id=self._next_id("send"),
            package_id=package_id,
            sent_by=sent_by,
            sent_at=now,
            snapshot_id=snapshot.snapshot_id,
            snapshot_digest=snapshot.digest,
            files=tuple(files),
            requirement_ids=tuple(r.requirement_id for r in in_force),
            seals=tuple(seals),
            approvals=tuple(package.approvals),
        )
        self.sends[record.send_id] = record
        package.send_ids.append(record.send_id)
        package.status = PackageStatus.SENT
        self._emit(
            "package_sent", sent_by, package_id=package_id,
            send_id=record.send_id, snapshot_digest=snapshot.digest,
        )
        return record

    def withdraw_package(self, package_id: str, reason: str, actor: str) -> SubmissionPackage:
        package = self._package(package_id)
        if package.status is not PackageStatus.SENT:
            raise StateError("仅已递交的提交包可撤回")
        package.status = PackageStatus.WITHDRAWN
        package.withdrawn_at = self._now()
        package.withdraw_reason = reason
        self._emit("package_withdrawn", actor, package_id=package_id, reason=reason)
        return package

    def resubmit_package(self, package_id: str, actor: str) -> SubmissionPackage:
        """撤回后重提：开启新一轮批准，历史递交记录与批准链保留在事件与递交记录中。"""
        package = self._package(package_id)
        if package.status is not PackageStatus.WITHDRAWN:
            raise StateError("仅已撤回的提交包可重提")
        package.status = PackageStatus.IN_APPROVAL
        package.approvals = []
        self._emit("package_resubmitted", actor, package_id=package_id, round=len(package.send_ids) + 1)
        return package

    # ---- 审评问询：答复只能引用可验证快照 ----

    def receive_inquiry(
        self, package_id: str, question: str, received_at: Optional[datetime] = None
    ) -> Inquiry:
        package = self._package(package_id)
        received = received_at or self._now()
        window = next(
            (
                r.response_window_days
                for r in self.requirements_in_force(package.jurisdiction, received.date())
                if r.kind is RequirementKind.INQUIRY_RESPONSE and r.response_window_days
            ),
            None,
        )
        due = received + timedelta(days=window) if window else None
        inquiry = Inquiry(
            self._next_id("inq"), package_id, package.jurisdiction, question, received, due
        )
        self.inquiries[inquiry.inquiry_id] = inquiry
        self.create_task(
            f"答复{package.jurisdiction}审评问询 {inquiry.inquiry_id}",
            owner=package.created_by,
            due_on=due.date() if due else None,
            related_ref=inquiry.inquiry_id,
        )
        self._emit(
            "inquiry_received", "system", inquiry_id=inquiry.inquiry_id, package_id=package_id,
            response_due_at=due.isoformat() if due else None,
        )
        return inquiry

    def respond_to_inquiry(
        self, inquiry_id: str, body: str, snapshot_ids: tuple[str, ...], created_by: str
    ) -> InquiryResponse:
        inquiry = self.inquiries.get(inquiry_id)
        if inquiry is None:
            raise NotFoundError(f"问询不存在：{inquiry_id}")
        if inquiry.status is InquiryStatus.ANSWERED:
            raise StateError("问询已答复")
        if not snapshot_ids:
            raise UnverifiableSnapshotError("答复必须引用至少一份可验证快照")
        for snapshot_id in snapshot_ids:
            if not self.verify_snapshot(snapshot_id):
                raise UnverifiableSnapshotError(f"快照不存在或校验失败：{snapshot_id}")
        response = InquiryResponse(
            self._next_id("resp"), inquiry_id, body, tuple(snapshot_ids), created_by, self._now()
        )
        self.responses[response.response_id] = response
        inquiry.response_ids.append(response.response_id)
        inquiry.status = InquiryStatus.ANSWERED
        for task in self.tasks.values():
            if task.related_ref == inquiry_id and task.status is TaskStatus.OPEN:
                task.status = TaskStatus.DONE
        self._emit(
            "inquiry_responded", created_by, inquiry_id=inquiry_id,
            response_id=response.response_id, snapshot_ids=list(snapshot_ids),
        )
        return response

    # ---- 事项与通知：负责人变更、时限调整 ----

    def create_task(
        self, title: str, owner: str, due_on: Optional[date] = None, related_ref: Optional[str] = None
    ) -> Task:
        task = Task(self._next_id("task"), title, owner, due_on, TaskStatus.OPEN, related_ref)
        self.tasks[task.task_id] = task
        self._emit("task_created", "system", task_id=task.task_id, owner=owner)
        return task

    def complete_task(self, task_id: str, actor: str = "system") -> Task:
        task = self._task(task_id)
        if task.status is TaskStatus.DONE:
            raise StateError("事项已完成")
        task.status = TaskStatus.DONE
        self._emit("task_completed", actor, task_id=task_id)
        return task

    def reassign_owner(self, old_owner: str, new_owner: str, changed_by: str) -> tuple[Task, ...]:
        """负责人变更：迁移其全部未完成事项，并留下通知记录。"""
        moved = []
        for task in self.tasks.values():
            if task.owner == old_owner and task.status is TaskStatus.OPEN:
                task.owner = new_owner
                moved.append(task)
                self._notify(
                    new_owner, "reassignment",
                    f"事项「{task.title}」由 {old_owner} 迁移至 {new_owner}", task.task_id,
                )
        self._emit(
            "tasks_reassigned", changed_by, from_owner=old_owner, to_owner=new_owner,
            task_ids=[t.task_id for t in moved],
        )
        return tuple(moved)

    def adjust_task_deadline(self, task_id: str, new_due: date, reason: str, changed_by: str) -> Task:
        """时限调整：更新未完成事项的截止日期，并留下通知记录。"""
        task = self._task(task_id)
        if task.status is TaskStatus.DONE:
            raise StateError("已完成事项不可调整时限")
        old_due = task.due_on
        task.due_on = new_due
        self._notify(
            task.owner, "deadline_change",
            f"事项「{task.title}」截止由 {old_due} 调整为 {new_due}：{reason}", task.task_id,
        )
        self._emit(
            "task_deadline_adjusted", changed_by, task_id=task_id,
            old=str(old_due), new=str(new_due), reason=reason,
        )
        return task

    def notifications_for(self, recipient: str) -> tuple[Notification, ...]:
        return tuple(n for n in self.notifications.values() if n.recipient == recipient)

    def _notify(self, recipient: str, kind: str, message: str, task_id: str) -> Notification:
        notification = Notification(self._next_id("ntf"), recipient, kind, message, task_id, self._now())
        self.notifications[notification.notification_id] = notification
        return notification

    # ---- 审计：精确还原某次递交 ----

    def audit_send(self, send_id: str) -> AuditReport:
        record = self.sends.get(send_id)
        if record is None:
            raise NotFoundError(f"递交记录不存在：{send_id}")
        package = self.packages[record.package_id]
        requirements = tuple(self.requirements[rid] for rid in record.requirement_ids)
        return AuditReport(
            send_id=record.send_id,
            package_id=record.package_id,
            jurisdiction=package.jurisdiction,
            language=package.language,
            sent_at=record.sent_at,
            snapshot_id=record.snapshot_id,
            snapshot_digest=record.snapshot_digest,
            files=record.files,
            requirements=requirements,
            seals=record.seals,
            approvals=record.approvals,
            verified=self._verify_send(record),
        )

    def _verify_send(self, record: SendRecord) -> bool:
        if not self.verify_snapshot(record.snapshot_id):
            return False
        package = self.packages[record.package_id]
        renditions = [self.renditions[rid] for rid in package.rendition_ids]
        for file in record.files:
            if not self._content_intact(file.sha256):
                return False
            if file.origin == "evidence":
                attachment = self.attachments.get(self._attachment_by_name.get(file.name, ""))
                if attachment is None or file.version is None:
                    return False
                try:
                    if attachment.versions[file.version - 1].sha256 != file.sha256:
                        return False
                except IndexError:
                    return False
            elif not any(r.name == file.name and r.sha256 == file.sha256 for r in renditions):
                return False
        return True

    # ---- 内部查找 ----

    def _package(self, package_id: str) -> SubmissionPackage:
        package = self.packages.get(package_id)
        if package is None:
            raise NotFoundError(f"提交包不存在：{package_id}")
        return package

    def _task(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"事项不存在：{task_id}")
        return task
