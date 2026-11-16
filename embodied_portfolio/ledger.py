from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator
import json
import sqlite3

from .contracts import PortfolioError, canonical


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
  digest TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL,
  UNIQUE(kind,key,revision));
CREATE INDEX IF NOT EXISTS document_lookup ON documents(kind,key,revision DESC);
CREATE TABLE IF NOT EXISTS analyses (
  id INTEGER PRIMARY KEY, requirement_key TEXT NOT NULL, requirement_revision INTEGER NOT NULL,
  input_digest TEXT NOT NULL, state TEXT NOT NULL, report TEXT NOT NULL, report_digest TEXT NOT NULL,
  created_at TEXT NOT NULL, approved_at TEXT);
CREATE INDEX IF NOT EXISTS analyses_by_requirement ON analyses(requirement_key,id DESC);
CREATE TABLE IF NOT EXISTS analysis_inputs (
  analysis_id INTEGER NOT NULL, document_id INTEGER NOT NULL,
  PRIMARY KEY(analysis_id,document_id), FOREIGN KEY(analysis_id) REFERENCES analyses(id),
  FOREIGN KEY(document_id) REFERENCES documents(id));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, object_key TEXT NOT NULL, details TEXT NOT NULL);
"""


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value: Any) -> str:
    return sha256(canonical(value).encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=20000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def event(self, kind: str, object_key: str, details: dict[str, Any]):
        self.db.execute("INSERT INTO events(at,kind,object_key,details) VALUES(?,?,?,?)",
                        (timestamp(), kind, object_key, canonical(details)))

    def events(self, object_key: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        args: tuple = ()
        if object_key is not None:
            sql += " WHERE object_key=?"
            args = (object_key,)
        sql += " ORDER BY id"
        return [{**dict(row), "details": json.loads(row["details"])} for row in self.db.execute(sql, args)]

    def document(self, document_id: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise PortfolioError("missing_document", f"document {document_id} not found")
        return self._document(row)

    def _document(self, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["body"] = json.loads(value["body"])
        return value

    def latest_document(self, kind: str, key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM documents WHERE kind=? AND key=? ORDER BY revision DESC LIMIT 1",
                              (kind, key)).fetchone()
        return self._document(row) if row else None

    def documents(self, kind: str, *, latest: bool = True) -> list[dict[str, Any]]:
        if latest:
            rows = self.db.execute("SELECT d.* FROM documents d JOIN (SELECT key,MAX(revision) revision "
                                   "FROM documents WHERE kind=? GROUP BY key) x ON x.key=d.key "
                                   "AND x.revision=d.revision WHERE d.kind=? ORDER BY d.key", (kind, kind))
        else:
            rows = self.db.execute("SELECT * FROM documents WHERE kind=? ORDER BY key,revision", (kind,))
        return [self._document(row) for row in rows]

    def insert_document(self, kind: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        content = canonical(body)
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        with self.transaction():
            old = self.latest_document(kind, key)
            if old and old["digest"] == content_hash:
                return old
            if kind == "notice" and old is not None:
                raise PortfolioError("notice_immutable", "a notice is recorded once and cannot be revised")
            revision = old["revision"] + 1 if old else 1
            cursor = self.db.execute("INSERT INTO documents(kind,key,revision,digest,body,received_at) "
                                     "VALUES(?,?,?,?,?,?)", (kind, key, revision, content_hash, content, timestamp()))
            self.event("import_" + kind, key, {"revision": revision, "digest": content_hash})
            return self.document(cursor.lastrowid)

    def insert_documents(self, entries: list[tuple[str, str, dict[str, Any]]],
                         prepare: Any = None) -> list[dict[str, Any]]:
        """供应商交付的一组修订要么全部提交，要么全部不提交。

        prepare(kind, key, body) 可在写入前补充派生字段；它在事务内顺序执行，
        因此批次中靠前的报价修订对后续通知可见。任一校验失败则整批回滚。
        """
        if not entries:
            raise PortfolioError("empty_batch", "batch requires documents")
        identities = [(kind, key) for kind, key, _ in entries]
        if len(set(identities)) != len(identities):
            raise PortfolioError("duplicate_batch_key", "batch repeats a document key")
        if any(kind not in {"offer", "requirement", "notice"} for kind, _, _ in entries):
            raise PortfolioError("document_kind", "batch contains unknown document kind")
        results = []
        with self.transaction():
            for kind, key, body in entries:
                if prepare is not None:
                    body = prepare(kind, key, body)
                content = canonical(body)
                content_hash = sha256(content.encode("utf-8")).hexdigest()
                old = self.latest_document(kind, key)
                if old and old["digest"] == content_hash:
                    results.append({**old, "changed": False})
                    continue
                if kind == "notice" and old is not None:
                    raise PortfolioError("notice_immutable", "a notice is recorded once and cannot be revised")
                revision = old["revision"] + 1 if old else 1
                cursor = self.db.execute(
                    "INSERT INTO documents(kind,key,revision,digest,body,received_at) VALUES(?,?,?,?,?,?)",
                    (kind, key, revision, content_hash, content, timestamp()))
                self.event("import_" + kind, key, {"revision": revision, "digest": content_hash})
                results.append({**self.document(cursor.lastrowid), "changed": True})
        return results

    def analysis(self, analysis_id: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
        if row is None:
            raise PortfolioError("missing_analysis", f"analysis {analysis_id} not found")
        result = dict(row)
        result["report"] = json.loads(result["report"])
        return result

    def analyses(self, requirement_key: str | None = None) -> list[dict[str, Any]]:
        if requirement_key:
            rows = self.db.execute("SELECT * FROM analyses WHERE requirement_key=? ORDER BY id", (requirement_key,))
        else:
            rows = self.db.execute("SELECT * FROM analyses ORDER BY id")
        return [{**dict(row), "report": json.loads(row["report"])} for row in rows]

    def insert_analysis(self, requirement: dict[str, Any], offer_docs: list[dict[str, Any]],
                        report: dict[str, Any],
                        notice_docs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        inputs = [requirement, *offer_docs, *(notice_docs or [])]
        input_refs = [(item["id"], item["digest"]) for item in inputs]
        input_hash = digest(input_refs)
        report_hash = digest(report)
        with self.transaction():
            cursor = self.db.execute("INSERT INTO analyses(requirement_key,requirement_revision,input_digest,state,report,report_digest,created_at) "
                                     "VALUES(?,?,?,?,?,?,?)", (requirement["key"], requirement["revision"],
                                     input_hash, "draft", canonical(report), report_hash, timestamp()))
            analysis_id = cursor.lastrowid
            self.db.executemany("INSERT INTO analysis_inputs VALUES(?,?)",
                                [(analysis_id, doc_id) for doc_id, _ in input_refs])
            self.event("analyze", f"analysis:{analysis_id}", {"input_digest": input_hash,
                       "report_digest": report_hash})
            return self.analysis(analysis_id)

    def analysis_inputs(self, analysis_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT d.* FROM documents d JOIN analysis_inputs a ON a.document_id=d.id "
                               "WHERE a.analysis_id=? ORDER BY CASE d.kind WHEN 'requirement' THEN 0 "
                               "WHEN 'offer' THEN 1 ELSE 2 END,d.key", (analysis_id,))
        return [self._document(row) for row in rows]

    def latest_input_digest(self, requirement_key: str) -> str:
        requirement = self.latest_document("requirement", requirement_key)
        if not requirement:
            raise PortfolioError("missing_requirement", "requirement not found")
        offers = self.documents("offer")
        notices = self.documents("notice")
        return digest([(doc["id"], doc["digest"]) for doc in [requirement, *offers, *notices]])

    def approve(self, analysis_id: int, expected_input_digest: str) -> dict[str, Any]:
        with self.transaction():
            value = self.analysis(analysis_id)
            if value["input_digest"] != expected_input_digest:
                raise PortfolioError("stale_input", "expected input fingerprint differs")
            if value["state"] == "approved":
                return value
            if self.latest_input_digest(value["requirement_key"]) != value["input_digest"]:
                raise PortfolioError("stale_input", "catalog changed since analysis")
            if not value["report"].get("feasible"):
                raise PortfolioError("infeasible", "cannot approve an infeasible portfolio")
            self.db.execute("UPDATE analyses SET state='approved',approved_at=? WHERE id=?",
                            (timestamp(), analysis_id))
            self.event("approve", f"analysis:{analysis_id}", {"report_digest": value["report_digest"]})
            return self.analysis(analysis_id)

    def verify(self, analysis_id: int) -> dict[str, Any]:
        value = self.analysis(analysis_id)
        inputs = self.analysis_inputs(analysis_id)
        current_report_digest = digest(value["report"])
        input_digest = digest([(doc["id"], doc["digest"]) for doc in inputs])
        source_integrity = all(digest(doc["body"]) == doc["digest"] for doc in inputs)
        ok = (current_report_digest == value["report_digest"]
              and input_digest == value["input_digest"] and source_integrity)
        return {"analysis_id": analysis_id, "verified": ok, "state": value["state"],
                "report_digest": value["report_digest"], "input_digest": value["input_digest"],
                "source_digest_valid": source_integrity}
