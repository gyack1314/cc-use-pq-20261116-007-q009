from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from .contracts import Notice, Offer, Requirement, PortfolioError, parse_document
from .ledger import Ledger


def read_json(path: str | Path, *, limit: int = 4_000_000) -> dict[str, Any]:
    file = Path(path)
    if not file.is_file() or file.is_symlink() or file.stat().st_size > limit:
        raise PortfolioError("input_file", "local JSON input missing or too large")
    try:
        value = json.loads(file.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise PortfolioError("input_json", "input must be UTF-8 JSON object") from None
    if not isinstance(value, dict):
        raise PortfolioError("input_shape", "input must be JSON object")
    return value


class Catalog:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def import_offer(self, value: dict[str, Any]) -> dict[str, Any]:
        offer = Offer.parse(value)
        return self.ledger.insert_document("offer", offer.package, offer.document())

    def import_requirement(self, value: dict[str, Any]) -> dict[str, Any]:
        requirement = Requirement.parse(value)
        return self.ledger.insert_document("requirement", requirement.key, requirement.document())

    def _prepare_notice(self, body: dict[str, Any]) -> dict[str, Any]:
        """把通知固定到当前报价修订，并校验撤回链；单条与批量导入共用。"""
        notice = Notice.parse(body)
        offer = self.ledger.latest_document("offer", notice.package)
        if offer is None:
            raise PortfolioError("unknown_offer", "notice package is not in catalog")
        if notice.status == "withdrawn":
            target = self.ledger.latest_document("notice", notice.supersedes or "")
            if target is None:
                raise PortfolioError("unknown_notice", "withdrawn notice is not registered")
            if target["body"]["status"] != "terminated":
                raise PortfolioError("notice_supersedes", "only a termination can be withdrawn")
            if target["body"]["package"] != notice.package:
                raise PortfolioError("notice_package", "withdrawal must name the same package")
            if notice.effective_on < target["body"]["effective_on"]:
                raise PortfolioError("notice_withdrawal_date",
                                     "withdrawal cannot take effect before the termination")
        result = notice.document()
        result["basis"] = {"offer_revision": offer["revision"], "offer_digest": offer["digest"]}
        return result

    def import_notice(self, value: dict[str, Any]) -> dict[str, Any]:
        notice = Notice.parse(value)
        body = self._prepare_notice(value)
        return self.ledger.insert_document("notice", notice.notice, body)

    def import_file(self, kind: str, path: str | Path) -> dict[str, Any]:
        value = read_json(path)
        if kind == "offer":
            return self.import_offer(value)
        if kind == "requirement":
            return self.import_requirement(value)
        if kind == "notice":
            return self.import_notice(value)
        raise PortfolioError("document_kind", "kind must be offer, requirement or notice")

    def import_batch(self, value: dict[str, Any]) -> dict[str, Any]:
        """整批校验完成后才写入任何修订。"""
        if not isinstance(value, dict) or not isinstance(value.get("documents"), list):
            raise PortfolioError("batch_shape", "batch requires documents array")
        documents = value["documents"]
        if not 1 <= len(documents) <= 100:
            raise PortfolioError("batch_size", "batch requires 1-100 documents")
        entries = []
        for position, item in enumerate(documents):
            if not isinstance(item, dict) or not isinstance(item.get("body"), dict):
                raise PortfolioError("batch_item", f"document {position} needs kind and body")
            kind = item.get("kind")
            parsed = parse_document(kind, item["body"])
            document = parsed.document()
            if isinstance(parsed, Offer):
                identity = parsed.package
            elif isinstance(parsed, Requirement):
                identity = parsed.key
            else:
                identity = parsed.notice
            entries.append((kind, identity, document))

        def prepare(kind: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
            return self._prepare_notice(body) if kind == "notice" else body

        imported = self.ledger.insert_documents(entries, prepare=prepare)
        return {"count": len(imported), "changed": sum(doc["changed"] for doc in imported),
                "documents": [{"kind": doc["kind"], "key": doc["key"],
                               "revision": doc["revision"], "changed": doc["changed"]}
                              for doc in imported]}

    def import_batch_file(self, path: str | Path) -> dict[str, Any]:
        return self.import_batch(read_json(path))

    def offers(self) -> list[tuple[dict[str, Any], Offer]]:
        return [(doc, Offer.parse(doc["body"])) for doc in self.ledger.documents("offer")]

    def requirement(self, key: str) -> tuple[dict[str, Any], Requirement]:
        doc = self.ledger.latest_document("requirement", key)
        if not doc:
            raise PortfolioError("missing_requirement", f"requirement {key} not found")
        return doc, Requirement.parse(doc["body"])

    def notices(self) -> list[dict[str, Any]]:
        return self.ledger.documents("notice")

    def history(self, kind: str, key: str) -> list[dict[str, Any]]:
        if kind not in {"offer", "requirement", "notice"}:
            raise PortfolioError("document_kind", "unknown document kind")
        return [doc for doc in self.ledger.documents(kind, latest=False) if doc["key"] == key]

    def summary(self) -> dict[str, Any]:
        offers = self.ledger.documents("offer")
        requirements = self.ledger.documents("requirement")
        return {"offer_count": len(offers), "requirement_count": len(requirements),
                "notice_count": len(self.ledger.documents("notice")),
                "offer_versions": sum(len(self.history("offer", item["key"])) for item in offers),
                "requirement_versions": sum(len(self.history("requirement", item["key"])) for item in requirements)}

    def compare_versions(self, kind: str, key: str, earlier: int, later: int) -> dict[str, Any]:
        if earlier == later or earlier < 1 or later < 1:
            raise PortfolioError("revision_pair", "choose distinct positive revisions")
        history = {doc["revision"]: doc for doc in self.history(kind, key)}
        if earlier not in history or later not in history:
            raise PortfolioError("missing_revision", "requested revision is absent")
        first, second = history[earlier]["body"], history[later]["body"]
        changed = []
        for field in sorted(first.keys() | second.keys()):
            if first.get(field) == second.get(field):
                continue
            if field == "observations" and kind == "offer":
                old = {str(item["slice"]): item for item in first[field]}
                new = {str(item["slice"]): item for item in second[field]}
                changes = [{"slice": token, "earlier_count": old.get(token, {}).get("count"),
                            "later_count": new.get(token, {}).get("count")}
                           for token in sorted(old.keys() | new.keys()) if old.get(token) != new.get(token)]
                changed.append({"field": field, "slice_changes": changes})
            elif field == "targets" and kind == "requirement":
                old = {str(item["slice"]): item for item in first[field]}
                new = {str(item["slice"]): item for item in second[field]}
                changes = [{"slice": token, "earlier": old.get(token), "later": new.get(token)}
                           for token in sorted(old.keys() | new.keys()) if old.get(token) != new.get(token)]
                changed.append({"field": field, "target_changes": changes})
            else:
                changed.append({"field": field, "earlier": first.get(field),
                                "later": second.get(field)})
        return {"kind": kind, "key": key, "earlier_revision": earlier,
                "later_revision": later, "changed": bool(changed),
                "earlier_digest": history[earlier]["digest"],
                "later_digest": history[later]["digest"], "changes": changed}
