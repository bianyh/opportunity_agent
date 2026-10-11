"""User-facing stage descriptions, without guessing provider outage causes."""

def run_failure_report(state):
    """Deterministic, safe diagnostics for both live events and historical Runs."""
    if hasattr(state, "model_dump"):
        state = state.model_dump(mode="json")
    state = state or {}
    completion = state.get("completion") or {}
    research = state.get("research_result") or {}
    diagnostics = research.get("diagnostics") or {}
    progress = diagnostics.get("web_progress") or {}
    descriptions = {
        "timeout": "LLM 模型字段提取超时；官网已读取，不能视为官网不可访问。",
        "empty_content": "模型返回空正文，没有可用的结构化提取结果。",
        "truncated_output": "模型输出达到 token 上限，结构化结果不完整。",
        "official_domain_boundary": "页面或重定向目标未通过官网 HTTPS／域名边界校验，已拒绝读取。",
        "extraction_service_unavailable": "模型提取连续服务故障已触发熔断。可通过 RESEARCH_EXTRACT_MODEL 配置当前网关支持的低延迟提取模型；RESEARCH_REPAIR_MODEL 只控制修复决策。",
        "read_failure": "官网读取失败，不能将未读取的页面作为证据。",
        "search_failure": "搜索工具请求失败。",
        "extract_failure": "模型字段提取失败。",
        "budget_exhausted": "检索时间预算耗尽。",
        "other": "研究工具链未能完成该步骤。",
        "tool_budget": "研究工具次数、修复次数、单页面提取次数或单校时间限制已到达。每校最多 6 次工具、2 次修复决策，单页面最多 2 次提取；RESEARCH_PER_SCHOOL_SECONDS 只调整时间，不增加调用次数。",
        "repeated_call": "相同的失败调用或未改变档位/字段的重复提取被阻止；这是调用去重限制，不是官网连接失败。",
        "ledger_unavailable": "研究预算账本不可用，已安全停止外部调用。",
        "source_mismatch": "页面证据不满足项目、入学季或字段要求，未计作合格结果。",
        "repair_planner_failure": "修复决策模型不可用或超时，已使用安全兜底，未无限重试。",
        "scope_rejected": "搜索改写与任务范围冲突，已拒绝执行，未修改学校、项目或入学季。",
    }
    grouped, seen = {}, set()
    for error in research.get("errors", []):
        if not isinstance(error, dict):
            continue
        stage, code, reason = error.get("stage"), error.get("code"), error.get("reason")
        category = ("timeout" if stage == "extract" and code in {"TimeoutError", "MODEL_TIMEOUT"} else
            "scope_rejected" if code == "QUERY_SCOPE_REJECTED" else
            "tool_budget" if code in {"TOOL_BUDGET_EXHAUSTED", "REPAIR_BUDGET_EXHAUSTED", "SCHOOL_BUDGET_EXHAUSTED", "PAGE_EXTRACTION_LIMIT"} else
            "repeated_call" if code in {"REPEATED_FAILED_CALL", "UNCHANGED_EXTRACTION_RETRY"} else
            "ledger_unavailable" if code == "LEDGER_UNAVAILABLE" else
            "source_mismatch" if code in {"PROGRAM_MISMATCH", "INTAKE_UNSUPPORTED", "MISSING_FIELDS", "NO_SUPPORTED_FACTS"} else
            "repair_planner_failure" if stage == "repair" else
            "empty_content" if code == "EMPTY_CONTENT" else
            "official_domain_boundary" if code in {"HTTPS_REQUIRED", "OFFICIAL_DOMAIN_REJECTED", "PORT_REJECTED", "URL_CREDENTIALS_REJECTED", "PRIVATE_ADDRESS_REJECTED"} else
            "extraction_service_unavailable" if code == "EXTRACTION_CIRCUIT_OPEN" else
            reason if reason in {"empty_content", "truncated_output", "official_domain_boundary"} else
            code if code in {"extraction_service_unavailable", "budget_exhausted"} else
            "read_failure" if stage == "read_page" else "search_failure" if stage == "search" else
            "extract_failure" if stage == "extract" else "other")
        signature = (stage, code, reason, error.get("target_id"), error.get("page_id"), error.get("call_id"))
        if signature in seen:
            continue
        seen.add(signature)
        item = grouped.setdefault(category, {"code": category, "message": descriptions[category], "count": 0, "schools": []})
        item["count"] += 1
        school = (progress.get(error.get("target_id")) or {}).get("university")
        if isinstance(school, str) and school[:180] not in item["schools"]:
            item["schools"].append(school[:180])
    rounds = diagnostics.get("rounds") or [diagnostics]
    skipped = 0
    skipped_schools = []
    extraction_seconds = 0.
    for current in rounds:
        if not isinstance(current, dict):
            continue
        for entry in current.get("extraction_attempts", []):
            if not isinstance(entry, dict):
                continue
            if any(a.get("error_code") == "TimeoutError" for a in entry.get("attempts", [])):
                extraction_seconds += max(0., float(entry.get("seconds") or 0))
        for rejected in current.get("web_rejections", []):
            if not isinstance(rejected, dict) or not rejected.get("precheck"):
                continue
            skipped += 1
            school = rejected.get("university")
            if isinstance(school, str) and school[:180] not in skipped_schools:
                skipped_schools.append(school[:180])
    if skipped:
        grouped["source_precheck"] = {"code": "source_precheck", "message": "页面未通过项目身份或目标年份／学期预筛选，未调用模型提取。",
            "count": skipped, "schools": skipped_schools}
    status = completion.get("status") or ("FAIL" if state.get("error") else None)
    active = status in {"PARTIAL", "FAIL", "NEED_USER"}
    labels = {"PARTIAL": "部分完成，查询条件尚未满足", "FAIL": "执行失败", "NEED_USER": "需要补充信息"}
    ledger = diagnostics.get("tool_execution", {})
    recovered = sum(1 for call in ledger.get("calls", []) if call.get("status") == "failed") if status == "PASS" else 0
    return {"status": status, "has_issues": active, "summary": labels.get(status, ""),
        "unverified_candidates": unverified_candidates(diagnostics),
        "issues": list(grouped.values())[:20] if active else [],
        "extraction_timeout_seconds": round(extraction_seconds, 3),
        "completion_reasons": list(completion.get("reasons", []))[:10] if active else [],
        "missing_fields": sorted({f for task in completion.get("missing_tasks", []) for f in task.get("missing_fields", [])}),
        "circuit_open": "extraction_service_unavailable" in grouped,
        "tool_usage": {k: ledger[k] for k in ("tools_used", "tool_limit", "decisions_used", "decision_limit") if k in ledger},
        "recovered_failures": recovered}


def unverified_candidates(diagnostics):
    """Expose candidates separately from evidence, including legacy Run calls."""
    from .repair import safe_url
    from urllib.parse import urlsplit
    rows = []
    for current in [diagnostics, *diagnostics.get("rounds", [])]:
        if not isinstance(current, dict):
            continue
        rows.extend(current.get("unverified_candidates", []))
        rows.extend({**r, "reason": r.get("error_code", "PROGRAM_MISMATCH")}
                    for r in current.get("web_rejections", []) if isinstance(r, dict) and r.get("url"))
    calls = diagnostics.get("tool_execution", {}).get("calls", [])
    pages = {c.get("data", {}).get("page_ref"): c for c in calls
             if c.get("tool") in {"read_official_page", "extract_official_page"} and c.get("status") == "ok"}
    for call in calls:
        if call.get("error_code") not in {"PROGRAM_MISMATCH", "INTAKE_UNSUPPORTED"}:
            continue
        page = pages.get(call.get("arguments", {}).get("page_ref"))
        if page:
            target = diagnostics.get("web_progress", {}).get(call.get("target_id"), {})
            rows.append({**target, "url": page.get("data", {}).get("url", ""),
                         "reason": call["error_code"]})
    output, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = safe_url(str(row.get("url", "")))
        if urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            continue
        key = (url, row.get("program", ""))
        if key in seen:
            continue
        seen.add(key)
        reason = row.get("reason")
        output.append({"title": str(row.get("title") or url)[:200], "url": url,
            "university": str(row.get("university", ""))[:180],
            "program": str(row.get("program", ""))[:180], "counted": False,
            "label": "可能不匹配（未计入合格结果）",
            "reason": "页面入学季与目标不符" if reason == "INTAKE_UNSUPPORTED" else "未确认属于目标项目"})
    return output[:20]


def failure_summary(result):
    report = run_failure_report({"completion": {"status": "PARTIAL"},
        "research_result": result.model_dump(mode="json")})
    return list(dict.fromkeys(item["message"] for item in report["issues"]))
