from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from .contracts import Offer, Requirement, Target, PortfolioError, decimal
from .coverage import CoverageEngine
from .economics import PriceEngine, money
from .optimizer import PortfolioOptimizer


class SensitivityAnalyzer:
    def __init__(self, optimizer: PortfolioOptimizer, coverage: CoverageEngine, prices: PriceEngine):
        self.optimizer = optimizer
        self.coverage = coverage
        self.prices = prices

    def budget_grid(self, requirement: Requirement, offers: list[Offer], budgets: list[str],
                    unavailable: dict[str, str] | None = None) -> list[dict[str, Any]]:
        if not budgets or len(budgets) > 20:
            raise PortfolioError("grid_size", "budget grid requires 1-20 points")
        results = []
        for raw in budgets:
            budget = decimal(raw, "budget", nonnegative=True)
            variant = replace(requirement, budget=str(budget))
            search = self.optimizer.search(variant, offers, unavailable=unavailable)
            results.append({"budget": money(budget), "feasible": search.best is not None,
                            "selected": list(search.best.packages) if search.best else [],
                            "cost": search.best.cost if search.best else None,
                            "reason": search.reason, "evaluated": search.evaluated})
        return results

    def target_grid(self, requirement: Requirement, offers: list[Offer],
                    target_token: str, minimums: list[int],
                    unavailable: dict[str, str] | None = None) -> list[dict[str, Any]]:
        if not minimums or len(minimums) > 20:
            raise PortfolioError("grid_size", "target grid requires 1-20 points")
        targets = list(requirement.targets)
        target_index = next((index for index, target in enumerate(targets)
                             if target.slice.token() == target_token), None)
        if target_index is None:
            raise PortfolioError("unknown_target", "target slice not in requirement")
        results = []
        for minimum in minimums:
            if type(minimum) is not int or minimum < 1:
                raise PortfolioError("invalid_minimum", "target minimum must be positive integer")
            revised = list(targets)
            revised[target_index] = replace(revised[target_index], minimum=minimum)
            variant = replace(requirement, targets=tuple(revised))
            search = self.optimizer.search(variant, offers, unavailable=unavailable)
            results.append({"minimum": minimum, "feasible": search.best is not None,
                            "selected": list(search.best.packages) if search.best else [],
                            "cost": search.best.cost if search.best else None,
                            "reason": search.reason})
        return results

    def remove_offer(self, requirement: Requirement, offers: list[Offer], package: str,
                     unavailable: dict[str, str] | None = None) -> dict[str, Any]:
        if package not in {offer.package for offer in offers}:
            raise PortfolioError("unknown_offer", "offer not in catalog")
        baseline = self.optimizer.search(requirement, offers, unavailable=unavailable)
        blocked = {**(unavailable or {}), package: "removed_for_scenario"}
        alternative = self.optimizer.search(requirement, offers, unavailable=blocked)
        return {"removed": package,
                "before_selection": list(baseline.best.packages) if baseline.best else [],
                "before_cost": baseline.best.cost if baseline.best else None,
                "after_selection": list(alternative.best.packages) if alternative.best else [],
                "after_cost": alternative.best.cost if alternative.best else None,
                "after_feasible": alternative.best is not None,
                "reason": alternative.reason}

    def price_change(self, requirement: Requirement, offers: list[Offer],
                     package: str, unit_price: str,
                     unavailable: dict[str, str] | None = None) -> dict[str, Any]:
        if package not in {offer.package for offer in offers}:
            raise PortfolioError("unknown_offer", "offer not in catalog")
        revised_price = decimal(unit_price, "unit_price", nonnegative=True)
        baseline = self.optimizer.search(requirement, offers, unavailable=unavailable)
        revised = [replace(offer, unit_price=str(revised_price)) if offer.package == package else offer
                   for offer in offers]
        changed = self.optimizer.search(requirement, revised, unavailable=unavailable)
        return {"package": package, "new_unit_price": str(revised_price),
                "before_selection": list(baseline.best.packages) if baseline.best else [],
                "after_selection": list(changed.best.packages) if changed.best else [],
                "before_cost": baseline.best.cost if baseline.best else None,
                "after_cost": changed.best.cost if changed.best else None,
                "after_feasible": changed.best is not None}

    def leave_one_out(self, requirement: Requirement, offers: list[Offer],
                      unavailable: dict[str, str] | None = None) -> list[dict[str, Any]]:
        baseline = self.optimizer.search(requirement, offers, unavailable=unavailable)
        if baseline.best is None:
            return []
        return [self.remove_offer(requirement, offers, package, unavailable=unavailable)
                for package in baseline.best.packages]
