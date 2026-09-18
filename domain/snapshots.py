"""证据快照冻结。

快照是一次申报的"证据原件"：选定试验、人群分组、分析结果与补充附件后
计算确定性清单（manifest）并封存。快照一经冻结不可修改——证据更新只
能重新冻结出一个新快照。所有法域的提交包都只能引用已冻结快照，因此
共享盘文件再怎么更新，已递交资料的原貌仍可逐字节还原。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from .artifacts import ArtifactStore
from .errors import IntegrityError, WorkflowError
from .events import EventLog
from .hashing import digest_json
from .study import StudyGraph


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    drug_id: str
    trial_id: str
    group_ids: tuple[str, ...]
    result_ids: tuple[str, ...]
    attachment_refs: tuple[dict, ...]
    manifest: dict
    manifest_sha256: str
    frozen_at: str
    frozen_by: str

    def ref(self) -> dict:
        """提交包/问询回复引用快照的唯一指针。"""
        return {
            "snapshot_id": self.snapshot_id,
            "manifest_sha256": self.manifest_sha256,
        }


class SnapshotService:
    def __init__(self, events: EventLog, graph: StudyGraph, artifacts: ArtifactStore):
        self._events = events
        self._graph = graph
        self._artifacts = artifacts
        self._snapshots: dict[str, Snapshot] = {}

    def freeze(
        self,
        trial_id: str,
        group_ids: list[str],
        result_ids: list[str],
        attachment_artifacts: list[tuple[str, int]],
        moment: datetime,
        frozen_by: str,
    ) -> Snapshot:
        """冻结一份证据快照。

        所有引用必须解析到具体的附件版本与字节哈希；任一引用悬空或
        不属于该试验都拒绝冻结。
        """
        trial = self._graph.get_trial(trial_id)
        drug = self._graph.get_drug(trial.drug_id)

        groups = sorted(set(group_ids))
        trial_group_ids = {g.group_id for g in self._graph.groups_of_trial(trial_id)}
        for group_id in groups:
            if group_id not in trial_group_ids:
                raise WorkflowError(f"分组 {group_id} 不属于试验 {trial_id}")

        results = []
        for result_id in sorted(set(result_ids)):
            result = self._graph.get_result(result_id)
            if result.trial_id != trial_id:
                raise WorkflowError(f"分析结果 {result_id} 不属于试验 {trial_id}")
            if not set(result.group_ids).issubset(set(groups)):
                raise WorkflowError(
                    f"分析结果 {result_id} 引用了未纳入快照的受试者分组"
                )
            results.append(result)

        # 汇总全部附件引用：结果统计表 + 显式补充附件，按哈希去重排序。
        refs: dict[str, dict] = {}
        for result in results:
            for ref in result.table_refs:
                refs[ref["sha256"]] = dict(ref)
        for artifact_id, version_no in attachment_artifacts:
            version = self._artifacts.get_version(artifact_id, version_no)
            refs[version.sha256] = version.ref()
        attachment_refs = tuple(refs[key] for key in sorted(refs))

        # 当场读取全部字节验证哈希，冻结后清单中每个哈希都取得到原件。
        for ref in attachment_refs:
            self._artifacts.resolve_ref(ref)

        manifest = {
            "drug": {
                "drug_id": drug.drug_id,
                "name": drug.name,
                "inn": drug.inn,
            },
            "trial": {
                "trial_id": trial.trial_id,
                "code": trial.code,
                "name": trial.name,
                "phase": trial.phase,
                "endpoint": trial.endpoint,
            },
            "groups": [
                {
                    "group_id": gid,
                    "code": self._graph.get_group(gid).code,
                    "name": self._graph.get_group(gid).name,
                    "population": self._graph.get_group(gid).population,
                    "sample_size": self._graph.get_group(gid).sample_size,
                }
                for gid in groups
            ],
            "results": [
                {
                    "result_id": result.result_id,
                    "code": result.code,
                    "title": result.title,
                    "group_ids": sorted(result.group_ids),
                    "table_refs": [dict(r) for r in result.table_refs],
                    "metrics": result.metrics,
                }
                for result in results
            ],
            "attachments": [dict(r) for r in attachment_refs],
        }
        manifest_sha = digest_json(manifest)
        snapshot = Snapshot(
            snapshot_id=str(uuid.uuid4()),
            drug_id=drug.drug_id,
            trial_id=trial_id,
            group_ids=tuple(groups),
            result_ids=tuple(r.result_id for r in results),
            attachment_refs=attachment_refs,
            manifest=manifest,
            manifest_sha256=manifest_sha,
            frozen_at=moment.isoformat(),
            frozen_by=frozen_by,
        )
        self._snapshots[snapshot.snapshot_id] = snapshot
        self._events.append(
            "snapshot.frozen",
            {
                "stream_id": snapshot.snapshot_id,
                "trial_id": trial_id,
                "drug_id": drug.drug_id,
                "group_ids": list(groups),
                "result_ids": sorted(set(result_ids)),
                "attachments": [dict(r) for r in attachment_refs],
                "manifest_sha256": manifest_sha,
            },
            actor=frozen_by,
            timestamp=moment,
        )
        return snapshot

    def get(self, snapshot_id: str) -> Snapshot:
        snapshot = self._snapshots.get(snapshot_id)
        if snapshot is None:
            raise IntegrityError(f"证据快照不存在: {snapshot_id}")
        return snapshot

    def resolve(self, ref: dict) -> Snapshot:
        """解析提交包保存的快照引用，并校验清单哈希未变。"""
        snapshot = self.get(ref["snapshot_id"])
        if snapshot.manifest_sha256 != ref["manifest_sha256"]:
            raise IntegrityError(
                f"快照 {snapshot.snapshot_id} 清单哈希与引用不一致"
            )
        if digest_json(snapshot.manifest) != snapshot.manifest_sha256:
            raise IntegrityError(f"快照 {snapshot.snapshot_id} 清单已被篡改")
        return snapshot

    def verify(self, snapshot: Snapshot) -> None:
        """重新计算清单哈希并逐份回取附件字节，证明快照原貌仍可还原。"""
        if digest_json(snapshot.manifest) != snapshot.manifest_sha256:
            raise IntegrityError(f"快照 {snapshot.snapshot_id} 清单哈希失配")
        for ref in snapshot.attachment_refs:
            self._artifacts.resolve_ref(ref)
