"""会话组维度的日志存储。

- 每个会话组（一个对话）是一个目录，包含：
    session.json  —— 元数据（id / 创建时间 / 当前 messages）
    events.jsonl  —— 逐条结构化日志
- 支持两个可配置上限：
    max_groups  —— 最多储存的会话组数量，超出后淘汰最早的组
    max_logs    —— 单组会话最多储存的日志条数，超出后淘汰最早的记录
- 内存态是磁盘态的一致性来源：加载时只读每组的最后 max_logs 条。
"""

import json
import shutil
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Session:
    def __init__(self, sid: str, created_at: str, messages: list | None = None, name: str | None = None):
        self.id = sid
        self.created_at = created_at
        self.messages = messages or []
        self.name = name or f"会话 {sid[:6]}"
        self.events: list[dict] = []


class SessionLogStore:
    def __init__(self, root: Path, max_groups: int, max_logs: int):
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.max_groups = max_groups
        self.max_logs = max_logs
        self._lock = threading.RLock()
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    # ---------------- 路径 ----------------

    def _dir(self, sid: str) -> Path:
        return self.sessions_dir / sid

    def _meta_path(self, sid: str) -> Path:
        return self._dir(sid) / "session.json"

    def _events_path(self, sid: str) -> Path:
        return self._dir(sid) / "events.jsonl"

    # ---------------- 加载 / 淘汰 ----------------

    def _load(self) -> None:
        with self._lock:
            loaded: list[Session] = []
            for d in self.sessions_dir.iterdir():
                if not d.is_dir():
                    continue
                meta_path = d / "session.json"
                if not meta_path.exists():
                    continue
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    s = Session(
                        sid=meta["id"],
                        created_at=meta.get("created_at", now_iso()),
                        messages=meta.get("messages", []),
                        name=meta.get("name"),
                    )
                    ev_path = d / "events.jsonl"
                    if ev_path.exists():
                        lines = ev_path.read_text(encoding="utf-8").splitlines()
                        s.events = [
                            json.loads(line)
                            for line in lines[-self.max_logs:]
                            if line.strip()
                        ]
                    loaded.append(s)
                except Exception:
                    continue

            # 按创建时间排序，最早的在前，便于淘汰。
            loaded.sort(key=lambda s: s.created_at)
            for s in loaded:
                self.sessions[s.id] = s
            self._evict_groups_locked()

    def _evict_groups_locked(self) -> None:
        while len(self.sessions) > self.max_groups:
            sid, _ = self.sessions.popitem(last=False)  # 最早的组
            shutil.rmtree(self._dir(sid), ignore_errors=True)

    # ---------------- 会话操作 ----------------

    def create(self, messages: list | None = None, name: str | None = None) -> Session:
        with self._lock:
            sid = uuid.uuid4().hex[:12]
            s = Session(sid, now_iso(), list(messages or []), name)
            self.sessions[sid] = s
            self._write_meta(s)
            self._evict_groups_locked()
            return s

    def get(self, sid: str) -> Session | None:
        with self._lock:
            return self.sessions.get(sid)

    def list(self) -> list[dict]:
        with self._lock:
            items = []
            for s in reversed(self.sessions.values()):
                items.append({
                    "id": s.id,
                    "name": s.name,
                    "created_at": s.created_at,
                    "message_count": len(s.messages),
                    "log_count": len(s.events),
                })
            return items

    def delete(self, sid: str) -> None:
        with self._lock:
            if sid in self.sessions:
                del self.sessions[sid]
            shutil.rmtree(self._dir(sid), ignore_errors=True)

    def set_messages(self, sid: str, messages: list) -> None:
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                return
            s.messages = list(messages)
            self._write_meta(s)

    def set_name(self, sid: str, name: str) -> None:
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                return
            s.name = name
            self._write_meta(s)

    # ---------------- 日志 ----------------

    def log(self, sid: str, event_type: str, data: dict) -> None:
        with self._lock:
            s = self.sessions.get(sid)
            if s is None:
                return
            record = {"ts": now_iso(), "event": event_type}
            record.update(data)
            s.events.append(record)

            if len(s.events) > self.max_logs:
                s.events = s.events[-self.max_logs:]
                self._write_events(s)  # 已超上限，重写以真正落盘淘汰
            else:
                with open(self._events_path(sid), "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def events(self, sid: str) -> list[dict]:
        with self._lock:
            s = self.sessions.get(sid)
            return list(s.events) if s else []

    def update_limits(self, max_groups: int, max_logs: int) -> None:
        """配置上限变化后，对现有会话组即时生效。"""
        with self._lock:
            self.max_groups = max_groups
            self.max_logs = max_logs
            for s in self.sessions.values():
                if len(s.events) > self.max_logs:
                    s.events = s.events[-self.max_logs:]
                    self._write_events(s)
            self._evict_groups_locked()

    # ---------------- 落盘 ----------------

    def _write_meta(self, s: Session) -> None:
        self._dir(s.id).mkdir(parents=True, exist_ok=True)
        self._meta_path(s.id).write_text(
            json.dumps(
                {
                    "id": s.id,
                    "name": s.name,
                    "created_at": s.created_at,
                    "messages": s.messages,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _write_events(self, s: Session) -> None:
        self._dir(s.id).mkdir(parents=True, exist_ok=True)
        self._events_path(s.id).write_text(
            "".join(
                json.dumps(e, ensure_ascii=False) + "\n" for e in s.events
            ),
            encoding="utf-8",
        )
