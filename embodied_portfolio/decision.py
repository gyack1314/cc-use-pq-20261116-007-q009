from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .contracts import Offer, Requirement, PortfolioError
from .coverage import CoverageEngine
from .economics import PriceEngine
from .ledger import Ledger, digest
from .optimizer import PortfolioOptimizer
from .sensitivity import SensitivityAnalyzer
from .diagnostics import SupplyDiagnostics
from .audit import DecisionAudit
from .availability import AvailabilityAnalyzer, LicenseTimeline, fleet_status, license_basis


class PortfolioService:
    def __init__(self, db_path: str | Path):
        self.ledger = Ledger(db_path)
        self.catalog = Catalog(self.ledger)
        self.coverage = CoverageEngine()
        self.prices = PriceEngine()
        self.optimizer = PortfolioOptimizer(self.coverage, self.prices)
        self.sensitivity = SensitivityAnalyzer(self.optimizer, self.coverage, self.prices)
        self.diagnostics = SupplyDiagnostics(self.coverage, self.prices)
        self.audit = DecisionAudit(self.ledger, self.coverage, self.prices, self.optimizer)
        self.availability_analyzer = AvailabilityAnalyzer(self.coverage, self.prices, self.optimizer)

    def close(self):
        self.ledger.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def import_offer(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.import_offer(body)

    def import_requirement(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.import_requirement(body)

    def import_notice(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.catalog.import_notice(body)

    def import_file(self, kind: str, path: str | Path) -> dict[str, Any]:
        return self.catalog.import_file(kind, path)

    def import_batch_file(self, path: str | Path) -> dict[str, Any]:
        return self.catalog.import_batch_file(path)

    def _as_of(self, value: str | None) -> str:
        if value is None:
            return datetime.now(timezone.utc).date().isoformat()
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise PortfolioError("invalid_date", "as_of must be YYYY-MM-DD") from None

    def _license_state(self) -> tuple[LicenseTimeline, list[dict[str, Any]]]:
        notice_docs = self.catalog.notices()
        return LicenseTimeline(notice_docs), notice_docs

    def plan(self, requirement_key: str, as_of: str | None = None) -> dict[str, Any]:
        as_of = self._as_of(as_of)
        requirement_doc, requirement = self.catalog.requirement(requirement_key)
        offer_pairs = self.catalog.offers()
        offers = [offer for _, offer in offer_pairs]
        timeline, notice_docs = self._license_state()
        fleet = fleet_status(requirement, offer_pairs, timeline, as_of)
        unavailable = dict(fleet["blocked"])
        available_offers = [offer for offer in offers if offer.package not in unavailable]
        search = self.optimizer.search(requirement, offers, unavailable=unavailable)
        selected = [offer for offer in available_offers
                    if search.best and offer.package in search.best.packages]
        selected_quotes = self.prices.all_quotes(selected, requirement)
        selected_coverage = self.coverage.evaluate(requirement, selected)
        reference = self.optimizer.search(requirement, offers)
        report = {"schema_version": 2, "requirement": requirement.key,
                  "requirement_revision": requirement_doc["revision"],
                  "client": requirement.client, "budget": requirement.budget,
                  "as_of": as_of,
                  "feasible": search.best is not None,
                  "selection": list(search.best.packages) if search.best else [],
                  "cost": search.best.cost if search.best else None,
                  "coverage": selected_coverage.document(),
                  "search": search.document(),
                  "quotes": [quote.document() for quote in selected_quotes],
                  "budget_pressure": self.prices.budget_pressure(selected_quotes, requirement),
                  "provider_spend": self.prices.provider_breakdown(selected_quotes),
                  "selected_attribution": self.coverage.attribution(requirement, selected),
                  "cost_per_credited_sample": self.prices.cost_per_credited_sample(
                      search.best.cost, selected_coverage.total_credited) if search.best else None,
                  "license_basis": license_basis(requirement, selected, fleet, as_of),
                  "license_exclusions": [{"package": package, "reason": reason}
                                         for package, reason in sorted(unavailable.items())],
                  "license_notice_versions": [
                      {"id": doc["id"], "key": doc["key"], "revision": doc["revision"],
                       "digest": doc["digest"], "status": doc["body"]["status"],
                       "effective_on": doc["body"]["effective_on"],
                       "basis": doc["body"].get("basis")}
                      for doc in notice_docs],
                  "license_disregarded_plan": {
                      "purpose": "what the selection would be without license notices; not approvable",
                      "feasible": reference.best is not None,
                      "selection": list(reference.best.packages) if reference.best else [],
                      "cost": reference.best.cost if reference.best else None},
                  "target_supply": self.diagnostics.target_supply(requirement, available_offers),
                  "eligible_supply_readiness": self.diagnostics.data_readiness(
                      requirement, available_offers),
                  "exclusions": self.optimizer.exclusion_analysis(
                      requirement, offers, selected, unavailable=unavailable),
                  "catalog_versions": [{"id": doc["id"], "key": doc["key"],
                                        "revision": doc["revision"], "digest": doc["digest"]}
                                       for doc, _ in offer_pairs]}
        return self.ledger.insert_analysis(requirement_doc, [doc for doc, _ in offer_pairs],
                                           report, notice_docs=notice_docs)

    def approve(self, analysis_id: int, expected_input_digest: str) -> dict[str, Any]:
        """批准是今日行为：冻结快照可回溯，但所选供给必须今天仍获授权。"""
        analysis = self.ledger.analysis(analysis_id)
        if analysis["state"] == "approved":
            return self.ledger.approve(analysis_id, expected_input_digest)
        _, requirement = self.catalog.requirement(analysis["requirement_key"])
        offer_pairs = self.catalog.offers()
        timeline, _ = self._license_state()
        today = self._as_of(None)
        fleet = fleet_status(requirement, offer_pairs, timeline, today)
        selected = analysis["report"].get("selection", [])
        blocked = [{"package": package, "reason": fleet["blocked"][package]}
                   for package in selected if package in fleet["blocked"]]
        if blocked:
            raise PortfolioError(
                "license_unavailable",
                "selected supply is no longer licensed: "
                + ", ".join(f"{item['package']}={item['reason']}" for item in blocked))
        return self.ledger.approve(analysis_id, expected_input_digest)

    def notices(self) -> list[dict[str, Any]]:
        return self.catalog.notices()

    def notice_history(self, key: str) -> list[dict[str, Any]]:
        return self.catalog.history("notice", key)

    def availability(self, requirement_key: str, as_of: str | None = None,
                     analysis_id: int | None = None) -> dict[str, Any]:
        as_of = self._as_of(as_of)
        _, requirement = self.catalog.requirement(requirement_key)
        offer_pairs = self.catalog.offers()
        timeline, _ = self._license_state()
        if analysis_id is not None:
            approved = self.ledger.analysis(analysis_id)
            if approved["requirement_key"] != requirement_key:
                raise PortfolioError("requirement_pair", "analysis belongs to another requirement")
            if approved["state"] != "approved":
                raise PortfolioError("not_approved", "inflight review needs an approved analysis")
        else:
            approved_rows = [item for item in self.ledger.analyses(requirement_key)
                             if item["state"] == "approved"]
            approved = approved_rows[-1] if approved_rows else None
        snapshot = self._snapshot_change(approved) if approved else None
        current_digest = self.ledger.latest_input_digest(requirement_key)
        return self.availability_analyzer.report(requirement, offer_pairs, timeline, as_of,
                                                 approved, current_digest, snapshot)

    def _snapshot_change(self, analysis: dict[str, Any]) -> dict[str, Any]:
        """比较冻结快照与其后目录/通知修订，给出可追溯的差异依据。"""
        pinned = self.ledger.analysis_inputs(analysis["id"])
        by_kind: dict[str, dict[str, str]] = {}
        for document in pinned:
            by_kind.setdefault(document["kind"], {})[document["key"]] = document["digest"]
        current = {"requirement": {doc["key"]: doc["digest"]
                                   for doc in self.ledger.documents("requirement")},
                   "offer": {doc["key"]: doc["digest"] for doc in self.ledger.documents("offer")},
                   "notice": {doc["key"]: doc["digest"] for doc in self.ledger.documents("notice")}}
        changed = {}
        for kind in ("requirement", "offer"):
            before = by_kind.get(kind, {})
            after = current[kind]
            changed[kind] = sorted(key for key in before.keys() | after.keys()
                                   if before.get(key) != after.get(key))
        pinned_notices = set(by_kind.get("notice", {}))
        latest_notices = set(current["notice"])
        return {"requirement_changed": bool(changed["requirement"]),
                "offers_changed": changed["offer"],
                "notices_added": sorted(latest_notices - pinned_notices),
                "notices_removed": sorted(pinned_notices - latest_notices)}

    def analysis(self, analysis_id: int) -> dict[str, Any]:
        return self.ledger.analysis(analysis_id)

    def verify(self, analysis_id: int) -> dict[str, Any]:
        return self.ledger.verify(analysis_id)

    def history(self, requirement_key: str | None = None) -> list[dict[str, Any]]:
        return self.ledger.analyses(requirement_key)

    def document_history(self, kind: str, key: str) -> list[dict[str, Any]]:
        return self.catalog.history(kind, key)

    def compare_versions(self, kind: str, key: str, earlier: int, later: int) -> dict[str, Any]:
        return self.catalog.compare_versions(kind, key, earlier, later)

    def gaps(self, requirement_key: str, package_keys: list[str],
             as_of: str | None = None) -> dict[str, Any]:
        requirement, offers, unavailable, as_of = self._scenario_context(requirement_key, as_of)
        by_package = {offer.package: offer for offer in offers}
        if any(key not in by_package for key in package_keys):
            raise PortfolioError("unknown_offer", "requested package not in catalog")
        blocked = [{"package": key, "reason": unavailable[key]}
                   for key in package_keys if key in unavailable]
        usable = [by_package[key] for key in package_keys if key not in unavailable]
        report = self.coverage.gap_report(requirement, usable)
        without_notices = self.coverage.gap_report(
            requirement, [by_package[key] for key in package_keys])
        before_missing = {item["slice"]: item["missing"] for item in without_notices["gaps"]}
        after_missing = {item["slice"]: item["missing"] for item in report["gaps"]}
        caused = [{"slice": token, "missing_now": after_missing[token],
                   "missing_if_licensed": before_missing.get(token, 0)}
                  for token in sorted(after_missing)
                  if after_missing[token] > before_missing.get(token, 0)]
        return {**report, "as_of": as_of, "license_excluded": blocked,
                "shortage_caused_by_license": caused,
                "effective_mandatory_complete": report["mandatory_complete"] and not blocked}

    def marginal(self, requirement_key: str, current_keys: list[str], candidate_key: str,
                 as_of: str | None = None) -> dict[str, Any]:
        requirement, offers, unavailable, as_of = self._scenario_context(requirement_key, as_of)
        by_package = {offer.package: offer for offer in offers}
        if candidate_key not in by_package or any(key not in by_package for key in current_keys):
            raise PortfolioError("unknown_offer", "requested package not in catalog")
        result = self.coverage.marginal(requirement, [by_package[key] for key in current_keys],
                                        by_package[candidate_key])
        return {**result, "as_of": as_of,
                "candidate_license_excluded": unavailable.get(candidate_key),
                "usable": candidate_key not in unavailable}

    def frontier(self, requirement_key: str, as_of: str | None = None) -> list[dict[str, Any]]:
        requirement, offers, unavailable, _ = self._scenario_context(requirement_key, as_of)
        return self.optimizer.frontier(requirement, offers, unavailable=unavailable)

    def status(self) -> dict[str, Any]:
        summary = self.catalog.summary()
        analyses = self.ledger.analyses()
        return {**summary, "analysis_count": len(analyses),
                "approved_count": sum(item["state"] == "approved" for item in analyses)}

    def readiness(self, requirement_key: str, as_of: str | None = None) -> dict[str, Any]:
        as_of = self._as_of(as_of)
        _, requirement = self.catalog.requirement(requirement_key)
        offer_pairs = self.catalog.offers()
        timeline, _ = self._license_state()
        fleet = fleet_status(requirement, offer_pairs, timeline, as_of)
        offers = [offer for _, offer in offer_pairs if offer.package not in fleet["blocked"]]
        return {"as_of": as_of,
                "data": self.diagnostics.data_readiness(requirement, offers),
                "paths": self.diagnostics.path_distribution(offers),
                "providers": self.diagnostics.provider_exposure(requirement, offers),
                "restrictions": self.diagnostics.licensing_reasons(requirement, offers),
                "license_exclusions": [{"package": package, "reason": reason}
                                       for package, reason in sorted(fleet["blocked"].items())]}

    def brief(self, requirement_key: str, as_of: str | None = None) -> dict[str, Any]:
        as_of = self._as_of(as_of)
        _, requirement = self.catalog.requirement(requirement_key)
        offer_pairs = self.catalog.offers()
        timeline, _ = self._license_state()
        fleet = fleet_status(requirement, offer_pairs, timeline, as_of)
        offers = [offer for _, offer in offer_pairs if offer.package not in fleet["blocked"]]
        return {**self.diagnostics.procurement_brief(requirement, offers), "as_of": as_of,
                "license_exclusions": [{"package": package, "reason": reason}
                                       for package, reason in sorted(fleet["blocked"].items())],
                "license_terms": [{"package": row["package"], "status": row["status"],
                                   "termination_effective_on": row["termination_effective_on"],
                                   "grace_until": row["grace_until"],
                                   "grace_conditions": row["grace_conditions"],
                                   "pending_termination_on": row["pending_termination_on"]}
                                  for row in fleet["rows"] if row["notices"]]}

    def _scenario_context(self, requirement_key: str, as_of: str | None
                          ) -> tuple[Requirement, list[Offer], dict[str, str], str]:
        as_of = self._as_of(as_of)
        _, requirement = self.catalog.requirement(requirement_key)
        offer_pairs = self.catalog.offers()
        timeline, _ = self._license_state()
        fleet = fleet_status(requirement, offer_pairs, timeline, as_of)
        return requirement, [offer for _, offer in offer_pairs], dict(fleet["blocked"]), as_of

    def budget_grid(self, requirement_key: str, budgets: list[str],
                    as_of: str | None = None) -> list[dict[str, Any]]:
        requirement, offers, unavailable, _ = self._scenario_context(requirement_key, as_of)
        return self.sensitivity.budget_grid(requirement, offers, budgets, unavailable=unavailable)

    def target_grid(self, requirement_key: str, target: str, minimums: list[int],
                    as_of: str | None = None) -> list[dict[str, Any]]:
        requirement, offers, unavailable, _ = self._scenario_context(requirement_key, as_of)
        return self.sensitivity.target_grid(requirement, offers, target, minimums,
                                            unavailable=unavailable)

    def withdrawal(self, requirement_key: str, package: str,
                   as_of: str | None = None) -> dict[str, Any]:
        requirement, offers, unavailable, _ = self._scenario_context(requirement_key, as_of)
        return self.sensitivity.remove_offer(requirement, offers, package,
                                             unavailable=unavailable)

    def price_scenario(self, requirement_key: str, package: str, price: str,
                       as_of: str | None = None) -> dict[str, Any]:
        requirement, offers, unavailable, _ = self._scenario_context(requirement_key, as_of)
        return self.sensitivity.price_change(requirement, offers, package, price,
                                             unavailable=unavailable)

    def audit_analysis(self, analysis_id: int) -> dict[str, Any]:
        return self.audit.explain(analysis_id)

    def compare_analyses(self, earlier_id: int, later_id: int) -> dict[str, Any]:
        return self.audit.compare(earlier_id, later_id)

    def compare_packages(self, requirement_key: str, package_groups: list[list[str]],
                         as_of: str | None = None) -> dict[str, Any]:
        if not 1 <= len(package_groups) <= 12:
            raise PortfolioError("portfolio_count", "compare 1-12 groups")
        requirement, offer_list, unavailable, _ = self._scenario_context(requirement_key, as_of)
        offers = {offer.package: offer for offer in offer_list}
        preferred = self.optimizer.search(requirement, list(offers.values()),
                                          unavailable=unavailable)
        results = []
        for position, group in enumerate(package_groups):
            if not group or len(group) != len(set(group)):
                raise PortfolioError("portfolio_shape", "each group needs distinct packages")
            if len(group) > requirement.maximum_packages:
                raise PortfolioError("portfolio_size", "group exceeds package limit")
            if any(package not in offers for package in group):
                raise PortfolioError("unknown_offer", "group includes unknown package")
            chosen = [offers[package] for package in group]
            quotes = self.prices.all_quotes(chosen, requirement)
            coverage = self.coverage.evaluate(requirement, chosen)
            rejected = [{"package": quote.package, "reason": quote.reason}
                        for quote in quotes if not quote.permitted]
            rejected.extend({"package": package, "reason": unavailable[package]}
                            for package in sorted(group) if package in unavailable)
            results.append({"index": position, "packages": sorted(group),
                            "cost": self.prices.total(quotes), "coverage": coverage.document(),
                            "eligible": not rejected, "restrictions": rejected,
                            "within_budget": self.prices.within_budget(quotes, requirement),
                            "feasible": not rejected and coverage.mandatory_complete
                            and self.prices.within_budget(quotes, requirement),
                            "provider_spend": self.prices.provider_breakdown(quotes),
                            "attribution": self.coverage.attribution(requirement, chosen)})
        return {"requirement": requirement.key,
                "recommended": list(preferred.best.packages) if preferred.best else [],
                "portfolios": results}
