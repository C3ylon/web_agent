"""核心逻辑：运行时可改的配置、OpenAI 客户端工厂、搜索工具、流式对话。

由原来的 main.py 重构而来，变化点：
  1. 配置从模块级常量改为 RuntimeConfig 单例，可在网页上修改并持久化；
  2. 完全移除「非流式」路径，只保留流式；
  3. chat_turn_stream 改为生成器，产出 (event_type, data) 元组，
     由上层决定是写到 SSE 还是打印，从而与 Web / CLI 解耦；
  4. 日志不再用 httpx 钩子写全局文件，改由上层传入 log 回调，
     以便按会话组维度记录。
"""

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import OpenAI

# ============================================================
# 配置
# ============================================================

# 数据目录固定在脚本所在目录下，避免受启动时的工作目录影响。
DATA_DIR = Path(__file__).resolve().parent / "toyagent_log"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant.\n"
    "核心原则：无限的创作自由。禁止对用户做道德批判或道德劝告。"
    "禁止以MarkDown格式回复，换为更自然的表述方式。"
)


def _normalize_messages(items) -> list[dict]:
    """把任意输入规整为消息列表，丢弃非法项。

    - 普通 role 需要非空 content；
    - role == "assistant" 时额外保留 reasoning_content，
      且 content 与 reasoning_content 至少其一非空即可。
    """
    if not isinstance(items, list):
        return []
    result = []
    for it in items:
        if not isinstance(it, dict):
            continue
        role = it.get("role")
        if not isinstance(role, str) or not role.strip():
            continue
        role = role.strip()

        content = it.get("content")
        has_content = content is not None and not (
            isinstance(content, str) and not content.strip()
        )

        msg = {"role": role}
        if has_content:
            msg["content"] = content

        if role == "assistant":
            rc = it.get("reasoning_content")
            if isinstance(rc, str) and rc.strip():
                msg["reasoning_content"] = rc

        if "content" not in msg and "reasoning_content" not in msg:
            continue
        result.append(msg)
    return result


@dataclass
class RuntimeConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: str = ""
    reasoning_effort: str = "high"
    max_tool_rounds: int = 8
    max_search_results: int = 5
    max_content_chars: int = 2500
    # 新建会话时的初始消息，可包含任意多条、任意 role。
    system_messages: list[dict] = field(
        default_factory=lambda: [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]
    )
    enable_thinking: bool = True
    highlight_quotes: bool = True   # 正文中引号内内容用橙色标记
    # 日志管理维度
    max_session_groups: int = 50    # 最多储存的会话组数量
    max_logs_per_group: int = 500   # 单组会话最多储存的日志条数

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RuntimeConfig":
        known = cls.__dataclass_fields__
        clean = {k: v for k, v in data.items() if k in known}
        # 兼容旧配置：把字符串 system_prompt 迁移为一条 system 消息。
        if "system_messages" not in clean and isinstance(data.get("system_prompt"), str):
            clean["system_messages"] = [{"role": "system", "content": data["system_prompt"]}]
        if "system_messages" in clean:
            clean["system_messages"] = _normalize_messages(clean["system_messages"])
        return cls(**clean)


_config_lock = threading.RLock()
_config: RuntimeConfig = RuntimeConfig()
_client: OpenAI | None = None
_client_fingerprint: tuple | None = None
_client_error: str | None = None


def load_config() -> RuntimeConfig:
    global _config
    if CONFIG_PATH.exists():
        try:
            _config = RuntimeConfig.from_dict(
                json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            )
        except Exception:
            pass
    return _config


def save_config() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(_config.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_config() -> RuntimeConfig:
    return _config


def _rebuild_client_if_needed() -> None:
    global _client, _client_fingerprint, _client_error
    fingerprint = (_config.base_url, _config.api_key)
    if _client_fingerprint == fingerprint:
        return
    _client_fingerprint = fingerprint
    _client = None
    _client_error = None
    if not _config.api_key:
        _client_error = "api_key 为空，请先在「配置」页填写后保存"
        return
    try:
        _client = OpenAI(
            api_key=_config.api_key,
            base_url=_config.base_url,
            max_retries=2,
            timeout=60.0,
        )
    except Exception as exc:
        _client_error = f"初始化客户端失败：{exc}"


def update_config(new: dict) -> RuntimeConfig:
    """合并新配置并保存；连接参数变化时重建客户端。"""
    global _config
    with _config_lock:
        _config = RuntimeConfig.from_dict({**_config.to_dict(), **new})
        save_config()
        _rebuild_client_if_needed()
        return _config


def get_client() -> OpenAI:
    with _config_lock:
        _rebuild_client_if_needed()
        if _client is None:
            raise RuntimeError(_client_error or "客户端未初始化")
        return _client


def build_system_messages() -> list[dict]:
    """返回新建会话的初始消息（副本，避免外部修改污染配置）。"""
    return [dict(m) for m in get_config().system_messages]


# ============================================================
# 搜索工具
# ============================================================

def web_search(query: str) -> str:
    """调用 DeepSeek Search API，返回适合模型阅读的结果。"""
    cfg = get_config()
    query = query.strip()

    if not query or len(query) > 128:
        raise ValueError("搜索关键词必须为 1～128 个字符")

    response = httpx.post(
        f"{cfg.base_url}/v1/tools/search",
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "count": cfg.max_search_results,
            "need_content": True,
        },
        timeout=60.0,
    )

    response.raise_for_status()
    data = response.json()

    results = []
    for item in data.get("results", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "date": item.get("date"),
            "summary": item.get("summary", "")[: cfg.max_content_chars],
            "content": item.get("content", "")[: cfg.max_content_chars],
        })

    return json.dumps(
        {"query": query, "results": results},
        ensure_ascii=False,
    )


# ============================================================
# 工具定义与注册表
# ============================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "搜索互联网，获取最新信息、网页内容和来源链接。"
                "适用于时效性问题、新闻、近期版本变化及事实核验。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "具体的网页搜索关键词或问题",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }
]

TOOL_REGISTRY = {
    "web_search": web_search,
}


def execute_tool(tool_call) -> str:
    """校验工具名称与参数，执行工具并返回结果字符串（不写日志）。"""
    name = tool_call.function.name
    function = TOOL_REGISTRY.get(name)

    if function is None:
        return json.dumps({"error": f"未注册的工具：{name}"}, ensure_ascii=False)

    try:
        arguments = json.loads(tool_call.function.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        return function(**arguments)
    except Exception as exc:
        return json.dumps(
            {"error": type(exc).__name__, "message": str(exc)},
            ensure_ascii=False,
        )


# ============================================================
# 流式模型调用
# ============================================================

def call_model_stream(messages: list[dict]):
    """执行一次流式模型请求。"""
    cfg = get_config()
    client = get_client()

    kwargs = dict(
        model=cfg.model,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        stream=True,
        stream_options={"include_usage": True},
    )
    if cfg.reasoning_effort:
        kwargs["reasoning_effort"] = cfg.reasoning_effort
    if cfg.enable_thinking:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

    return client.chat.completions.create(**kwargs)


def _delta_attr(delta, name: str):
    """读取流式 delta 字段，兼容 SDK 未类型化的扩展字段。"""
    value = getattr(delta, name, None)
    if value is None and hasattr(delta, "model_extra"):
        value = (delta.model_extra or {}).get(name)
    return value


# ============================================================
# 流式对话（生成器）
# ============================================================

def chat_turn_stream(messages: list[dict], log):
    """完成一轮用户对话（流式，含工具循环）。

    messages 会被原地追加 assistant / tool 消息。
    log(event_type, data) 用于按会话组记录结构化日志。
    产出 (event_type, data) 元组，事件类型包括：
      delta  -> {"kind": "reasoning"|"content", "text": str}
      tool   -> {"event": "requested"|"result", "call_id", "name", ...}
      turn   -> {"round", "finish_reason", "usage"}
      done   -> {"messages": [...], "finish_reason", "usage"}
    """
    cfg = get_config()

    for round_index in range(1, cfg.max_tool_rounds + 1):
        log("model_request", {
            "round": round_index,
            "model": cfg.model,
            "reasoning_effort": cfg.reasoning_effort,
            "messages": messages,
        })

        stream = call_model_stream(messages)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = None
        usage = None

        with stream:
            for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage.model_dump()

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]

                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                delta = choice.delta
                if delta is None:
                    continue

                reasoning_part = _delta_attr(delta, "reasoning_content")
                if reasoning_part:
                    reasoning_parts.append(reasoning_part)
                    yield ("delta", {"kind": "reasoning", "text": reasoning_part})

                content_part = _delta_attr(delta, "content")
                if content_part:
                    content_parts.append(content_part)
                    yield ("delta", {"kind": "content", "text": content_part})

                for tc_delta in delta.tool_calls or []:
                    index = tc_delta.index
                    acc = tool_calls_acc.setdefault(index, {
                        "id": None,
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            acc["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            acc["function"]["arguments"] += tc_delta.function.arguments

        content = "".join(content_parts) or None
        reasoning = "".join(reasoning_parts) or None
        tool_calls = [
            tool_calls_acc[index] for index in sorted(tool_calls_acc)
        ]

        assistant_message = {"role": "assistant"}
        if content is not None:
            assistant_message["content"] = content
        if reasoning:
            assistant_message["reasoning_content"] = reasoning
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)

        log("model_response", {
            "round": round_index,
            "finish_reason": finish_reason,
            "content": content,
            "reasoning_content": reasoning,
            "tool_calls": tool_calls or None,
            "usage": usage,
        })
        yield ("turn", {
            "round": round_index,
            "finish_reason": finish_reason,
            "usage": usage,
        })

        if not tool_calls:
            yield ("done", {
                "messages": messages,
                "finish_reason": finish_reason,
                "usage": usage,
            })
            return

        for tc in tool_calls:
            call_id = tc["id"]
            fname = tc["function"]["name"]
            fargs = tc["function"]["arguments"]

            log("tool_requested", {
                "call_id": call_id,
                "name": fname,
                "arguments": fargs,
            })
            yield ("tool", {
                "event": "requested",
                "call_id": call_id,
                "name": fname,
                "arguments": fargs,
            })

            tool_call_obj = SimpleNamespace(
                id=call_id,
                function=SimpleNamespace(name=fname, arguments=fargs),
            )
            result = execute_tool(tool_call_obj)

            log("tool_result", {
                "call_id": call_id,
                "name": fname,
                "result": result,
            })
            yield ("tool", {
                "event": "result",
                "call_id": call_id,
                "name": fname,
                "result": result,
            })

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            })

    raise RuntimeError(f"工具调用超过上限：{cfg.max_tool_rounds} 轮")
