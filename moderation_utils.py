from __future__ import annotations

from typing import Iterable

import MySQLdb
import MySQLdb.cursors


def ensure_chapter_moderation_table(conn) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chapter_moderation (
                    chapters_id INT NOT NULL PRIMARY KEY,
                    novels_id INT NOT NULL,
                    closed_by INT NULL,
                    closed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reason VARCHAR(255) NULL,
                    KEY idx_chapter_moderation_novel (novels_id)
                )
                """
            )
        return True
    except Exception:
        return False


def fetch_closed_chapter_ids(conn, novels_id: int) -> set[int]:
    if not ensure_chapter_moderation_table(conn):
        return set()
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT chapters_id FROM chapter_moderation WHERE novels_id = %s AND reason = %s",
                (novels_id, "closed_by_admin"),
            )
            rows = cur.fetchall() or []
        return {int(r.get("chapters_id")) for r in rows if r.get("chapters_id")}
    except Exception:
        return set()


def fetch_closed_chapter_counts(conn, novel_ids: Iterable[int]) -> dict[int, int]:
    ids = [int(x) for x in (novel_ids or []) if x is not None]
    if not ids:
        return {}
    if not ensure_chapter_moderation_table(conn):
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(
                f"""
                SELECT novels_id, COUNT(*) AS cnt
                  FROM chapter_moderation
                 WHERE novels_id IN ({placeholders})
                   AND reason = %s
                 GROUP BY novels_id
                """,
                [*ids, "closed_by_admin"],
            )
            rows = cur.fetchall() or []
        return {int(r.get("novels_id")): int(r.get("cnt") or 0) for r in rows if r.get("novels_id")}
    except Exception:
        return {}


def fetch_pending_chapter_counts(conn, novel_ids: Iterable[int]) -> dict[int, int]:
    ids = [int(x) for x in (novel_ids or []) if x is not None]
    if not ids:
        return {}
    if not ensure_chapter_moderation_table(conn):
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(
                f"""
                SELECT novels_id, COUNT(*) AS cnt
                  FROM chapter_moderation
                 WHERE novels_id IN ({placeholders})
                   AND reason = %s
                 GROUP BY novels_id
                """,
                [*ids, "pending_review"],
            )
            rows = cur.fetchall() or []
        return {int(r.get("novels_id")): int(r.get("cnt") or 0) for r in rows if r.get("novels_id")}
    except Exception:
        return {}


def fetch_chapter_moderation_map(conn, novels_id: int) -> dict[int, str]:
    if not ensure_chapter_moderation_table(conn):
        return {}
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT chapters_id, reason FROM chapter_moderation WHERE novels_id = %s",
                (novels_id,),
            )
            rows = cur.fetchall() or []
        result = {}
        for row in rows:
            chap_id = row.get("chapters_id")
            reason = (row.get("reason") or "").strip()
            if chap_id:
                result[int(chap_id)] = reason
        return result
    except Exception:
        return {}


def get_chapter_moderation_reason(conn, chapters_id: int) -> str | None:
    if not ensure_chapter_moderation_table(conn):
        return None
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT reason FROM chapter_moderation WHERE chapters_id = %s",
                (chapters_id,),
            )
            row = cur.fetchone() or {}
        reason = (row.get("reason") or "").strip()
        return reason or None
    except Exception:
        return None


def mark_chapter_closed(conn, chapters_id: int, novels_id: int, closed_by: int | None = None, reason: str | None = None) -> bool:
    if not ensure_chapter_moderation_table(conn):
        return False
    final_reason = reason or "closed_by_admin"
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chapter_moderation (chapters_id, novels_id, closed_by, closed_at, reason)
                VALUES (%s, %s, %s, NOW(), %s)
                ON DUPLICATE KEY UPDATE
                    novels_id = VALUES(novels_id),
                    closed_by = VALUES(closed_by),
                    closed_at = NOW(),
                    reason = VALUES(reason)
                """,
                (chapters_id, novels_id, closed_by, final_reason),
            )
        return True
    except Exception:
        return False


def mark_chapter_pending_review(conn, chapters_id: int, novels_id: int | None = None) -> bool:
    if not ensure_chapter_moderation_table(conn):
        return False
    try:
        with conn.cursor(MySQLdb.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT novels_id, reason FROM chapter_moderation WHERE chapters_id = %s",
                (chapters_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            current_reason = (row.get("reason") or "").strip()
            if current_reason not in ("closed_by_admin", "pending_review"):
                return False
            target_novel = novels_id or row.get("novels_id")
            cur.execute(
                """
                UPDATE chapter_moderation
                   SET novels_id = %s,
                       closed_by = NULL,
                       closed_at = NOW(),
                       reason = %s
                 WHERE chapters_id = %s
                """,
                (target_novel, "pending_review", chapters_id),
            )
        return True
    except Exception:
        return False


def clear_chapter_closed(conn, chapters_id: int) -> bool:
    if not ensure_chapter_moderation_table(conn):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chapter_moderation WHERE chapters_id = %s",
                (chapters_id,),
            )
        return True
    except Exception:
        return False


def clear_novel_closed(conn, novels_id: int) -> bool:
    if not ensure_chapter_moderation_table(conn):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chapter_moderation WHERE novels_id = %s",
                (novels_id,),
            )
        return True
    except Exception:
        return False
