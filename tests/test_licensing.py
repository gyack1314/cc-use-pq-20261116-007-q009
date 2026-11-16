from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from embodied_portfolio.availability import LicenseTimeline
from embodied_portfolio.cli import demo_payloads
from embodied_portfolio.contracts import Notice, PortfolioError
from embodied_portfolio.decision import PortfolioService


def notice(**changes):
    body = {"notice": "nt_remote_end", "package": "remote_01", "status": "terminated",
            "effective_on": "2026-03-01", "reference": "LEGAL-2026-11"}
    body.update(changes)
    return body


class NoticeContractTests(unittest.TestCase):
    def rejects(self, body, code):
        with self.assertRaises(PortfolioError) as caught:
            Notice.parse(body)
        self.assertEqual(caught.exception.code, code)

    def test_notice_requires_real_calendar_date(self):
        self.assertEqual(Notice.parse(notice()).effective_on, "2026-03-01")
        self.rejects(notice(effective_on="2026-02-30"), "invalid_date")
        self.rejects(notice(effective_on="03/01/2026"), "invalid_date")
        self.rejects(notice(effective_on=20260301), "invalid_date")

    def test_notice_status_is_constrained(self):
        self.rejects(notice(status="expired"), "notice_status")

    def test_grace_cannot_precede_effective_date(self):
        self.rejects(notice(grace_until="2026-02-28"), "notice_grace")
        self.assertEqual(Notice.parse(notice(grace_until="2026-04-30")).grace_until, "2026-04-30")

    def test_conditions_require_a_grace_window(self):
        self.rejects(notice(grace_conditions="仅限已交付批次"), "notice_grace")

    def test_withdrawal_must_name_the_termination_it_withdraws(self):
        self.rejects(notice(status="withdrawn"), "notice_supersedes")
        self.rejects(notice(status="withdrawn", supersedes="nt_missing",
                            grace_until="2026-04-30"), "notice_grace")
        parsed = Notice.parse(notice(status="withdrawn", supersedes="nt_remote_end",
                                     effective_on="2026-05-01"))
        self.assertEqual(parsed.supersedes, "nt_remote_end")


class TimelineTests(unittest.TestCase):
    def timeline(self, *bodies):
        documents = [{"id": index + 1, "kind": "notice", "key": body["notice"],
                      "revision": 1, "digest": "x", "body": body}
                     for index, body in enumerate(bodies)]
        return LicenseTimeline(documents)

    def test_notice_before_effective_date_keeps_package_available(self):
        row = self.timeline(notice()).standing("remote_01", "2026-02-01")
        self.assertEqual(row["status"], "active")
        self.assertTrue(row["available"])
        self.assertEqual(row["pending_termination_on"], "2026-03-01")

    def test_effective_notice_without_grace_terminates(self):
        row = self.timeline(notice()).standing("remote_01", "2026-03-02")
        self.assertEqual(row["status"], "terminated")
        self.assertEqual(row["reason"], "license_terminated")
        self.assertFalse(row["available"])

    def test_grace_window_keeps_package_conditional(self):
        row = self.timeline(notice(grace_until="2026-04-30",
                                   grace_conditions="仅限已交付批次")).standing("remote_01", "2026-03-15")
        self.assertEqual(row["status"], "grace")
        self.assertTrue(row["available"])
        self.assertTrue(row["conditional"])
        self.assertEqual(row["grace_until"], "2026-04-30")
        self.assertEqual(row["grace_conditions"], "仅限已交付批次")

    def test_grace_expires_into_termination(self):
        timeline = self.timeline(notice(grace_until="2026-04-30"))
        self.assertEqual(timeline.standing("remote_01", "2026-04-30")["status"], "grace")
        self.assertEqual(timeline.standing("remote_01", "2026-05-01")["status"], "terminated")

    def test_withdrawal_restores_availability_from_its_own_effective_date(self):
        timeline = self.timeline(
            notice(),
            notice(notice="nt_remote_end_drop", status="withdrawn",
                   effective_on="2026-06-10", supersedes="nt_remote_end"))
        self.assertEqual(timeline.standing("remote_01", "2026-05-15")["status"], "terminated")
        restored = timeline.standing("remote_01", "2026-06-10")
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["cancelled_notices"], ["nt_remote_end"])
        self.assertTrue(restored["available"])

    def test_withdrawal_does_not_erase_the_notice_record(self):
        timeline = self.timeline(
            notice(),
            notice(notice="nt_remote_end_drop", status="withdrawn",
                   effective_on="2026-06-10", supersedes="nt_remote_end"))
        self.assertEqual(len(timeline.notice_documents()), 2)
        row = timeline.standing("remote_01", "2026-06-10")
        self.assertEqual([item["status"] for item in row["notices"]],
                         ["terminated", "withdrawn"])
        self.assertTrue(row["notices"][0]["cancelled"])

    def test_withdrawal_that_takes_effect_later_leaves_pending_window(self):
        timeline = self.timeline(
            notice(effective_on="2026-03-01"),
            notice(notice="nt_remote_end_drop", status="withdrawn",
                   effective_on="2026-06-10", supersedes="nt_remote_end"))
        self.assertEqual(timeline.standing("remote_01", "2026-02-01")["status"], "active")
        self.assertEqual(timeline.standing("remote_01", "2026-03-01")["status"], "terminated")
        self.assertEqual(timeline.standing("remote_01", "2026-06-09")["status"], "terminated")
        self.assertEqual(timeline.standing("remote_01", "2026-06-10")["status"], "active")

    def test_quote_revision_interleaved_with_notice_is_disclosed(self):
        timeline = self.timeline({**notice(), "basis": {"offer_revision": 1,
                                                        "offer_digest": "old"}})
        row = timeline.standing("remote_01", "2026-03-02",
                                offer_document={"revision": 2, "digest": "new"})
        self.assertTrue(row["offer_revised_since_notice"])
        self.assertIs(row["notices"][0]["basis_current"], False)


class AvailabilityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = PortfolioService(Path(self.tmp.name) / "portfolio.db")
        self.addCleanup(self.service.close)
        self.requirement, self.offers = demo_payloads()
        self.service.import_requirement(self.requirement)
        for offer in self.offers:
            self.service.import_offer(offer)

    def approved(self, as_of="2026-01-10"):
        analysis = self.service.plan("warehouse_trial", as_of=as_of)
        return self.service.approve(analysis["id"], analysis["input_digest"])

    def test_plan_records_why_supply_was_usable(self):
        analysis = self.service.plan("warehouse_trial", as_of="2026-01-10")
        basis = {row["package"]: row for row in analysis["report"]["license_basis"]}
        self.assertTrue(basis["remote_01"]["client_permitted"])
        self.assertTrue(basis["remote_01"]["available"])
        self.assertTrue(basis["portable_02"]["path_permitted"])
        self.assertEqual(analysis["report"]["as_of"], "2026-01-10")

    def test_import_notice_pins_offer_revision_and_is_immutable(self):
        first = self.service.import_notice(notice())
        self.assertEqual(first["body"]["basis"]["offer_revision"], 1)
        self.assertEqual(self.service.notices()[0]["key"], "nt_remote_end")
        with self.assertRaises(PortfolioError) as caught:
            self.service.import_notice(notice(reference="LEGAL-2026-12"))
        self.assertEqual(caught.exception.code, "notice_immutable")

    def test_notice_for_unknown_package_is_rejected(self):
        with self.assertRaises(PortfolioError) as caught:
            self.service.import_notice(notice(package="ghost_pkg"))
        self.assertEqual(caught.exception.code, "unknown_offer")
        self.assertEqual(self.service.notices(), [])

    def test_withdrawal_requires_registered_termination(self):
        with self.assertRaises(PortfolioError) as caught:
            self.service.import_notice(notice(notice="nt_drop", status="withdrawn",
                                              supersedes="nt_missing", effective_on="2026-05-01"))
        self.assertEqual(caught.exception.code, "unknown_notice")

    def test_termination_blocks_new_plan_selection(self):
        self.service.import_notice(notice())
        analysis = self.service.plan("warehouse_trial", as_of="2026-05-01")
        report = analysis["report"]
        self.assertNotIn("remote_01", report["selection"])
        self.assertFalse(report["feasible"])
        self.assertEqual(report["license_exclusions"],
                         [{"package": "remote_01", "reason": "license_terminated"}])
        self.assertIn("remote_01", report["license_disregarded_plan"]["selection"])

    def test_terminated_plan_cannot_be_approved(self):
        self.service.import_notice(notice())
        analysis = self.service.plan("warehouse_trial", as_of="2026-05-01")
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(caught.exception.code, "infeasible")

    def test_backdated_plan_cannot_be_approved_as_current_coverage(self):
        """回溯快照可证明历史，但不能在终止生效后再次被批准为今日覆盖。"""
        self.service.import_notice(notice())
        analysis = self.service.plan("warehouse_trial", as_of="2026-01-10")
        self.assertTrue(analysis["report"]["feasible"])
        self.assertIn("remote_01", analysis["report"]["selection"])
        with self.assertRaises(PortfolioError) as caught:
            self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(caught.exception.code, "license_unavailable")
        self.assertIn("remote_01=license_terminated", caught.exception.message)
        self.assertEqual(self.service.analysis(analysis["id"])["state"], "draft")

    def test_grace_period_does_not_block_approval_yet(self):
        """宽限期内的有条件可用不应被误判为已终止。"""
        self.service.import_notice(notice(effective_on="2026-01-01",
                                          grace_until="2999-12-31"))
        analysis = self.service.plan("warehouse_trial", as_of="2026-01-10")
        approved = self.service.approve(analysis["id"], analysis["input_digest"])
        self.assertEqual(approved["state"], "approved")

    def test_old_snapshot_still_proves_original_purchase(self):
        approved = self.approved()
        self.service.import_notice(notice())
        audit = self.service.audit_analysis(approved["id"])
        self.assertTrue(audit["historical_valid"])
        self.assertFalse(audit["input_current"])
        self.assertEqual(audit["selection"], ["portable_02", "remote_01"])
        self.assertEqual(audit["cost"], "150.00")
        basis = {row["package"]: row for row in audit["license_basis"]}
        self.assertTrue(basis["remote_01"]["available"])
        self.assertEqual(audit["license_exclusions"], [])

    def test_snapshot_after_notice_recomputes_without_terminated_package(self):
        self.service.import_notice(notice())
        analysis = self.service.plan("warehouse_trial", as_of="2026-05-01")
        audit = self.service.audit_analysis(analysis["id"])
        self.assertTrue(audit["historical_valid"], audit["mismatches"])
        self.assertEqual(audit["selection"], [])
        self.assertTrue(audit["input_current"])

    def test_inflight_before_effective_date_can_continue(self):
        approved = self.approved()
        self.service.import_notice(notice())
        report = self.service.availability("warehouse_trial", as_of="2026-02-01")
        self.assertTrue(report["inflight"]["usable_today"])
        self.assertTrue(report["inflight"]["can_continue_without_reapproval"])
        self.assertEqual(report["approval"]["reasons"], [])
        self.assertIn("scheduled_termination_affects_selected_package",
                      report["approval"]["warnings"])
        self.assertEqual(report["inflight"]["analysis_id"], approved["id"])

    def test_inflight_during_grace_is_conditional_with_deadline(self):
        self.approved()
        self.service.import_notice(notice(grace_until="2026-04-30",
                                          grace_conditions="仅限已交付批次继续训练"))
        report = self.service.availability("warehouse_trial", as_of="2026-03-15")
        self.assertTrue(report["inflight"]["usable_today"])
        self.assertEqual(report["inflight"]["conditional_supply"], ["remote_01"])
        self.assertEqual(report["approval"]["deadline"], "2026-04-30")
        self.assertTrue(report["approval"]["grace_allows_interim_use"])
        self.assertFalse(report["approval"]["requires_new_analysis"])
        status = {row["package"]: row for row in report["inflight"]["selected_license_status"]}
        self.assertEqual(status["remote_01"]["grace_conditions"], "仅限已交付批次继续训练")

    def test_inflight_after_termination_cannot_claim_current_coverage(self):
        self.approved()
        self.service.import_notice(notice(grace_until="2026-04-30"))
        report = self.service.availability("warehouse_trial", as_of="2026-05-01")
        self.assertFalse(report["inflight"]["usable_today"])
        self.assertTrue(report["inflight"]["uses_unavailable_supply"])
        self.assertTrue(report["approval"]["requires_new_analysis"])
        self.assertIn("approved_package_terminated", report["approval"]["reasons"])
        self.assertIn("no_available_feasible_plan", report["approval"]["reasons"])
        self.assertEqual(report["approval"]["affected_packages"], ["remote_01"])
        affected = report["approval"]["affected_slices"][0]
        self.assertEqual(affected["slice"], "warehouse_pick|grasp|failure|arm_a")
        # remote_01 独有 2 条（trial_a/trial_b），trial_c 与 portable_02 重复
        self.assertEqual(affected["lost_credited"], 2)
        self.assertEqual(affected["packages_removed"], ["remote_01"])

    def test_gap_report_reflects_terminated_supply(self):
        self.service.import_notice(notice())
        report = self.service.gaps("warehouse_trial", [], as_of="2026-05-01")
        self.assertFalse(report["mandatory_complete"])
        self.assertEqual(report["gaps"][0]["missing"], 4)
        self.assertEqual(report["license_excluded"], [])

    def test_gap_report_separates_license_gap_from_shortfall(self):
        self.service.import_notice(notice())
        report = self.service.gaps("warehouse_trial", ["remote_01", "portable_02"],
                                   as_of="2026-05-01")
        self.assertEqual(report["total_shortage"], 2)
        self.assertEqual(report["license_excluded"],
                         [{"package": "remote_01", "reason": "license_terminated"}])
        self.assertEqual(report["shortage_caused_by_license"],
                         [{"slice": "warehouse_pick|grasp|failure|arm_a",
                           "missing_now": 2, "missing_if_licensed": 0}])
        self.assertFalse(report["effective_mandatory_complete"])
        before = self.service.gaps("warehouse_trial", ["remote_01", "portable_02"],
                                   as_of="2026-02-01")
        self.assertEqual(before["shortage_caused_by_license"], [])
        self.assertTrue(before["effective_mandatory_complete"])

    def test_manual_group_shows_license_termination_as_restriction(self):
        self.service.import_notice(notice())
        comparison = self.service.compare_packages("warehouse_trial", [["remote_01"]],
                                                   as_of="2026-05-01")
        portfolio = comparison["portfolios"][0]
        self.assertFalse(portfolio["eligible"])
        self.assertFalse(portfolio["feasible"])
        self.assertEqual(portfolio["restrictions"],
                         [{"package": "remote_01", "reason": "license_terminated"}])

    def test_readiness_and_brief_exclude_terminated_package(self):
        self.service.import_notice(notice())
        readiness = self.service.readiness("warehouse_trial", as_of="2026-05-01")
        self.assertEqual(readiness["data"]["eligible_offers"], 1)
        self.assertEqual(readiness["license_exclusions"],
                         [{"package": "remote_01", "reason": "license_terminated"}])
        brief = self.service.brief("warehouse_trial", as_of="2026-05-01")
        self.assertEqual(brief["eligible_offer_count"], 1)
        self.assertEqual(brief["license_terms"][0]["package"], "remote_01")

    def test_scenarios_do_not_resurrect_terminated_supply(self):
        self.service.import_notice(notice())
        budgets = self.service.budget_grid("warehouse_trial", ["150", "180"], as_of="2026-05-01")
        self.assertTrue(all(not row["feasible"] for row in budgets))
        withdrawal = self.service.withdrawal("warehouse_trial", "portable_02", as_of="2026-05-01")
        self.assertFalse(withdrawal["after_feasible"])

    def test_replacement_cost_is_reported_when_alternative_exists(self):
        replacement = deepcopy(self.offers[0])
        replacement["package"] = "remote_03"
        replacement["unit_price"] = "220"
        replacement["hours"] = "1"
        replacement["observations"][0]["episode_ids"] = ["trial_x", "trial_y", "trial_z"]
        self.service.import_offer(replacement)
        self.approved()
        self.service.import_notice(notice())
        report = self.service.availability("warehouse_trial", as_of="2026-05-01")
        impact = report["impact"]["replacement"]
        self.assertEqual(impact["minimum_cost"], "270.00")
        self.assertEqual(impact["additional_budget_vs_approved"], "120.00")
        self.assertEqual(report["current_plan"]["feasible"], False)
        self.assertEqual(report["current_plan"]["reason"], "mandatory_targets_uncovered")

    def test_withdrawal_restores_plan_but_keeps_notice_history(self):
        approved = self.approved()
        self.service.import_notice(notice())
        self.service.import_notice(notice(notice="nt_remote_end_drop", status="withdrawn",
                                          effective_on="2026-06-10",
                                          supersedes="nt_remote_end",
                                          reference="LEGAL-2026-19"))
        report = self.service.availability("warehouse_trial", as_of="2026-06-10")
        self.assertEqual(report["license"]["excluded"], {})
        self.assertEqual(report["current_plan"]["selection"], ["portable_02", "remote_01"])
        self.assertEqual(report["approval"]["reasons"], [])
        self.assertIn("license_notices_changed_since_approval",
                      report["approval"]["warnings"])
        self.assertEqual(len(self.service.notices()), 2)
        self.assertTrue(self.service.audit_analysis(approved["id"])["historical_valid"])

    def test_version_history_supports_audit_of_termination_and_reinstatement(self):
        approved = self.approved()
        self.service.import_notice(notice())
        terminated = self.service.plan("warehouse_trial", as_of="2026-05-01")
        self.service.import_notice(notice(notice="nt_remote_end_drop", status="withdrawn",
                                          effective_on="2026-06-10",
                                          supersedes="nt_remote_end"))
        restored = self.service.plan("warehouse_trial", as_of="2026-06-10")
        comparison = self.service.compare_analyses(approved["id"], terminated["id"])
        self.assertEqual(comparison["notice_changes"][0]["notice"], "nt_remote_end")
        # 强制覆盖失去后整体无可行组合，旧方案整体退出
        self.assertEqual(comparison["packages_removed"], ["portable_02", "remote_01"])
        reopened = self.service.compare_analyses(terminated["id"], restored["id"])
        self.assertEqual(reopened["packages_added"], ["portable_02", "remote_01"])
        self.assertEqual(reopened["notice_changes"][0]["notice"], "nt_remote_end_drop")
        self.assertTrue(self.service.audit_analysis(restored["id"])["historical_valid"])

    def test_approval_state_is_consistent_after_reinstatement(self):
        self.approved()
        self.service.import_notice(notice())
        self.service.import_notice(notice(notice="nt_remote_end_drop", status="withdrawn",
                                          effective_on="2026-06-10",
                                          supersedes="nt_remote_end"))
        report = self.service.availability("warehouse_trial", as_of="2026-06-10")
        self.assertTrue(report["approval"]["reusable"])
        self.assertFalse(report["approval"]["requires_new_analysis"])
        self.assertTrue(report["approval"]["reanalysis_recommended"])

    def test_notice_body_cannot_be_rewritten_through_ledger(self):
        """直接改库改不动通知来源摘要，审计必须标为无效而不是静默接受。"""
        self.service.import_notice(notice())
        document = self.service.notices()[0]
        changed = {**document["body"], "effective_on": "2026-01-01"}
        self.service.ledger.db.execute("UPDATE documents SET body=? WHERE id=?",
                                       (__import__("json").dumps(changed), document["id"]))
        analysis = self.service.plan("warehouse_trial", as_of="2026-05-01")
        audit = self.service.audit_analysis(analysis["id"])
        self.assertFalse(audit["historical_valid"])
        self.assertIn("source_digest", audit["mismatches"])
        self.assertFalse(self.service.verify(analysis["id"])["verified"])

    def test_batch_import_pins_notice_to_same_batch_offer_revision(self):
        batch = {"documents": [
            {"kind": "offer", "body": {**self.offers[0], "unit_price": "110"}},
            {"kind": "notice", "body": notice()}]}
        result = self.service.catalog.import_batch(batch)
        self.assertEqual(result["changed"], 2)
        stored = self.service.notices()[0]["body"]
        self.assertEqual(stored["basis"]["offer_revision"], 2)
        self.assertEqual(stored["basis"]["offer_digest"],
                         self.service.document_history("offer", "remote_01")[1]["digest"])

    def test_batch_rolls_back_when_notice_is_invalid(self):
        batch = {"documents": [
            {"kind": "offer", "body": {**self.offers[0], "unit_price": "110"}},
            {"kind": "notice", "body": notice(package="ghost_pkg")}]}
        with self.assertRaises(PortfolioError) as caught:
            self.service.catalog.import_batch(batch)
        self.assertEqual(caught.exception.code, "unknown_offer")
        self.assertEqual(len(self.service.document_history("offer", "remote_01")), 1)
        self.assertEqual(self.service.notices(), [])

    def test_withdrawal_cannot_predate_its_termination(self):
        self.service.import_notice(notice())
        with self.assertRaises(PortfolioError) as caught:
            self.service.import_notice(notice(notice="nt_drop", status="withdrawn",
                                              effective_on="2026-01-01",
                                              supersedes="nt_remote_end"))
        self.assertEqual(caught.exception.code, "notice_withdrawal_date")


if __name__ == "__main__":
    unittest.main()
