# V2.2 重构计划：Custom Orchestrator + jiuwen 多 Agent + RAG + 记忆 + 独立 Synthesizer

## Context

`opportunity_agent/plan2.md` 规划了面试级 V2 系统，但选定 LangGraph 作为编排运行时。本计划以 `opportunity_agent_v2_2_plan.md` 和最新架构图为依据：Custom Python Orchestrator 负责确定性控制，openJiuwen 用于 Router 和领域 Agent；本阶段不加入在线评估 Agent 或 Answer Validator，离线评测与可观测性仍保留。

> **框架有的用框架，框架没有的才自己写。**

该原则的依据是对 jiuwen 的运行时实测审计，完整记录在 `C:\Users\xjy\.claude\plans\jiuwen-capability-audit.md`。

### 为什么换掉 LangGraph

1. **v1 已有可用的 jiuwen 资产**：`chat_orchestrator.py` 跑通了 Rust ReActAgent、A2A JSON-RPC、`LocalFunction` 工具注册、`context_token` 侧通道、`_mark_retry` 失败状态机。
2. **LangGraph 版本实测不可用**：`v2/agents/orchestrator.py:48` 的 `classify_intent` 三元表达式两个分支都落到 `"profile"`，`planning` 路由不可达；`TASK_TERMS` 是死代码；`research_agent` 依赖的 `state["evidence"]` 无人填充，永远走"无官网证据"分支；`final_answer` 只返回三句硬编码文案。整张图零 LLM 调用。
3. **A2A 是已验证的能力**，不应丢弃。

### 框架能力边界（实测结论）

| 能力 | 状态 | 处置 |
|---|---|---|
| A2A Client / Server | ✅ 完整，且 `send_streaming` / `get_task` 是 v1 未用的 | **用框架** |
| MCP Client（stdio + streamable_http） | ✅ 完整 | **用框架** |
| `OutputSchema` 流式分帧 | ✅ | **用框架**做 SSE |
| `Runner` / `ResourceMgr` / `AbilityManager` | ✅ | **用框架** |
| `ReActAgent` | ✅ | **用框架** |
| Session / 跨会话状态 | ❌ **桩**，调用抛 `NotImplementedError: not yet bridged from Rust (block 2)` | **自己写** |
| `ControllerAgent` / `DuplexReActAgent` | ❌ 未导出到 Python（Rust 有文档） | 用 `ReActAgent` + 手写 A2A 委托 |
| 长期记忆 | ❌ 零支持 | **自己写** |
| embedding / 向量检索 / RAG | ❌ 零支持 | **自己写** |
| MCP Server | ❌ 只有 Client | **自己写**（项目已有 `v2/mcp/server.py`） |

`ReActAgent` 实例属性实测只有 `['ability_manager', 'card', 'configure', 'invoke', 'register_rail', 'stream']` —— 无 `context_engine`，故 `demo_py` 的历史注入模式（`session.pre_run()` → `context_engine.create_context()` → `save_contexts()` → `session.commit()`）不应成为 V2 的记忆实现。V2 部署默认采用官方纯 Python `openjiuwen[all-a2a]`（Python 3.11–3.13、Linux 可用）；当前 Windows 的 `openjiuwenrust` 仅保留为本机兼容与回归测试后端，不进入 Docker 镜像。

---

## 目标架构

**4 个 jiuwen Agent（Router + 3 个领域 Agent），另有独立目标解析器和 Response Synthesizer。** 后两者是由 Orchestrator 调用的 LLM 组件，不承担 Agent 工具调用或执行调度。

```
User / Browser → FastAPI (Auth / REST / SSE)
                         │
                         ▼
Custom Python Orchestrator ── Memory Service (PostgreSQL / Redis)
  │ Guard → 目标解析器生成 SuccessCriteria（闲聊可为空）→ Router Agent 只给 RouteDecision
  │                                              ├─ direct_reply → Response Synthesizer
  │                                              │                 （跳过领域 Agent / Checker）
  │                    ┌─────────────────────────┼─────────────────────────┐
  │                    ▼                         ▼                         ▼
  │             Profile Agent              Research Agent             Planning Agent
  │               A2A :8771                 A2A :8772                 A2A :8773
  │                                     内部 Query Router
  │                               SQL / RAG / Hybrid / MCP-Web
  │                    └─────────────────────────┼─────────────────────────┘
  │                                              ▼
  └─ ExecutionState 保存结果 ← Result Aggregation & Context Builder
                         │
                         ▼
             Completion Checker（无状态；PASS / RETRY / NEED_USER / FAIL）
                         │ RETRY：缺失任务回 Orchestrator，定向补查、合并、重检
                         │ PASS 或预算耗尽后 PARTIAL
                         ▼
              Response Synthesizer → 基础引用/格式校验 → SSE 答案
                         │
                         └─ 成功后触发 Memory Consolidator → Memory Service
```

Orchestrator 持有对话与运行状态、读取相关记忆、调用 A2A、控制重试与预算；Router 不持有执行状态，不组织结果或回答，也不直接写记忆。Profile 是唯一有画像/申请状态写入权限的领域 Agent，写入仍须过审批门；Research / Planning 是只读领域 Agent。Memory Service / Consolidator 专管用户偏好记忆，Redis 保存临时工作状态。相对 v1，只有 Profile Agent 需要必要的状态快照，Research / Planning 不传全量快照。

**保留 LangGraph 版本的核心不变量**：模型只产出提案，写入必须经审批门（`v2/services/applications.py` 已实现且测试通过）。

---

## 用框架的部分（照 demo_py 官方写法）

`demo_py` 是框架自带的三 Agent 演示（`jiuwen_sa` 8768 Host + `jiuwen_nav` 8001 + `jiuwen_pc` 8002）。其 A2A / MCP 客户端写法可复用，但本计划的执行控制归 Custom Orchestrator，不照搬演示中由 Agent 调工具完成编排的拓扑。

### 1. A2A 连接池（v1 每次新建）

部署后端用 Python SDK：每个 Agent endpoint 一个进程级 `A2AClient`，复用其异步 HTTP 连接，API 关闭时调用 `await client.stop()`。客户端根据 `AgentCard(interface_url=endpoint)` 建立 JSON-RPC 连接：

```python
_CLIENT_POOL: Dict[str, A2AClient] = {}
_POOL_LOCK = asyncio.Lock()

async def _get_pooled_client(endpoint: str) -> A2AClient:
    async with _POOL_LOCK:
        client = _CLIENT_POOL.get(endpoint)
        if client is None:
            client = A2AClient(card=AgentCard(interface_url=endpoint, ...))
            _CLIENT_POOL[endpoint] = client
    return client
```

Rust `PyA2aClient` 的 `destroy()` 仅用于 Windows 本机兼容后端；不要把 Windows wheel 放入 Linux 镜像。

### 2. Python A2A 直接 await（不再转线程）

```python
result = await asyncio.wait_for(
    client.invoke({"query": request_json, "conversation_id": conversation_id}),
    timeout=timeout_seconds,
)
```

Python `A2AClient.invoke()` 和 `stream()` 是异步接口；`asyncio.to_thread` 只保留给 Rust 兼容实现的同步 `send_message()`。

### 3. 领域 Agent 的 A2A 事件可用 `send_streaming` 消费

Orchestrator 消费框架流式 A2A 事件，并映射为现有 HTTP/SSE 进度与最终回答事件：

```python
async for agent_result in client.stream({"query": request_json, "conversation_id": conversation_id}):
    for text in _extract_artifact_texts(agent_result):
        yield text
```

须区分 Agent 的进度 artifact 与 Synthesizer 的最终回答：领域 Agent 的文本不是可直接返回给用户的最终答案。SSE 事件类型从 `OutputSchema` 的分帧结构（`type` / `index` / `llm_output` / `tool_call` / `tool_result` / `answer` / `error` / `payload`）映射，不凭空定义。

### 4. A2A 适配器由 Orchestrator 持有

官方 Python `A2AClient` / `A2AServer` 和连接池、分帧可供适配器复用，但不把领域 Agent 注册为 Router 的可执行工具。由 Orchestrator 构造带 `query` / `conversation_id` / `run_id` / 缺失任务约束的 A2A 请求，验证响应并转为 `ProfileResult` / `ResearchResult` / `PlanResult`；沿用 `a2a_protocol.py` 的字段白名单与证据校验。

### 5. Planning 长任务用 `get_task`

规划长任务时使用 Python `a2a-sdk` 的任务状态/取消能力；在其接入前，现阶段 Planning 保持同步、带超时的结构化请求，不能假装已实现后台轮询。

### 6. Research Agent 的 MCP/Web 路径用 `McpClient`

按 Python SDK 的 MCP client 接口为每个 server 建立持久连接，按 `server_name` 跨会话复用。MCP 工具只供 Research Agent 的外部检索路径使用，不装到顶层 Router。Rust 类型与 Python 类型不能混用；Docker 中只 import `openjiuwen.*`。

---

## 目标解析、确定性守卫与顶层路由

关键词路由（v2 现状）覆盖太少，纯 ReAct（v1 现状）会在模型误判时静默丢事实。Orchestrator 先取用户记忆并执行确定性 Guard，再调用独立目标解析器生成 `SuccessCriteria`，最后调用顶层 Router；Guard 的强制路径优先于 Router 的语义选择。

**第一层 · 确定性前置守卫**（零 LLM 成本，复用 v1 已验证函数）

- `profile.py:282 _requires_semantic_context(msg)` —— 含"不再/不考虑/改成/撤销"等纠正词 → **强制** Profile 路由
- `turn_understanding.py:111 facts_cover_message(...)` —— 规则已完整覆盖句子 → 直接 Profile 路由
- **新增**：`FastExtractor` 抽出高置信结构化事实（GPA/TOEFL/GRE/日期，`confidence=0.99`）且句子其余部分已覆盖 → 强制 Profile 路由

第三条直接堵住"我托福考了105"被模型判为闲聊而丢事实。

**目标解析器 · 成功标准**

目标解析器是 Orchestrator 调用的独立组件，不是 Router 或评估 Agent。它把用户明确要求转成可检查的数量、日期区间、GRE 条件、引用/证据要求及待澄清项；不把模糊的“AI 较强”等主观判断伪装成确定性硬阈值。没有可执行目标的纯闲聊允许返回空成功标准。输出随 `ExecutionState` 保存，后续补查不重新解析，除非用户修改了目标。

**第二层 · ReAct 语义路由**

守卫未命中时交给 `ReActAgent` 决策 Profile / Research / Planning 的组合与并行性，或选择无需领域 Agent 的 `direct_reply`。Router 只返回结构化 `RouteDecision(mode, agents, parallel, reason)`：`mode=direct_reply` 时 `agents=[]` 且 `parallel=false`，其他模式至少有一个目标 Agent。Router 不生成 `SuccessCriteria`，不调用领域 Agent、MCP、数据库或 Synthesizer。A2A 执行与重试由 Orchestrator 负责。

**纯闲聊分支**：Guard 先排除画像/申请状态更新和明确的偏好写入；Router 确认为寒暄、感谢、一般情绪交流等无需查询或规划的请求后，Orchestrator 跳过 A2A、Result Aggregation 和 Completion Checker，直接调用同一个 Response Synthesizer，以用户消息、当前会话和相关记忆生成自然语言回复。该分支没有检索证据，不凭空添加学校事实或引用；会话消息仍按正常流程保存并通过 SSE 返回。无新偏好时不触发长期记忆写入。若一句话同时包含闲聊与截止日期查询、状态更新或规划任务，不能走 `direct_reply`，应路由到相应领域 Agent；不确定是否需要查证时优先走任务路径或请求澄清，而非无证据直接回答。

**失败兜底**：沿用 `_mark_retry` / `force_delegation`。**需修 v1 缺口**：`web/index.html:93` 重试按钮只认 `awaiting_agent_retry` 和 `direct_answer`，不认 `failed`，导致 revision 冲突失败时无入口（而 `SessionService.retry_a2a` 实际能处理 `failed`，只检查 `event.source == "chat"`）。

---

## ExecutionState、完成检查与缺失修复

### Research 工具级有界修复（实现开关：`RESEARCH_TOOL_REPAIR_ENABLED`）

保留首轮 SQL/RAG/Hybrid/MCP-Web 流程。失败、无结果或项目/入学季证据不匹配时，ResearchRepairPlanner 读取结构化 ToolObservation，提议一个工具和参数；统一执行器校验权限、来源和预算再调用。顶层 Router 不参与工具决策。

- 工具为 `search_official_pages`、`validate_official_url`、`read_official_page`、`extract_official_page`、`extract_program_facts`；复用 Tavily MCP，不添加浏览器/PDF或新 MCP 服务。直接读取和备用 MCP 提取分别计数。
- 每校最多 6 次工具、2 次修复决策，默认 90 秒（显式环境变量覆盖）；全 Run 为 `min(60,6×N)` 次工具及 `min(20,2×N)` 次决策。N 首次估算并冻结；Research/执行总时间上限仍为 600/1800 秒，预留合成时间。
- Redis Run 级账本请求前原子扣减；成功、失败、格式降级、备用请求均不能绕过计数。账本故障则停止外部调用。Research A2A 转发 `research_tool_state`，ResultAggregator 按调用 ID 去重，不重加累计快照。
- 修复模型最多等待 20 秒，无隐式重试；失效时采用安全备用读取、一次 compact 提取或继续已有合法候选，再进行有界字段拆分搜索；连续 3 次修复服务故障后暂停该服务等待。简化提取最多 4000 字符、两个缺失字段；同页最多一次额外提取，必须更改档位或字段。统一执行器对所有入口禁止原样重复提取；其他工具的瞬时故障最多一次原参数重试。
- 查询改写须保持当前学校、项目、国家、年份/学期和 GRE 筛选目标；拒绝明确冲突并补齐省略的任务锚点。候选按项目相关性排序，HTTP 仅构造 HTTPS 候选重新校验；错误专业/年份优先转到已有候选，受控备用读取不等待 LLM。
- Redis Run 账本私有进度压缩保存候选、完整 URL、原始页面、待执行动作及已验证结果，公开快照只传递计数与安全状态；跨轮次续查不重启相同搜索。提取和修复模型不隐式注入聊天历史/画像。没有新动作时 Orchestrator 提前 PARTIAL，前端及 Synthesizer 使用统一的精确失败分类。
- 官网边界拒绝明确区分协议/域名/端口/私网/重定向；LLM 不得放宽来源、安全策略或改写目标年份。取消“官网页面必须出现目标入学季”的硬门槛：无入学季的当前官方政策可用于筛选，但证据标记 `temporal_scope=current_policy`、来源入学季留空，回答说明“适用入学季未确认”；检索日期不能证明适用年份。明确标注其他入学季的页面仍拒绝，截止日期须有可核对的完整年份、支持月份缩写，当前/未来申请查询不能把已过期截止日期当作新日期。该区别须在缓存回读和 Synthesizer 中保留，不增加数据库迁移。模型连续三次超时/连接失败触发熔断；全 Run 最多一次、等待至少 30 秒的半开探测。
- 工具级修复不等于 Checker PASS。Orchestrator 仍保留所有已验证结果，只补缺口；工具预算耗尽后 PARTIAL，由 Synthesizer 如实说明。技术故障不得伪装成需要用户提供官网事实。
- SSE 显示失败分析、策略调整、备用读取及工具步数；Trace 和历史诊断保存安全错误码与计数，不记录密钥、完整网页或模型思维过程。默认关闭，先部署 Research 再部署 API 后启用；无需数据库迁移。

验收覆盖搜索改写、403 备用读取、空响应/超时简化提取、非法提议、来源拒绝、熔断及跨轮次计数；已有 4 个合格项目时只补第 5 个，不合格证据不计数，预算耗尽诚实返回缺口。

`ExecutionState` 归 Orchestrator 持有，按 `run_id` 保存原始用户目标、`SuccessCriteria`（纯闲聊可为空）、`RouteDecision`、每轮 Agent 结果、已接受的证据、轮数/时间预算及缺失任务。Result Aggregation & Context Builder 对领域结果按项目身份去重、保留来源与版本，生成给 Checker / Synthesizer 使用的当前汇总视图。**Completion Checker 不持有或缓存旧结果**，每次只读取该视图和成功标准；`direct_reply` 不调用 Checker，也不伪造 `PASS`。

Checker 第一版只做可操作的完成判断：被选中的 Agent 是否返回有效结构化结果、学校/项目数量、明确的日期范围、GRE 等结构化约束，以及证据来源是否可追溯、适用该项目、足够新且与查询高度相关。来源可靠性依官方域名、项目级匹配、抓取时间和证据标识校验；相关性依检索/重排分数与所问字段是否匹配。阈值在离线样例上校准并作为配置记录；缺来源、来源被拒绝、低相关或未知值均**不计为合格项**。Checker 不评价答案文风，也不把宽泛的“AI 强”直接变成主观通过/失败判定。

Checker 输出 `PASS`、`RETRY`、`NEED_USER` 或 `FAIL`，并携带未满足条件及 `MissingTask`（目标 Agent、约束、已查项目/来源、缺口数量）。`FAIL` 用于不可恢复的执行错误；达到预算但仍有可补缺口时，Orchestrator 将运行状态标为 `PARTIAL`，而不是伪造 `PASS`。首版总执行轮数最多 3 轮，同时设总时间与工具调用预算；一次轮次只处理明确缺失任务。

```text
目标：5 个符合日期、GRE 与证据要求的项目
首轮：ResearchResult 有 4 个合格项目 → 汇总进 ExecutionState
Checker：RETRY + MissingTask(research, 缺 1 个, 保留原约束与已查集合)
Orchestrator：不清空旧结果，不重跑 Profile / Planning，不重算 SuccessCriteria
              直接定向调用 Research Agent 补查；仅在缺口转向新领域时重问 Router
新 ResearchResult → 按项目身份去重并与旧结果合并 → Checker 重检全部合格项
PASS → Synthesizer；仍不足且有预算 → 继续定向补查；预算耗尽 → PARTIAL
```

`NEED_USER` 时暂停补查并向用户请求必要澄清；不能靠重复搜索猜测用户限制。`PASS` 与 `PARTIAL` 都经 Synthesizer 生成面向用户的回答，但 `PARTIAL` 必须清楚区分已核实项、未知项和未满足的数量。Synthesizer 不调用工具、不更改 Profile/Memory、不创造 Context 中不存在的事实。本阶段不接在线 Answer Validator 或评估 Agent；输出前仅做程序化的引用可解析性与格式检查，离线评测仍保留。

---

## 跨会话长期记忆（自己写）

### 为什么自己写

框架 Session 是桩；外部记忆框架（Mem0 / Zep / Letta）的核心卖点"抽取事实 → 冲突消解 → ADD/UPDATE/DELETE"恰好是本项目已有且更严格的部分：

| 环节 | 本项目 | 外部框架 |
|---|---|---|
| 抽取 | 三层链路（正则 → 关键词 → LLM） | 单次 LLM |
| 语气 | `statement_kind` 离散枚举 → 确定性映射 confidence/operation；假设句丢弃、否定强制 `remove` | 模型自报 |
| 反幻觉 | evidence 必须原文子串，协议层 validator 拦截 | 无 |
| 冲突 | 来源优先级 + 0.75 阈值 + 确认队列 | LLM 判断 |
| 写入 | 审批门，模型只能产出 proposal | 直接写 |

引入外部框架会**替换掉这套边界**。且 LangGraph `store.put()` 是立即写入，与"模型输出只能是提案"的不变量冲突——包一层提案/审批之后 store 就退化成一张表。

**框架能省掉的是长对话 token 压缩**（而 Rust core 内部已自动注册 `DialogueCompressor` / `MessageOffloader` / `ToolResultBudgetProcessor` 等 8 个处理器），**省不掉用户偏好长期记忆**——后者需对齐本项目的来源优先级、置信度阈值、审批门。

### 现状对比（用户问的"原来有没有"）

| 能力 | v1 | v2 现状 | 本次 |
|---|---|---|---|
| 结构化画像 | 每会话快照 | `profiles` 按 user 唯一 ✅ 跨会话 | 保留 |
| 事实溯源 | `facts` 对象内 | `profile_facts` 表 ✅ | 保留 |
| 变更审计 | `change_history`（冲突解析会读） | `profile_changes` 表（**写入但从无读取**） | 补回读取 |
| 阶段证据（带过期） | `StageEvidence` ✅ | **丢失** | 移植 |
| 偏好 / 情景记忆 | ❌ | `memory_items` 表**零读写**（实测全仓只有 `models.py:176` 三行定义） | 实现 |
| 跨会话召回 | ❌（`session_state.py:20` 把 `user_id` 覆写为 `f"web_{session_id}"`） | 仅画像 | 实现 |
| 记忆语义检索 | ❌ | ❌ | 实现 |

**结论：跨会话记忆是全新能力，不是"原来有"。** `profile.facts` / `change_history` 是会话内审计日志，且被所有 Prompt 构造点 `exclude` 掉。

### 表已设计好，只缺两处

`v2/db/models.py:176` 现有定义（`source` / `confidence` / `expires_at` 恰好承载现有语义）：

```python
class MemoryItem(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "memory_items"
    user_id: ...      ForeignKey("users.id", ondelete="CASCADE"), index=True
    memory_type: ...  String(32), index=True      # 本阶段使用 preference
    key: ...          String(160), index=True
    value: ...        JSON
    source: ...       String(64)                  # user_explicit / model_inferred
    confidence: ...   float                       # 对齐 0.75 阈值
    expires_at: ...   DateTime | None              # 衰减
```

**Alembic `0002` 需加**（当前只有 `0001_v2_foundation.py`）：
1. `UniqueConstraint("user_id", "memory_type", "key")` —— 当前无此约束，无法去重
2. `embedding: Vector(384).with_variant(JSON, "sqlite")` —— 语义召回用，与 `KnowledgeChunk:172` 同规格

### 分层与写入策略（已确认：显式直写，推断走审批）

```
Profile Memory      → profiles 表（已有）
Preference Memory   → memory_items[memory_type="preference"]    例："不考虑 GRE required 项目"
Application State   → applications / application_tasks（已有）
Working Memory      → Redis（当前 Agent Run 临时上下文，不入库）
```

本阶段长期记忆仅实现用户偏好，不把 Agent 执行经验、检索中间结果或单次咨询摘要自动写成长期记忆。Profile Agent 负责画像/申请状态的提案，偏好由 Memory Service 按显式/推断规则处理；Orchestrator 仅在回答完成后触发 Memory Consolidator，不直接写长期记忆。

| 来源 | 判定 | 路径 |
|---|---|---|
| 用户明确陈述 | `source == "user_explicit"` 且 `confidence >= 0.75` | 直接写 `memory_items` |
| 模型推断 | 偏好归纳 | `change_proposals` → `approval_requests` → 确认后落库 |

偏好默认不设 TTL，但可被用户新陈述撤销或覆盖；冲突更新沿用来源优先级与确认门。`expires_at` 保留给有明确有效期的偏好，不在本阶段扩展情景记忆。

### 实现清单

```
1. Alembic 0002：唯一约束 + 向量列
2. MemoryRepository：put / get / search_semantic
3. 复用 RAG 的 EmbeddingProvider 做语义召回
4. Memory Service 读取并由 Orchestrator 传给目标解析器/Router/Synthesizer
5. 回答后触发 Memory Consolidator；推断类记忆走 change_proposals 审批
```

Memory Service 负责所有偏好写入；审批门保持模型只产出提案的边界。现有画像抽取逻辑可复用，但偏好抽取、冲突和跨会话召回仍需单独验证。

---

## 学校信息 RAG（自己写）

Research Agent 内部的 Query Router 与顶层 Router 分离：结构化学校/项目字段走 **SQL → PostgreSQL**，课程、研究方向等语义问题走 **RAG → pgvector + Reranker**，同时含结构化条件与语义条件走 **Hybrid → PostgreSQL + RAG**；本地资料缺失或用户要求最新信息时走 **MCP/Web → 受控外部来源**。每条路径均返回带项目身份、来源、抓取时间和相关性信息的 `ResearchResult`，不生成最终回答。Hybrid 结果同样按项目身份合并并保留各字段证据，不把低可信网页结果覆盖已验证的结构化字段。

### 现状：骨架在，管道断了

```
已存在                            缺失
────────────────────────────────  ────────────────────────────────
✅ knowledge_documents / _chunks   ❌ ingest 从未被生产代码调用
✅ Vector(384) 列                  ❌ embedding 是调用方可选参数，无人生成
✅ CREATE EXTENSION vector         ❌ 从未使用 pgvector <=> 算子
✅ RRF 融合（真实实现，k=60）        ❌ 无 HNSW/IVFFlat 索引
✅ E5-small 懒加载配置              ❌ chunk_text 按空白分词，中文失效
✅ authority=="official" 过滤       ❌ 丢掉 classify_program_page 分级
                                   ❌ reranker 配置是死的
```

`OfficialIngestionService` 全仓只被 `tests/test_v2_foundation.py:78` 调用过——生产环境表恒为空，`/api/v1/research/sources` 是空壳。`HybridRetriever` 把所有 chunk 加载进内存做 Python 余弦。

### 建设内容

**1. 采集管道**（修 `v2/worker.py:20` 存根，当前只 `print`）

改为消费 Redis 队列并调 ingest。抓取复用 v1 的 `OfficialResearchTools.read_official_program_page`（`official_research.py:343`）——已有 HTTPS 校验、域名白名单、重定向重验、2MB 上限、`_TextParser` 剥离 script/style。

**2. CJK 分块**（修 `v2/rag/ingest.py:12`）

`chunk_text` 按 `text.split()` 分词，中文整段会成一个 token。改为标点 + 空白混合切分，保留滑窗重叠。

**3. Embedding 生成**（修管道断点）

`OfficialIngestionService.ingest` 的 `embeddings` 改为内部调 `EmbeddingProvider`。修正 E5 前缀约定：`retrieval.py:39` 查询侧用 `"query: "`，但索引侧应用 `"passage: "`，当前未实现。

**4. 数据库侧向量检索**

`HybridRetriever.search`（`retrieval.py:63`）改用 pgvector `<=>` 算子做 ANN，建 HNSW 索引，加 `LIMIT`。

**5. 移植项目级判别（关键，否则回归已修好的 bug）**

v2 只有 `authority == "official"` 过滤，丢掉了 v1 `classify_program_page` 的 `exact/generic/rejected` × `program/department/university_wide` 分级。

现成反例：`data/official_cache.json` 里 CMU MSCS 挂着 `www.ece.cmu.edu/admissions/graduate-faq.html`，`requirements[0].value` 是页头导航噪声。

把 `classify_program_page` / `program_identity` / `_application_page_priority`（`official_research.py:524-598, 662`）的结果写进 `knowledge_chunks.metadata`，检索时作硬过滤 + 排序权重。**只有 `program_match == "exact"` 能闭合项目级待核验项。**

**6. Reranker**（`core/config.py:18-19` 配置无人读取）

实现可开关 cross-encoder 重排。

**7. 历史数据迁移**

`data/official_cache.json` 有 36 条 entry（186 KB，10 个学校-项目-入学年组合，97 条 requirements）。一次性脚本灌入 `knowledge_documents` / `knowledge_chunks`，注意 `get()` 的惰性 revoke——被判 `rejected` 的不迁移。

**8. 保留只读边界**

RAG 结果**不能**直接改画像或时间轴。咨询路径只更新 `lifecycle.last_official_research`（侧栏展示）；写进路线图必须走"重新规划"。这是 v1 已验证边界（`chat_orchestrator.py:320-321`）。

---

## 实施阶段

### 阶段 1 · Foundation

FastAPI 替换 `ThreadingHTTPServer`；JWT + Argon2；PostgreSQL + Redis + Docker Compose；Alembic；Repository；基础 SSE。沿用现有 V2 基础设施，不要求迁移 `data/conversations.json`。

**已可直接用**：`v2/api/app.py`（273 行，路由齐全）、`v2/core/security.py`、`v2/repositories.py`、`v2/migration.py`、`v2/services/auth.py`（含 refresh token 单次轮换）、`docker-compose.yml`、`Dockerfile`、`alembic/versions/0001_v2_foundation.py`。

**暂缓**：`data/conversations.json` 迁移及 `v2/migration.py` 幂等键改造不属于本阶段交付。日后决定迁移时须以 `legacy_session_id` 幂等，不能仅按 `(user_id, title)`；现有脚本也只搬标题与消息文本，不搬 profile / facts / roadmap / tasks。

**验收**：注册登录、跨浏览器会话、消息幂等、服务重启、版本冲突均可验证；不以旧会话迁移作为通过条件。

### 阶段 2 · Orchestrator 与多 Agent 主链路

**新建 3 个 A2A 领域 Agent**，仿 `opportunity_a2a.py`（`handle_mutation` + `opportunity_invoke_handler` + `create_*_a2a_server`）：

- **Profile Agent**（:8771）：复用 V1 的抽取、标准化、冲突消解与状态转换规则，返回结构化 `ProfileResult` 和必要的画像／申请／进度提案；对话记录归 Orchestrator，写入经审批门，偏好记忆交 Memory Service。
- **Research Agent**（:8772）：先用可控的结构化结果跑通主链路，再接入内部 Query Router 与 SQL / RAG / Hybrid / MCP-Web 检索；返回 `ResearchResult`，不接收全量会话快照。
- **Planning Agent**（:8773）：复用 `build_timeline_skeleton` / `TimelineComposerSkill` / `RoadmapArticleSkill`，接收最小画像、当前计划与进度快照及已汇总 Research 结果。A2A 返回混合 `PlanResult` JSON：`article_markdown` 保存 LLM 自然语言正文，`timeline` / `tasks` / `roadmap` 保存可追踪、可审批的结构化数据。完整 `roadmap` 可形成 `plan.replace`；专项 `advice` 只参与回答，不替换现有计划。当前 A2A 服务在线程中执行同步规划；Redis 队列与异步任务查询仍是后续长任务增强项。

**Orchestrator 主链路**：检索记忆 → Guard → 独立目标解析器产出 `SuccessCriteria`（闲聊可为空）→ Router 产出 `RouteDecision`。`direct_reply` 直接调用 Synthesizer；领域任务由 Orchestrator 调用 Agent，独立 `ResultAggregator` 跨轮合并结果与构造审批上下文，再交 Completion Checker 和 Response Synthesizer。Planning 依赖当前 Research 结果时后执行；Research 补查改变证据版本时，Completion Checker 定向重跑 Planning，避免最终文章继续使用旧证据。两支最终通过 SSE 返回。领域 A2A 请求／结果契约由 `v2/agents/a2a.py` 和 `contracts.py` 定义。

**Router Agent**：只负责顶层语义路由，`RouteDecision` 允许 `mode=direct_reply, agents=[]`；不装 A2A 委托和 MCP 工具，不生成 `SuccessCriteria` 或最终答案。`chat_orchestrator.py` 的历史对话与失败处理逻辑作为迁移参考；连接池、`asyncio.to_thread`、`send_streaming` 放到 Orchestrator 的 A2A 适配层。

**审批流**：`v2/services/applications.py` 已实现且测试通过（`test_langgraph_produces_proposal_not_direct_write`、`test_approval_gate_applies_profile_only_after_acceptance`），直接复用。

**替换**：现有 `v2/agents/orchestrator.py` 的 LangGraph 占位图由 Custom Python Orchestrator 接替；先完成新主链路验证，再下线旧入口，不清理无关用户数据。

**验收**：Router 输出只有路由决策；目标解析结果独立保存；三个 Agent 返回结构化结果；`direct_reply` 不调用领域 Agent / Checker；Synthesizer 对任务只用已汇总证据回答、对闲聊不伪造引用；确认后才修改画像/申请状态；Run 可回放。

### 阶段 3 · Research Query Routing + RAG + 跨会话记忆 + MCP

实现 Research 四路径、RAG 八项（见上）及偏好记忆五项（见上）；MCP Client 仅接到 Research Agent。Server 补认证——当前 `v2/mcp/server.py` 的 `list_application_tasks` / `propose_application_change` 接受明文 `user_id`，任何 MCP 客户端可冒充任意用户。

**验收**：
- CMU/UIUC 合成场景有正确引用；无来源时不编造
- deadline 走 SQL、课程语义走 RAG、组合条件走 Hybrid、最新官网信息走 MCP/Web
- 检索资料不能直接改 Profile
- 会话 A 说"我不考虑 GRE 项目" → 新建会话 B → Orchestrator 召回该偏好并注入相关组件
- 显式陈述立即生效；模型推断的偏好停在 `approval_requests` 待确认

### 阶段 4 · Completion Checker 与缺失修复循环

实现无状态 Checker、`MissingTask`、最多 3 轮的有界补查、项目身份去重、`PASS` / `RETRY` / `NEED_USER` / `FAIL` / 预算耗尽 `PARTIAL` 分支。缺口明确属于 Research 时 Orchestrator 直接定向补查；只有任务类型改变才重新请求顶层 Router。Checker 不存旧结果，不调用领域 Agent，不生成最终答案。

**验收**：要求 5 个项目而首轮只有 4 个时，旧 4 个始终留在 `ExecutionState`，仅 Research 补查，合并去重后重新检查；不合格新结果不计数；预算耗尽输出标注缺口的部分回答；需要用户澄清时暂停补查。

### 阶段 5 · 离线评测 + 可观测性（无评估 Agent）

Gold dataset（100-150 条）；指标 `Profile Fact F1` / `Conflict Safety Rate` / `Retrieval Recall@5` / `MRR` / `Citation Precision` / `Answer Faithfulness`；RAG baseline 对比（vector only → hybrid → +rerank → +query rewrite）；OTel trace。

**已存在**：`v2/evaluation/metrics.py`（`fact_f1` / `retrieval_metrics`）、`runner.py`（仅 profile 套件）、`v2/core/telemetry.py`（`span()` 已脱敏 `message`/`content`/`token`/`resume`/`api_key`）。

**验收**：输出量化指标、实验表、失败样例、Agent trace；关键回归进 CI。评测不进入在线控制循环，不增加在线 Answer Validator。

---

## 关键文件

**复用（不要重写）**
- `lifecycle_agent.py:433` — `apply_structured_update`，Profile Agent 写入入口
- `profile.py:18/126/282` — `FastExtractor` / `LegacySemanticRuleExtractor` / `_requires_semantic_context`
- `turn_understanding.py:111` — `facts_cover_message`
- `official_research.py:343/524/646/723` — 页面读取 / 分级 / 确定性检索 / SSRF
- `a2a_protocol.py` — 协议校验模式
- `opportunity_a2a.py:15` — 领域 Agent 服务器模板
- `chat_orchestrator.py:396-438` — v1 jiuwen 工具装配，仅作迁移参考；不作为新 Router 执行层
- `session_state.py:14-20` — `snapshot` / `restore_agent` 契约
- `models.py:213` — `StageEvidence`，记忆衰减原语
- `v2/services/applications.py` — 审批门
- `v2/db/models.py` — 21 张表
- **`demo_py/src/jiuwen_sa/tools/rust_a2a_tool.py`** — A2A 官方写法
- **`demo_py/src/jiuwen_sa/tools/rust_mcp_tool.py`** — MCP 官方写法

**改造**
- `v2/agents/orchestrator.py` — 替换 LangGraph 占位图，改为管理 ExecutionState、目标解析、A2A、结果合并与有界控制循环的 Custom Orchestrator
- `v2/agents/` — 新增只返回 RouteDecision 的顶层 Router、独立目标解析器、无状态 Completion Checker 与只负责最终回答的 Synthesizer
- `v2/agents/` / `v2/rag/` — Research 内部 Query Router 及 SQL / RAG / Hybrid / MCP-Web 四路径；返回结构化 ResearchResult
- `v2/rag/ingest.py:12` — CJK 分块 + 内部生成 embedding
- `v2/rag/retrieval.py:63` — pgvector 算子 + ANN + 项目级过滤
- `v2/worker.py:20` — 实现真实 ingest
- `v2/migration.py:29` — 暂不改、不运行旧会话迁移；未来启用前修正幂等键
- `v2/db/models.py:176` — `memory_items` 加唯一约束 + 向量列
- `config.py` — 新增 Research / Planning 端口与超时
- `web/index.html:93` + `web/progress_ui.js` — 重试按钮补 `failed`；改消费 SSE

**记录但可暂不修**
- `official_research.py:190` — `cache_key` 算了但 `get()` 从不读
- `official_research.py:60` — `_OFFICIAL_TOOLS` 模块级单例，`sources`/`requirements`/`trace` 跨请求共享可变状态
- `official_research.py:353` — 重定向后未重跑 `_assert_public_host`（`_verify_dynamic_candidate` 是重跑的）

---

## 验证

```powershell
# 当前已实现的 Foundation
docker compose up -d --build          # API: http://127.0.0.1:8000/docs
python -m pytest tests/test_v2_foundation.py -q

# 纯本地 Foundation 备选；不是完整 A2A/RAG 环境
powershell -ExecutionPolicy Bypass -File .\scripts\run_v2_local.ps1
```

### 阶段 2 已实现的真实 Python A2A 容器验证

V2 的 Docker 镜像安装官方 `openjiuwen[all-a2a]==0.1.19`，Compose 会启动 API、Profile、Research、Planning、PostgreSQL 与 Redis。API 通过容器 DNS (`profile` / `research` / `planning`) 调用三个 A2A 服务；不使用也不打包 Windows `openjiuwenrust` wheel：

```powershell
# 首次会下载较大的 Python SDK 与其依赖，随后使用 Docker layer cache
docker compose up -d --build
docker compose ps
docker compose logs api profile research planning --tail 100

# API 容器必须是 Python 后端，并将三个 endpoint 指向 Compose 服务名
docker compose exec api python -c "from opportunity_agent.v2.core.config import settings; print(settings.domain_agent_transport, settings.jiuwen_backend, settings.profile_a2a_url)"
```

端口和超时可通过 `PROFILE_A2A_URL` / `RESEARCH_A2A_URL` / `PLANNING_A2A_URL` 及对应的 `*_TIMEOUT_SECONDS` 覆盖。`DOMAIN_AGENT_TRANSPORT=local` 仍是 Foundation 的本地确定性 Stub；Compose 默认明确设为 `a2a`，`JIUWEN_BACKEND=python`。如需回归原 Windows Rust binding，可在宿主机显式设 `JIUWEN_BACKEND=rust` 并以 `--backend rust` 启动三个服务，不能和 Python 类型或 Linux 容器混用。

**端到端必查**

1. 模型误判兜底：构造确定性守卫应命中的输入，确认 Router 未选 Profile 时事实仍能形成提案并经审批保存
2. 记忆跨会话：会话 A 说偏好 → 新建会话 B → 确认 Orchestrator 召回并向相关组件提供该偏好
3. 记忆写入分层：显式陈述立即生效；推断偏好停在 `approval_requests`
4. RAG 可溯源：结果含 `source_id` 可点回原页；无来源时说"待核验"
5. 项目级判别：CMU 域名下 ECE FAQ 不得闭合 MSCS 的 GRE 问题（用 `official_cache.json` 历史脏数据做反例回归）
6. 只读边界：RAG 检索不改 profile / roadmap
7. 并发：聊天与表单同时操作，revision 冲突被正确拒绝且原文保留
8. A2A 连接池：连续多轮对话，确认未反复 handshake
9. 职责隔离：Router 只输出 RouteDecision；目标解析器独立输出 SuccessCriteria；Synthesizer 不调工具或写状态
10. 缺失修复：目标 5 个，首轮 4 个合格项目；保留旧 4 个，定向 Research 补 1 个，按项目身份去重后 Checker 检查全部结果，达标才输出完整答案
11. 补查失败：重复项目、日期/GRE 不符、无可追溯来源或低相关性的新结果不计数；有预算继续补查，预算耗尽输出 PARTIAL 与明确缺口
12. 用户澄清：约束不明确时 NEED_USER，暂停补查；不能把未证实事实填入最终答案
13. 纯闲聊："你好"、"谢谢"等走 `direct_reply`，三个领域 Agent 与 Checker 均不调用，Synthesizer 回复且不伪造引用或长期记忆
14. 混合输入："你好，帮我查今年截止日期"不能走 `direct_reply`；"我不考虑 GRE 项目了"须进入偏好/状态处理，不能被当成闲聊

```powershell
python -m pytest -q
```

---

## 风险与边界

- **API/Orchestrator + 3 个领域 Agent 的多进程运维成本**是真实代价，另需 PostgreSQL/Redis。缓解：每个领域 Agent 可单独启动并用 A2A JSON-RPC 端点测试。
- **RAG 冷启动**需下载 E5-small（~470MB）。`EmbeddingProvider` 已设计为权重缺失时降级关键词检索，需在 UI 标注当前模式。
- **MCP Server 无认证**是已知缺口（`v2/mcp/server.py:27` 只有 docstring 声明"调用方必须自行校验身份"）。阶段 3 必补。
- **`state_snapshot` 全量传输**在 Profile Agent 路径上仍存在（会话越长越大）。本次不解决，文档记录为已知瓶颈（后续改增量 diff）。
- **框架 Session 未来可能可用**：桩文件说 `block 2 will bridge`。若框架更新后 `create_agent_session` 可用，可考虑用它承接对话上下文压缩，但**长期记忆仍应保留自建**——它需对齐本项目的来源优先级与审批门。

## 2026-10-05 偏好记忆实施进度

偏好闭环已在源码实现并通过离线验收：Memory Service 统一读写，PreferenceSnapshot 与工作状态分离；明确偏好保存、推断审批、撤销、版本冲突、审计，以及 PASS／合成成功／事务提交后的 Consolidation outbox 和后台重试均已接通。新迁移是 `0007_preference_memory`，不再按上文历史基线新增 0002。

完整 V2 回归为 222 passed（原 176 项加 46 项记忆测试）。接口与更新时机见 [偏好记忆说明](../docs/v2_preference_memory.md)，结果与未验收项见 [实施报告](../deliverables/v2-memory-20261005-report.md)。真实模型、最新部署、浏览器与人工 gold 检索质量仍需分别验收；逐 token 回复继续留待第二轮，JSON 会话迁移继续暂缓。
