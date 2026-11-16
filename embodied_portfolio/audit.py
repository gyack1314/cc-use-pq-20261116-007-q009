from __future__ import annotations

from decimal import Decimal
from typing import Any

from .contracts import Offer, Requirement, PortfolioError, canonical
from .coverage import CoverageEngine
from .economics import PriceEngine
from .ledger import Ledger, digest
from .optimizer import PortfolioOptimizer
from .availability import LicenseTimeline, fleet_status


class DecisionAudit:
    """仅使用冻结输入重算历史决策，不依赖当前报价清单。"""

    def __init__(self, ledger: Ledger, coverage: CoverageEngine, prices: PriceEngine,
                 optimizer: PortfolioOptimizer):
        self.ledger = ledger
        self.coverage = coverage
        self.prices = prices
        self.optimizer = optimizer

    def _inputs(self, analysis_id: int) -> tuple[dict[str, Any], Requirement, list[dict[str, Any]], list[Offer],
                                                list[dict[str, Any]]]:
        documents = self.ledger.analysis_inputs(analysis_id)
        requirements = [doc for doc in documents if doc["kind"] == "requirement"]
        if len(requirements) != 1:
            raise PortfolioError("snapshot_inputs", "analysis needs one requirement revision")
        offer_docs = [doc for doc in documents if doc["kind"] == "offer"]
        notice_docs = [doc for doc in documents if doc["kind"] == "notice"]
        return (requirements[0], Requirement.parse(requirements[0]["body"]), offer_docs,
                [Offer.parse(doc["body"]) for doc in offer_docs], notice_docs)

    def explain(self, analysis_id: int) -> dict[str, Any]:
        analysis = self.ledger.analysis(analysis_id)
        requirement_doc, requirement, offer_docs, offers, notice_docs = self._inputs(analysis_id)
        stored = analysis["report"]
        snapshot = self.ledger.verify(analysis_id)
        source_integrity = all(digest(doc["body"]) == doc["digest"]
                               for doc in [requirement_doc, *offer_docs, *notice_docs])
        report_selection = stored.get("selection", [])
        selected = [offer for offer in offers if offer.package in report_selection]
        unknown = sorted(set(report_selection) - {offer.package for offer in offers})
        as_of = stored.get("as_of")
        if as_of is None:
            # 旧快照没有授权评估日：按当时未登记任何通知重算，保持历史语义。
            timeline = LicenseTimeline([])
            blocked: dict[str, str] = {}
        else:
            timeline = LicenseTimeline(notice_docs)
            blocked = fleet_status(requirement, list(zip(offer_docs, offers)),
                                   timeline, as_of)["blocked"]
        search = self.optimizer.search(requirement, offers, unavailable=blocked)
        recomputed = list(search.best.packages) if search.best else []
        selected_coverage = self.coverage.evaluate(requirement, selected)
        quotes = self.prices.all_quotes(selected, requirement)
        recomputed_cost = self.prices.total(quotes) if search.best else None
        mismatches = []
        if not snapshot["verified"]:
            mismatches.append("snapshot_digest")
        if not source_integrity:
            mismatches.append("source_digest")
        if unknown:
            mismatches.append("unknown_selected_package")
        if report_selection != recomputed:
            mismatches.append("selection")
        if stored.get("feasible") != (search.best is not None):
            mismatches.append("feasibility")
        if stored.get("cost") != recomputed_cost:
            mismatches.append("cost")
        if canonical(stored.get("coverage")) != canonical(selected_coverage.document()):
            mismatches.append("coverage")
        if stored.get("quotes") != [quote.document() for quote in quotes]:
            mismatches.append("quotes")
        if canonical(stored.get("search")) != canonical(search.document()):
            mismatches.append("search")
        if stored.get("provider_spend") != self.prices.provider_breakdown(quotes):
            mismatches.append("provider_spend")
        if stored.get("selected_attribution") != self.coverage.attribution(requirement, selected):
            mismatches.append("attribution")
        if stored.get("cost_per_credited_sample") != (
                self.prices.cost_per_credited_sample(recomputed_cost, selected_coverage.total_credited)
                if recomputed_cost is not None else None):
            mismatches.append("unit_cost")
        lineage = [{"kind": doc["kind"], "key": doc["key"],
                    "revision": doc["revision"], "digest": doc["digest"]}
                   for doc in [requirement_doc, *offer_docs, *notice_docs]]
        current = self.ledger.latest_input_digest(requirement.key)
        return {"analysis_id": analysis_id, "state": analysis["state"],
                "historical_valid": not mismatches, "mismatches": mismatches,
                "input_current": current == analysis["input_digest"],
                "lineage": lineage, "selection": recomputed,
                "cost": recomputed_cost, "coverage": selected_coverage.document(),
                "as_of": as_of,
                "license_exclusions": stored.get("license_exclusions", []),
                "license_basis": stored.get("license_basis", []),
                "snapshot_digest_valid": snapshot["verified"],
                "source_digest_valid": source_integrity}

    def compare(self, earlier_id: int, later_id: int) -> dict[str, Any]:
        if earlier_id == later_id:
            raise PortfolioError("analysis_pair", "choose two distinct analyses")
        earlier = self.ledger.analysis(earlier_id)
        later = self.ledger.analysis(later_id)
        if earlier["requirement_key"] != later["requirement_key"]:
            raise PortfolioError("requirement_pair", "analyses must share requirement key")
        first = self.explain(earlier_id)
        second = self.explain(later_id)
        if not first["historical_valid"] or not second["historical_valid"]:
            raise PortfolioError("invalid_snapshot", "cannot compare invalid analysis")
        a = {doc["key"]: doc for doc in first["lineage"] if doc["kind"] == "offer"}
        b = {doc["key"]: doc for doc in second["lineage"] if doc["kind"] == "offer"}
        a_selected = set(first["selection"])
        b_selected = set(second["selection"])
        changes = []
        for package in sorted(a.keys() | b.keys()):
            left = a.get(package)
            right = b.get(package)
            if left is None or right is None or left["digest"] != right["digest"]:
                changes.append({"package": package,
                                "earlier_revision": left["revision"] if left else None,
                                "later_revision": right["revision"] if right else None})
        a_notices = {doc["key"]: doc for doc in first["lineage"] if doc["kind"] == "notice"}
        b_notices = {doc["key"]: doc for doc in second["lineage"] if doc["kind"] == "notice"}
        notice_changes = [{"notice": key, "present_earlier": key in a_notices,
                           "present_later": key in b_notices}
                          for key in sorted(a_notices.keys() ^ b_notices.keys())]
        left_cost = earlier["report"].get("cost")
        right_cost = later["report"].get("cost")
        cost_delta = str(Decimal(right_cost) - Decimal(left_cost)) if left_cost and right_cost else None
        a_cells = {cell["slice"]: cell for cell in first["coverage"]["cells"]}
        b_cells = {cell["slice"]: cell for cell in second["coverage"]["cells"]}
        coverage_changes = [{"slice": token,
                             "credited_before": a_cells.get(token, {}).get("credited", 0),
                             "credited_after": b_cells.get(token, {}).get("credited", 0),
                             "shortage_before": a_cells.get(token, {}).get("shortage", 0),
                             "shortage_after": b_cells.get(token, {}).get("shortage", 0)}
                            for token in sorted(a_cells.keys() | b_cells.keys())
                            if a_cells.get(token) != b_cells.get(token)]
        return {"requirement": earlier["requirement_key"], "earlier": earlier_id,
                "later": later_id, "requirement_revision_changed":
                earlier["requirement_revision"] != later["requirement_revision"],
                "offer_revisions_changed": changes,
                "notice_changes": notice_changes,
                "packages_added": sorted(b_selected - a_selected),
                "packages_removed": sorted(a_selected - b_selected),
                "cost_delta": cost_delta, "coverage_changes": coverage_changes}
