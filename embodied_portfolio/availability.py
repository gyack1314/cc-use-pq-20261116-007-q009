from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations
from typing import Any, Iterable

from .contracts import Notice, Offer, PortfolioError, Requirement
from .coverage import CoverageEngine
from .economics import PriceEngine, money
from .optimizer import PortfolioOptimizer

AVAILABLE_STATUSES = {"active", "grace"}


@dataclass(frozen=True)
class NoticeEntry:
    document_id: int
    notice: Notice
    basis: dict[str, Any]
    digest: str

    @property
    def key(self) -> str:
        return self.notice.notice


def notice_entries(documents: list[dict[str, Any]]) -> list[NoticeEntry]:
    entries = []
    for document in documents:
        body = document["body"]
        entries.append(NoticeEntry(document["id"], Notice.parse(body),
                                   dict(body.get("basis") or {}), document["digest"]))
    return entries


class LicenseTimeline:
    """只按已登记通知和生效日推导状态，不删除或覆盖任何历史通知。"""

    def __init__(self, documents: list[dict[str, Any]]):
        self.entries = sorted(notice_entries(documents),
                              key=lambda item: (item.notice.effective_on, item.document_id))
        self.by_package: dict[str, list[NoticeEntry]] = {}
        for entry in self.entries:
            self.by_package.setdefault(entry.notice.package, []).append(entry)

    def packages(self) -> set[str]:
        return set(self.by_package)

    def notice_documents(self) -> list[dict[str, Any]]:
        return [{"notice": entry.key, "package": entry.notice.package,
                 "status": entry.notice.status, "effective_on": entry.notice.effective_on,
                 "document_id": entry.document_id, "digest": entry.digest,
                 "basis": dict(entry.basis)}
                for entry in self.entries]

    def standing(self, package: str, as_of: str,
                 offer_document: dict[str, Any] | None = None) -> dict[str, Any]:
        entries = self.by_package.get(package, [])
        effective = [entry for entry in entries if entry.notice.effective_on <= as_of]
        future = [entry for entry in entries if entry.notice.effective_on > as_of]

        cancelled: set[str] = set()
        for entry in effective:
            if entry.notice.status == "withdrawn":
                cancelled.add(entry.notice.supersedes or "")

        in_force = [entry for entry in effective
                    if entry.notice.status == "terminated" and entry.key not in cancelled]
        # 撤回不能早于被撤回的终止（导入时已校验），因此未来终止在生效到撤回之间仍有窗口；
        # 只有评估日之前已生效的撤回才可取消它。
        pending = [entry for entry in future
                   if entry.notice.status == "terminated" and entry.key not in cancelled]

        status, reason, grace_until, grace_conditions = "active", None, None, None
        termination_effective_on = None
        if in_force:
            termination_effective_on = min(entry.notice.effective_on for entry in in_force)
            graced = [entry for entry in in_force
                      if entry.notice.grace_until is not None and entry.notice.grace_until >= as_of]
            if len(graced) == len(in_force):
                status, reason = "grace", "license_grace"
                grace_until = min(entry.notice.grace_until for entry in graced)
                conditions = [entry.notice.grace_conditions for entry in in_force
                              if entry.notice.grace_conditions]
                grace_conditions = "; ".join(dict.fromkeys(conditions)) or None
            else:
                status, reason = "terminated", "license_terminated"
                grace_until = min((entry.notice.grace_until for entry in in_force
                                   if entry.notice.grace_until), default=None)
                conditions = [entry.notice.grace_conditions for entry in in_force
                              if entry.notice.grace_conditions]
                grace_conditions = "; ".join(dict.fromkeys(conditions)) or None
        elif pending:
            reason = "license_termination_pending"

        current_revision = offer_document["revision"] if offer_document else None
        notice_rows = []
        for entry in entries:
            basis_revision = entry.basis.get("offer_revision")
            notice_rows.append({
                "notice": entry.key, "status": entry.notice.status,
                "effective_on": entry.notice.effective_on,
                "grace_until": entry.notice.grace_until,
                "grace_conditions": entry.notice.grace_conditions,
                "reference": entry.notice.reference,
                "supersedes": entry.notice.supersedes,
                "in_force": entry in in_force,
                "cancelled": entry.key in cancelled,
                "pending": entry in pending,
                "document_id": entry.document_id, "digest": entry.digest,
                "basis_offer_revision": basis_revision,
                "basis_offer_digest": entry.basis.get("offer_digest"),
                "basis_current": (basis_revision == current_revision
                                  if basis_revision is not None and current_revision is not None
                                  else None),
            })
        return {"package": package, "status": status,
                "available": status in AVAILABLE_STATUSES,
                "conditional": status == "grace",
                "reason": reason,
                "termination_effective_on": termination_effective_on,
                "grace_until": grace_until, "grace_conditions": grace_conditions,
                "pending_termination_on": min((entry.notice.effective_on for entry in pending),
                                              default=None),
                "scheduled_withdrawal_on": min((entry.notice.effective_on for entry in future
                                                if entry.notice.status == "withdrawn"), default=None),
                "in_force_notices": [entry.key for entry in in_force],
                "cancelled_notices": sorted(cancelled),
                "notices": notice_rows,
                "current_offer_revision": current_revision,
                "offer_revised_since_notice": any(
                    row["basis_current"] is False for row in notice_rows if row["in_force"])}


def fleet_status(requirement: Requirement, offer_pairs: list[tuple[dict[str, Any], Offer]],
                 timeline: LicenseTimeline, as_of: str) -> dict[str, Any]:
    rows = []
    by_package: dict[str, dict[str, Any]] = {}
    for document, offer in offer_pairs:
        row = timeline.standing(offer.package, as_of, offer_document=document)
        catalog_reasons = []
        if not offer.permits(requirement.client):
            catalog_reasons.append("license_does_not_permit_client")
        if offer.path not in requirement.allowed_paths:
            catalog_reasons.append("collection_path_not_permitted")
        row.update({"provider": offer.provider, "path": offer.path,
                    "license": offer.license, "allowed_clients": list(offer.allowed_clients),
                    "current_offer_digest": document["digest"],
                    "client_permitted": offer.permits(requirement.client),
                    "path_permitted": offer.path in requirement.allowed_paths,
                    "catalog_reasons": catalog_reasons})
        rows.append(row)
        by_package[offer.package] = row
    blocked = {package: row["reason"] or "not_available"
               for package, row in by_package.items() if not row["available"]}
    return {"rows": rows, "by_package": by_package, "blocked": blocked,
            "offer_revisions_changed": sorted(package for package, row in by_package.items()
                                              if row["offer_revised_since_notice"])}


def license_basis(requirement: Requirement, offers: Iterable[Offer],
                  fleet: dict[str, dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
    """冻结分析里的授权依据：当时为什么可以使用这些包。"""
    rows = []
    for offer in offers:
        status = fleet["by_package"].get(offer.package)
        rows.append({"package": offer.package, "as_of": as_of,
                     "license": offer.license, "allowed_clients": list(offer.allowed_clients),
                     "client_permitted": offer.permits(requirement.client),
                     "path_permitted": offer.path in requirement.allowed_paths,
                     "license_status": status["status"] if status else None,
                     "available": status["available"] if status else None,
                     "conditional": status["conditional"] if status else None,
                     "termination_effective_on": status["termination_effective_on"] if status else None,
                     "grace_until": status["grace_until"] if status else None,
                     "grace_conditions": status["grace_conditions"] if status else None,
                     "in_force_notices": status["in_force_notices"] if status else []})
    return rows


def _slice_delta(before, after) -> list[dict[str, Any]]:
    first = {cell.slice: cell for cell in before.cells}
    second = {cell.slice: cell for cell in after.cells}
    rows = []
    for token in sorted(first.keys() | second.keys()):
        left = first.get(token)
        right = second.get(token)
        if left == right:
            continue
        rows.append({"slice": token,
                     "available_before": left.available if left else 0,
                     "available_after": right.available if right else 0,
                     "credited_before": left.credited if left else 0,
                     "credited_after": right.credited if right else 0,
                     "shortage_before": left.shortage if left else 0,
                     "shortage_after": right.shortage if right else 0,
                     "lost_available": (left.available if left else 0) - (right.available if right else 0),
                     "lost_credited": (left.credited if left else 0) - (right.credited if right else 0),
                     "mandatory": (right or left).mandatory})
    return rows


class AvailabilityAnalyzer:
    def __init__(self, coverage: CoverageEngine, prices: PriceEngine, optimizer: PortfolioOptimizer):
        self.coverage = coverage
        self.prices = prices
        self.optimizer = optimizer

    def _minimum_feasible(self, requirement: Requirement, offers: list[Offer],
                          unavailable: dict[str, str]) -> dict[str, Any]:
        """在授权可用范围内找恢复强制覆盖的最低成本方案；超预算也要报告。"""
        eligible, _ = self.optimizer.eligible(requirement, offers, unavailable)
        if len(eligible) > self.optimizer.maximum_candidates:
            raise PortfolioError("candidate_limit",
                                 f"more than {self.optimizer.maximum_candidates} eligible offers")
        cheapest = None
        for size in range(1, min(len(eligible), requirement.maximum_packages) + 1):
            for subset in combinations(eligible, size):
                chosen = list(subset)
                coverage = self.coverage.evaluate(requirement, chosen)
                if not coverage.mandatory_complete:
                    continue
                quotes = self.prices.all_quotes(chosen, requirement)
                amount = self.prices.total(quotes)
                key = (Decimal(amount), len(chosen), tuple(sorted(offer.package for offer in chosen)))
                if cheapest is None or key < cheapest[0]:
                    cheapest = (key, amount, tuple(sorted(offer.package for offer in chosen)))
        if cheapest is None:
            return {"minimum_cost": None, "selection": [], "within_budget": False}
        return {"minimum_cost": cheapest[1], "selection": list(cheapest[2]),
                "within_budget": Decimal(cheapest[1]) <= Decimal(requirement.budget)}

    def report(self, requirement: Requirement, offer_pairs: list[tuple[dict[str, Any], Offer]],
               timeline: LicenseTimeline, as_of: str, approved: dict[str, Any] | None,
               current_input_digest: str,
               snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        offers = [offer for _, offer in offer_pairs]
        fleet = fleet_status(requirement, offer_pairs, timeline, as_of)
        unavailable = dict(fleet["blocked"])
        eligible_before, _ = self.optimizer.eligible(requirement, offers)
        eligible_after = [offer for offer in eligible_before if offer.package not in unavailable]
        coverage_before = self.coverage.evaluate(requirement, eligible_before)
        coverage_after = self.coverage.evaluate(requirement, eligible_after)
        gap_report = self.coverage.gap_report(requirement, eligible_after)

        reference = self.optimizer.search(requirement, offers)
        current = self.optimizer.search(requirement, offers, unavailable=unavailable)
        replacement = self._minimum_feasible(requirement, offers, unavailable)
        reference_cheapest = self._minimum_feasible(requirement, offers, {})
        # 已批准成本以冻结报告为准，不用后来修订的报价重算。
        approved_cost = approved["report"].get("cost") if approved is not None else None
        if replacement["minimum_cost"] is not None:
            baseline = approved_cost or requirement.budget
            extra = Decimal(replacement["minimum_cost"]) - Decimal(baseline)
            replacement["additional_budget_vs_approved"] = money(extra) if extra > 0 else "0.00"
            if reference_cheapest["minimum_cost"] is not None:
                delta = (Decimal(replacement["minimum_cost"])
                         - Decimal(reference_cheapest["minimum_cost"]))
                replacement["additional_cost_vs_unconstrained"] = money(delta) if delta > 0 else "0.00"
                replacement["unconstrained_selection"] = reference_cheapest["selection"]
                replacement["unconstrained_cost"] = reference_cheapest["minimum_cost"]
        else:
            replacement["additional_budget_vs_approved"] = None
            replacement["additional_cost_vs_unconstrained"] = None
            replacement["reason"] = current.reason
        replacement["approved_cost"] = approved_cost
        replacement["budget"] = requirement.budget

        approval = self._approval(requirement, offers, fleet, as_of,
                                  approved, current_input_digest, current, snapshot)
        return {
            "requirement": requirement.key, "client": requirement.client, "as_of": as_of,
            "budget": requirement.budget,
            "license": {"packages": fleet["rows"], "excluded": unavailable,
                        "offer_revisions_changed": fleet["offer_revisions_changed"],
                        "notice_documents": timeline.notice_documents()},
            "current_plan": {"feasible": current.best is not None,
                             "selection": list(current.best.packages) if current.best else [],
                             "cost": current.best.cost if current.best else None,
                             "reason": current.reason,
                             "coverage": coverage_after.document(),
                             "conditional_packages": [row["package"] for row in fleet["rows"]
                                                      if row["status"] == "grace"],
                             "license_exclusions": [{"package": package, "reason": reason}
                                                    for package, reason in sorted(unavailable.items())]},
            "reference_plan": {"purpose": "selection if license notices are disregarded; not approvable",
                               "feasible": reference.best is not None,
                               "selection": list(reference.best.packages) if reference.best else [],
                               "cost": reference.best.cost if reference.best else None,
                               "coverage": coverage_before.document()},
            "impact": {"slice_changes": _slice_delta(coverage_before, coverage_after),
                       "gaps": gap_report["gaps"],
                       "mandatory_complete_before": coverage_before.mandatory_complete,
                       "mandatory_complete_after": coverage_after.mandatory_complete,
                       "replacement": replacement},
            "inflight": approval["inflight"],
            "approval": approval["approval"],
        }

    def _approval(self, requirement: Requirement, offers: list[Offer],
                  fleet: dict[str, dict[str, Any]], as_of: str,
                  approved: dict[str, Any] | None, current_input_digest: str,
                  current, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        if approved is None:
            return {"inflight": None,
                    "approval": {"existing_analysis": None, "reusable": False,
                                 "requires_new_analysis": True,
                                 "reasons": ["no_approved_analysis"],
                                 "affected_packages": [], "affected_slices": [],
                                 "deadline": None, "grace_allows_interim_use": False}}

        by_package = {offer.package: offer for offer in offers}
        selection = list(approved["report"].get("selection", []))
        selected = [by_package[package] for package in selection if package in by_package]
        selected_rows = []
        terminated_packages = []
        graced_packages = []
        deadline = None
        for offer in selected:
            row = fleet["by_package"].get(offer.package)
            if row is None:
                continue
            selected_rows.append({"package": offer.package, "status": row["status"],
                                  "available": row["available"], "reason": row["reason"],
                                  "termination_effective_on": row["termination_effective_on"],
                                  "grace_until": row["grace_until"],
                                  "grace_conditions": row["grace_conditions"],
                                  "pending_termination_on": row["pending_termination_on"]})
            if row["status"] == "terminated":
                terminated_packages.append(offer.package)
                candidate = row["termination_effective_on"]
                if candidate and (deadline is None or candidate < deadline):
                    deadline = candidate
            elif row["status"] == "grace":
                graced_packages.append(offer.package)
                candidate = row["grace_until"]
                if candidate and (deadline is None or candidate < deadline):
                    deadline = candidate

        gone = [offer for offer in selected if offer.package in terminated_packages]
        kept = [offer for offer in selected if offer.package not in terminated_packages]
        before = self.coverage.evaluate(requirement, selected)
        after = self.coverage.evaluate(requirement, kept)
        affected_slices = [row for row in _slice_delta(before, after) if row["lost_credited"]]
        for row in affected_slices:
            row["packages_removed"] = sorted(offer.package for offer in gone
                                             if any(obs.slice.token() == row["slice"]
                                                    for obs in offer.observations))

        blocking = []
        warnings = []
        if terminated_packages:
            blocking.append("approved_package_terminated")
        if snapshot:
            if snapshot.get("requirement_changed"):
                blocking.append("requirement_revised_since_approval")
            if snapshot.get("offers_changed"):
                blocking.append("offers_revised_since_approval")
            if snapshot.get("notices_added") or snapshot.get("notices_removed"):
                warnings.append("license_notices_changed_since_approval")
        elif approved["input_digest"] != current_input_digest:
            warnings.append("catalog_or_license_changed_since_approval")
        if current.best is not None and sorted(current.best.packages) != sorted(selection):
            blocking.append("available_selection_differs")
        if current.best is None and selection:
            blocking.append("no_available_feasible_plan")
        if any(item["pending_termination_on"] for item in selected_rows):
            warnings.append("scheduled_termination_affects_selected_package")

        usable_today = not terminated_packages and all(
            row["client_permitted"] and row["path_permitted"] for row in fleet["rows"]
            if row["package"] in selection)
        returns = {"inflight": {"analysis_id": approved["id"], "approved_at": approved["approved_at"],
                                "input_digest": approved["input_digest"],
                                "selection": selection,
                                "selected_license_status": selected_rows,
                                "uses_unavailable_supply": bool(terminated_packages),
                                "conditional_supply": graced_packages,
                                "usable_today": usable_today,
                                "can_continue_without_reapproval": usable_today and not blocking},
                  "approval": {"existing_analysis": approved["id"],
                               "reusable": usable_today and not blocking,
                               "requires_new_analysis": bool(blocking),
                               "reanalysis_recommended": bool(warnings),
                               "reapproval_required_before": deadline if graced_packages
                               and not terminated_packages and not blocking else None,
                               "reasons": blocking, "warnings": warnings,
                               "affected_packages": sorted(set(terminated_packages + graced_packages)),
                               "affected_slices": affected_slices,
                               "deadline": deadline,
                               "grace_allows_interim_use": bool(graced_packages)
                               and not terminated_packages,
                               "snapshot_changes": snapshot}}
        return returns
