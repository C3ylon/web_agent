"""FastAPI 应用：配置、会话组、SSE 流式对话、日志查看。

启动：
    python app.py
然后浏览器打开 http://127.0.0.1:8000
"""

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core
from logstore import SessionLogStore

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="DeepSeek Web Chat")

# 启动时加载配置与既有会话组。
core.load_config()
store = SessionLogStore(
    root=core.DATA_DIR,
    max_groups=core.get_config().max_session_groups,
    max_logs=core.get_config().max_logs_per_group,
)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ============================================================
# 配置
# ============================================================

class ConfigUpdate(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    reasoning_effort: str | None = None
    max_tool_rounds: int | None = None
    max_search_results: int | None = None
    max_content_chars: int | None = None
    system_messages: list[dict] | None = None
    enable_thinking: bool | None = None
    highlight_quotes: bool | None = None
    max_session_groups: int | None = None
    max_logs_per_group: int | None = None


@app.get("/api/config")
def get_config():
    return core.get_config().to_dict()


@app.put("/api/config")
def put_config(body: ConfigUpdate):
    updates = body.model_dump(exclude_none=True)
    cfg = core.update_config(updates)
    # 上限变化即时作用于已存在的会话组。
    store.update_limits(cfg.max_session_groups, cfg.max_logs_per_group)
    return cfg.to_dict()


# ============================================================
# 会话组
# ============================================================

class ChatRequest(BaseModel):
    messages: list[dict]


class SessionCreate(BaseModel):
    name: str | None = None


class SessionRename(BaseModel):
    name: str


@app.post("/api/sessions")
def create_session(body: SessionCreate | None = None):
    name = body.name if body else None
    s = store.create(core.build_system_messages(), name=name)
    return {
        "id": s.id,
        "name": s.name,
        "created_at": s.created_at,
        "messages": s.messages,
    }


@app.get("/api/sessions")
def list_sessions():
    return store.list_sessions()


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    s = store.get(sid)
    if s is None:
        raise HTTPException(404, "会话不存在")
    return {
        "id": s.id,
        "name": s.name,
        "created_at": s.created_at,
        "messages": s.messages,
        "log_count": len(s.events),
    }


@app.patch("/api/sessions/{sid}")
def rename_session(sid: str, body: SessionRename):
    if store.get(sid) is None:
        raise HTTPException(404, "会话不存在")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    store.set_name(sid, name)
    return {"ok": True, "name": name}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str):
    store.delete(sid)
    return {"ok": True}


@app.get("/api/sessions/{sid}/logs")
def get_logs(sid: str):
    s = store.get(sid)
    if s is None:
        raise HTTPException(404, "会话不存在")
    return store.events(sid)


@app.get("/api/sessions/{sid}/export")
def export_session(sid: str):
    s = store.get(sid)
    if s is None:
        raise HTTPException(404, "会话不存在")
    payload = {
        "id": s.id,
        "name": s.name,
        "created_at": s.created_at,
        "events": store.events(sid),
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    ascii_name = f"session-{s.id}.json"
    utf8_name = quote(f"{s.name}.json")
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{utf8_name}"
            ),
        },
    )


# ============================================================
# 流式对话（SSE）
# ============================================================

@app.post("/api/sessions/{sid}/stream")
def stream_chat(sid: str, body: ChatRequest):
    if store.get(sid) is None:
        raise HTTPException(404, "会话不存在")

    if not body.messages:
        raise HTTPException(400, "messages 不能为空")

    # 工作副本：用户提交的（可能编辑过的）messages，会被工具循环原地追加。
    messages = [dict(m) for m in body.messages]

    def log(event_type: str, data: dict) -> None:
        store.log(sid, event_type, data)

    def gen():
        try:
            for etype, data in core.chat_turn_stream(messages, log=log):
                yield _sse(etype, data)
                if etype == "done":
                    store.set_messages(sid, data["messages"])
        except Exception as exc:
            store.log(sid, "error", {
                "type": type(exc).__name__,
                "message": str(exc),
            })
            yield _sse("error", {
                "type": type(exc).__name__,
                "message": str(exc),
            })

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================
# 静态页面
# ============================================================

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import argparse
    import os

    import uvicorn

    parser = argparse.ArgumentParser(description="DeepSeek Web Chat")
    parser.add_argument(
        "--host",
        default=os.environ.get("DSH_WEB_HOST", "127.0.0.1"),
        help="listen host (default 127.0.0.1, or env DSH_WEB_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DSH_WEB_PORT", "8000")),
        help="listen port (default 8000, or env DSH_WEB_PORT)",
    )
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
