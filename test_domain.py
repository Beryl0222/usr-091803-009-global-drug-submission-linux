"""领域端到端测试：覆盖跨境申报的全部关键不变量。"""

import unittest
from datetime import datetime, timezone

from domain.app import App
from domain.artifacts import ArtifactStore
from domain.clock import FixedClock
from domain.errors import (
    IntegrityError,
    NotFoundError,
    RuleViolation,
    WorkflowError,
)
from domain.events import EventLog
from domain.hashing import sha256_bytes
from domain.snapshots import SnapshotService
from domain.study import StudyGraph

APPROVAL_LEVELS = {
    "US": ["manager", "ra_head", "qp"],
    "EU": ["ra_lead", "qp"],
    "JP": ["ra_lead", "qp"],
}


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class Scenario:
    """搭建一套可复用的药品/试验/分组/证据场景。"""

    def __init__(self):
        self.clock = FixedClock(ts("2026-01-01T09:00:00"))
        self.app = App(clock=self.clock, approval_levels=APPROVAL_LEVELS)
        a = self.app

        self.table_v1 = a.artifacts.register(
            "efficacy_table_14_3.csv", b"v1,ORR,0.42", "biostat",
            self.clock.now(), media_type="text/csv",
            tags={"kind": "stat_table", "language": "en"},
        )
        self.protocol = a.artifacts.register(
            "protocol.pdf", b"%PDF-protocol-en", "cta", self.clock.now(),
            media_type="application/pdf",
            tags={"kind": "protocol", "language": "en"},
        )

        self.drug = a.graph.register_drug(
            "Glovatinib", "GLO-001", self.clock.now(), "ra_head"
        )
        self.trial = a.graph.register_trial(
            self.drug.drug_id, "GLO-001-301", "全球三期", "III",
            "OS", self.clock.now(), "ra_head"
        )
        self.group_overall = a.graph.register_subject_group(
            self.trial.trial_id, "ALL", "全人群", "overall", 600,
            self.clock.now(), "biostat"
        )
        self.group_asian = a.graph.register_subject_group(
            self.trial.trial_id, "ASN", "亚洲亚组", "asian", 180,
            self.clock.now(), "biostat"
        )
        self.result = a.graph.register_analysis_result(
            self.trial.trial_id, "ORR-14.3", "14.3 客观缓解率",
            [self.group_overall.group_id, self.group_asian.group_id],
            [(self.table_v1.artifact_id, 1)],
            {"orr": 0.42, "ci95": [0.38, 0.46]},
            self.clock.now(), "biostat",
        )
        self.snapshot = a.snapshots.freeze(
            self.trial.trial_id,
            [self.group_overall.group_id, self.group_asian.group_id],
            [self.result.result_id],
            [(self.protocol.artifact_id, 1)],
            self.clock.now(), "ra_head",
        )


def register_baseline_requirements(app: App, at: datetime):
    app.requirements.register(
        "US", "IND Submission", at, at, "ra_head",
        required_populations={"overall"},
        required_table_kinds={"stat_table"},
        required_languages={"en"},
        require_signature=False,
    )
    app.requirements.register(
        "EU", "CTA Variation", at, at, "ra_head",
        required_populations={"overall"},
        required_table_kinds={"stat_table"},
        required_languages={"en"},
        require_signature=True,
    )
    app.requirements.register(
        "JP", "PMDA Foreign Data", at, at, "ra_head",
        required_populations={"asian"},
        required_table_kinds={"stat_table"},
        required_languages={"ja"},
        require_signature=True,
    )


# --------------------------------------------------------------------- 基础设施


class ArtifactHistoryTests(unittest.TestCase):
    def setUp(self):
        self.events = EventLog()
        self.store = ArtifactStore(self.events)
        self.t = ts("2026-01-01T09:00:00")

    def test_same_name_replacement_keeps_old_version(self):
        v1 = self.store.register("table.csv", b"A", "u", self.t,
                                 tags={"kind": "stat_table"})
        v2 = self.store.register("table.csv", b"B", "u",
                                 ts("2026-01-02T09:00:00"))
        self.assertEqual(v1.version, 1)
        self.assertEqual(v2.version, 2)
        self.assertEqual(self.store.get_version(v1.artifact_id, 1).sha256,
                         sha256_bytes(b"A"))
        self.assertEqual(self.store.get_bytes(v1.sha256), b"A")
        self.assertEqual(self.store.get_bytes(v2.sha256), b"B")
        self.assertEqual(len(self.store.list_versions("table.csv")), 2)

    def test_retry_same_bytes_does_not_append_history(self):
        v1 = self.store.register("table.csv", b"A", "u", self.t)
        before = len(self.events.all())
        v1_retry = self.store.register("table.csv", b"A", "u", self.t)
        self.assertEqual(v1_retry, v1)
        self.assertEqual(len(self.events.all()), before)

    def test_idempotency_key_replays_first_result(self):
        v1 = self.store.register("table.csv", b"A", "u", self.t,
                                 idempotency_key="up-77")
        v1_retry = self.store.register("table.csv", b"A", "u", self.t,
                                       idempotency_key="up-77")
        self.assertEqual(v1_retry, v1)

    def test_corrupted_bytes_detected(self):
        v1 = self.store.register("table.csv", b"A", "u", self.t)
        self.store._blobs[v1.sha256] = b"TAMPERED"
        with self.assertRaises(IntegrityError):
            self.store.get_bytes(v1.sha256)


class EventChainTests(unittest.TestCase):
    def test_tampering_with_exported_log_is_detected(self):
        log = EventLog()
        log.append("x.test", {"a": 1}, "u", ts("2026-01-01T09:00:00"))
        raw = log.export_jsonl()
        self.assertIn(b"x.test", raw)
        EventLog.from_jsonl(raw).verify()  # 原样回放通过
        tampered = raw.replace(b'"a": 1', b'"a": 2')
        with self.assertRaises(IntegrityError):
            EventLog.from_jsonl(tampered)


# --------------------------------------------------------------------- 快照冻结


class SnapshotFreezeTests(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()

    def test_shared_drive_update_does_not_change_frozen_evidence(self):
        a = self.s.app
        old_hash = self.s.table_v1.sha256
        # 共享盘上同名文件被更新：产生 v2，但快照引用 v1。
        new_version = a.artifacts.register(
            "efficacy_table_14_3.csv", b"v2,ORR,0.45", "biostat",
            ts("2026-02-15T09:00:00"),
            tags={"kind": "stat_table", "language": "en"},
        )
        self.assertEqual(new_version.version, 2)
        snapshot = a.snapshots.get(self.s.snapshot.snapshot_id)
        a.snapshots.verify(snapshot)  # 旧哈希仍逐字节可取
        table_hash = next(
            ref["sha256"] for ref in snapshot.attachment_refs
            if ref["name"] == "efficacy_table_14_3.csv"
        )
        self.assertEqual(table_hash, old_hash)
        self.assertEqual(a.artifacts.get_bytes(old_hash), b"v1,ORR,0.42")
        self.assertEqual(a.artifacts.get_bytes(new_version.sha256),
                         b"v2,ORR,0.45")

    def test_manifest_hash_is_stable_and_tamper_evident(self):
        snapshot = self.s.snapshot
        self.assertEqual(len(snapshot.manifest_sha256), 64)
        # 引用指针可以跨服务解析并复核。
        again = self.s.app.snapshots.resolve(snapshot.ref())
        self.assertEqual(again.snapshot_id, snapshot.snapshot_id)
        with self.assertRaises(IntegrityError):
            self.s.app.snapshots.resolve({
                "snapshot_id": snapshot.snapshot_id,
                "manifest_sha256": "f" * 64,
            })

    def test_dangling_group_reference_rejected(self):
        with self.assertRaises(NotFoundError):
            self.s.app.graph.register_analysis_result(
                self.s.trial.trial_id, "X", "X", ["missing-group"],
                [], {}, ts("2026-01-02T09:00:00"), "biostat",
            )

    def test_snapshot_rejects_group_from_other_trial(self):
        other_trial = self.s.app.graph.register_trial(
            self.s.drug.drug_id, "GLO-001-302", "另一试验", "III", "PFS",
            ts("2026-01-02T09:00:00"), "ra_head",
        )
        with self.assertRaises(WorkflowError):
            self.s.app.snapshots.freeze(
                other_trial.trial_id, [self.s.group_overall.group_id], [],
                [], ts("2026-01-02T09:00:00"), "ra_head",
            )


# --------------------------------------------------------------------- 法域规则


class RequirementTimelineTests(unittest.TestCase):
    def test_requirements_are_time_versioned(self):
        s = Scenario()
        t0 = ts("2026-01-01T09:00:00")
        register_baseline_requirements(s.app, t0)
        # 日本 2026-06-01 起改为同时接受英文。
        s.app.requirements.register(
            "JP", "PMDA Foreign Data", ts("2026-06-01T00:00:00"),
            ts("2026-06-01T00:00:00"), "ra_head",
            required_populations={"asian"},
            required_table_kinds={"stat_table"},
            required_languages={"ja", "en"},
            require_signature=True,
        )
        may = s.app.requirements.effective_version(
            "JP", "PMDA Foreign Data", ts("2026-05-31T23:59:00"))
        june = s.app.requirements.effective_version(
            "JP", "PMDA Foreign Data", ts("2026-06-01T00:00:00"))
        self.assertEqual(may.version, 1)
        self.assertEqual(set(may.required_languages), {"ja"})
        self.assertEqual(june.version, 2)
        self.assertEqual(set(june.required_languages), {"ja", "en"})
        # 旧版本的区间已被关闭，但历史仍在。
        self.assertEqual(len(s.app.requirements.history(
            "JP", "PMDA Foreign Data")), 2)

    def test_missing_population_rejected(self):
        s = Scenario()
        register_baseline_requirements(s.app, ts("2026-01-01T09:00:00"))
        # 只冻全人群，无法满足日本的亚洲亚组要求。
        snap = s.app.snapshots.freeze(
            s.trial.trial_id, [s.group_overall.group_id], [], [],
            ts("2026-01-02T09:00:00"), "ra_head",
        )
        with self.assertRaises(RuleViolation):
            s.app.submissions.build_package(
                snap.ref(), "JP", "ja", "ra_lead",
                moment=ts("2026-01-03T09:00:00"),
            )


# --------------------------------------------------------------------- 多语言提交


class SubmissionPackageTests(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()
        self.t = ts("2026-02-01T09:00:00")
        register_baseline_requirements(self.s.app, ts("2025-06-01T00:00:00"))

    def _approve_all(self, package):
        for _ in APPROVAL_LEVELS[package.jurisdiction]:
            self.s.app.submissions.approve(package.package_id, "approver",
                                           self.s.clock.now())

    def test_same_snapshot_produces_district_language_packages(self):
        a = self.s.app
        # 为两份英文原件登记日文译文。
        table_ja = a.artifacts.register(
            "efficacy_table_14_3.ja.csv", "v1,ORR,0.42(JP)".encode(),
            "translator-kk", self.t, tags={"language": "ja"},
        )
        protocol_ja = a.artifacts.register(
            "protocol.ja.pdf", b"%PDF-protocol-ja", "translator-kk", self.t,
            tags={"language": "ja"},
        )
        a.submissions.add_translation(
            self.s.table_v1.sha256, "ja", table_ja.artifact_id, 1,
            "translator-kk", self.t)
        a.submissions.add_translation(
            self.s.protocol.sha256, "ja", protocol_ja.artifact_id, 1,
            "translator-kk", self.t)

        us = a.submissions.build_package(
            self.s.snapshot.ref(), "US", "en", "ra_head", moment=self.t)
        jp = a.submissions.build_package(
            self.s.snapshot.ref(), "JP", "ja", "ra_head", moment=self.t)

        self.assertNotEqual(us.manifest_sha256, jp.manifest_sha256)
        self.assertEqual({e.language for e in us.entries}, {"en"})
        self.assertEqual({e.language for e in jp.entries}, {"ja"})
        # 日文包逐文件保留到英文原件的溯源指针。
        for entry in jp.entries:
            self.assertIsNotNone(entry.source_sha256)
        # 两个包指向同一份冻结快照，原始结果未被复制或改动。
        self.assertEqual(us.snapshot_manifest_sha256,
                         jp.snapshot_manifest_sha256)

    def test_missing_translation_blocks_package(self):
        with self.assertRaises(RuleViolation):
            self.s.app.submissions.build_package(
                self.s.snapshot.ref(), "JP", "ja", "ra_head", moment=self.t)

    def test_wrong_language_rejected(self):
        with self.assertRaises(RuleViolation):
            self.s.app.submissions.build_package(
                self.s.snapshot.ref(), "US", "fr", "ra_head", moment=self.t)

    def test_approval_must_go_level_by_level(self):
        a = self.s.app
        pkg = a.submissions.build_package(
            self.s.snapshot.ref(), "US", "en", "ra_head",
            deadline="2026-03-01T00:00:00+00:00", moment=self.t)
        # 未完成逐级批准前不能提交。
        with self.assertRaises(WorkflowError):
            a.submissions.submit(pkg.package_id, "qp", self.t)
        levels = []
        for _ in APPROVAL_LEVELS["US"]:
            approval = a.submissions.approve(pkg.package_id, "approver", self.t)
            levels.append(approval.level)
        self.assertEqual(levels, APPROVAL_LEVELS["US"])
        submitted = a.submissions.submit(pkg.package_id, "qp", self.t)
        self.assertEqual(submitted.status, "SUBMITTED")

    def test_eu_requires_signatures_before_send(self):
        a = self.s.app
        pkg = a.submissions.build_package(
            self.s.snapshot.ref(), "EU", "en", "ra_head", moment=self.t)
        # 签章必须在逐级批准之前完成：批准后包即冻结，不得再补签。
        for entry in pkg.entries:
            sig_page = a.artifacts.register(
                f"sig_{entry.sha256[:8]}.p7s",
                b"SIG-" + entry.sha256.encode(), "qp", self.t)
            a.submissions.sign_entry(
                pkg.package_id, entry.sha256, "qp", "EU-QP-001",
                sig_page.artifact_id, 1, self.t)
        # 未批准不能发送。
        with self.assertRaises(WorkflowError):
            a.submissions.submit(pkg.package_id, "qp", self.t)
        for _ in APPROVAL_LEVELS["EU"]:
            a.submissions.approve(pkg.package_id, "approver", self.t)
        # 批准后补签被拒绝（包已冻结）。
        with self.assertRaises(WorkflowError):
            any_page = a.artifacts.register(
                "late_sig.p7s", b"LATE", "qp", self.t)
            a.submissions.sign_entry(
                pkg.package_id, pkg.entries[0].sha256, "qp", "EU-QP-001",
                any_page.artifact_id, 1, self.t)
        submitted = a.submissions.submit(pkg.package_id, "qp", self.t)
        self.assertEqual(submitted.status, "SUBMITTED")
        self.assertEqual(len(submitted.signatures), len(pkg.entries))

    def test_submit_after_deadline_rejected(self):
        a = self.s.app
        pkg = a.submissions.build_package(
            self.s.snapshot.ref(), "US", "en", "ra_head",
            deadline="2026-01-15T00:00:00+00:00", moment=self.t)
        for _ in APPROVAL_LEVELS["US"]:
            a.submissions.approve(pkg.package_id, "approver", self.t)
        with self.assertRaises(RuleViolation):
            a.submissions.submit(pkg.package_id, "qp", self.t)

    def test_regulation_change_after_build_blocks_stale_submission(self):
        a = self.s.app
        pkg = a.submissions.build_package(
            self.s.snapshot.ref(), "US", "en", "ra_head", moment=self.t)
        for _ in APPROVAL_LEVELS["US"]:
            a.submissions.approve(pkg.package_id, "approver", self.t)
        # 建包之后法规换版：旧包不许带旧版本直接发送。
        a.requirements.register(
            "US", "IND Submission", ts("2026-02-10T00:00:00"),
            ts("2026-02-10T00:00:00"), "ra_head",
            required_populations={"overall"},
            required_table_kinds={"stat_table"},
            required_languages={"en"},
            require_signature=True,
        )
        with self.assertRaises(RuleViolation):
            a.submissions.submit(
                pkg.package_id, "qp", ts("2026-02-11T09:00:00"))


# --------------------------------------------------------------------- 撤回与重提


class WithdrawResubmitTests(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()
        self.t = ts("2026-02-01T09:00:00")
        register_baseline_requirements(self.s.app, ts("2025-06-01T00:00:00"))

    def _build_approve_submit_us(self, moment):
        a = self.s.app
        pkg = a.submissions.build_package(
            self.s.snapshot.ref(), "US", "en", "ra_head", moment=moment)
        for _ in APPROVAL_LEVELS["US"]:
            a.submissions.approve(pkg.package_id, "approver", moment)
        return a.submissions.submit(pkg.package_id, "qp", moment)

    def test_withdraw_and_resubmit_preserves_history(self):
        a = self.s.app
        first = self._build_approve_submit_us(self.t)
        a.submissions.withdraw(
            first.package_id, "数据需要补充说明", "ra_head",
            ts("2026-02-05T10:00:00"))
        revision = a.submissions.resubmit(
            first.package_id, "ra_head", ts("2026-02-06T09:00:00"))
        self.assertEqual(revision.revision_of, first.package_id)
        self.assertEqual(a.submissions.get_package(first.package_id).status,
                         "WITHDRAWN")
        # 修订版是全新草稿，负责人变更后批准链必须重走。
        self.assertEqual(revision.status, "DRAFT")
        self.assertEqual(revision.approvals, ())
        for _ in APPROVAL_LEVELS["US"]:
            a.submissions.approve(revision.package_id, "new-qp",
                                  ts("2026-02-06T10:00:00"))
        second = a.submissions.submit(
            revision.package_id, "new-qp", ts("2026-02-06T11:00:00"))
        self.assertEqual(second.status, "SUBMITTED")
        # 两次发送引用同一冻结快照。
        self.assertEqual(second.snapshot_manifest_sha256,
                         first.snapshot_manifest_sha256)


# --------------------------------------------------------------------- 问询回复


class InquiryTests(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()

    def test_response_can_only_cite_verified_snapshot(self):
        a = self.s.app
        inquiry, item = a.inquiries.receive(
            "EU", "Q12", "OS 成熟度", "请补充 OS 数据成熟度",
            "alice", ts("2026-03-01T09:00:00"))
        valid_hash = self.s.table_v1.sha256
        response = a.inquiries.respond(
            inquiry.inquiry_id, [self.s.snapshot.ref()], [valid_hash],
            "见冻结快照 14.3 表", "alice", ts("2026-03-02T09:00:00"))
        self.assertEqual(response.evidence_hashes, (valid_hash,))
        # 回复提交后对应待办自动完成。
        self.assertEqual(a.workflow.get(item.item_id).status, "DONE")

    def test_response_rejects_unknown_or_latest_drive_file(self):
        a = self.s.app
        inquiry, _item = a.inquiries.receive(
            "EU", "Q13", "亚组界值", "请提供亚组分析",
            "alice", ts("2026-03-01T09:00:00"))
        # 共享盘上后来的文件即便哈希真实存在，只要不属于冻结快照也被拒绝。
        rogue = a.artifacts.register(
            "rogue_analysis.csv", b"unfrozen,latest", "alice",
            ts("2026-03-02T08:00:00"))
        with self.assertRaises(RuleViolation):
            a.inquiries.respond(
                inquiry.inquiry_id, [self.s.snapshot.ref()], [rogue.sha256],
                "补交最新分析", "alice", ts("2026-03-02T09:00:00"))
        with self.assertRaises(RuleViolation):
            a.inquiries.respond(
                inquiry.inquiry_id, [], [self.s.table_v1.sha256],
                "无快照引用", "alice", ts("2026-03-02T09:00:00"))

    def test_old_snapshot_hash_remains_citable_after_file_replaced(self):
        a = self.s.app
        old_hash = self.s.table_v1.sha256
        a.artifacts.register(
            "efficacy_table_14_3.csv", b"v2,ORR,0.45", "biostat",
            ts("2026-02-15T09:00:00"),
            tags={"kind": "stat_table", "language": "en"})
        inquiry, _item = a.inquiries.receive(
            "EU", "Q14", "历史数据", "请核对当时版本",
            "alice", ts("2026-03-01T09:00:00"))
        response = a.inquiries.respond(
            inquiry.inquiry_id, [self.s.snapshot.ref()], [old_hash],
            "引用的是递交当天版本", "alice", ts("2026-03-02T09:00:00"))
        self.assertEqual(response.evidence_hashes, (old_hash,))
        self.assertEqual(a.artifacts.get_bytes(old_hash), b"v1,ORR,0.42")


# --------------------------------------------------------------------- 责任迁移


class OwnershipAndDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.s = Scenario()

    def test_owner_change_migrates_open_items_and_notifies(self):
        a = self.s.app
        inquiry1, item1 = a.inquiries.receive(
            "EU", "Q20", "问题一", "..." , "alice",
            ts("2026-03-01T09:00:00"))
        inquiry2, item2 = a.inquiries.receive(
            "JP", "Q21", "问题二", "..." , "alice",
            ts("2026-03-01T10:00:00"))
        # 第一件已回复（待办完成），不应被迁移。
        a.inquiries.respond(
            inquiry1.inquiry_id, [self.s.snapshot.ref()],
            [self.s.table_v1.sha256], "done", "alice",
            ts("2026-03-02T09:00:00"))
        moved = a.workflow.transfer_ownership(
            "alice", "bob", "ra_head", ts("2026-03-03T09:00:00"),
            reason="负责人轮岗")
        self.assertEqual([i.item_id for i in moved], [item2.item_id])
        self.assertEqual(a.workflow.get(item1.item_id).owner, "alice")
        self.assertEqual(a.workflow.get(item2.item_id).owner, "bob")
        notes = a.notifications.for_recipient("bob")
        self.assertTrue(any(n.category == "ownership_transfer" for n in notes))
        self.assertIn("问题二", notes[0].subject)
        summary = a.notifications.for_recipient("alice")
        self.assertEqual(summary[-1].category, "ownership_transfer_summary")

    def test_deadline_adjustment_keeps_history_and_notifies(self):
        a = self.s.app
        _inquiry, item = a.inquiries.receive(
            "EU", "Q30", "时限问题", "...", "alice",
            ts("2026-03-01T09:00:00"))
        updated = a.workflow.adjust_deadline(
            item.item_id, "2026-04-15T00:00:00+00:00", "审评方同意延期",
            "ra_head", ts("2026-03-10T09:00:00"))
        self.assertEqual(updated.due_at, "2026-04-15T00:00:00+00:00")
        self.assertEqual(len(updated.deadline_history), 1)
        self.assertEqual(updated.deadline_history[0].new_due_at, updated.due_at)
        notes = a.notifications.for_recipient("alice")
        self.assertTrue(any(n.category == "deadline_change" for n in notes))
        # 再次调整轨迹叠加。
        again = a.workflow.adjust_deadline(
            item.item_id, "2026-05-01T00:00:00+00:00", "二次延期",
            "ra_head", ts("2026-03-20T09:00:00"))
        self.assertEqual(len(again.deadline_history), 2)


# --------------------------------------------------------------------- 审计


class AuditTests(unittest.TestCase):
    def test_audit_reconstructs_exact_sent_bundle(self):
        s = Scenario()
        t = ts("2026-02-01T09:00:00")
        register_baseline_requirements(s.app, ts("2025-06-01T00:00:00"))
        a = s.app

        pkg = a.submissions.build_package(
            s.snapshot.ref(), "EU", "en", "ra_head",
            deadline="2026-03-01T00:00:00+00:00", moment=t)
        sig_refs = {}
        for entry in pkg.entries:
            sig_page = a.artifacts.register(
                f"sig_{entry.sha256[:8]}.p7s",
                b"SIG-" + entry.sha256.encode(), "qp", t)
            a.submissions.sign_entry(
                pkg.package_id, entry.sha256, "qp", "EU-QP-001",
                sig_page.artifact_id, 1, t)
            sig_refs[entry.sha256] = sig_page.sha256
        approvers = []
        for level in APPROVAL_LEVELS["EU"]:
            approvers.append(
                a.submissions.approve(pkg.package_id, f"{level}-person", t))
        a.submissions.submit(pkg.package_id, "qp", t)

        audit = a.audit.audit_submission(pkg.package_id)
        self.assertEqual(audit.jurisdiction, "EU")
        self.assertEqual(audit.sent_at, t.isoformat())
        self.assertEqual(audit.sent_by, "qp")
        # 逐文件哈希可回取原件。
        sent_names = {f["name"] for f in audit.files}
        self.assertIn("efficacy_table_14_3.csv", sent_names)
        for f in audit.files:
            self.assertEqual(a.artifacts.get_bytes(f["sha256"]) is not None,
                             True)
        # 法规版本精确锁定。
        titles = {r["title"] for r in audit.requirement_versions}
        self.assertIn("CTA Variation", titles)
        # 签章状态：签署人、凭证、签章页哈希齐全。
        self.assertEqual(len(audit.signatures), len(pkg.entries))
        for sig in audit.signatures:
            self.assertEqual(sig["signer"], "qp")
            self.assertEqual(sig["credential"], "EU-QP-001")
            self.assertEqual(sig["signature_sha256"], sig_refs[sig["doc_sha256"]])
        # 逐级批准人按顺序齐全。
        self.assertEqual(
            [a_["level"] for a_ in audit.approvers], APPROVAL_LEVELS["EU"])
        self.assertEqual(
            [a_["approver"] for a_ in audit.approvers],
            ["ra_lead-person", "qp-person"])

        # 按发送日检索，且能定位到这一次递交。
        day_list = a.audit.submissions_sent_on(ts("2026-02-01T15:00:00"))
        self.assertEqual([x.package_id for x in day_list], [pkg.package_id])
        self.assertEqual(
            a.audit.submissions_sent_on("2026-02-02"), [])

    def test_audit_shows_withdrawal_but_keeps_original_receipt(self):
        s = Scenario()
        t = ts("2026-02-01T09:00:00")
        register_baseline_requirements(s.app, ts("2025-06-01T00:00:00"))
        a = s.app
        pkg = a.submissions.build_package(
            s.snapshot.ref(), "US", "en", "ra_head", moment=t)
        for _ in APPROVAL_LEVELS["US"]:
            a.submissions.approve(pkg.package_id, "approver", t)
        a.submissions.submit(pkg.package_id, "qp", t)
        a.submissions.withdraw(pkg.package_id, "主动撤回", "ra_head",
                               ts("2026-02-05T09:00:00"))
        audit = a.audit.audit_submission(pkg.package_id)
        self.assertIsNotNone(audit.withdrawn)
        self.assertEqual(audit.withdrawn["reason"], "主动撤回")
        # 原始发送回执内容不变。
        self.assertEqual(audit.sent_at, t.isoformat())
        self.assertEqual(len(audit.files), len(pkg.entries))

    def test_full_system_integrity_passes(self):
        s = Scenario()
        register_baseline_requirements(s.app, ts("2025-06-01T00:00:00"))
        s.app.verify_integrity()
        raw = s.app.events.export_jsonl()
        EventLog.from_jsonl(raw).verify()


if __name__ == "__main__":
    unittest.main()
