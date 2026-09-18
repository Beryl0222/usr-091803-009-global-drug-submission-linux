"""跨法域提交包：从同一份冻结证据生成多语言递交。

关键不变量：
* 提交包只引用已冻结快照（snapshot_id + manifest_sha256），原始分析
  结果字节永不进包、永不改动；
* 翻译稿是独立的内容寻址附件，记录其来源文件哈希，可验证"译文对应
  的就是冻结版原件"；
* 提交时锁定当时生效的法规版本、截止日期、签章状态与逐级批准人，
  形成发送回执；撤回只追加事件，重提产生新修订版，历史不断。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from .artifacts import ArtifactStore
from .clock import Clock
from .errors import NotFoundError, RuleViolation, WorkflowError
from .events import EventLog
from .hashing import digest_json
from .requirements import RequirementRegistry, RequirementVersion
from .snapshots import Snapshot, SnapshotService

DRAFT = "DRAFT"
APPROVED = "APPROVED"
SUBMITTED = "SUBMITTED"
WITHDRAWN = "WITHDRAWN"


@dataclass(frozen=True)
class Translation:
    source_sha256: str
    language: str
    artifact_id: str
    version: int
    translated_sha256: str
    translator: str
    created_at: str


@dataclass(frozen=True)
class Signature:
    doc_sha256: str
    signer: str
    credential: str
    signed_at: str
    signature_sha256: str  # 签章页/电子签章负载的内容哈希


@dataclass(frozen=True)
class Approval:
    level: str
    approver: str
    approved_at: str


@dataclass(frozen=True)
class PackageEntry:
    role: str
    name: str
    sha256: str
    language: str
    source_sha256: str | None  # 译文指向快照原件；原件为 None


@dataclass(frozen=True)
class SubmissionPackage:
    package_id: str
    revision_of: str | None     # 撤回重提时指向上一版包
    jurisdiction: str
    language: str
    snapshot_id: str
    snapshot_manifest_sha256: str
    requirement_refs: tuple[dict, ...]
    due_at: str | None
    entries: tuple[PackageEntry, ...]
    manifest_sha256: str
    status: str
    approvals: tuple[Approval, ...]
    signatures: tuple[Signature, ...]
    created_by: str
    created_at: str
    sent_at: str | None = None
    sent_by: str | None = None
    withdrawn_at: str | None = None
    withdraw_reason: str | None = None


class SubmissionService:
    def __init__(
        self,
        events: EventLog,
        snapshots: SnapshotService,
        artifacts: ArtifactStore,
        requirements: RequirementRegistry,
        clock: Clock,
        approval_levels: dict[str, list[str]] | None = None,
    ):
        self._events = events
        self._snapshots = snapshots
        self._artifacts = artifacts
        self._requirements = requirements
        self._clock = clock
        self._approval_levels = approval_levels or {}
        self._translations: dict[tuple[str, str], Translation] = {}
        self._packages: dict[str, SubmissionPackage] = {}

    # ------------------------------------------------------------------ 翻译

    def add_translation(
        self,
        source_sha256: str,
        language: str,
        translated_artifact_id: str,
        version: int,
        translator: str,
        moment: datetime,
    ) -> Translation:
        """登记译文。源哈希必须能在附件库取到原件，译文是另一版本附件。"""
        self._artifacts.get_bytes(source_sha256)
        translated = self._artifacts.get_version(translated_artifact_id, version)
        if translated.tag("language") != language:
            raise RuleViolation(
                f"译文附件必须带 language={language} 标记，实际: "
                f"{translated.tag('language')}"
            )
        key = (source_sha256, language)
        if key in self._translations:
            raise WorkflowError(
                f"原件 {source_sha256[:8]}… 的 {language} 译文已存在；"
                "更新需登记新附件版本后再引用"
            )
        record = Translation(
            source_sha256=source_sha256,
            language=language,
            artifact_id=translated_artifact_id,
            version=version,
            translated_sha256=translated.sha256,
            translator=translator,
            created_at=moment.isoformat(),
        )
        self._translations[key] = record
        self._events.append(
            "translation.registered",
            {
                "source_sha256": source_sha256,
                "language": language,
                "artifact_id": translated_artifact_id,
                "version": version,
                "translated_sha256": translated.sha256,
                "translator": translator,
            },
            actor=translator,
            timestamp=moment,
        )
        return record

    def get_translation(self, source_sha256: str, language: str) -> Translation | None:
        return self._translations.get((source_sha256, language))

    # ------------------------------------------------------------------ 建包

    def build_package(
        self,
        snapshot_ref: dict,
        jurisdiction: str,
        language: str,
        created_by: str,
        deadline: str | None = None,
        moment: datetime | None = None,
    ) -> SubmissionPackage:
        """从冻结快照组装某法域/语言的提交包草稿。

        组装时即按该法域"当前有效"的要求版本校验人群、统计表、语言；
        要求版本被固化进包，之后指南换版不影响本包。
        """
        moment = moment or self._clock.now()
        snapshot = self._snapshots.resolve(snapshot_ref)
        rules = self._requirements.effective_requirements(jurisdiction, moment)
        self._check_populations(snapshot, rules)
        self._check_table_kinds(snapshot, rules)

        required_languages = set()
        require_signature = False
        for rule in rules:
            required_languages.update(rule.required_languages)
            require_signature = require_signature or rule.require_signature
        if required_languages and language not in required_languages:
            raise RuleViolation(
                f"{jurisdiction} 当前要求语言 {sorted(required_languages)}，"
                f"不接受 {language}"
            )

        entries = self._build_entries(snapshot, language)
        requirement_refs = tuple(
            {
                "requirement_id": rule.requirement_id,
                "version": rule.version,
                "jurisdiction": rule.jurisdiction,
                "title": rule.title,
                "effective_from": rule.effective_from,
            }
            for rule in rules
        )
        manifest = {
            "jurisdiction": jurisdiction,
            "language": language,
            "snapshot": snapshot.ref(),
            "requirements": list(requirement_refs),
            "entries": [
                {
                    "role": entry.role,
                    "name": entry.name,
                    "sha256": entry.sha256,
                    "language": entry.language,
                    "source_sha256": entry.source_sha256,
                }
                for entry in entries
            ],
        }
        package = SubmissionPackage(
            package_id=str(uuid.uuid4()),
            revision_of=None,
            jurisdiction=jurisdiction,
            language=language,
            snapshot_id=snapshot.snapshot_id,
            snapshot_manifest_sha256=snapshot.manifest_sha256,
            requirement_refs=requirement_refs,
            due_at=deadline,
            entries=tuple(entries),
            manifest_sha256=digest_json(manifest),
            status=DRAFT,
            approvals=(),
            signatures=(),
            created_by=created_by,
            created_at=moment.isoformat(),
        )
        self._packages[package.package_id] = package
        self._events.append(
            "package.built",
            {
                "stream_id": package.package_id,
                "jurisdiction": jurisdiction,
                "language": language,
                "snapshot": snapshot.ref(),
                "requirements": list(requirement_refs),
                "entries": manifest["entries"],
                "manifest_sha256": package.manifest_sha256,
                "due_at": deadline,
                "require_signature": require_signature,
            },
            actor=created_by,
            timestamp=moment,
        )
        return package

    def _build_entries(
        self, snapshot: Snapshot, language: str
    ) -> list[PackageEntry]:
        entries: list[PackageEntry] = []
        for ref in snapshot.attachment_refs:
            version = self._artifacts.get_version(ref["artifact_id"], ref["version"])
            source_language = version.tag("language")
            if source_language == language:
                entries.append(
                    PackageEntry(
                        role=version.tag("kind") or "attachment",
                        name=version.name,
                        sha256=version.sha256,
                        language=language,
                        source_sha256=None,
                    )
                )
                continue
            translation = self.get_translation(version.sha256, language)
            if translation is None:
                raise RuleViolation(
                    f"附件 {version.name} 缺少 {language} 译文，"
                    f"原件语言 {source_language}"
                )
            entries.append(
                PackageEntry(
                    role=(version.tag("kind") or "attachment") + ".translation",
                    name=version.name,
                    sha256=translation.translated_sha256,
                    language=language,
                    source_sha256=version.sha256,
                )
            )
        return entries

    def _check_populations(
        self, snapshot: Snapshot, rules: list[RequirementVersion]
    ) -> None:
        required = set()
        for rule in rules:
            required.update(rule.required_populations)
        if not required:
            return
        present = {
            self._snapshot_population(snapshot, gid)
            for gid in snapshot.group_ids
        }
        missing = required - present
        if missing:
            raise RuleViolation(f"快照缺少法域要求的人群: {sorted(missing)}")

    def _snapshot_population(self, snapshot: Snapshot, group_id: str) -> str:
        for group in snapshot.manifest["groups"]:
            if group["group_id"] == group_id:
                return group["population"]
        raise NotFoundError(f"清单中找不到分组: {group_id}")

    def _check_table_kinds(
        self, snapshot: Snapshot, rules: list[RequirementVersion]
    ) -> None:
        required = set()
        for rule in rules:
            required.update(rule.required_table_kinds)
        if not required:
            return
        present = set()
        for ref in snapshot.attachment_refs:
            version = self._artifacts.get_version(ref["artifact_id"], ref["version"])
            kind = version.tag("kind")
            if kind:
                present.add(kind)
        missing = required - present
        if missing:
            raise RuleViolation(f"快照缺少法域要求的统计表: {sorted(missing)}")

    # ------------------------------------------------------------------ 签章

    def sign_entry(
        self,
        package_id: str,
        entry_sha256: str,
        signer: str,
        credential: str,
        signature_artifact_id: str,
        signature_version: int,
        moment: datetime,
    ) -> Signature:
        package = self._require_package(package_id)
        self._require_draft(package)
        entry_hashes = {e.sha256 for e in package.entries}
        if entry_sha256 not in entry_hashes:
            raise NotFoundError("包内不存在该文件，无法签章")
        sig_artifact = self._artifacts.get_version(
            signature_artifact_id, signature_version
        )
        signature = Signature(
            doc_sha256=entry_sha256,
            signer=signer,
            credential=credential,
            signed_at=moment.isoformat(),
            signature_sha256=sig_artifact.sha256,
        )
        signatures = tuple(
            s for s in package.signatures if s.doc_sha256 != entry_sha256
        ) + (signature,)
        self._packages[package_id] = self._replace(package, signatures=signatures)
        self._events.append(
            "package.signed",
            {
                "stream_id": package_id,
                "doc_sha256": entry_sha256,
                "signer": signer,
                "credential": credential,
                "signature_sha256": sig_artifact.sha256,
            },
            actor=signer,
            timestamp=moment,
        )
        return signature

    # ------------------------------------------------------------------ 批准

    def approve(
        self, package_id: str, approver: str, moment: datetime | None = None
    ) -> Approval:
        """按该法域配置的批准级别逐级批准，不得跳级、重复。"""
        moment = moment or self._clock.now()
        package = self._require_package(package_id)
        self._require_draft(package)
        levels = self._approval_levels.get(package.jurisdiction, [])
        if not levels:
            raise WorkflowError(f"{package.jurisdiction} 未配置批准级别")
        done = [a.level for a in package.approvals]
        if len(done) >= len(levels):
            raise WorkflowError("各级别均已批准")
        expected = levels[len(done)]
        approval = Approval(
            level=expected, approver=approver, approved_at=moment.isoformat()
        )
        approvals = package.approvals + (approval,)
        status = APPROVED if len(approvals) == len(levels) else DRAFT
        self._packages[package_id] = self._replace(
            package, approvals=approvals, status=status
        )
        self._events.append(
            "package.approved",
            {
                "stream_id": package_id,
                "level": expected,
                "index": len(approvals),
                "of_levels": len(levels),
                "approver": approver,
                "status": status,
            },
            actor=approver,
            timestamp=moment,
        )
        return approval

    # ------------------------------------------------------------------ 发送

    def submit(
        self,
        package_id: str,
        submitted_by: str,
        moment: datetime | None = None,
    ) -> SubmissionPackage:
        moment = moment or self._clock.now()
        package = self._require_package(package_id)
        levels = self._approval_levels.get(package.jurisdiction, [])
        if len(package.approvals) != len(levels) or not levels:
            raise WorkflowError("提交前必须完成全部逐级批准")
        if package.status != APPROVED:
            raise WorkflowError(f"包状态为 {package.status}，不可提交")
        if package.due_at is not None:
            due = datetime.fromisoformat(package.due_at)
            if moment > due:
                raise RuleViolation(
                    f"已超过截止日期 {package.due_at}（当前 {moment.isoformat()}）"
                )

        # 发送时点法规必须仍是建包时锁定的版本；期间若已换版，必须基于
        # 新版本重建，确保"当天适用的法规版本"与所审内容一致。
        current_rules = self._requirements.effective_requirements(
            package.jurisdiction, moment
        )
        current_refs = {(r.requirement_id, r.version) for r in current_rules}
        locked_refs = {(r["requirement_id"], r["version"])
                       for r in package.requirement_refs}
        if current_refs != locked_refs:
            raise RuleViolation(
                f"{package.jurisdiction} 法规在建包后已换版，"
                "请基于现行版本重新生成提交包"
            )

        # 重新解析快照并回取全部字节，确保发出去的就是冻结证据。
        snapshot = self._snapshots.resolve(
            {"snapshot_id": package.snapshot_id,
             "manifest_sha256": package.snapshot_manifest_sha256}
        )
        self._snapshots.verify(snapshot)

        require_signature = any(rule.require_signature for rule in current_rules)
        signed_docs = {s.doc_sha256 for s in package.signatures}
        unsigned = [e.name for e in package.entries if e.sha256 not in signed_docs]
        if require_signature and unsigned:
            raise RuleViolation(f"以下文件尚未签章: {unsigned}")

        sent = self._replace(package, status=SUBMITTED, sent_at=moment.isoformat(),
                             sent_by=submitted_by)
        self._packages[package_id] = sent
        self._events.append(
            "submission.sent",
            {
                "stream_id": package_id,
                "jurisdiction": package.jurisdiction,
                "language": package.language,
                "sent_at": sent.sent_at,
                "snapshot": {
                    "snapshot_id": package.snapshot_id,
                    "manifest_sha256": package.snapshot_manifest_sha256,
                },
                "package_manifest_sha256": package.manifest_sha256,
                "requirements": list(package.requirement_refs),
                "files": [
                    {"name": e.name, "sha256": e.sha256, "language": e.language}
                    for e in package.entries
                ],
                "signatures": [
                    {
                        "doc_sha256": s.doc_sha256,
                        "signer": s.signer,
                        "credential": s.credential,
                        "signed_at": s.signed_at,
                        "signature_sha256": s.signature_sha256,
                    }
                    for s in package.signatures
                ],
                "approvers": [
                    {"level": a.level, "approver": a.approver,
                     "approved_at": a.approved_at}
                    for a in package.approvals
                ],
                "due_at": package.due_at,
            },
            actor=submitted_by,
            timestamp=moment,
        )
        return sent

    # ---------------------------------------------------------- 撤回与重提

    def withdraw(
        self,
        package_id: str,
        reason: str,
        withdrawn_by: str,
        moment: datetime | None = None,
    ) -> SubmissionPackage:
        moment = moment or self._clock.now()
        package = self._require_package(package_id)
        if package.status != SUBMITTED:
            raise WorkflowError(f"只有已提交的包可撤回，当前 {package.status}")
        withdrawn = self._replace(
            package, status=WITHDRAWN, withdrawn_at=moment.isoformat(),
            withdraw_reason=reason,
        )
        self._packages[package_id] = withdrawn
        self._events.append(
            "submission.withdrawn",
            {
                "stream_id": package_id,
                "reason": reason,
                "previous_status": SUBMITTED,
            },
            actor=withdrawn_by,
            timestamp=moment,
        )
        return withdrawn

    def resubmit(
        self,
        withdrawn_package_id: str,
        submitted_by: str,
        moment: datetime,
        deadline: str | None = None,
    ) -> SubmissionPackage:
        """撤回后重提：基于同一冻结快照生成新修订版包并完成发送。

        旧包保持 WITHDRAWN 原貌；新包带 revision_of 指针，批准与签章
        需重新完成（责任人可能已变更），发送回执独立成链。
        """
        previous = self._require_package(withdrawn_package_id)
        if previous.status != WITHDRAWN:
            raise WorkflowError("只有已撤回的包可以重提")
        revision = self.build_package(
            snapshot_ref={
                "snapshot_id": previous.snapshot_id,
                "manifest_sha256": previous.snapshot_manifest_sha256,
            },
            jurisdiction=previous.jurisdiction,
            language=previous.language,
            created_by=submitted_by,
            deadline=deadline if deadline is not None else previous.due_at,
            moment=moment,
        )
        # 固化修订血缘（修订号在事件中体现）。
        revision = self._replace(revision, revision_of=previous.package_id)
        self._packages[revision.package_id] = revision
        self._events.append(
            "package.revision_opened",
            {
                "stream_id": revision.package_id,
                "revision_of": previous.package_id,
                "revision": self._revision_number(previous.package_id) + 1,
            },
            actor=submitted_by,
            timestamp=moment,
        )
        return revision

    def _revision_number(self, package_id: str) -> int:
        number = 1
        current = self._packages.get(package_id)
        while current is not None and current.revision_of is not None:
            number += 1
            current = self._packages.get(current.revision_of)
        return number

    # ------------------------------------------------------------------ 读取

    def get_package(self, package_id: str) -> SubmissionPackage:
        return self._require_package(package_id)

    def packages_for_jurisdiction(self, jurisdiction: str) -> list[SubmissionPackage]:
        return [
            p for p in self._packages.values() if p.jurisdiction == jurisdiction
        ]

    def _require_package(self, package_id: str) -> SubmissionPackage:
        package = self._packages.get(package_id)
        if package is None:
            raise NotFoundError(f"提交包不存在: {package_id}")
        return package

    @staticmethod
    def _require_draft(package: SubmissionPackage) -> None:
        if package.status != DRAFT:
            raise WorkflowError(f"包状态为 {package.status}，不可修改")

    @staticmethod
    def _replace(package: SubmissionPackage, **changes) -> SubmissionPackage:
        values = {
            "package_id": package.package_id,
            "revision_of": package.revision_of,
            "jurisdiction": package.jurisdiction,
            "language": package.language,
            "snapshot_id": package.snapshot_id,
            "snapshot_manifest_sha256": package.snapshot_manifest_sha256,
            "requirement_refs": package.requirement_refs,
            "due_at": package.due_at,
            "entries": package.entries,
            "manifest_sha256": package.manifest_sha256,
            "status": package.status,
            "approvals": package.approvals,
            "signatures": package.signatures,
            "created_by": package.created_by,
            "created_at": package.created_at,
            "sent_at": package.sent_at,
            "sent_by": package.sent_by,
            "withdrawn_at": package.withdrawn_at,
            "withdraw_reason": package.withdraw_reason,
        }
        values.update(changes)
        return SubmissionPackage(**values)
