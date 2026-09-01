# 开放检索与 Office P0：需求—实现—测试追踪矩阵

> 基线：2026-08-24。本文把《QA_OPEN_RESEARCH_OFFICE_P0_TEST_CASES.md》的 **116** 条编号用例作为唯一验收清单；“主链路通过”不构成完成条件。
>
> 计数已复核：`GATE-001–018` 18 条、`101–112` 12 条、`201–217` 17 条、`301–309` 9 条、`401–411` 11 条、`501–510` 10 条、`601–612` 12 条、`701–716` 16 条、`E2E-001–011` 11 条，合计 116。此前“109 条”是追踪文档计数错误，不是删减验收范围。

## 判定规则

- `已自动化（待逐条判定）`：存在对应单元、集成或 HTTP 断言；不能据此把整段范围都标为通过。
- `待测`：实现完成前或尚未有对应自动化断言，不计入通过率。
- `环境阻断`：代码可测，但缺少方案要求的稳定生产运行时/外部基础设施；不得标记为通过或可灰度。
- 每个 `BLOCK / DEGRADE / REQUIRE_CONFIRMATION` 用例必须同时验证稳定错误码、下游零副作用、审计脱敏和既有巡检零回归。

## 分组追踪

| 用例范围 | 方案要求 | 目标实现模块 | 目标自动化 | 当前状态 |
|---|---|---|---|---|
| GATE-001–018 | G0/G1 身份、开关、路由、模式锁、Query Rewrite、回退 | `agent_governance/*`、`server.py`、`open_research/intent.py` | `agent_governance_test.py`、HTTP 烟测 | 已自动化（待逐条判定） |
| GATE-101–112 | Tavily-only、出站最小化、PII/企业语义/URL 阻断、证据注入防护 | `open_research/boundary.py`、`gateway.py`、`evidence.py` | `open_research_test.py` | 已自动化；真实 Tavily 待 F2 |
| GATE-201–217 | 文件/批量限制、DLP、扫描、ACL、提取、最小模型片段 | `office_agent/policy.py`、`assets.py`、`extraction.py` | `office_agent_test.py`、HTTP 烟测 | 已自动化；生产扫描/对象存储待 F0 |
| GATE-301–309 | ResearchBrief v1 单向交接、反向 Office 出网默认拒绝、父工作流追踪 | `workflow_store.py`、`office_agent/jobs.py`、`server.py` | `open_research_office_smoke_test.py` | 已自动化（GATE-308 为后续预留） |
| GATE-401–411 | 计划上限、模板/Spec 白名单、幂等、P0 高写动作拒绝 | `planner.py`、`specs.py`、`jobs.py` | 单元与 HTTP 测试 | 已自动化（GATE-411 为后续预留） |
| GATE-501–510 | Tavily 限流/超时、Office 队列、取消/重试、隔离、40MB 资源验证 | `runtime.py`、`office_agent/jobs.py`、Worker 适配 | 单元、HTTP 边界、既有巡检回归 | 已自动化；真实隔离 Worker/压测待 F0 |
| GATE-601–612 | 证据/时效/冲突、可重开、来源和数值、PDF/PNG、失败不交付 | `evidence.py`、`specs.py`、`jobs.py`、`rendering.py` | 单元、真实渲染集成 | 已自动化；稳定 LO/字体镜像待 F0 |
| GATE-701–716 | 分层知识/会话生命周期、私有 ACL、开放检索记录页、实时重检、反馈、脱敏审计、聚合看板 | `memory.py`、`history.py`、`api.py`、`server.py`、`static/app.js` | 单元、HTTP 回归 | 701–712 已自动化（需逐条报告核验）；713–716 待开发与回归 |
| E2E-001–011 | 统一入口、浏览器状态、协同 DAG、回退、记录页与 Tavily 灰度基线 | `server.py`、`static/app.js` | HTTP/浏览器/灰度脚本 | HTTP 已自动化；E2E-006–009 已在隔离本地浏览器执行（真实 Tavily 成功态/生产 Blob 预览仍待 F2/F3）；E2E-010 Tavily 灰度、E2E-011 记录页待执行 |

## 不可降级的发布结论

1. F0 需要稳定版 LibreOffice、阿里巴巴普惠体、对象存储、病毒扫描、独立 Office Worker/队列和资源限制；当前开发机的 LibreOfficeDev 不能替代。
2. F2 需要受审批 Tavily 测试 Key 与人工标注集；Fake 仅证明协议和安全边界。
3. F3/F4 需要所有 P0 文件安全、生命周期、渲染、ACL 和 `ResearchBrief` 用例通过；Office → Research 仍必须保持关闭。

审查纠偏记录：非 Tavily 配置不再能让 `OPEN_RESEARCH` 回落到旧搜索；Query Rewrite 已改为租户级受控别名目录并保留低置信/歧义澄清；普通 DOCX 的样式 ZIP 误拦截已修复；PDF 的每一页先栅格校验后才生成 PNG 联系表。最终报告仍必须按每个编号记录 `PASS / FAIL / BLOCKED`，不用范围汇总掩盖未执行的单条用例。
