"""跨境申报领域核心的行为测试。

每条测试对应跨境申报的一项关键诉求：冻结证据不失原貌、要求随时间生效、
多语言提交包同源派生、问询答复引用可验证快照、事项迁移留痕、
递交审计精确还原。
"""

import hashlib
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from regulatory import (
    InquiryStatus,
    PackageStatus,
    RegulatoryService,
    RequirementKind,
    SealRequiredError,
    StateError,
    TaskStatus,
    UnverifiableSnapshotError,
)


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class FakeClock:
    def __init__(self, start=datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)
        return self.now


def build() -> SimpleNamespace:
    """搭建一套基础档案：药品→试验→分组→分析结果→附件→冻结快照。"""
    clock = FakeClock()
    svc = RegulatoryService(clock)
    drug = svc.register_drug("阿伐替尼", "某创新药企业", actor="reg")
    trial = svc.register_trial(drug.drug_id, "III期", "关键确证性试验", actor="reg")
    g1 = svc.register_subject_group(trial.trial_id, "试验组", "晚期实体瘤成人", 150, actor="reg")
    g2 = svc.register_subject_group(trial.trial_id, "对照组", "晚期实体瘤成人", 148, actor="reg")
    result = svc.register_analysis_result(
        trial.trial_id, (g1.group_id, g2.group_id), "总生存期", b"HR=0.73", actor="stat"
    )
    a1, _ = svc.upload_attachment("疗效统计表.pdf", b"efficacy-v1", uploaded_by="stat")
    a2, _ = svc.upload_attachment("安全性分析.pdf", b"safety-v1", uploaded_by="stat")
    snapshot = svc.freeze_snapshot(
        trial.trial_id, (result.result_id,), (a1.attachment_id, a2.attachment_id), created_by="reg"
    )
    return SimpleNamespace(
        svc=svc, clock=clock, drug=drug, trial=trial, groups=(g1, g2),
        result=result, attachments=(a1, a2), snapshot=snapshot,
    )


def approved_package(svc, snapshot_id, jurisdiction="US", language="en",
                     documents=(("提交说明.pdf", b"rendition-1"),), created_by="reg"):
    package = svc.generate_package(
        jurisdiction, language, snapshot_id, list(documents), created_by=created_by
    )
    svc.approve(package.package_id, "qa-lead")
    svc.approve(package.package_id, "reg-head")
    return svc.packages[package.package_id]


class DomainChainTest(unittest.TestCase):
    def test_objects_reference_each_other(self):
        env = build()
        self.assertEqual(env.trial.drug_id, env.drug.drug_id)
        self.assertTrue(all(g.trial_id == env.trial.trial_id for g in env.groups))
        self.assertEqual(env.result.trial_id, env.trial.trial_id)
        self.assertEqual(set(env.result.subject_group_ids), {g.group_id for g in env.groups})
        self.assertEqual(env.snapshot.trial_id, env.trial.trial_id)
        self.assertIn(env.result.result_id, env.snapshot.result_ids)
        names = {ref.name for ref in env.snapshot.attachment_refs}
        self.assertEqual(names, {"疗效统计表.pdf", "安全性分析.pdf"})

    def test_analysis_result_update_registers_new_result(self):
        env = build()
        updated = env.svc.register_analysis_result(
            env.trial.trial_id, tuple(g.group_id for g in env.groups),
            "总生存期", b"HR=0.71", supersedes=env.result.result_id, actor="stat",
        )
        self.assertEqual(updated.supersedes, env.result.result_id)
        self.assertNotEqual(updated.sha256, env.result.sha256)
        self.assertTrue(env.svc.verify_snapshot(env.snapshot.snapshot_id))


class AttachmentHistoryTest(unittest.TestCase):
    def test_upload_retry_is_idempotent(self):
        svc = RegulatoryService(FakeClock())
        v1, created1 = svc.upload_attachment("a.pdf", b"x", uploaded_by="u")
        v2, created2 = svc.upload_attachment("a.pdf", b"x", uploaded_by="u")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(v1.attachment_id, v2.attachment_id)
        self.assertEqual(v2.version, 1)
        self.assertEqual(len(svc.attachments[v1.attachment_id].versions), 1)

    def test_same_name_replacement_appends_version_and_keeps_history(self):
        svc = RegulatoryService(FakeClock())
        v1, _ = svc.upload_attachment("a.pdf", b"x", uploaded_by="u")
        v2, created = svc.upload_attachment("a.pdf", b"y", uploaded_by="u")
        self.assertTrue(created)
        self.assertEqual(v2.version, 2)
        versions = svc.attachments[v1.attachment_id].versions
        self.assertEqual([v.sha256 for v in versions], [sha(b"x"), sha(b"y")])


class SnapshotTest(unittest.TestCase):
    def test_snapshot_survives_later_file_updates(self):
        env = build()
        digest_before = env.snapshot.digest
        env.svc.upload_attachment("疗效统计表.pdf", b"efficacy-v2", uploaded_by="stat")
        snapshot = env.svc.snapshots[env.snapshot.snapshot_id]
        self.assertEqual(snapshot.digest, digest_before)
        ref = next(r for r in snapshot.attachment_refs if r.name == "疗效统计表.pdf")
        self.assertEqual((ref.version, ref.sha256), (1, sha(b"efficacy-v1")))
        self.assertTrue(env.svc.verify_snapshot(snapshot.snapshot_id))

    def test_new_snapshot_picks_up_new_versions(self):
        env = build()
        env.svc.upload_attachment("疗效统计表.pdf", b"efficacy-v2", uploaded_by="stat")
        newer = env.svc.freeze_snapshot(
            env.trial.trial_id, (env.result.result_id,),
            tuple(a.attachment_id for a in env.attachments), created_by="reg",
        )
        ref = next(r for r in newer.attachment_refs if r.name == "疗效统计表.pdf")
        self.assertEqual((ref.version, ref.sha256), (2, sha(b"efficacy-v2")))
        self.assertNotEqual(newer.digest, env.snapshot.digest)

    def test_tampered_result_breaks_verification(self):
        env = build()
        env.svc.results[env.result.result_id] = replace(env.result, sha256="0" * 64)
        self.assertFalse(env.svc.verify_snapshot(env.snapshot.snapshot_id))


class RequirementTest(unittest.TestCase):
    def test_requirements_take_effect_over_time(self):
        svc = RegulatoryService(FakeClock())
        old = svc.add_requirement(
            "US", RequirementKind.STAT_TABLE, date(2025, 1, 1), "旧版统计表格式",
            effective_to=date(2025, 12, 31), deadline=date(2025, 11, 30),
        )
        new = svc.add_requirement(
            "US", RequirementKind.STAT_TABLE, date(2026, 1, 1), "新版统计表格式",
            deadline=date(2026, 10, 15),
        )
        svc.add_requirement("EU", RequirementKind.TRANSLATION_SEAL, date(2026, 1, 1), "翻译件须签章")

        us_2025 = svc.requirements_in_force("US", date(2025, 6, 1))
        self.assertEqual([r.requirement_id for r in us_2025], [old.requirement_id])
        self.assertEqual(us_2025[0].deadline, date(2025, 11, 30))

        us_2026 = svc.requirements_in_force("US", date(2026, 6, 1))
        self.assertEqual([r.requirement_id for r in us_2026], [new.requirement_id])
        self.assertEqual(us_2026[0].version, 2)

        eu_2026 = svc.requirements_in_force("EU", date(2026, 6, 1))
        self.assertEqual([r.kind for r in eu_2026], [RequirementKind.TRANSLATION_SEAL])
        self.assertEqual(svc.requirements_in_force("JP", date(2026, 6, 1)), ())


class PackageTest(unittest.TestCase):
    def test_multilingual_packages_share_snapshot_without_altering_it(self):
        env = build()
        digest_before = env.snapshot.digest
        us = env.svc.generate_package(
            "US", "en", env.snapshot.snapshot_id, [("cover.pdf", b"us-cover")], created_by="reg"
        )
        cn = env.svc.generate_package(
            "CN", "zh", env.snapshot.snapshot_id, [("封面.pdf", "中文封面".encode())], created_by="reg"
        )
        self.assertEqual(us.snapshot_id, env.snapshot.snapshot_id)
        self.assertEqual(cn.snapshot_id, env.snapshot.snapshot_id)
        snapshot = env.svc.snapshots[env.snapshot.snapshot_id]
        self.assertEqual(snapshot, env.snapshot)
        self.assertEqual(snapshot.digest, digest_before)
        for package in (us, cn):
            for rendition_id in package.rendition_ids:
                rendition = env.svc.renditions[rendition_id]
                self.assertEqual(rendition.source_snapshot_id, env.snapshot.snapshot_id)
                self.assertEqual(rendition.language, package.language)

    def test_approval_levels_are_sequential_and_distinct(self):
        env = build()
        package = env.svc.generate_package(
            "US", "en", env.snapshot.snapshot_id, [("cover.pdf", b"us-cover")], created_by="reg"
        )
        with self.assertRaises(StateError):
            env.svc.send_package(package.package_id, sent_by="reg")
        env.svc.approve(package.package_id, "qa-lead")
        self.assertEqual(package.status, PackageStatus.IN_APPROVAL)
        with self.assertRaises(StateError):
            env.svc.approve(package.package_id, "qa-lead")
        env.svc.approve(package.package_id, "reg-head")
        self.assertEqual(package.status, PackageStatus.APPROVED)
        self.assertEqual([(a.level, a.approver) for a in package.approvals],
                         [(1, "qa-lead"), (2, "reg-head")])

    def test_translation_seal_enforced_per_jurisdiction(self):
        env = build()
        env.svc.add_requirement("EU", RequirementKind.TRANSLATION_SEAL, date(2026, 1, 1), "翻译件须签章")
        eu = approved_package(env.svc, env.snapshot.snapshot_id, jurisdiction="EU", language="de")
        with self.assertRaises(SealRequiredError):
            env.svc.send_package(eu.package_id, sent_by="reg")
        env.svc.seal_rendition(eu.rendition_ids[0], sealed_by="translator", seal_no="SEAL-1")
        record = env.svc.send_package(eu.package_id, sent_by="reg")
        self.assertTrue(record.seals[0].sealed)
        self.assertEqual(record.seals[0].seal_no, "SEAL-1")

        us = approved_package(env.svc, env.snapshot.snapshot_id, jurisdiction="US", language="en")
        self.assertIsNotNone(env.svc.send_package(us.package_id, sent_by="reg"))


class SendHistoryTest(unittest.TestCase):
    def test_withdraw_and_resubmit_preserves_history(self):
        env = build()
        package = approved_package(env.svc, env.snapshot.snapshot_id)
        first = env.svc.send_package(package.package_id, sent_by="reg")
        env.svc.withdraw_package(package.package_id, "补充稳定性数据", actor="reg")
        self.assertEqual(package.status, PackageStatus.WITHDRAWN)

        env.svc.resubmit_package(package.package_id, actor="reg")
        self.assertEqual(package.status, PackageStatus.IN_APPROVAL)
        env.svc.approve(package.package_id, "qa-lead")
        env.svc.approve(package.package_id, "reg-head")
        env.clock.advance(days=2)
        second = env.svc.send_package(package.package_id, sent_by="reg")

        self.assertEqual(package.send_ids, [first.send_id, second.send_id])
        self.assertEqual(env.svc.sends[first.send_id].files, first.files)
        self.assertNotEqual(first.sent_at, second.sent_at)
        event_types = [e.type for e in env.svc.history()]
        for expected in ("package_sent", "package_withdrawn", "package_resubmitted", "package_sent"):
            event_types.remove(expected)  # 顺序与次数都可追溯
        self.assertTrue(env.svc.audit_send(first.send_id).verified)


class InquiryTest(unittest.TestCase):
    def test_inquiry_response_must_cite_verifiable_snapshot(self):
        env = build()
        env.svc.add_requirement(
            "US", RequirementKind.INQUIRY_RESPONSE, date(2026, 1, 1),
            "问询须在30日内答复", response_window_days=30,
        )
        package = approved_package(env.svc, env.snapshot.snapshot_id)
        env.svc.send_package(package.package_id, sent_by="reg")

        inquiry = env.svc.receive_inquiry(package.package_id, "请说明亚组分析口径")
        self.assertEqual(inquiry.response_due_at, inquiry.received_at + timedelta(days=30))
        task = next(t for t in env.svc.tasks.values() if t.related_ref == inquiry.inquiry_id)
        self.assertEqual((task.owner, task.status), ("reg", TaskStatus.OPEN))

        with self.assertRaises(UnverifiableSnapshotError):
            env.svc.respond_to_inquiry(inquiry.inquiry_id, "答复", ("snap-999",), created_by="reg")
        with self.assertRaises(UnverifiableSnapshotError):
            env.svc.respond_to_inquiry(inquiry.inquiry_id, "答复", (), created_by="reg")

        response = env.svc.respond_to_inquiry(
            inquiry.inquiry_id, "亚组口径见快照附件", (env.snapshot.snapshot_id,), created_by="reg"
        )
        self.assertEqual(response.snapshot_ids, (env.snapshot.snapshot_id,))
        self.assertEqual(inquiry.status, InquiryStatus.ANSWERED)
        self.assertEqual(task.status, TaskStatus.DONE)

    def test_response_rejected_when_snapshot_no_longer_verifiable(self):
        env = build()
        package = approved_package(env.svc, env.snapshot.snapshot_id)
        env.svc.send_package(package.package_id, sent_by="reg")
        inquiry = env.svc.receive_inquiry(package.package_id, "请提供原始统计表")
        env.svc.results[env.result.result_id] = replace(env.result, sha256="0" * 64)
        with self.assertRaises(UnverifiableSnapshotError):
            env.svc.respond_to_inquiry(
                inquiry.inquiry_id, "答复", (env.snapshot.snapshot_id,), created_by="reg"
            )


class TaskMigrationTest(unittest.TestCase):
    def test_reassignment_migrates_open_tasks_and_notifies(self):
        svc = RegulatoryService(FakeClock())
        t1 = svc.create_task("准备EU统计表", owner="alice", due_on=date(2026, 4, 1))
        t2 = svc.create_task("答复US问询", owner="alice")
        t3 = svc.create_task("已归档的翻译件", owner="alice")
        svc.complete_task(t3.task_id)
        t4 = svc.create_task("他人事项", owner="carol")

        moved = svc.reassign_owner("alice", "bob", changed_by="pm")
        self.assertEqual({t.task_id for t in moved}, {t1.task_id, t2.task_id})
        self.assertEqual(svc.tasks[t1.task_id].owner, "bob")
        self.assertEqual(svc.tasks[t3.task_id].owner, "alice")
        self.assertEqual(svc.tasks[t4.task_id].owner, "carol")

        notifications = svc.notifications_for("bob")
        self.assertEqual(len(notifications), 2)
        self.assertTrue(all(n.kind == "reassignment" for n in notifications))
        self.assertEqual({n.related_task_id for n in notifications}, {t1.task_id, t2.task_id})

    def test_deadline_adjustment_updates_open_task_and_notifies(self):
        svc = RegulatoryService(FakeClock())
        task = svc.create_task("准备EU统计表", owner="alice", due_on=date(2026, 4, 1))
        svc.adjust_task_deadline(task.task_id, date(2026, 5, 1), "法域延长审评时限", changed_by="pm")
        self.assertEqual(svc.tasks[task.task_id].due_on, date(2026, 5, 1))
        notifications = svc.notifications_for("alice")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].kind, "deadline_change")
        self.assertIn("法域延长审评时限", notifications[0].message)

        svc.complete_task(task.task_id)
        with self.assertRaises(StateError):
            svc.adjust_task_deadline(task.task_id, date(2026, 6, 1), "已完成", changed_by="pm")


class AuditTest(unittest.TestCase):
    def test_audit_reconstructs_exactly_what_was_sent(self):
        env = build()
        requirement = env.svc.add_requirement(
            "US", RequirementKind.STAT_TABLE, date(2026, 1, 1), "新版统计表格式",
            deadline=date(2026, 10, 15),
        )
        package = approved_package(env.svc, env.snapshot.snapshot_id)
        record = env.svc.send_package(package.package_id, sent_by="reg")

        report = env.svc.audit_send(record.send_id)
        self.assertEqual(report.sent_at, env.clock.now)
        self.assertEqual(report.snapshot_digest, env.snapshot.digest)
        self.assertEqual(
            {f.name: f.sha256 for f in report.files},
            {
                "疗效统计表.pdf": sha(b"efficacy-v1"),
                "安全性分析.pdf": sha(b"safety-v1"),
                "提交说明.pdf": sha(b"rendition-1"),
            },
        )
        self.assertEqual([r.requirement_id for r in report.requirements], [requirement.requirement_id])
        self.assertEqual([(a.level, a.approver) for a in report.approvals],
                         [(1, "qa-lead"), (2, "reg-head")])
        self.assertEqual([(s.name, s.sealed) for s in report.seals], [("提交说明.pdf", False)])
        self.assertTrue(report.verified)

    def test_audit_unchanged_after_same_name_replacement(self):
        env = build()
        package = approved_package(env.svc, env.snapshot.snapshot_id)
        record = env.svc.send_package(package.package_id, sent_by="reg")
        env.svc.upload_attachment("疗效统计表.pdf", b"efficacy-v2", uploaded_by="stat")
        env.clock.advance(days=30)

        report = env.svc.audit_send(record.send_id)
        files = {f.name: (f.version, f.sha256) for f in report.files}
        self.assertEqual(files["疗效统计表.pdf"], (1, sha(b"efficacy-v1")))
        self.assertTrue(report.verified)

    def test_event_log_is_append_only_and_ordered(self):
        env = build()
        package = approved_package(env.svc, env.snapshot.snapshot_id)
        env.svc.send_package(package.package_id, sent_by="reg")
        events = env.svc.history()
        self.assertEqual([e.seq for e in events], list(range(1, len(events) + 1)))
        types = [e.type for e in events]
        for milestone in ("drug_registered", "snapshot_frozen", "package_generated",
                          "package_approved", "package_sent"):
            self.assertIn(milestone, types)


if __name__ == "__main__":
    unittest.main()
