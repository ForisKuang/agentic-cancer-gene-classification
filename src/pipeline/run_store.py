"""MySQL-backed persistence for run sharing and reusable gene annotations.

Lets a peer fetch a previously-computed run (GET /v1/annotate/{run_id})
without recomputing it, so a curator can share results by link. Also stores the
latest per-gene annotation for freshness-aware reuse in future runs. Uses a
connection pool created once at app startup and reused across requests.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiomysql

from src.api_keys import ApiKeyRecord
from src.config import settings
from src.models.schema import GeneAnnotation, PMIDEvidenceRecord

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR(36) PRIMARY KEY,
    created_at DATETIME NOT NULL,
    request_json JSON NOT NULL,
    result_json JSON NOT NULL
)
"""

_CREATE_GENE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gene_annotations (
    gene VARCHAR(64) NOT NULL,
    tumor_type VARCHAR(128) NOT NULL DEFAULT '',
    updated_at DATETIME NOT NULL,
    last_pubmed_checked_at DATETIME NULL,
    in_oncokb BOOLEAN NULL,
    evidence_support_score DOUBLE NOT NULL DEFAULT 0,
    insufficient_evidence BOOLEAN NOT NULL DEFAULT FALSE,
    annotation_json JSON NOT NULL,
    PRIMARY KEY (gene, tumor_type),
    INDEX idx_gene_annotations_gene (gene),
    INDEX idx_gene_annotations_updated_at (updated_at),
    INDEX idx_gene_annotations_last_pubmed_checked_at (last_pubmed_checked_at)
)
"""

_CREATE_PMID_EVIDENCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pmid_evidence (
    pmid VARCHAR(16) PRIMARY KEY,
    doi VARCHAR(128) NULL,
    title VARCHAR(512) NOT NULL,
    journal VARCHAR(128) NOT NULL,
    publication_year INT NULL,
    evidence_type ENUM('clinical', 'preclinical', 'case_report', 'review', 'other') DEFAULT 'other',
    oncogenic_role ENUM('oncogene', 'tumor_suppressor', 'resistance', 'neutral', 'unknown') DEFAULT 'unknown',
    distilled_takeaway TEXT NOT NULL,
    supporting_quote TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_journal (journal),
    INDEX idx_evidence_type (evidence_type)
)
"""

_CREATE_FEEDBACK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id VARCHAR(36) PRIMARY KEY,
    created_at DATETIME NOT NULL,
    category VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    contact_email VARCHAR(255) NULL,
    run_id VARCHAR(36) NULL,
    gene VARCHAR(64) NULL,
    page_url VARCHAR(512) NULL,
    user_agent VARCHAR(512) NULL,
    INDEX idx_feedback_created_at (created_at),
    INDEX idx_feedback_run_id (run_id)
)
"""


# Only the SHA-256 of the key is stored; key_prefix ("acgc_" + 8 chars) is
# kept in the clear for display and indexed lookup. See src/api_keys.py.
_CREATE_API_KEYS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id VARCHAR(36) PRIMARY KEY,
    key_prefix VARCHAR(16) NOT NULL,
    key_hash CHAR(64) NOT NULL,
    owner_email VARCHAR(255) NOT NULL,
    name VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL,
    last_used_at DATETIME NULL,
    revoked_at DATETIME NULL,
    expires_at DATETIME NULL,
    UNIQUE INDEX idx_api_keys_key_hash (key_hash),
    INDEX idx_api_keys_key_prefix (key_prefix),
    INDEX idx_api_keys_owner_email (owner_email)
)
"""

_API_KEY_COLUMNS = (
    "id, key_prefix, key_hash, owner_email, name, created_at, last_used_at, revoked_at, expires_at"
)


def _to_utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _from_mysql_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def _parse_jdbc_mysql_host_port(url: str) -> tuple[str, int]:
    """Parse host and port from a jdbc:mysql:// URL, e.g.
    "jdbc:mysql://host:3306" or "jdbc:mysql://host:3306/dbname?useSSL=false".
    """
    prefix = "jdbc:mysql://"
    if not url.startswith(prefix):
        raise ValueError(f"Expected a jdbc:mysql:// URL, got {url!r}")
    host_port = url[len(prefix) :].split("/", 1)[0].split("?", 1)[0]
    host, sep, port = host_port.rpartition(":")
    if not sep:
        raise ValueError(f"jdbc:mysql URL missing port: {url!r}")
    return host, int(port)


def _normalize_cache_tumor_type(tumor_type: Optional[str]) -> str:
    """Normalize tumor type for the reusable gene annotation cache key."""
    return " ".join((tumor_type or "").strip().lower().split())


def _mysql_connection_kwargs() -> Dict[str, Any]:
    if settings.db_url:
        host, port = _parse_jdbc_mysql_host_port(settings.db_url)
    else:
        host, port = settings.mysql_host, settings.mysql_port
    return {
        "host": host,
        "port": port,
        "user": settings.db_username or settings.mysql_user,
        "password": settings.db_password or settings.mysql_password,
        "db": settings.mysql_database,
    }


class RunStore:
    """Save/fetch runs and reusable gene annotations. Calling code never issues raw SQL."""

    def __init__(self, pool: aiomysql.Pool):
        self._pool = pool

    @classmethod
    async def create(cls) -> "RunStore":
        pool = await aiomysql.create_pool(
            **_mysql_connection_kwargs(),
            autocommit=True,
        )
        store = cls(pool)
        await store._ensure_schema()
        return store

    async def _ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(_CREATE_TABLE_SQL)
                await cursor.execute(_CREATE_GENE_TABLE_SQL)
                await self._ensure_gene_annotation_schema(cursor)
                # Only provisioned when PMID distillation is on, so the
                # default-off path makes no schema change beyond main's.
                if settings.pmid_distillation_enabled:
                    await cursor.execute(_CREATE_PMID_EVIDENCE_TABLE_SQL)
                await cursor.execute(_CREATE_FEEDBACK_TABLE_SQL)
                await cursor.execute(_CREATE_API_KEYS_TABLE_SQL)

    async def _ensure_gene_annotation_schema(self, cursor) -> None:
        """Migrate older gene-only annotation caches to gene + tumor-type keys."""
        await cursor.execute("SHOW COLUMNS FROM gene_annotations LIKE 'tumor_type'")
        if await cursor.fetchone() is None:
            await cursor.execute(
                "ALTER TABLE gene_annotations "
                "ADD COLUMN tumor_type VARCHAR(128) NOT NULL DEFAULT '' AFTER gene"
            )

        await cursor.execute("SHOW INDEX FROM gene_annotations WHERE Key_name = 'PRIMARY'")
        rows = await cursor.fetchall()
        primary_columns = [row[4] for row in sorted(rows, key=lambda row: row[3])]
        if primary_columns == ["gene"]:
            await cursor.execute(
                "ALTER TABLE gene_annotations "
                "DROP PRIMARY KEY, ADD PRIMARY KEY (gene, tumor_type)"
            )

    async def save_run(
        self,
        run_id: str,
        created_at: str,
        request_payload: Dict[str, Any],
        result_payload: Dict[str, Any],
    ) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "INSERT INTO runs (run_id, created_at, request_json, result_json) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        run_id,
                        _to_utc_datetime(created_at),
                        json.dumps(request_payload),
                        json.dumps(result_payload),
                    ),
                )

    async def update_run_result(self, run_id: str, result_payload: Dict[str, Any]) -> None:
        """Overwrite a saved run's result (e.g. once a background job, like fusion
        evidence retrieval, completes after the initial save)."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE runs SET result_json = %s WHERE run_id = %s",
                    (json.dumps(result_payload), run_id),
                )

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT result_json FROM runs WHERE run_id = %s", (run_id,)
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    async def save_gene_annotation(
        self,
        annotation: GeneAnnotation,
        updated_at: str | datetime,
        tumor_type: Optional[str] = None,
    ) -> None:
        """Upsert the latest reusable annotation for a canonical gene and tumor context."""
        payload = annotation.model_dump()
        cache_tumor_type = _normalize_cache_tumor_type(tumor_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO gene_annotations (
                        gene,
                        tumor_type,
                        updated_at,
                        last_pubmed_checked_at,
                        in_oncokb,
                        evidence_support_score,
                        insufficient_evidence,
                        annotation_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        updated_at = VALUES(updated_at),
                        last_pubmed_checked_at = VALUES(last_pubmed_checked_at),
                        in_oncokb = VALUES(in_oncokb),
                        evidence_support_score = VALUES(evidence_support_score),
                        insufficient_evidence = VALUES(insufficient_evidence),
                        annotation_json = VALUES(annotation_json)
                    """,
                    (
                        annotation.gene,
                        cache_tumor_type,
                        _to_utc_datetime(updated_at),
                        _to_utc_datetime(annotation.last_pubmed_checked_at)
                        if annotation.last_pubmed_checked_at
                        else None,
                        annotation.in_oncokb,
                        annotation.evidence_support_score,
                        annotation.insufficient_evidence,
                        json.dumps(payload),
                    ),
                )

    async def get_gene_annotation(
        self,
        gene: str,
        tumor_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return cached annotation payload and freshness metadata for a gene/tumor context."""
        cache_tumor_type = _normalize_cache_tumor_type(tumor_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT annotation_json, updated_at, last_pubmed_checked_at, tumor_type
                    FROM gene_annotations
                    WHERE gene = %s AND tumor_type = %s
                    """,
                    (gene, cache_tumor_type),
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "annotation": json.loads(row[0]),
            "updated_at": _from_mysql_datetime(row[1]),
            "last_pubmed_checked_at": _from_mysql_datetime(row[2]),
            "tumor_type": row[3] or None,
        }

    async def mark_gene_pubmed_checked(
        self,
        gene: str,
        checked_at: str | datetime,
        annotation: GeneAnnotation | None = None,
        tumor_type: Optional[str] = None,
    ) -> None:
        """Record that PubMed freshness was checked without needing regeneration."""
        checked = _to_utc_datetime(checked_at)
        cache_tumor_type = _normalize_cache_tumor_type(tumor_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                if annotation is None:
                    await cursor.execute(
                        "UPDATE gene_annotations SET last_pubmed_checked_at = %s "
                        "WHERE gene = %s AND tumor_type = %s",
                        (checked, gene, cache_tumor_type),
                    )
                else:
                    payload = annotation.model_dump()
                    await cursor.execute(
                        """
                        UPDATE gene_annotations
                        SET last_pubmed_checked_at = %s, annotation_json = %s
                        WHERE gene = %s AND tumor_type = %s
                        """,
                        (checked, json.dumps(payload), gene, cache_tumor_type),
                    )

    async def get_pmid_evidence_batch(self, pmids: List[str]) -> Dict[str, PMIDEvidenceRecord]:
        """Return cached distilled-abstract records for any of `pmids` that
        have already been distilled, keyed by PMID. PMIDs with no cached
        record are simply absent from the result — callers fall back to the
        raw abstract for those (see synthesis.py's _build_user_prompt)."""
        if not pmids:
            return {}
        placeholders = ", ".join(["%s"] * len(pmids))
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"""
                    SELECT pmid, doi, title, journal, publication_year, evidence_type,
                           oncogenic_role, distilled_takeaway, supporting_quote,
                           created_at, updated_at
                    FROM pmid_evidence
                    WHERE pmid IN ({placeholders})
                    """,
                    tuple(pmids),
                )
                rows = await cursor.fetchall()
        records: Dict[str, PMIDEvidenceRecord] = {}
        for row in rows:
            record = PMIDEvidenceRecord(
                pmid=row[0],
                doi=row[1],
                title=row[2],
                journal=row[3],
                publication_year=row[4],
                evidence_type=row[5],
                oncogenic_role=row[6],
                distilled_takeaway=row[7],
                supporting_quote=row[8],
                created_at=_from_mysql_datetime(row[9]),
                updated_at=_from_mysql_datetime(row[10]),
            )
            records[record.pmid] = record
        return records

    async def save_pmid_evidence_batch(self, records: List[PMIDEvidenceRecord]) -> None:
        """Upsert distilled-abstract records. Published papers are permanent
        historical records, so — unlike the gene-annotation cache above —
        there is no TTL/age-based eviction here; a record is only ever
        overwritten by a fresher distillation of the same PMID."""
        if not records:
            return
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    """
                    INSERT INTO pmid_evidence (
                        pmid, doi, title, journal, publication_year, evidence_type,
                        oncogenic_role, distilled_takeaway, supporting_quote
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        doi = VALUES(doi),
                        title = VALUES(title),
                        journal = VALUES(journal),
                        publication_year = VALUES(publication_year),
                        evidence_type = VALUES(evidence_type),
                        oncogenic_role = VALUES(oncogenic_role),
                        distilled_takeaway = VALUES(distilled_takeaway),
                        supporting_quote = VALUES(supporting_quote)
                    """,
                    [
                        (
                            record.pmid,
                            record.doi,
                            record.title,
                            record.journal,
                            record.publication_year,
                            record.evidence_type,
                            record.oncogenic_role,
                            record.distilled_takeaway,
                            record.supporting_quote,
                        )
                        for record in records
                    ],
                )

    async def save_feedback(
        self,
        feedback_id: str,
        created_at: str | datetime,
        category: str,
        message: str,
        contact_email: Optional[str],
        run_id: Optional[str],
        gene: Optional[str],
        page_url: Optional[str],
        user_agent: Optional[str],
    ) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO feedback (
                        feedback_id, created_at, category, message,
                        contact_email, run_id, gene, page_url, user_agent
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        feedback_id,
                        _to_utc_datetime(created_at),
                        category,
                        message,
                        contact_email,
                        run_id,
                        gene,
                        page_url,
                        user_agent,
                    ),
                )

    # -- API keys ------------------------------------------------------------

    @staticmethod
    def _api_key_from_row(row) -> ApiKeyRecord:
        return ApiKeyRecord(
            id=row[0],
            key_prefix=row[1],
            key_hash=row[2],
            owner_email=row[3],
            name=row[4],
            created_at=_from_mysql_datetime(row[5]),
            last_used_at=_from_mysql_datetime(row[6]),
            revoked_at=_from_mysql_datetime(row[7]),
            expires_at=_from_mysql_datetime(row[8]),
        )

    async def create_api_key(self, record: ApiKeyRecord) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"INSERT INTO api_keys ({_API_KEY_COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        record.id,
                        record.key_prefix,
                        record.key_hash,
                        record.owner_email,
                        record.name,
                        _to_utc_datetime(record.created_at),
                        _to_utc_datetime(record.last_used_at) if record.last_used_at else None,
                        _to_utc_datetime(record.revoked_at) if record.revoked_at else None,
                        _to_utc_datetime(record.expires_at) if record.expires_at else None,
                    ),
                )

    async def get_api_keys_by_prefix(self, key_prefix: str) -> List[ApiKeyRecord]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_API_KEY_COLUMNS} FROM api_keys WHERE key_prefix = %s",
                    (key_prefix,),
                )
                rows = await cursor.fetchall()
        return [self._api_key_from_row(row) for row in rows]

    async def get_api_key(self, key_id: str) -> Optional[ApiKeyRecord]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_API_KEY_COLUMNS} FROM api_keys WHERE id = %s", (key_id,)
                )
                row = await cursor.fetchone()
        return self._api_key_from_row(row) if row else None

    async def list_api_keys(self, owner_email: Optional[str] = None) -> List[ApiKeyRecord]:
        """List keys for one owner, or every key when owner_email is None."""
        sql = f"SELECT {_API_KEY_COLUMNS} FROM api_keys"
        params: tuple = ()
        if owner_email is not None:
            sql += " WHERE owner_email = %s"
            params = (owner_email,)
        sql += " ORDER BY created_at DESC"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)
                rows = await cursor.fetchall()
        return [self._api_key_from_row(row) for row in rows]

    async def revoke_api_key(self, key_id: str, revoked_at: datetime) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE api_keys SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL",
                    (_to_utc_datetime(revoked_at), key_id),
                )

    async def touch_api_key(self, key_id: str, used_at: datetime) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE api_keys SET last_used_at = %s WHERE id = %s",
                    (_to_utc_datetime(used_at), key_id),
                )

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()
