from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from itertools import combinations
from typing import Any

from .contracts import Offer, Requirement, PortfolioError
from .coverage import CoverageEngine, CoverageResult
from .economics import PriceEngine, Quote


@dataclass(frozen=True)
class Candidate:
    packages: tuple[str, ...]
    cost: str
    score: str
    credited: int
    shortage: int
    duplicates: int
    mandatory_complete: bool
    within_budget: bool

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResult:
    best: Candidate | None
    evaluated: int
    feasible: int
    filtered: tuple[dict[str, str], ...]
    alternatives: tuple[Candidate, ...]
    reason: str | None

    def document(self) -> dict[str, Any]:
        return {"best": self.best.document() if self.best else None,
                "evaluated": self.evaluated, "feasible": self.feasible,
                "filtered": list(self.filtered),
                "alternatives": [item.document() for item in self.alternatives],
                "reason": self.reason}


class PortfolioOptimizer:
    def __init__(self, coverage: CoverageEngine, prices: PriceEngine, maximum_candidates: int = 18):
        self.coverage = coverage
        self.prices = prices
        self.maximum_candidates = maximum_candidates

    def eligible(self, requirement: Requirement, offers: list[Offer],
                 unavailable: dict[str, str] | None = None) -> tuple[list[Offer], list[dict[str, str]]]:
        eligible = []
        filtered = []
        unavailable = unavailable or {}
        target_tokens = {target.slice.token() for target in requirement.targets}
        for offer in offers:
            if offer.package in unavailable:
                filtered.append({"package": offer.package,
                                 "reason": unavailable[offer.package] or "not_available"})
                continue
            quote = self.prices.quote(offer, requirement)
            if not quote.permitted:
                filtered.append({"package": offer.package, "reason": quote.reason or "not_permitted"})
                continue
            if not any(observation.slice.token() in target_tokens for observation in offer.observations):
                filtered.append({"package": offer.package, "reason": "no_requested_slice"})
                continue
            eligible.append(offer)
        return eligible, filtered

    def evaluate(self, requirement: Requirement, offers: list[Offer]) -> Candidate:
        coverage = self.coverage.evaluate(requirement, offers)
        quotes = self.prices.all_quotes(offers, requirement)
        amount = self.prices.total(quotes)
        return Candidate(tuple(sorted(offer.package for offer in offers)), amount,
                         coverage.weighted_fraction, coverage.total_credited,
                         coverage.total_shortage, coverage.total_duplicate,
                         coverage.mandatory_complete, Decimal(amount) <= Decimal(requirement.budget))

    @staticmethod
    def ranking(candidate: Candidate) -> tuple:
        return (-Decimal(candidate.score), Decimal(candidate.cost),
                candidate.duplicates, len(candidate.packages), candidate.packages)

    def search(self, requirement: Requirement, offers: list[Offer],
               unavailable: dict[str, str] | None = None) -> SearchResult:
        eligible, filtered = self.eligible(requirement, offers, unavailable)
        if len(eligible) > self.maximum_candidates:
            raise PortfolioError("candidate_limit", f"more than {self.maximum_candidates} eligible offers")
        evaluated = 0
        feasible = []
        affordable = []
        for size in range(1, min(len(eligible), requirement.maximum_packages) + 1):
            for subset in combinations(eligible, size):
                evaluated += 1
                candidate = self.evaluate(requirement, list(subset))
                if candidate.within_budget:
                    affordable.append(candidate)
                    if candidate.mandatory_complete:
                        feasible.append(candidate)
        feasible.sort(key=self.ranking)
        affordable.sort(key=self.ranking)
        if feasible:
            return SearchResult(feasible[0], evaluated, len(feasible), tuple(filtered),
                                tuple(feasible[1:4]), None)
        if not eligible:
            reason = "no_eligible_offers"
        elif not affordable:
            reason = "all_combinations_exceed_budget"
        else:
            reason = "mandatory_targets_uncovered"
        return SearchResult(None, evaluated, 0, tuple(filtered),
                            tuple(affordable[:3]), reason)

    def frontier(self, requirement: Requirement, offers: list[Offer],
                 unavailable: dict[str, str] | None = None) -> list[dict[str, Any]]:
        eligible, _ = self.eligible(requirement, offers, unavailable)
        if len(eligible) > self.maximum_candidates:
            raise PortfolioError("candidate_limit", "too many offers for frontier")
        candidates = []
        for size in range(1, min(len(eligible), requirement.maximum_packages) + 1):
            for subset in combinations(eligible, size):
                candidate = self.evaluate(requirement, list(subset))
                if candidate.within_budget:
                    candidates.append(candidate)
        frontier = []
        for candidate in candidates:
            dominated = any(other != candidate and
                            Decimal(other.cost) <= Decimal(candidate.cost) and
                            Decimal(other.score) >= Decimal(candidate.score) and
                            (Decimal(other.cost) < Decimal(candidate.cost) or
                             Decimal(other.score) > Decimal(candidate.score))
                            for other in candidates)
            if not dominated:
                frontier.append(candidate)
        frontier.sort(key=self.ranking)
        return [item.document() for item in frontier]

    def exclusion_analysis(self, requirement: Requirement, offers: list[Offer],
                           chosen: list[Offer],
                           unavailable: dict[str, str] | None = None) -> list[dict[str, Any]]:
        unavailable = unavailable or {}
        chosen_keys = {offer.package for offer in chosen}
        baseline = self.evaluate(requirement, chosen)
        explanations = []
        for offer in offers:
            if offer.package in chosen_keys:
                continue
            quote = self.prices.quote(offer, requirement)
            if offer.package in unavailable:
                reason = unavailable[offer.package] or "not_available"
                gain = None
            elif not quote.permitted:
                reason = quote.reason
                gain = None
            else:
                with_offer = self.evaluate(requirement, [*chosen, offer])
                gain = with_offer.credited - baseline.credited
                if not with_offer.within_budget:
                    reason = "exceeds_budget_with_selection"
                elif gain == 0:
                    reason = "no_marginal_coverage"
                else:
                    reason = "lower_ranked_feasible_choice"
            explanations.append({"package": offer.package, "reason": reason,
                                 "marginal_credited": gain,
                                 "quote": quote.amount})
        return explanations
