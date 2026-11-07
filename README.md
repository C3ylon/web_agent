# DeepSeek Web Chat（FastAPI · 仅流式）

以 `main.py` 为基础重构的本地交互网页。完全移除了非流式路径，只保留流式；日志按「会话组」维度管理，支持配置最大会话组数与单组最大日志条数。

## 文件结构

| 文件 | 说明 |
|---|---|
| `app.py` | FastAPI 应用：配置、会话组、SSE 流式对话、日志查看、静态页面 |
| `core.py` | 核心逻辑：运行时可改配置、OpenAI 客户端工厂、搜索工具、流式生成器 |
| `logstore.py` | 会话组维度的日志存储（容量上限 + 淘汰） |
| `static/index.html` | 单页前端：配置表单、会话列表、messages 编辑器、流式渲染、日志查看 |
| `sitecustomize.py` | Windows 下 `tempfile.mkdtemp` 的兼容补丁（详见文末） |
| `main.py` | 原始脚本，保留未动 |

## 运行

```powershell
.\.venv\Scripts\python.exe app.py
```

然后浏览器打开 http://127.0.0.1:8000

端口 / 地址可配置，三种方式（优先级从高到低）：

```powershell
# 1. 命令行参数
.\.venv\Scripts\python.exe app.py --port 8010 --host 127.0.0.1

# 2. 环境变量
$env:DSH_WEB_PORT = "8010"
.\.venv\Scripts\python.exe app.py

# 3. 什么都不写 -> 默认 127.0.0.1:8000
```

首次使用：进入「配置」页，填入你的 `api_key`（其余字段默认即可），点「保存配置」。

## 使用说明

- **会话组**：左侧是会话列表，每个会话组独立保存对话历史与日志。「＋ 新建会话」创建新组；悬停会话项可点 ✎ 重命名、🗑 删除。
- **流式对话**：在输入框输入消息回车发送（Shift+Enter 换行，输入框可拖动变高、可上下滚动），正文逐字输出并渲染为 Markdown；思考内容（reasoning_content）折叠显示；工具调用以标签形式展示。「重新生成」按钮会去掉最近一次回复并重新请求。
- **自定义 messages**：展开「messages 数组」编辑区，可直接修改要发送的完整数组（JSON），再点「直接发送编辑器内容」；或点「重置为系统提示词」。
- **配置**：所有脚本设置（base_url / model / api_key / reasoning_effort / 工具轮次上限 / 搜索参数 / 思考开关 / 引号高亮 / 日志上限）均可在网页修改并持久化到 `toyagent_log/config.json`。
- **初始消息（可多角色）**：配置页的「初始消息」可自由增删任意多条 `role / content`（不再限于 system）；`role` 为 `assistant` 时额外支持 `reasoning_content`（思考内容）字段，仅在 role 填 assistant 时显示。也支持把 JSON 数组字符串直接「导入 JSON」，导入后界面会逐条展开为可编辑的行。新建会话时即以这些消息作为开头。
- **日志**：按会话组维度存放于 `toyagent_log/sessions/<会话id>/events.jsonl`，网页「日志」页可查看；点「导出日志」可下载当前会话的完整日志 JSON（含会话元数据与全部事件）。

## 界面与渲染

- 页面采用 **Solarized Light** 配色。
- 回复正文默认用 **Markdown** 渲染（标题 / 粗斜体 / 代码块 / 列表 / 表格 / 引用等）。
- 「引号内容橙色高亮」配置项：开启后，正文里中文引号（`“”` `‘’` `「」` `『』`）或英文双引号 `"..."` 内的内容会以橙色标记；代码块内不生效。

## 日志管理

两个可配置上限（配置页可改，修改即时生效）：

- `max_session_groups`（最大储存会话组数）：超出后淘汰最早的会话组（整组删除）。
- `max_logs_per_group`（单组会话最大日志条数）：超出后淘汰该组最早的日志记录。

## 关于 sitecustomize.py

本机是 Python 3.14：CPython 3.12+ 在 Windows 上会把 `os.mkdir(path, mode)` 的 POSIX mode 翻译成 DACL，`tempfile.mkdtemp()` 传入的 `0o700` 会生成一个当前令牌无法写入的目录，导致 pip / ensurepip 解包时报 `PermissionError 13`。

`sitecustomize.py` 只对 `tempfile` 打补丁，让它忽略 mode、改用继承的正常 ACL。安装依赖时需确保它在 `PYTHONPATH` 上：

```powershell
$env:PYTHONPATH = "D:\DSHwork_space\aiagent"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

若在普通（非沙箱）环境运行，不加载此补丁也不影响使用。
