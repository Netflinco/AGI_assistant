# 开放检索事实核验修复回归报告

| 项目 | 结论 |
|---|---|
| 日期 | 2026-08-20 |
| 范围 | Open Research / Tavily 的事实核验修复；不改动 Inspection/VLM 路由与 Office 私有处理链路 |
| 目标 Query | `《长安的离职》什么时候上映？` |
| 总体结论 | 代码与无网络金样本回归通过；运行中 8000 服务必须重启后再做一次灰度 Tavily E2E |

## 1. 已修复行为

1. 同音实体自动改写为 `《长安的荔枝》`，并按 `EVENT_DATE` 走通用检索，不再使用日级新闻窗口。
2. 平台审核来源策略决定 `PUBLISHER` 等级，未审核聚合站不可形成确定日期。
3. 日期 Claim 必须由同一标题或同一摘要分句中的上映谓词支撑；标题与摘要不互相借词。
4. `首映礼`、票房、推荐阅读/页面更新时间等非上映日期被排除。
5. 年份优先级为“来源发布时间 → 已审核 URL 年份 → 同次可信证据共同年份 → 当前年”，抓取时间不再作为事件年份。
6. 只有同一**显式地区**的合格 Claim 才能触发冲突；无地区旧定档不会覆盖或否定“中国大陆”结论。

## 2. 回归明细

| 门禁/用例 | 验证点 | 结果 |
|---|---|---|
| GATE-OR-201/202 | 错别字改写、`EVENT_DATE`、中国大陆 `2025-07-18` 直接结论 | 通过 |
| GATE-OR-203 | 未审核来源含日期不得形成确定事实 | 通过 |
| GATE-OR-204/205 | 跨地区不替代；同地区不同日期降级为冲突 | 通过 |
| GATE-OR-206 | 证据/记忆不保存网页全文；用户记忆隔离 | 通过 |
| GATE-OR-209 | 旧定档 + 页面日期 + 正确内地上映日期混合摘要 | 通过：仅输出 `2025-07-18 / CN-MAINLAND` |
| 开放检索基础回归 | Tavily-only 计划、出站最小化、记忆 60 天、改写 | 通过 |
| 治理回归 | 功能开关、门禁、路由、审计、改写 | 通过 |
| 前端静态检查 | `static/app.js` 语法 | 通过 |
| GATE-OR-207/208 HTTP | 真实服务 UI/HTTP 以及 INSPECTION 零 Tavily | 待服务重启后的灰度验证；源码无巡检路由改动 |

## 3. 执行记录

```text
PYTHONDONTWRITEBYTECODE=1 python3 open_research_fact_gate_test.py
PASS GATE-OR-201..206,209

PYTHONDONTWRITEBYTECODE=1 python3 open_research_test.py
PASS open research tests

PYTHONDONTWRITEBYTECODE=1 python3 agent_governance_test.py
PASS agent governance tests

node --check static/app.js
PASS
```

## 4. 放行前动作

现运行中的 Python 服务没有热加载能力，必须重启并在隔离测试租户中用目标 Query 做一次真实 Tavily 请求。验收要求是：页面同时显示“已按《长安的荔枝》检索”、`中国大陆`、`2025 年 7 月 18 日` 和可信引用；若没有满足条件的 Claim，G6 必须继续降级而非猜测答案。巡检模式重复同一问题时，Tavily 调用数必须为 0。
