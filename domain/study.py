"""研究领域图谱：药品 → 试验 → 受试者分组 → 分析结果 → 附件。

每一层都持有明确的父引用，登记时即校验引用存在，杜绝悬空外键；
统计报表等证据以 ArtifactVersion 引用（artifact_id + version + sha256）
挂在分析结果上，而不是复制字节。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .artifacts import ArtifactStore
from .errors import NotFoundError, WorkflowError
from .events import EventLog


@dataclass(frozen=True)
class Drug:
    drug_id: str
    name: str
    inn: str
    created_at: str
    created_by: str


@dataclass(frozen=True)
class Trial:
    trial_id: str
    drug_id: str
    code: str
    name: str
    phase: str
    endpoint: str
    created_at: str
    created_by: str


@dataclass(frozen=True)
class SubjectGroup:
    group_id: str
    trial_id: str
    code: str
    name: str
    population: str
    sample_size: int
    created_at: str
    created_by: str


@dataclass(frozen=True)
class AnalysisResult:
    result_id: str
    trial_id: str
    code: str
    title: str
    group_ids: tuple[str, ...]
    # 统计表/图等证据的不可变引用列表。
    table_refs: tuple[dict, ...]
    metrics: dict[str, Any]
    created_at: str
    created_by: str


class StudyGraph:
    def __init__(self, events: EventLog, artifacts: ArtifactStore):
        self._events = events
        self._artifacts = artifacts
        self._drugs: dict[str, Drug] = {}
        self._trials: dict[str, Trial] = {}
        self._groups: dict[str, SubjectGroup] = {}
        self._results: dict[str, AnalysisResult] = {}

    # ------------------------------------------------------------------ 登记

    def register_drug(
        self, name: str, inn: str, moment: datetime, created_by: str
    ) -> Drug:
        drug = Drug(
            drug_id=str(uuid.uuid4()),
            name=name,
            inn=inn,
            created_at=moment.isoformat(),
            created_by=created_by,
        )
        self._drugs[drug.drug_id] = drug
        self._events.append(
            "drug.registered",
            {
                "stream_id": drug.drug_id,
                "name": name,
                "inn": inn,
            },
            actor=created_by,
            timestamp=moment,
        )
        return drug

    def register_trial(
        self,
        drug_id: str,
        code: str,
        name: str,
        phase: str,
        endpoint: str,
        moment: datetime,
        created_by: str,
    ) -> Trial:
        if drug_id not in self._drugs:
            raise NotFoundError(f"药品不存在: {drug_id}")
        trial = Trial(
            trial_id=str(uuid.uuid4()),
            drug_id=drug_id,
            code=code,
            name=name,
            phase=phase,
            endpoint=endpoint,
            created_at=moment.isoformat(),
            created_by=created_by,
        )
        self._trials[trial.trial_id] = trial
        self._events.append(
            "trial.registered",
            {
                "stream_id": trial.trial_id,
                "drug_id": drug_id,
                "code": code,
                "name": name,
                "phase": phase,
                "endpoint": endpoint,
            },
            actor=created_by,
            timestamp=moment,
        )
        return trial

    def register_subject_group(
        self,
        trial_id: str,
        code: str,
        name: str,
        population: str,
        sample_size: int,
        moment: datetime,
        created_by: str,
    ) -> SubjectGroup:
        if trial_id not in self._trials:
            raise NotFoundError(f"试验不存在: {trial_id}")
        if sample_size < 0:
            raise ValueError("样本量不能为负")
        group = SubjectGroup(
            group_id=str(uuid.uuid4()),
            trial_id=trial_id,
            code=code,
            name=name,
            population=population,
            sample_size=sample_size,
            created_at=moment.isoformat(),
            created_by=created_by,
        )
        self._groups[group.group_id] = group
        self._events.append(
            "subject_group.registered",
            {
                "stream_id": group.group_id,
                "trial_id": trial_id,
                "code": code,
                "name": name,
                "population": population,
                "sample_size": sample_size,
            },
            actor=created_by,
            timestamp=moment,
        )
        return group

    def register_analysis_result(
        self,
        trial_id: str,
        code: str,
        title: str,
        group_ids: list[str],
        table_artifacts: list[tuple[str, int]],
        metrics: dict[str, Any],
        moment: datetime,
        created_by: str,
    ) -> AnalysisResult:
        """登记分析结果。

        table_artifacts 为 (artifact_id, version) 列表：只接受已冻结的
        具体版本，禁止"引用某文件的最新版"，避免共享盘更新后结论走样。
        """
        if trial_id not in self._trials:
            raise NotFoundError(f"试验不存在: {trial_id}")
        if not group_ids:
            raise WorkflowError("分析结果必须至少关联一个受试者分组")
        for group_id in group_ids:
            if group_id not in self._groups:
                raise NotFoundError(f"受试者分组不存在: {group_id}")
            if self._groups[group_id].trial_id != trial_id:
                raise WorkflowError(f"分组 {group_id} 不属于试验 {trial_id}")

        table_refs: list[dict] = []
        for artifact_id, version_no in table_artifacts:
            version = self._artifacts.get_version(artifact_id, version_no)
            table_refs.append(version.ref())

        result = AnalysisResult(
            result_id=str(uuid.uuid4()),
            trial_id=trial_id,
            code=code,
            title=title,
            group_ids=tuple(group_ids),
            table_refs=tuple(table_refs),
            metrics=dict(metrics),
            created_at=moment.isoformat(),
            created_by=created_by,
        )
        self._results[result.result_id] = result
        self._events.append(
            "analysis_result.registered",
            {
                "stream_id": result.result_id,
                "trial_id": trial_id,
                "code": code,
                "title": title,
                "group_ids": list(group_ids),
                "table_refs": table_refs,
                "metrics": dict(metrics),
            },
            actor=created_by,
            timestamp=moment,
        )
        return result

    # ------------------------------------------------------------------ 读取

    def get_drug(self, drug_id: str) -> Drug:
        return self._require(self._drugs, drug_id, "药品")

    def get_trial(self, trial_id: str) -> Trial:
        return self._require(self._trials, trial_id, "试验")

    def get_group(self, group_id: str) -> SubjectGroup:
        return self._require(self._groups, group_id, "受试者分组")

    def get_result(self, result_id: str) -> AnalysisResult:
        return self._require(self._results, result_id, "分析结果")

    def groups_of_trial(self, trial_id: str) -> list[SubjectGroup]:
        return [g for g in self._groups.values() if g.trial_id == trial_id]

    def results_of_trial(self, trial_id: str) -> list[AnalysisResult]:
        return [r for r in self._results.values() if r.trial_id == trial_id]

    @staticmethod
    def _require(store: dict, entity_id: str, label: str):
        entity = store.get(entity_id)
        if entity is None:
            raise NotFoundError(f"{label}不存在: {entity_id}")
        return entity
