# 开放检索事件日期伪冲突修复回归报告

| 项目 | 结果 |
|---|---|
| 日期 | 2026-08-24 |
| 范围 | Open Research 的事件日期 Claim、G2R 详情回退、G6 冲突判定、历史留存与冲突态 UI |
| 线上坏例 | `《长安的离职》什么时候上映？`，Run `res_7634ad7af6c84704` |
| 结论 | 通过离线与隔离真实 Tavily 回归；运行服务已重启。 |

## 1. 缺陷与修复

真实检索源包含“7 月 13 日官宣提档至 7 月 18 日全国上映，原定 7 月 25 日”。旧实现以“日期附近出现上映词”为条件，将公告日、有效定档和旧档期混合为 `RELEASE_DATE`，使同一来源内的 7 月 13 日与 7 月 18 日被判为中国大陆冲突；CCTV 节目页日期也可能被聚合进候选。

修复后：

1. Claim 新增 `date_role`，只有正式上映和仍有效的未来定档可进入 G6。
2. 公告日、原定档、首映/点映、节目播出和页面元数据均被拒绝为最终日期。
3. 年份只能来自同一 Evidence 的发布时间或 URL，禁止同批结果/抓取时刻补年。
4. 已过期定档必须继续查找正式上映证据；同一 Evidence 多个最终值先视为抽取歧义，不能触发 `CONFLICTING`。
5. 详情读取的直通判断改为复用 Claim 语义结果；冲突态 UI 只显示“待进一步核验的来源”，不显示候选日期为核验结论。
6. 无地区的正式上映日期只能显示为“地区待确认”；仅地区明确的 `ACTUAL_RELEASE` 能进入永久事实记忆，冲突、无权威来源及无地区的部分核验 Run 均标记 `NO_MEMORY`。
7. 搜索供应商发生 `IncompleteRead` 时统一映射为 `WEB_SEARCH_UNAVAILABLE`，返回可恢复错误而非 HTTP 500。

## 2. 执行结果

| 用例 | 验收点 | 结果 |
|---|---|---|
| GATE-OR-230 | 7 月 13 日公告日不借用后文上映谓词；只保留 7 月 18 日定档线索 | 通过 |
| GATE-OR-231 | 正式上映证据优先；独立同日定档可补充中国大陆地区；单独无地区正式上映只做部分核验且不入长期记忆 | 通过 |
| GATE-OR-232 | 原定 7 月 25 日和节目播出 7 月 23 日不形成上映 Claim | 通过 |
| GATE-OR-233 | 冲突态不渲染内部候选为“核验结论” | 通过 |
| GATE-OR-201..209 | 原事件日期、来源策略、地区、冲突、隐私门禁 | 通过 |
| GATE-OR-210..229 | 声明归属、详情安全、生命周期、记录 ACL 与实时隔离 | 通过 |

执行命令：

```text
PYTHONDONTWRITEBYTECODE=1 python3 open_research_event_date_semantics_test.py
PYTHONDONTWRITEBYTECODE=1 python3 open_research_fact_gate_test.py
PYTHONDONTWRITEBYTECODE=1 python3 open_research_p05_test.py
PYTHONDONTWRITEBYTECODE=1 python3 open_research_test.py
PYTHONDONTWRITEBYTECODE=1 python3 agent_governance_test.py
```

## 3. 风险与发布建议

隔离租户真实 Tavily 回归已完成：新 Run 返回 `PARTIALLY_VERIFIED` 的 2025 年 7 月 18 日 `ACTUAL_RELEASE`，没有再纳入 7 月 13 日、7 月 25 日或 7 月 23 日的伪冲突。该次供应商返回的正式上映来源未在受限片段中直接写明地区，系统按设计展示“地区待确认”，而非推断为中国大陆；计划已增加中国电影报等审核来源的精确交叉查询，待供应商返回含地区的直接证据时即可升级为中国大陆的 `VERIFIED`。若没有正式上映证据则降级为 `NO_AUTHORITATIVE_SOURCE`，绝不重新出现伪冲突或候选日期结论。详情页仍只保留受限事实片段哈希，网页全文不落库。
