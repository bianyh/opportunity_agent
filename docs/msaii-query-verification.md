# MSAII 原句查询修复与验证

## 验收问题

用户不需要修改拼写，直接发送：

```text
cmu MSAll 2027Fall的申请截止日期和GRE要求是什么
```

这次修复覆盖项目身份识别、入学季规范化、GRE 意图解析、官网发现、字段证据提取、资料缓存和前端 HTTP 请求 ID。

## 原因与处理

- `MSAll` 的两个小写 `l` 没有被识别为 `MSAII` 的两个大写 `I`，项目实体为空。新增学校范围内的项目目录和视觉混淆匹配，记录原始写法，并在答案中说明按 MSAII 查询。不会在其他学校全局替换项目名。
- `2027Fall` 被规范成只有年份的 `2027`。现在保留季节，并支持紧凑、倒序和中文季节写法。
- “GRE 要求是什么”被误识别为 GRE 必需的筛选条件。政策查询现在不加 GRE 筛选，明确的项目筛选请求仍保留约束。
- 不同官网子域名上的同一路径被错误去重，猜测出来的地址挤掉真实项目申请页。现在按主机和路径去重，并优先使用目录入口和官网实际链接；有已知项目入口时不先遍历学校级 sitemap。
- 提取上下文包含无关语言成绩和申请材料。现在优先选择包含目标字段的正文块，结构化政策查询不把营销或联系页面作为回答证据。
- 模型漏填入学季会导致已读取的 GRE 原文被拒绝。只有事实所在的同一正文块明确写出唯一且匹配的入学季时，才补齐来源元数据；其他块、用户要求的年份或冲突年份不能补齐它。随后仍经过原有身份、范围、引文和字段校验。
- 内层模型超时预留原文保留时间，字段已获得支持后停止额外读取。通过校验的官网事实可自动保存，无需人工审核。
- 非安全上下文的 LAN HTTP 没有 `crypto.randomUUID`。前端现在提供兼容的请求 ID，并覆盖聊天、画像、任务和偏好操作。

项目目录只保存项目身份和官网地址，不保存截止日期或 GRE 政策。生产答案来自官网正文；测试中的日期和原文是记录的测试夹具，不是生产规则。

## 官方来源（2026-10-12 读取）

<https://msaii.cs.cmu.edu/apply-msaii-program>

页面标题：`Apply to the MSAII Program | Carnegie Mellon University - Master of Science in Artificial Intelligence and Innovation`。

> The application system for entry in Fall 2027 is now open! The deadline to apply is December 9, 2026.

> GRE scores: GRE is required. Our Institution Code is 2074; Department Code is 0402.

页面明确支持 **2027 Fall 截止日期为 2026-12-09，GRE 必须提交**。
这些是读取当日的网页信息；如果官网后来更新，应重新读取，不能把本文当作永久政策。

## 真实前端验收

在当前本地试用环境中，使用已配置的 `gpt-6.1-sol` 网关模型，通过真实浏览器登录、新建对话、发送上述原句，检查前端回复及后台结果。没有提交模型密钥、私有环境配置、账号凭据或运行数据库。

- 官网提取后的 Run：`2f6d11dc6ccf4347b87b1fb0a350ecda`，`completion.status=PASS`，`missing_tasks=[]`，没有浏览器 JavaScript 错误。提取上下文为一个 806 字符的正文块，两个字段通过来源校验并进入正式资料库。
- 最终修改加载后再次查询的 Run：`b0f644e22e064f3caae88b585352ce5b`，`completion.status=PASS`，研究状态 `complete`，`cache_hit=true`，没有补查轮次，没有浏览器 JavaScript 错误。

第二次系统实际回复：

> 将你输入的 MSAll 按 Carnegie Mellon University 的 MSAII 项目查询。
>
> CMU MSAII（Master of Science in Artificial Intelligence and Innovation）：
>
> - 2027年秋季入学申请截止日期：2026年12月9日。官方页面写道：“The application system for entry in Fall 2027 is now open! The deadline to apply is December 9, 2026.”
> - GRE：要求提交。页面写道：“GRE scores: GRE is required.”

上述两条均附有官方申请页链接。

## 回归检查与复现

这次没有使用全量 pytest 作为验收门槛。针对性检查：

```bash
python -m pytest -q tests/test_v2_msaii_question.py tests/test_v2_research_routing_repairs.py tests/test_v2_semantic_extraction.py tests/test_v2_orchestration_skeleton.py
node --check opportunity_agent/v2/web/app.js
node tests/router_frontend_check.cjs
git diff --check
```

发布前检查结果：Python `108 passed, 1 skipped`；前端语法、请求 ID/SSE 检查及 `git diff --check` 均通过。

离线测试使用记录的响应，验证解析、检索顺序和来源约束，不代表真实模型在所有查询上的准确率。

手动复现时，在正常配置了模型和官方网页检索的 V2 服务中，通过 LAN HTTP 打开 `/v2`，登录、新建对话并发送原句。应看到项目纠错说明、两个字段及官网引用，运行状态为 `PASS`；重复发送可以使用尚未过期的正式资料缓存。

本次不涉及数据库 schema 变更。外部模型网关仍可能延迟、超时或返回 502；本修复不承诺消除提供商故障。项目目录目前仅收录本次验证的 CMU MSAII，不声称已解决所有学校的项目缩写或拼写歧义。
