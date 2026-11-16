from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
import json
import re


class PortfolioError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


ID = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def key(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise PortfolioError("invalid_key", f"{name} must be lower-case identifier")
    return value


def text(value: Any, name: str, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PortfolioError("invalid_text", f"{name} must contain 1-{maximum} characters")
    return value.strip()


def decimal(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        raise PortfolioError("invalid_number", f"{name} must be finite decimal")
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise PortfolioError("invalid_number", f"{name} must be finite decimal") from None
    if not result.is_finite() or positive and result <= 0 or nonnegative and result < 0:
        raise PortfolioError("invalid_number", f"{name} is out of range")
    return result


def count(value: Any, name: str, *, minimum: int = 0, maximum: int = 1_000_000_000) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise PortfolioError("invalid_count", f"{name} must be integer {minimum}-{maximum}")
    return value


def identifiers(value: Any, name: str, *, empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not empty):
        raise PortfolioError("invalid_list", f"{name} requires a list")
    results = tuple(key(item, name) for item in value)
    if len(set(results)) != len(results):
        raise PortfolioError("duplicate_item", f"{name} contains duplicates")
    return results


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day(value: Any, name: str) -> str:
    """只接受 ISO 日历日期，避免依赖运行时区域设置。"""
    if not isinstance(value, str) or not DAY.fullmatch(value):
        raise PortfolioError("invalid_date", f"{name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise PortfolioError("invalid_date", f"{name} is not a calendar date") from None
    return value


def optional_day(value: Any, name: str) -> str | None:
    return None if value is None else day(value, name)


PATHS = {"teleoperation", "simulation", "portable", "internet"}
OUTCOMES = {"success", "failure", "intervention"}
PRICING = {"per_record", "per_hour", "flat"}
LICENSES = {"nonexclusive", "single_customer", "restricted"}


@dataclass(frozen=True)
class Slice:
    scenario: str
    stage: str
    outcome: str
    robot: str

    @classmethod
    def parse(cls, value: Any) -> "Slice":
        if not isinstance(value, dict):
            raise PortfolioError("slice_shape", "slice must be an object")
        outcome = value.get("outcome")
        if outcome not in OUTCOMES:
            raise PortfolioError("outcome", "outcome must be success, failure or intervention")
        return cls(key(value.get("scenario"), "scenario"), key(value.get("stage"), "stage"),
                   outcome, key(value.get("robot"), "robot"))

    def token(self) -> str:
        return "|".join((self.scenario, self.stage, self.outcome, self.robot))


@dataclass(frozen=True)
class Observation:
    slice: Slice
    count: int
    episode_ids: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any) -> "Observation":
        if not isinstance(value, dict):
            raise PortfolioError("observation_shape", "observation must be an object")
        sample_count = count(value.get("count"), "observation count", minimum=1)
        ids = identifiers(value.get("episode_ids", []), "episode_ids", empty=True)
        if ids and len(ids) != sample_count:
            raise PortfolioError("episode_count", "episode identifiers must match count")
        return cls(Slice.parse(value.get("slice")), sample_count, ids)

    def document(self) -> dict[str, Any]:
        return {"slice": asdict(self.slice), "count": self.count, "episode_ids": list(self.episode_ids)}


@dataclass(frozen=True)
class Offer:
    provider: str
    package: str
    path: str
    robots: tuple[str, ...]
    observations: tuple[Observation, ...]
    hours: str
    price_mode: str
    unit_price: str
    license: str
    allowed_clients: tuple[str, ...]
    validity: str
    notes: str

    @classmethod
    def parse(cls, value: Any) -> "Offer":
        if not isinstance(value, dict):
            raise PortfolioError("offer_shape", "offer must be an object")
        path = value.get("path")
        if path not in PATHS:
            raise PortfolioError("collection_path", "unknown collection path")
        robots = identifiers(value.get("robots"), "robots")
        raw_observations = value.get("observations")
        if not isinstance(raw_observations, list) or not raw_observations:
            raise PortfolioError("observations", "offer needs observations")
        observations = tuple(Observation.parse(item) for item in raw_observations)
        if len({obs.slice.token() for obs in observations}) != len(observations):
            raise PortfolioError("duplicate_slice", "offer repeats a slice")
        if any(obs.slice.robot not in robots for obs in observations):
            raise PortfolioError("robot_scope", "observation robot must be declared")
        all_ids = [episode for obs in observations for episode in obs.episode_ids]
        if len(set(all_ids)) != len(all_ids):
            raise PortfolioError("duplicate_episode", "episode id repeats within offer")
        hours = decimal(value.get("hours", 0), "hours", nonnegative=True)
        price_mode = value.get("price_mode")
        if price_mode not in PRICING:
            raise PortfolioError("price_mode", "unknown price mode")
        if price_mode == "per_hour" and hours == 0:
            raise PortfolioError("hours", "hourly offer requires positive hours")
        unit_price = decimal(value.get("unit_price"), "unit_price", nonnegative=True)
        license_name = value.get("license")
        if license_name not in LICENSES:
            raise PortfolioError("license", "unknown license")
        clients = identifiers(value.get("allowed_clients", []), "allowed_clients", empty=True)
        if license_name != "nonexclusive" and not clients:
            raise PortfolioError("license_scope", "restricted license requires allowed clients")
        validity = text(value.get("validity", "indefinite"), "validity", 80)
        notes = value.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 500:
            raise PortfolioError("notes", "notes must be short text")
        return cls(key(value.get("provider"), "provider"), key(value.get("package"), "package"),
                   path, robots, observations, str(hours), price_mode, str(unit_price),
                   license_name, clients, validity, notes)

    def document(self) -> dict[str, Any]:
        return {"provider": self.provider, "package": self.package, "path": self.path,
                "robots": list(self.robots), "observations": [obs.document() for obs in self.observations],
                "hours": self.hours, "price_mode": self.price_mode, "unit_price": self.unit_price,
                "license": self.license, "allowed_clients": list(self.allowed_clients),
                "validity": self.validity, "notes": self.notes}

    @property
    def sample_count(self) -> int:
        return sum(obs.count for obs in self.observations)

    def permits(self, client: str) -> bool:
        return self.license == "nonexclusive" or client in self.allowed_clients


@dataclass(frozen=True)
class Target:
    slice: Slice
    minimum: int
    mandatory: bool
    weight: str

    @classmethod
    def parse(cls, value: Any) -> "Target":
        if not isinstance(value, dict):
            raise PortfolioError("target_shape", "target must be an object")
        minimum = count(value.get("minimum"), "target minimum", minimum=1)
        mandatory = value.get("mandatory", True)
        if type(mandatory) is not bool:
            raise PortfolioError("target_mandatory", "mandatory must be boolean")
        weight = decimal(value.get("weight", 1), "weight", positive=True)
        return cls(Slice.parse(value.get("slice")), minimum, mandatory, str(weight))

    def document(self) -> dict[str, Any]:
        return {"slice": asdict(self.slice), "minimum": self.minimum,
                "mandatory": self.mandatory, "weight": self.weight}


@dataclass(frozen=True)
class Requirement:
    key: str
    client: str
    title: str
    budget: str
    targets: tuple[Target, ...]
    allowed_paths: tuple[str, ...]
    maximum_packages: int

    @classmethod
    def parse(cls, value: Any) -> "Requirement":
        if not isinstance(value, dict):
            raise PortfolioError("requirement_shape", "requirement must be object")
        raw_targets = value.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise PortfolioError("targets", "requirement needs targets")
        targets = tuple(Target.parse(item) for item in raw_targets)
        if len({target.slice.token() for target in targets}) != len(targets):
            raise PortfolioError("duplicate_target", "target slice repeats")
        paths = value.get("allowed_paths", sorted(PATHS))
        if not isinstance(paths, list) or not paths or any(path not in PATHS for path in paths):
            raise PortfolioError("allowed_paths", "invalid allowed collection paths")
        if len(set(paths)) != len(paths):
            raise PortfolioError("allowed_paths", "duplicate collection path")
        maximum = count(value.get("maximum_packages", 12), "maximum_packages", minimum=1, maximum=24)
        return cls(key(value.get("key"), "requirement key"), key(value.get("client"), "client"),
                   text(value.get("title"), "title"), str(decimal(value.get("budget"), "budget", nonnegative=True)),
                   targets, tuple(paths), maximum)

    def document(self) -> dict[str, Any]:
        return {"key": self.key, "client": self.client, "title": self.title,
                "budget": self.budget, "targets": [item.document() for item in self.targets],
                "allowed_paths": list(self.allowed_paths), "maximum_packages": self.maximum_packages}


NOTICE_STATUSES = {"terminated", "withdrawn"}


@dataclass(frozen=True)
class Notice:
    """法务授权通知。只追加，不修改；撤回也必须作为独立通知登记。"""

    notice: str
    package: str
    status: str
    effective_on: str
    grace_until: str | None
    grace_conditions: str | None
    reference: str
    supersedes: str | None
    notes: str

    @classmethod
    def parse(cls, value: Any) -> "Notice":
        if not isinstance(value, dict):
            raise PortfolioError("notice_shape", "notice must be an object")
        status = value.get("status")
        if status not in NOTICE_STATUSES:
            raise PortfolioError("notice_status", "status must be terminated or withdrawn")
        effective_on = day(value.get("effective_on"), "effective_on")
        grace_until = optional_day(value.get("grace_until"), "grace_until")
        if grace_until is not None and grace_until < effective_on:
            raise PortfolioError("notice_grace", "grace_until cannot precede effective_on")
        raw_conditions = value.get("grace_conditions")
        grace_conditions = None
        if raw_conditions is not None:
            grace_conditions = text(raw_conditions, "grace_conditions", 300)
            if grace_until is None:
                raise PortfolioError("notice_grace", "grace_conditions requires grace_until")
        supersedes = value.get("supersedes")
        if supersedes is not None:
            supersedes = key(supersedes, "supersedes")
        if status == "withdrawn":
            if supersedes is None:
                raise PortfolioError("notice_supersedes", "withdrawal must name the notice it withdraws")
            if grace_until is not None:
                raise PortfolioError("notice_grace", "withdrawal cannot grant grace")
        notes = value.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 500:
            raise PortfolioError("notes", "notes must be short text")
        return cls(key(value.get("notice"), "notice"), key(value.get("package"), "package"),
                   status, effective_on, grace_until, grace_conditions,
                   text(value.get("reference"), "reference"), supersedes, notes)

    def document(self) -> dict[str, Any]:
        return {"notice": self.notice, "package": self.package, "status": self.status,
                "effective_on": self.effective_on, "grace_until": self.grace_until,
                "grace_conditions": self.grace_conditions, "reference": self.reference,
                "supersedes": self.supersedes, "notes": self.notes}


def parse_document(kind: str, value: Any) -> Offer | Requirement | "Notice":
    if kind == "offer":
        return Offer.parse(value)
    if kind == "requirement":
        return Requirement.parse(value)
    if kind == "notice":
        return Notice.parse(value)
    raise PortfolioError("document_kind", "kind must be offer, requirement or notice")
