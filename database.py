"""SQLite persistence for the PMP bot.

Tables:
  attempts       — every answered question (for accuracy stats)
  marked         — questions the user marked for later review
  active_question — the question currently shown to a user (for Ask-Claude follow-up)
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "pmp_bot.db"


def init_db() -> None:
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS attempts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                question_id   TEXT    NOT NULL,
                test_number   INTEGER NOT NULL,
                is_correct    INTEGER NOT NULL,
                exam_mode     INTEGER NOT NULL DEFAULT 0,
                answered_at   INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id);

            CREATE TABLE IF NOT EXISTS marked (
                user_id       INTEGER NOT NULL,
                question_id   TEXT    NOT NULL,
                marked_at     INTEGER NOT NULL,
                PRIMARY KEY (user_id, question_id)
            );

            CREATE TABLE IF NOT EXISTS active_question (
                user_id       INTEGER PRIMARY KEY,
                question_id   TEXT    NOT NULL,
                last_correct  INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL
            );
        """)


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ---- attempts -----------------------------------------------------------

def record_attempt(user_id: int, question_id: str, test_number: int,
                   is_correct: bool, exam_mode: bool = False) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO attempts (user_id, question_id, test_number, is_correct, exam_mode, answered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, question_id, test_number, int(is_correct), int(exam_mode), int(time.time())),
        )


def get_overall_stats(user_id: int) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS total, SUM(is_correct) AS correct "
            "FROM attempts WHERE user_id = ?", (user_id,),
        ).fetchone()
        total = row["total"] or 0
        correct = row["correct"] or 0
        return {
            "total": total,
            "correct": correct,
            "accuracy": (correct / total * 100) if total else 0.0,
        }


def get_per_test_stats(user_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT test_number, COUNT(*) AS total, SUM(is_correct) AS correct "
            "FROM attempts WHERE user_id = ? GROUP BY test_number ORDER BY test_number",
            (user_id,),
        ).fetchall()
        return [
            {
                "test_number": r["test_number"],
                "total": r["total"],
                "correct": r["correct"] or 0,
                "accuracy": ((r["correct"] or 0) / r["total"] * 100) if r["total"] else 0.0,
            }
            for r in rows
        ]


# ---- marked questions ---------------------------------------------------

def mark_question(user_id: int, question_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO marked (user_id, question_id, marked_at) VALUES (?, ?, ?)",
            (user_id, question_id, int(time.time())),
        )


def unmark_question(user_id: int, question_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM marked WHERE user_id = ? AND question_id = ?",
                  (user_id, question_id))


def is_marked(user_id: int, question_id: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM marked WHERE user_id = ? AND question_id = ?",
            (user_id, question_id),
        ).fetchone()
        return row is not None


def list_marked(user_id: int) -> list[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT question_id FROM marked WHERE user_id = ? ORDER BY marked_at DESC",
            (user_id,),
        ).fetchall()
        return [r["question_id"] for r in rows]


# ---- active question (for Ask Claude) -----------------------------------

def set_active_question(user_id: int, question_id: str, last_correct: bool) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO active_question (user_id, question_id, last_correct, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  question_id = excluded.question_id, "
            "  last_correct = excluded.last_correct, "
            "  updated_at = excluded.updated_at",
            (user_id, question_id, int(last_correct), int(time.time())),
        )


def get_active_question(user_id: int) -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT question_id FROM active_question WHERE user_id = ?", (user_id,),
        ).fetchone()
        return row["question_id"] if row else None
