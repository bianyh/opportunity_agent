# Research 第一阶段改造与验收记录

日期：2026-10-11。范围：给定已验证官方页面，完成通用招生字段提取与验证。

## 结论与范围

已替换在线招生字段的正则优先路径，接入 Pydantic 语义抽取、独立证据验证、局部上下文选择和结果缓存。未引入新 Agent、Crawl/Map 或新框架。

**代码改造不等于线上质量验收通过。** 已配置模型的真实调用有明显超时与输出波动；不能根据离线响应桩测试声称跨学校问题已解决。新链路宁可保留缺口，也不回退到推测年份或跨项目借用证据。

## 原接口与问题定位

| 组件 | 位置 | 本次处理 |
| --- | --- | --- |
| Tavily 搜索、官网读取、SSRF/重定向校验 | `v2/research/web.py` | 搜索架构不变；HTML 表格保留同一行的单元格关系 |
| 规则任务解析、全局项目别名 | `task.py`、`identity.py`、`search_scope.py` | 不做第二阶段 Entity Resolution；增加可传入的 degree/home_unit 字段 |
| 页面处理与 LLM 调用 | `service.py::_accept_page/_extract_with_model` | 去掉在线 keyword_facts 优先及旧 supported_value 二次拦截 |
| 主要招生字段正则 | `normalization.py` | 仅保留历史导入兼容和基线测试，不再负责在线主抽取 |
| 新抽取契约、上下文准备、Verifier、缓存 | `extraction.py` | 新增 |
| 入库及 SQL 读回 | `fact_store.py`、`catalog.py` | 新证据重新执行 Verifier；保存和恢复结构化来源信息 |
| 补查工具循环、工具预算、错误码 | `tool_loop.py`、`repair.py`、`tool_ledger.py` | 保留；校验失败回到现有受控补查，熔断时不走正则捷径 |

旧链路的关键问题：

- 正则已覆盖的字段不会进入模型；其错误会优先于模型结果。
- 提示词允许依据请求入学季推算缺失年份。
- 整页匹配和日期检查无法区分兄弟项目/学位章节。
- 模型正确理解的 GRE 同义表述、文字序数日期仍可能被入库前旧正则拒绝。
- 同页矛盾值可能被覆盖，而不是保留冲突。

## 新链路行为

1. 根据标题层级建立带原文偏移的 source block；优先目标项目章节，排除明确的其他项目章节。普通上限 10,000 字符，compact 上限 4,000 字符。
2. 一次模型调用提取全部请求字段，输出值、项目、学位、Home Unit、入学季、适用人群、原文引用、block_id。日期同时输出原文 date_text；GRE 同时说明是否必须提交、是否纳入评估。

默认沿用兼容网关的 `prompt` JSON 模式，并使用 Pydantic 严格校验；不是声称所有服务端都原生支持 JSON Schema。已有 `RESEARCH_EXTRACT_OUTPUT_MODE=json_schema` 可用于支持原生结构化输出的服务端。
3. Python 检查官方域名与学校、局部项目身份、学位、Home Unit、申请季、引用位置、日期格式和范围、GRE 结构化语义一致性。缩写只允许本页有明确别名声明，不新增学校规则。
4. 日期缺年份不补全；纯数字日期存在日/月歧义则拒绝。未知适用人群、受限人群截止日期、混合项目/学位/周期作用域均不能作为通用结论。
5. 结果合并保留矛盾，同页也不覆盖；跨页同名项目对应不同 Home Unit 或不同全称时停止合并。
6. 有效结果采用进程内 TTL/LRU 缓存（15 分钟、128 项），键覆盖正文、URL、目标、字段、模型/输出模式/推理配置及上下文。命中缓存后仍重跑 Verifier；失败、空结果和校验失败不缓存。
7. 单次调用仍受原研究/学校/页面预算限制，传递传输 deadline 并增加 async timeout；不因取消等待而假设后台线程已停止，不隐式增加网络重试。
8. 工具调用、提取耗时、原文/上下文大小、缓存命中、校验失败原因继续写 diagnostics。

## 测试范围

`tests/fixtures/research_extraction_stage1.json` 是 5 所学校域名下的 **10 个合成样例**，不是这些学校真实招生政策。共 16 个应当返回的字段，覆盖：

- 英文文字序数、ISO、中文、日-月-年日期；
- 缺失年份与 copyright 年份干扰；
- HTML/Markdown 表格、标题章节、普通段落；
- MS/PhD 混合、多项目同页、不同申请季、国内/国际申请者不同日期。

额外单元测试验证：跨学校污染、别名无证据、Home Unit 冲突、编造引用、无效日期、GRE 结构矛盾、缓存键与过期重验、单次多字段调用、超时、SQL 回读、篡改后拒绝入库。

模拟响应测试只证明管道与验证器行为；**不计入真实模型准确率**。

运行记录：

- 核心相关专项：251 passed、1 skipped。
- 新增抽取专项单独运行：40 passed（包含真实调用暴露的中文 GRE 边界回归）。
- 最后局部复核：155 passed、3 skipped、1 deselected（明确排除仍失败的旧 CSE 搜索范围测试，并未修改它使其假通过）。
- 全量：713 passed、9 failed、10 skipped。9 项失败未隐藏，详见下方。
- `git diff --check` 无格式错误。

全量仍失败的测试：

1. `test_lifecycle_tools_register_with_openjiuwenrust`：当前虚拟环境缺少 openjiuwenrust。
2. `test_unknown_university_uses_safe_dynamic_domain_discovery`：动态域名缓存影响请求顺序断言。
3. `test_reextract_rebuilds_an_unconfirmed_draft_from_retained_text`：异步任务已进入 reading，与断言不符。
4. `test_experiences_and_project_count`。
5. `test_orchestrator_produces_proposal_not_direct_write`。
6. `test_http_multiturn_short_score_uses_history_and_conflict_modal`。
7. `test_one_approval_applies_profile_and_plan_then_replan_keeps_progress`。
8. `test_cse_full_name_is_not_mistaken_for_a_change_to_mscs`：现有 search_scope 仍把完整 CSE 名称误判为 MSCS。
9. `test_browser_api_flow`。

第 4–7、9 项涉及画像/规划/审批结果，本次没有更换这些模块的实现；没有据此宣称全量通过。

## 基准方法与已观察结果

先保留修改前 `_accept_page/_extract_with_model` 至 `scripts/research_legacy_baseline.py`，只供基线脚本使用。不会进入生产路径。旧、新使用相同固定页面、截止校验日期 2026-10-11、请求入学季 2027 Fall，禁用数据库和 Redis 写入；直接给页面，不统计搜索质量。

- Precision = 正确返回字段 / 全部返回字段。
- Coverage = 正确返回字段 / 应返回的 16 个字段；超时和拒绝也算覆盖率损失。
- 请求 30 秒提取预算；使用当前配置的 `gpt-5.5`，并保留服务端返回的耗时/usage。数字是小样本现场测试，不是生产 SLA。
- 旧主正则阶段（不包含模型补齐/验证）的起始基线：8/11 字段正确，precision 72.7%，coverage 50%。
- 最后冻结旧链路整体验证：10/12 字段正确，precision 83.3%，coverage 62.5%；1/10 超时，总耗时 128.685 秒，中位数 14.46 秒。
- 新链路测试过程中暴露出模型超时、scope/deadline_kind 过度 abstain，以及一次 GRE not_accepted/not_required 混淆。随后补充通用 scope 定义和 GRE 两个语义维度的一致性检查，未增加学校补丁。
- `low` 推理配置未稳定降低延迟，未改动用户 .env 或将其设为生产默认。

最后完整新链路批次：8/8 返回字段正确，precision 100%，coverage **50%**；4/10 超时，总耗时 244.758 秒，中位数 24.352 秒。此批次采用最终的通用 scope/deadline_kind 提示词，但其中文 GRE 字符边界问题在随后才修正；该修正已经通过单元回归，单例实时复测另列，不回填此完整批次的数据。

| 完整现场批次 | 正确/返回/应返回字段 | Precision | Coverage | 超时页 | 中位耗时 |
| --- | --- | --- | --- | --- | --- |
| 冻结旧实现 | 10 / 12 / 16 | 83.3% | 62.5% | 1 / 10 | 14.460 s |
| 新语义链路 | 8 / 8 / 16 | 100% | 50.0% | 4 / 10 | 24.352 s |

这不能支持“覆盖率和延迟已经改善”的结论。新版本拒绝了无年份补全和不适用人群日期，但真实调用超时仍损害覆盖率；8 个返回字段均正确也不构成普遍准确率保证。**第一阶段的稳定可靠提取验收尚未通过，不建议把当前结果直接当作可上线结论。**

新批次逐例记录（`-` 表示无字段返回，不表示事实不存在）：

| 样例 | 已接受字段 | 耗时 | 未返回原因 |
| --- | --- | --- | --- |
| us_explicit | deadline、gre_policy | 18.087 s | 无 |
| word_ordinal | deadline、gre_policy | 18.327 s | 无 |
| iso_table | - | 30.009 s | 超时 |
| chinese_date | deadline | 22.860 s | GRE 中文边界误拒绝，随后已修正 |
| missing_year | - | 30.008 s | 超时；正确行为只应返回 GRE |
| mixed_degrees | - | 30.005 s | 超时 |
| mixed_programs | deadline、gre_policy | 17.341 s | 无 |
| wrong_cycle | - | 25.844 s | 正确拒绝不匹配申请季 |
| scope_conflict | gre_policy | 22.275 s | 正确拒绝受限申请人群日期 |
| day_month | - | 30.002 s | 超时 |

中文字符边界修正后的单例实时复测：deadline 与 gre_policy **2/2 均正确**，无拒绝、无超时，19.729 秒。该单例不替换上表批次结果，也不能代表剩余超时页面已恢复。

## 本次仍未解决

- 真实模型/网关的延迟波动：短至百余字符的输入也可能超时；有预算边界不等于已解决服务可用性。
- Verifier 能验证来源、身份、作用域及格式/结构一致性，不能从程序层面证明任意自然语言语义完全正确。多维 GRE 输出仍依赖模型理解，需更多独立标注样例检验。
- 现有任务解析、搜索候选排序和搜索范围仍有全局项目别名逻辑；第二阶段才替换学校内 Entity Resolution。不新增学校缩写特例。
- 混合项目没有可靠章节边界、跨段落年份、跨页面 Home Unit、未明确声明的缩写可能被保守拒绝。
- 本次未新增 PDF 解析/OCR、Tavily Map/Crawl、链接图探索、跨进程页面缓存或语义评估 Agent。
- 旧库已有事实不会自动重新核验；本次也未迁移 `data/conversations.json`。

## 复现与启动前操作

本地环境已有 dateparser 1.4.3，项目 v2 extras 已显式声明依赖。升级部署环境：

```powershell
python -m pip install -e ".[dev,v2]"
python -m alembic upgrade head
```

新增 `0008_extraction_provenance` 仅给 `research_requirements` 增加来源 JSON 列；已测 SQLite 全新迁移与数据回读。**未对用户运行中的 PostgreSQL 执行迁移。** 不运行迁移就启动新 ORM 会因缺列报错。

Docker 测试环境可按顺序执行（数据库先备份；当前报告不建议直接宣称生产验收通过）：

```powershell
docker compose build
docker compose up -d postgres redis
docker compose run --rm --no-deps api python -m alembic upgrade head
docker compose up -d
```

离线测试：

```powershell
.\.venv-research\Scripts\python.exe -m pytest tests/test_v2_semantic_extraction.py -q
```

旧主正则基线、旧完整链路和新链路现场测试：

```powershell
.\.venv-research\Scripts\python.exe -m scripts.benchmark_extraction_stage1
.\.venv-research\Scripts\python.exe -m scripts.benchmark_extraction_stage1 --legacy-live
.\.venv-research\Scripts\python.exe -m scripts.benchmark_extraction_stage1 --live
```

`--live/--legacy-live` 会调用已配置的模型并产生费用；不查询用户资料、不保存招生事实、不执行 Tavily 搜索。脚本逐样例输出正确/错误字段、拒绝原因、耗时和模型 usage。

## 后续修订：无年份保留、候选结构与模型分工（2026-10-11）

本次没有变更 Orchestrator/Agent 框架，也没有开展 Crawl。

- `AdmissionFact.date_year_status` 区分明确年份与未知年份。没有年份的截止日期必须逐字保留 `date_text/value`，有原文定位、项目/学位/Home Unit/申请人范围约束，日期日历有效性检查，不借版权年份或目标申请季补全年份。
- 来源可确认但年份未知的日期放入 `ResearchFact(status=unknown)` 和对应证据，Synthesizer 可以引用并说明年份未确认。它不填充 `ProgramResult.deadline`、不通过 Completion Checker、不写入已核实 requirement；持久化入口独立拒绝此类日期。无年份的一般 GRE 政策继续可作为 `current_policy`，不宣称已确认某申请季。
- 页面准备使用已有 HTML 正文解析得到的标题层级、段落换行与表格行。来源块最大 1200 字符，优先段落/行边界，保留原文偏移与标题祖先；只向模型发送命中字段提示的块，项目名称本身不再拉入无关新闻。默认总输入 5000 字符，compact 2500；页面标题仅发送一次，不发送重复 offset/title 元数据。没有候选时不调用模型。仍是启发式候选筛选，不等于完备 DOM/PDF 解析，超长表格及跨块关系可能遗漏。
- `research/models.py` 分离 parse（6 秒）、discovery（10 秒）、extract、repair 客户端。优先角色级 `RESEARCH_<ROLE>_MODEL`，其次共享 `RESEARCH_FAST_MODEL`，最后兼容全局模型；本地 .env 与 Compose 都支持。Verifier 不调用模型，Synthesizer 保持原配置。本阶段未新增强模型自动复核，避免无条件增加模型调用和费用。
- 已读取现有网关模型列表。`gpt-6-luna` 在当前 extraction/effort=none 组合返回 HTTP 400，不能视为“列表存在即兼容”。本地配置改为本次实测的 `RESEARCH_EXTRACT_MODEL=gpt-5.6-luna` 和共享 `RESEARCH_FAST_MODEL=gpt-5.6-luna`；未修改密钥/全局回答模型。网关收费未核实，不承诺具体降费比例。
- 增加角色/model/block_ids 诊断、GRE 枚举空格/连字符的格式归一化（不解释招生原文），测试覆盖来源日期保留、不得计数、不得持久化、无关正文排除、模型职责独立配置。

同一 10 页面 / 16 gold 字段 Fixture 的真实模型调用对比：

| 实现/配置 | 正确字段 | 返回字段正确率 | 已核实字段覆盖率 | 超时页数 | 中位耗时 | 总耗时 |
|---|---:|---:|---:|---:|---:|---:|
| 先前旧完整链路 | 10/16 | 83.3% | 62.5% | 1/10 | 14.460s | 128.685s |
| 先前新链路实测 | 8/16 | 100% | 50% | 4/10 | 24.352s | 244.758s |
| 本次结构候选 + gpt-5.6-luna | 15/16 | 100% | 93.75% | 0/10 | 3.969s | 52.995s |

本次无年份样例保留 `February 1` 原文部分日期，正确提取 GRE，不拒绝整页。ISO 表格样例 GRE 输出未满足允许枚举，被校验拒绝，是本次唯一遗漏字段。两处受限申请人截止日期和错误申请季继续被拒绝；没有为了覆盖率放开证据校验。

上述是合成固定页面、小样本单批次结果，不是跨学校真实官网准确率或服务延迟保证。此批同时更换模型和输入准备，不能将改善全部归因于候选分段；短页之前本就很短，模型/网关性能是重要因素。旧批 4/10 超时说明先前低覆盖率主要不能归因于缺少年份。

目前 `prompt` 模式是模型生成 JSON 后由 Pydantic 验证，而不是供应商原生 strict JSON Schema 保证。只有网关确认支持时才切换 `RESEARCH_EXTRACT_OUTPUT_MODE=json_schema`。OpenAI Docs 对 JSON Schema/Pydantic 支持及 JSON mode 与严格结构输出的区别有说明：[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)。本次参考该说明保留输出模式显式配置，不假定兼容网关支持全部官方 API 功能。

本执行环境无法调用 Docker（PATH 及常见安装路径均无 docker.exe），因此仅完成本地 Python 测试和配置修改，没有构建/重启你的容器，数据库也未执行升级。

最终相关回归：11 个测试文件合并运行，**253 passed / 1 failed / 3 skipped**，22.53 秒。唯一失败为已有 `test_cse_full_name_is_not_mistaken_for_a_change_to_mscs`：`search_scope.py` 对 CSE 全称的全局项目匹配仍错误拒绝查询；本阶段没有用学校特例绕过它。新增语义抽取文件 48 个测试均通过。Compose YAML 可解析，但未能用 Docker Compose 引擎执行 config/build 验证。
