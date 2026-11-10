"""MongoDB with a local SQLite fallback for the quiz bot."""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


class Storage:
    def __init__(self) -> None:
        self.mongo_uri = os.getenv("MONGO_URI", "").strip()
        self.sqlite_path = os.getenv("DB_PATH", "data/quizbot.sqlite3")
        self.mongo = None
        self.db = None
        self.conn: aiosqlite.Connection | None = None
        if self.mongo_uri:
            try:
                import motor.motor_asyncio
                self.mongo = motor.motor_asyncio.AsyncIOMotorClient(self.mongo_uri)
                self.db = self.mongo[os.getenv("MONGO_DB", "quiz_bot_db")]
            except Exception as exc:  # pragma: no cover - environment-specific
                raise RuntimeError(f"Could not initialize MongoDB: {exc}") from exc

    async def init(self) -> None:
        if self.db is not None:
            await self.db.saved_quizzes.create_index("id", unique=True)
            await self.db.saved_quizzes.create_index("user_id")
            await self.db.scheduled_quizzes.create_index([("run_at", 1)])
            await self.db.user_settings.create_index("user_id", unique=True)
            return
        path = Path(self.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(str(path))
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS quizzes (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                source_text TEXT NOT NULL,
                questions_json TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'structured',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_quizzes_user ON quizzes(user_id, created_at);
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                data_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                quiz_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                run_at REAL NOT NULL,
                timer INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_schedules_run ON schedules(run_at);
            """
        )
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
        if self.mongo:
            self.mongo.close()

    async def save_quiz(self, user_id: int, title: str, source_text: str, questions: list[dict[str, Any]], source_type: str) -> str:
        for _ in range(8):
            quiz_id = secrets.token_hex(3).upper()
            record = {
                "id": quiz_id, "user_id": user_id, "title": title[:80],
                "source_text": source_text, "questions": questions,
                "source_type": source_type, "created_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                if self.db is not None:
                    await self.db.saved_quizzes.insert_one(record)
                else:
                    assert self.conn is not None
                    await self.conn.execute(
                        "INSERT INTO quizzes(id,user_id,title,source_text,questions_json,source_type) VALUES(?,?,?,?,?,?)",
                        (quiz_id, user_id, title[:80], source_text, json.dumps(questions, ensure_ascii=False), source_type),
                    )
                    await self.conn.commit()
                return quiz_id
            except Exception as exc:
                if "duplicate" not in str(exc).lower() and "unique" not in str(exc).lower():
                    raise
        raise RuntimeError("Could not allocate a unique quiz ID. Please try again.")

    async def get_quiz(self, quiz_id: str, user_id: int | None = None) -> dict[str, Any] | None:
        query: dict[str, Any] = {"id": quiz_id.upper()}
        if user_id is not None:
            query["user_id"] = user_id
        if self.db is not None:
            doc = await self.db.saved_quizzes.find_one(query)
            if not doc:
                return None
            doc.pop("_id", None)
            # Backward compatibility with the original bot's raw data field.
            if "questions" not in doc and isinstance(doc.get("data"), str):
                doc["source_text"] = doc["data"]
                doc["questions"] = []
            return doc
        assert self.conn is not None
        sql = "SELECT * FROM quizzes WHERE id=?" + (" AND user_id=?" if user_id is not None else "")
        params = (quiz_id.upper(), user_id) if user_id is not None else (quiz_id.upper(),)
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"], "user_id": row["user_id"], "title": row["title"],
            "source_text": row["source_text"], "questions": json.loads(row["questions_json"]),
            "source_type": row["source_type"], "created_at": row["created_at"],
        }

    async def list_quizzes(self, user_id: int, limit: int = 30) -> list[dict[str, Any]]:
        if self.db is not None:
            cursor = self.db.saved_quizzes.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
            docs = await cursor.to_list(length=limit)
            for doc in docs:
                doc.pop("_id", None)
            return docs
        assert self.conn is not None
        cursor = await self.conn.execute("SELECT id,title,source_type,created_at FROM quizzes WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def set_title(self, quiz_id: str, title: str, user_id: int) -> bool:
        if self.db is not None:
            result = await self.db.saved_quizzes.update_one({"id": quiz_id.upper(), "user_id": user_id}, {"$set": {"title": title[:80]}})
            return result.matched_count > 0
        assert self.conn is not None
        cursor = await self.conn.execute("UPDATE quizzes SET title=? WHERE id=? AND user_id=?", (title[:80], quiz_id.upper(), user_id))
        await self.conn.commit()
        return cursor.rowcount > 0

    async def delete_quiz(self, quiz_id: str, user_id: int) -> bool:
        if self.db is not None:
            result = await self.db.saved_quizzes.delete_one({"id": quiz_id.upper(), "user_id": user_id})
            return result.deleted_count > 0
        assert self.conn is not None
        cursor = await self.conn.execute("DELETE FROM quizzes WHERE id=? AND user_id=?", (quiz_id.upper(), user_id))
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_settings(self, user_id: int) -> dict[str, Any]:
        defaults = {"anonymous": False, "timer": 0, "shuffle_q": False, "shuffle_o": False, "question_count": 10}
        if self.db is not None:
            doc = await self.db.user_settings.find_one({"user_id": user_id})
            if doc:
                doc.pop("_id", None)
                defaults.update({k: v for k, v in doc.items() if k != "user_id"})
            else:
                await self.db.user_settings.insert_one({"user_id": user_id, **defaults})
            return defaults
        assert self.conn is not None
        cursor = await self.conn.execute("SELECT data_json FROM settings WHERE user_id=?", (user_id,))
        row = await cursor.fetchone()
        if row:
            defaults.update(json.loads(row["data_json"]))
        else:
            await self.conn.execute("INSERT INTO settings(user_id,data_json) VALUES(?,?)", (user_id, json.dumps(defaults)))
            await self.conn.commit()
        return defaults

    async def update_setting(self, user_id: int, key: str, value: Any) -> None:
        settings = await self.get_settings(user_id)
        settings[key] = value
        if self.db is not None:
            await self.db.user_settings.update_one({"user_id": user_id}, {"$set": {key: value}}, upsert=True)
            return
        assert self.conn is not None
        await self.conn.execute("INSERT INTO settings(user_id,data_json) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data_json=excluded.data_json", (user_id, json.dumps(settings)))
        await self.conn.commit()

    async def add_schedule(self, quiz_id: str, user_id: int, chat_id: int, thread_id: int | None, run_at: float, timer: int) -> str:
        schedule_id = secrets.token_hex(4).upper()
        record = {"id": schedule_id, "quiz_id": quiz_id.upper(), "user_id": user_id, "chat_id": chat_id, "thread_id": thread_id, "run_at": run_at, "timer": timer}
        if self.db is not None:
            await self.db.scheduled_quizzes.insert_one(record)
        else:
            assert self.conn is not None
            await self.conn.execute("INSERT INTO schedules(id,quiz_id,user_id,chat_id,thread_id,run_at,timer) VALUES(?,?,?,?,?,?,?)", (schedule_id, quiz_id.upper(), user_id, chat_id, thread_id, run_at, timer))
            await self.conn.commit()
        return schedule_id

    async def due_schedules(self, now: float) -> list[dict[str, Any]]:
        if self.db is not None:
            docs = await self.db.scheduled_quizzes.find({"run_at": {"$lte": now}}).to_list(length=100)
            for doc in docs:
                doc.pop("_id", None)
            return docs
        assert self.conn is not None
        cursor = await self.conn.execute("SELECT * FROM schedules WHERE run_at<=? ORDER BY run_at LIMIT 100", (now,))
        return [dict(row) for row in await cursor.fetchall()]

    async def remove_schedule(self, schedule_id: str, user_id: int | None = None) -> bool:
        if self.db is not None:
            query = {"id": schedule_id}
            if user_id is not None:
                query["user_id"] = user_id
            result = await self.db.scheduled_quizzes.delete_one(query)
            return result.deleted_count > 0
        assert self.conn is not None
        sql = "DELETE FROM schedules WHERE id=?" + (" AND user_id=?" if user_id is not None else "")
        params = (schedule_id, user_id) if user_id is not None else (schedule_id,)
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor.rowcount > 0

    async def list_schedules(self, chat_id: int, user_id: int | None = None) -> list[dict[str, Any]]:
        if self.db is not None:
            query: dict[str, Any] = {"chat_id": chat_id}
            if user_id is not None:
                query["user_id"] = user_id
            docs = await self.db.scheduled_quizzes.find(query).sort("run_at", 1).to_list(length=50)
            for doc in docs:
                doc.pop("_id", None)
            return docs
        assert self.conn is not None
        sql = "SELECT * FROM schedules WHERE chat_id=?" + (" AND user_id=?" if user_id is not None else "") + " ORDER BY run_at LIMIT 50"
        params = (chat_id, user_id) if user_id is not None else (chat_id,)
        cursor = await self.conn.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]
