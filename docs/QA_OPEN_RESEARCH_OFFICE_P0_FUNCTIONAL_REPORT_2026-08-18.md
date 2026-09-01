# 开放检索与 Office P0 功能回归报告

> 执行日期：2026-08-18  
> 代码基线：`02a8ee1` 上的未提交工作区实现  
> 结论：**本地自动化回归通过；F0–F4 均不得放行生产或灰度。**

## 1. 验收基线与范围

冻结用例按编号复核为 **111 条**，而非旧追踪文档中误写的 109 条。构成为：101 条 `GATE-*` 用例和 10 条 `E2E-*` 用例。

本次执行仅使用临时 SQLite、合成 XLSX/DOCX/PPTX、Fake Tavily/模型/扫描器和本机回环 HTTP 服务；浏览器验收使用隔离的 `localhost` 临时库与测试工作簿。未使用真实用户数据、真实 Tavily Key 或真实模型网关。

## 2. 回归结果

| 类型 | 脚本 | 结果 |
|---|---|---|
| 共享治理 | `agent_governance_test.py` | PASS |
| 开放检索 | `open_research_test.py` | PASS |
| Office 策略/资产/Job | `office_agent_test.py` | PASS |
| Office 队列恢复 | `office_worker_test.py` | PASS |
| Office 生产 fail-closed 自检 | `office_readiness_test.py` | PASS（正确拒绝未就绪生产环境） |
| 真实 Office 渲染 | `office_render_integration_test.py` | PASS（XLSX、DOCX → PPTX/PDF/PNG） |
| 40MB/120MB 流式边界 | `office_stream_boundary_test.py` | PASS |
| 跨域 HTTP | `open_research_office_smoke_test.py` | PASS |
| 既有巡检 HTTP | `smoke_test.py` | PASS |
| 既有公开检索/在线/凭证/旅行 | `web_search_test.py`、`online_agent_test.py`、`credential_vault_test.py`、`travel_enrichment_test.py` | PASS |
| 浏览器 E2E-006 | 隔离本地服务 | PARTIAL：`SEARCH_UNAVAILABLE` 卡、高置信 Query Rewrite、反馈已验证；真实 Tavily 的成功/冲突/无证据卡待 F2 |
| 浏览器 E2E-007 | 隔离本地服务 + 实际 XLSX | PARTIAL：上传、QUEUED、CANCELED、SUCCEEDED、阶段/进度、PPT/PDF/PNG 按钮与私有 PNG 路由已验证；浏览器安全策略阻止本机 Blob 预览新标签，需在目标受控浏览器复验视觉打开 |
| 浏览器 E2E-008 | 隔离本地服务 | PASS（本地）：用户切换完成后旧 Research/Office、资产、反馈和下载状态均清空；切换入口同步清屏后才启动新用户 bootstrap |
| 浏览器 E2E-009 | 隔离本地服务 | PASS（本地）：普通角色配置/审计入口不可见，聚合效果 API 返回 `403 PERMISSION_DENIED` |

已自动验证的重点包括：

- 截图 Query 的“离职 → 荔枝”高置信改写、低置信澄清、租户受控别名目录。
- Tavily-only Open Research；非 Tavily 配置不允许回落到旧搜索 Provider。
- G2R/G2O/G2H 的 PII、密钥、企业数据、内网 URL、宏、外链、压缩炸弹、DLP 与反向出网阻断。
- 60 天私有检索记忆、30 天 Office 生命周期、跨用户 ACL、删除、反馈、来源点击和脱敏聚合指标。
- Excel/Word → 默认阿里巴巴普惠体 16:9 管理层 PPT → PPTX 结构、PDF、逐页 PNG 栅格校验和联系表预览。
- Worker 并发限制、取消、失败重试、崩溃恢复、Feature Flag 关闭态，以及既有巡检/OPEN_QA 回归。

## 3. 本轮发现并修复的缺陷

| 编号 | 等级 | 问题 | 处理与回归证据 |
|---|---|---|---|
| P0-R01 | P0 | 高置信动态路由会错误接管既有“今天的天气” OPEN_QA 请求 | 路由收窄到显式联网核验、公共事件谓词或截图作品事件；`smoke_test.py` 复绿 |
| P0-R02 | P0 | 非 Tavily 配置可能让 Open Research 落到旧 OPEN_QA/Brave 路径 | Open Research 仅 Tavily 或 `SEARCH_UNAVAILABLE`；跨域 HTTP 回归覆盖 |
| P0-R03 | P0 | Query Rewrite 只有单一截图别名，无法作为受控泛化能力运营 | 增加租户级别名目录、置信度阈值、歧义/低置信澄清和管理员 ACL |
| P0-R04 | P0 | 正常 DOCX 的高压缩率样式 XML 被误判 ZIP bomb | 静态样式部件单独限制为 ≤2MB，其余用户可控分区仍执行 10:1；真实 DOCX 渲染通过 |
| P0-R05 | P1 | PNG 仅校验第一页，后页空白可能漏检 | PDF 每页先栅格、逐页非空校验，再生成单个联系表 PNG |
| P0-R06 | P0 | bootstrap 后前端会携带用户自身本地租户头，后端却要求其已有线上接入，导致本地 P0 后续 API 被误拒绝 | 仅将“请求租户 = 当前用户自身租户”留在本地 ACL 范围；其他租户仍必须通过已连接集成解析；`credential_vault_test.py` 与浏览器回归通过 |
| P0-R07 | P0 | Office Worker 完成后，会话历史中的 Job 卡仍停留在 QUEUED；取消与 PNG 预览路径不完整 | 轮询按私有 Job API 合并最新状态，展示阶段/进度/错误码，补取消与 `preview-png` 路由，失败/部分成功绝不显示正式交付按钮 |

## 4. 未执行/环境阻断

以下状态不可写为 PASS：

- **F0 BLOCKED**：当前 `soffice` 为 `LibreOfficeDev 26.8.0.0.alpha0`，无稳定镜像与已安装字体自检；仓库只含本地文件、无操作扫描和 SQLite 轮询开发适配器。生产启动会以 `OFFICE_F0_*_ADAPTER_NOT_IMPLEMENTED` fail-closed。
- **F2 BLOCKED**：没有审批的 Tavily 测试 Key、人工标注动态事实集和实际 Credits/P95 效果基线。Fake 仅证明协议、门禁和失败降级。
- **F3 BLOCKED**：依赖 F0。浏览器 E2E 已在隔离本地服务执行，但真实生产 Office 运行时、真实浏览器 Blob 预览打开和跨租户 SSO 切换尚未验证；E2E-006 的真实 Tavily 成功/冲突/无证据卡也待 F2。
- **F4 BLOCKED**：依赖 F2/F3；`ResearchBrief` 本地 HTTP 交接已通过，但不能据此开放协同灰度。
- **保留项**：GATE-308、GATE-411 是后续 Office → Research/高写能力的预留测试，不属于 P0 可开启能力。

浏览器在点击 PNG 预览时已向私有 `/preview-png` 端点发出并获得 `200` 响应；但当前自动化浏览器的安全策略阻止其继续打开本机 Blob 新标签。未绕过该策略，视觉预览页需在目标受控浏览器复验。

## 5. 发布建议

保持所有生产租户的新域 Feature Flag 默认关闭。下一步必须先交付 F0 运行时契约（稳定 LibreOffice、阿里巴巴普惠体、对象存储、病毒扫描、外部队列/Worker、资源限额和集成 Fake），再按 F1 → F2 → F3 → F4 顺序逐关验证。不得以本报告的本地绿色回归替代任一发布门禁。
