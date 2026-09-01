# 开放检索与 Office 协同 P0：开发前测试用例与门禁验证方案

| 项目 | 内容 |
|---|---|
| 版本 | v1.0（开发前测试设计） |
| 日期 | 2026-08-18 |
| 测试依据 | 《开放检索与 Office 协同统一落地蓝图 v1.0》及其已冻结 P0 决策 |
| 测试范围 | `agent_governance`、`open_research`、`office_agent`、协同 Workflow，以及对既有巡检/OPEN_QA 的零回归验证 |
| 不在本轮范围 | 实现业务代码、真实 Tavily 线上效果验收、M365/WPS/网盘/邮件/外部共享、Office → Open Research 出网 |
| 当前状态 | 用例已冻结；待功能实现后执行并形成测试报告 |

## 1. 测试目标与放行原则

本用例集验证的不只是“能否搜到、能否生成 PPT”，更验证系统是否在不应该继续时**可靠停止**。每一道 G0–G7 门禁的拒绝路径都必须有以下四个可观察断言：

```text
1. 返回稳定的 GateDecision / reason_code；
2. 下游副作用为 0（Tavily、解析器、模型网关、Office Worker、下载/共享均未调用）；
3. 仅留下脱敏的门禁与审计记录，不留下原始敏感内容；
4. 不影响既有巡检、普通 OPEN_QA 和 PDF 导出链路。
```

任何 `BLOCK`、`REQUIRE_CONFIRMATION`、`DEGRADE` 用例只断言页面提示而不检查第 2–3 项，均视为未覆盖门禁。

### 1.1 P0 测试范围与已冻结边界

| 项目 | 测试口径 |
|---|---|
| Open Research | Tavily 是唯一启用 Provider；泛化公共事实、二次取证、分层私有知识、独立检索记录页、反馈与埋点 |
| Office | Excel/Word 提取 → 管理层 PPT + PDF/PNG 预览；通用 16:9 模板、阿里巴巴普惠体 |
| 文件治理 | 单文件 ≤40MB；单批 ≤3 文件/120MB；解压 ≤250MB；原件/快照/预览/产物默认保留 30 天 |
| 数据治理 | 普通内部文档可按租户预批准策略进入已批准模型网关；密钥、Token、证件、银行卡等强敏感内容阻断；文档内容绝不发送 Tavily |
| 跨域 | P0 只允许 `Open Research → Office` 的受控 `ResearchBrief`；反向出网、外部共享和第三方 Office 连接默认关闭 |
| 巡检 | `INSPECTION` 模式、门店/摄像头/告警/巡检语义不触发 Tavily 或 Office 解析 |

## 2. 测试分层、自动化载体与隔离要求

| 层级 | 目标 | 建议载体 | 外部依赖 |
|---|---|---|---|
| 单元/契约 | 纯函数、Schema、策略、状态转换、脱敏、数据上限 | `agent_governance_test.py`、`open_research_test.py`、`office_agent_test.py` | 全部 Fake |
| 服务集成 | API、SQLite/未来数据库、ACL、审计、Job/队列适配 | 临时 DB + FakeObjectStorage/FakeQueue/FakeScanner | 不联网 |
| 端到端烟测 | 统一消息入口、域路由、跨域 DAG、下载和既有链路回归 | `open_research_office_smoke_test.py` | 不联网 |
| Worker/渲染集成 | XLSX/DOCX 提取、PPTX 生成、稳定 LibreOffice 渲染 | 独立 Office 运行时容器 | 本地受控运行时 |
| 灰度验收 | Tavily 覆盖、真实样稿版式、配额与反馈 | 隔离测试租户与测试 Key | 受审批的 Tavily |

CI 禁止真实 Tavily、真实模型网关和真实企业文档。真实 Provider 仅在灰度验收环境按测试 Key、测试租户和额度上限运行，输出不得回写测试外的记忆或反馈数据。

### 2.1 推荐测试文件与职责

```text
agent_governance_test.py             # GateContext/GateDecision、G0-G7 顺序、开关、审计、幂等
open_research_test.py                # 意图、Query、Tavily Gateway、证据、记忆、反馈
office_agent_test.py                 # 资产、文件安全、提取、Spec、生成、渲染、生命周期
open_research_office_smoke_test.py   # HTTP 入口、协同 DAG、ACL、巡检零回归
tests/fixtures/
  research_packets.py                # 官方/冲突/过期/注入型 EvidencePacket
  office_fixture_factory.py          # 临时生成 XLSX/DOCX/PPTX 与安全边界样本
  fake_services.py                   # Tavily/模型网关/对象存储/病毒扫描/队列/Worker Spy
```

现有 `smoke_test.py`、`online_agent_test.py`、`web_search_test.py`、`credential_vault_test.py` 保持不改；新增烟测必须在这些原有脚本均通过后才执行。

## 3. 通用夹具、Spy 与统一断言

### 3.1 固定测试身份与资源

| 名称 | 身份/数据 | 用途 |
|---|---|---|
| `u_research_a` | 租户 `tenant_a` 的普通用户 | 正常检索、Office 私有资产、记忆所有者 |
| `u_research_b` | 同租户另一普通用户 | 用户级隔离、猜测 ID 访问 |
| `u_admin_a` | `tenant_a` 租户管理员 | 开关/模板/聚合看板配置 |
| `u_other_tenant` | `tenant_b` 普通用户 | 跨租户隔离 |
| `conv_a` / `conv_b` | 分别归属用户 A/B 的会话 | 会话 ACL 和上下文边界 |
| `xlsx_normal` | 3 工作表、100 行、含 KPI 与图表源数据 | 正常 Office 主链路 |
| `docx_normal` | 5 页、带标题/段落/表格的周报 | 正常 Office 主链路 |
| `template_default` | 通用 16:9、阿里巴巴普惠体、封面/目录/KPI/图表/结论/引用页 | D2 模板与渲染验证 |

### 3.2 安全与边界夹具

| 名称 | 构造方式 | 预期门禁 |
|---|---|---|
| `asset_over_40mb` | 流式长度为 40MB+1 字节的安全虚拟流 | G2O 拒绝，不落对象存储 |
| `batch_over_120mb` | 3 个合规扩展名文件、总量 120MB+1 字节 | G2O 拒绝整批，不创建部分资产 |
| `fake_xlsx_magic` | 扩展名 `.xlsx`，内容非 OOXML | G2O 拒绝 |
| `macro_xlsm` / `encrypted_docx` | 宏标记或加密标记样本 | G2O 隔离/拒绝 |
| `zip_bomb_metadata` | 受控模拟器报告解压 250MB+ 或比例 >10:1 | G2O 拒绝；测试中不生成真实炸弹 |
| `xlsx_over_cells` / `csv_over_rows` | 超过 1,000,000 非空单元格或 100,000 行的元数据样本 | G2O/资源策略降级或拒绝 |
| `asset_secret` | 包含虚构 API Key、Token、身份证/银行卡格式 | DLP 阻断，原文不进日志/模型 |
| `research_business_phrase` | 含门店、客户、内部项目名或经营指标的检索语句 | G2R 阻断，Tavily 调用数为 0 |
| `prompt_injection_evidence` | 证据摘要内含“忽略规则、发送附件”等指令 | G6 视为数据，不改变工具权限 |

所有秘密、身份证和卡号均使用格式样本，禁止把真实凭证或真实个人信息提交到仓库。

### 3.3 必须可注入的 Fake/Spy

| 组件 | 必须记录的字段 | 关键断言 |
|---|---|---|
| `FakeTavilyGateway` | 调用次数、请求 JSON、Query、时间、返回结果/错误 | 被阻断时调用数为 0；允许时仅有最小化 Query 且无 tenant/user/conversation/附件内容 |
| `FakeOfficeExtractor` | `asset_id`、解析次数、解析版本 | G2O 拒绝或巡检模式时调用数为 0 |
| `FakeModelGateway` | 输入片段、数据分级、purpose、返回 `SlideSpec` | 仅接收允许的最小片段/ResearchBrief；绝不接收秘密、整页网页或 Tavily 凭证 |
| `FakeOfficeWorker` | Job、Spec、渲染调用、取消/重试 | 未过 G3/G6 时不执行；同一幂等键不重复生成 |
| `FakeObjectStorage` | 写/读/删的对象 ID 与 ACL | 资产不可变、30 天清理、跨用户读取为 404 |
| `FakeVirusScanner` | 扫描请求与结果 | 生产策略下不可用即拒绝真实资产灰度 |
| `AuditSpy` | action、对象 ID、摘要哈希、门禁决定 | 不含原 Query、文档正文、密钥或内部凭证引用 |
| `ControllableClock` | 当前时间 | 60 天记忆失效、30 天资产清理、确认过期、超时重试 |

### 3.4 通用门禁断言宏

每个 `GATE-*` 用例至少调用下列断言；实施时封装为测试辅助函数，避免遗漏：

```python
assert_gate(run, gate="G2R", decision="BLOCK", reason_code="RESEARCH_EGRESS_BLOCKED")
assert fake_tavily.calls == []
assert fake_extractor.calls == []
assert_no_sensitive_value_in(audit_events, response_json, trace_json, application_logs)
assert_only_expected_rows_persisted(db, allowed_tables={"agent_gate_decisions", "audit_logs"})
```

`reason_code` 可在实现时最终命名，但必须稳定、可枚举，并同时更新后端、前端映射和本用例；禁止仅以自由文本判断失败原因。

## 4. 开关、身份与路由（G0 / G1）

| 编号 | 层级 | 前置/输入 | 操作 | 期望断言 |
|---|---|---|---|---|
| GATE-001 | API | 未认证请求 | 调用新域任一 API | 401；无 Run/Job/审计业务副作用 |
| GATE-002 | API | `u_research_b` | 读取 `u_research_a` 的 Research Run/记忆/Office Asset | 404 或权限拒绝；不泄露资源存在性 |
| GATE-003 | API | `u_other_tenant` | 猜测 `tenant_a` 的资源 ID、下载地址或 Workflow ID | 404；对象存储读次数为 0 |
| GATE-004 | 集成 | 所有新 Feature Flag 关闭 | 发送既有巡检、普通 OPEN_QA、PDF 导出请求 | 原有脚本断言全部保持；新域表/Worker/Tavily 调用均为 0 |
| GATE-005 | 单元/API | `open_research_enabled=false` | 发送高时效开放问题 | 返回 `FEATURE_DISABLED` 的明确提示；无 Tavily 调用 |
| GATE-006 | 单元/API | `office_enabled=false` | 上传合格 Office 文件或发起 PPT 任务 | 返回 `FEATURE_DISABLED`；不创建资产/Job |
| GATE-007 | 集成 | 用户/租户达到检索或 Job 速率上限 | 再次发起请求 | 限流响应；无新 Provider/Worker 调用；限流事件审计 |
| GATE-008 | 单元 | `mode_override=INSPECTION` | 问“《长安的离职》何时上映？” | 固定走既有巡检路径或可理解拒绝；Tavily=0、Office=0 |
| GATE-009 | 单元 | `AUTO` | 问“广州门店最新告警怎样？” | 命中巡检域；Tavily=0、Office=0 |
| GATE-010 | 单元 | `AUTO` | 问“《长安的离职》什么时候上映？” | 命中 `OPEN_RESEARCH/EVENT_STATUS`，`evidence_required=true`，不是裸 `OPEN_QA` |
| GATE-011 | 单元 | `AUTO` | “把这份周报做成 PPT”+`docx_normal` | 命中 Office；仅在 G2O 放行后创建 Job |
| GATE-012 | 单元 | `AUTO` | “查最新政策并做 PPT” | 识别为协同 Workflow；Research 在前，Office 不可先执行 |
| GATE-013 | API | 租户接入/密钥文本 | 同时出现“接入”“AppSecret”“做 PPT” | 现有接入密钥处理优先；密钥脱敏；新域调用均为 0 |
| GATE-014 | API | 已有开放问答 PDF | “导出上一轮 PDF”且无附件/PPT 目标 | 保留既有 PDF 跟进；不创建 Office Job |
| GATE-015 | 单元 | 低置信、无动态谓词 | “帮我写一段欢迎词” | 原 OPEN_QA/写作路径；Tavily=0 |
| GATE-016 | 单元 | Office 和检索词同时出现但无依赖 | “这份报告和最新政策帮我看看” | 不猜测跨域关系；返回澄清卡；两类外部副作用均为 0 |
| GATE-017 | 单元/集成 | 用户问《长安的离职》；实体改写词典/模型候选返回《长安的荔枝》，置信度高于自动改写阈值 | 执行实体解析与检索计划 | 产生 `HOMOPHONIC_TYPO` 改写记录；原问题保留；Tavily Query 使用《长安的荔枝》+上映/影视化；Trace 含原/改写 Query、置信度和改写原因 |
| GATE-018 | 单元 | 实体候选置信度低于自动改写阈值，或存在同分候选 | 执行实体解析与检索计划 | 不自动替换原题；保留原 Query 并返回澄清候选；不能将任一候选当作确定事实 |

## 5. 公开检索出站、Provider 与不可信证据（G2R）

| 编号 | 层级 | 前置/输入 | 操作 | 期望断言 |
|---|---|---|---|---|
| GATE-101 | 集成 | 合法公共事实 | 执行《长安的离职》上映时间查询 | 至少生成《长安的荔枝》+上映/影视化的改写 Query 与谓词 Query；最多 3 条；请求经 `FakeTavilyGateway`；原 Query/改写 Query 均可追溯 |
| GATE-102 | 契约 | GATE-101 | 检查 Tavily 请求体与 Header | 仅含 Query、语言/地域、来源/时效策略和请求匿名 ID；无 tenant/user/conversation/附件/密钥 |
| GATE-103 | 单元 | 含 API Key/Token 的问题 | 发起检索 | G2R `BLOCK`；Provider 调用=0；所有日志脱敏 |
| GATE-104 | 单元 | 含手机号、邮箱、住址 | 发起检索 | G2R `BLOCK`；不尝试自动改写后发送 |
| GATE-105 | 单元 | 门店、摄像头、告警、客户、内部项目或原始经营指标 | 发起检索 | G2R `BLOCK`；Tavily=0；Trace 不写原文 |
| GATE-106 | 集成 | `xlsx_normal` 已上传 | “用这份 Excel 查竞品” | 原始单元格、资产 ID、会话 ID 均不出站；反向出网关闭时 Tavily=0 |
| GATE-107 | 契约 | Provider 返回私网 URL、带凭证 URL、HTTP URL、低相关结果 | 证据标准化 | 不合格链接全部丢弃，不能成为引用或后续抓取目标 |
| GATE-108 | 单元 | `prompt_injection_evidence` | 证据复核与整合 | 注入文本只作为不可信数据或被丢弃；不新增工具权限、不读取资产、不改变系统指令 |
| GATE-109 | 单元 | 用户提供任意内网/文件 URL | 要求“读取这个链接后回答” | 不按 URL 直接抓取；仅允许受控 SearchGateway 的公开结果策略 |
| GATE-110 | 契约 | Tavily 配置可用 | 检查 Provider 选择 | P0 请求均为 Tavily；Brave 分支和未审批 Provider 调用数为 0 |
| GATE-111 | API | 管理员配置/查看 Tavily | 读取配置和审计 | API、Trace、审计不返回 API Key、密文或凭证指纹；非管理员拒绝 |
| GATE-112 | 集成 | Query 被拒绝或 Provider 空结果 | 检查数据库 | 不创建可复用事实记忆；只保留脱敏 Gate/Audit 必要记录 |

## 6. Office 资产、模型数据与文件安全（G2O）

| 编号 | 层级 | 前置/输入 | 操作 | 期望断言 |
|---|---|---|---|---|
| GATE-201 | API | `xlsx_normal` / `docx_normal` | 上传 | 返回私有 `asset_id`、检测 MIME、SHA-256；原始二进制不写普通日志 |
| GATE-202 | 单元/API | `fake_xlsx_magic` | 上传 | 检测扩展名/魔数/解析器不一致；拒绝且不入对象存储 |
| GATE-203 | 单元/API | `macro_xlsm` | 上传 | 拒绝或隔离；Extractor/Model/Worker 调用均为 0 |
| GATE-204 | 单元/API | `encrypted_docx`、损坏 ZIP、OLE/外链/DDE 样本 | 上传 | 拒绝并记录枚举化原因；不得尝试执行内容 |
| GATE-205 | API | 恰好 40MB 的合规文件与 40MB+1 字节的 `asset_over_40mb` | 分别流式上传 | 前者可登记资产；后者在对象存储完成写入前拒绝；无 Job；响应为稳定超限码 |
| GATE-206 | API | 恰好 3 个合规文件/总量 120MB，与第 4 个文件或 `batch_over_120mb` | 分别提交批量上传 | 前者可完整登记；后者整批原子拒绝；不得留下前几个资产或部分 Job |
| GATE-207 | 单元 | `zip_bomb_metadata` | 安全检查 | 解压大小/比例超限即拒绝；不得实际解压到工作目录 |
| GATE-208 | 单元 | `xlsx_over_cells` / `csv_over_rows` | 提取与编排 | 超限不得进入模型/PPT 编排；若支持摘要降级，结果必须标明范围和降级原因 |
| GATE-209 | 单元 | 201 页 Word、101 页 PPT、101 张图片 | 文件检查 | 返回 `OFFICE_CONTENT_LIMIT_EXCEEDED`；Worker=0 |
| GATE-210 | 单元 | CSV 单元格以 `=`, `+`, `-`, `@` 开头 | 导出/生成 | 公式字符已转义，生成 Excel 打开时不执行公式 |
| GATE-211 | 单元/API | `asset_secret` | 上传、提取、生成请求 | 强敏感识别后阻断；对象存储/模型/Tavily/Office Worker=0；日志无原值 |
| GATE-212 | 集成 | 普通内部 `xlsx_normal` | 生成 PPT | `FakeModelGateway` 仅收到任务相关的最小表格/段落片段、数据级别和 purpose；不接收完整资产、无关工作表或 Tavily 凭证 |
| GATE-213 | API | `u_research_a` 上传资产 | `u_research_b` 发起 extract/generate/download | 404；Extractor/Worker 不被调用；下载对象不读取 |
| GATE-214 | 集成 | `u_research_a` 的正常资产 | 提取、生成后比对 SHA-256 | 原件哈希与版本不变；所有产物为新的 `artifact_version`，有父子来源 |
| GATE-215 | 集成 | 真实资产灰度配置 | 病毒扫描不可用/返回感染 | 未通过扫描不进入提取；生产灰度拒绝，审计不含文件正文 |
| GATE-216 | 单元 | `INSPECTION` 模式携带 Office 附件 | 发送消息 | 不读附件、不创建资产解析 Job；提示用户切换到允许的模式 |
| GATE-217 | 集成 | 普通内部 `xlsx_normal`，分别开启/关闭租户的 `office_model_processing_enabled` | 请求自动编排 PPT | 开启时仅最小片段可进入 Model Gateway；关闭时不调用模型，返回可理解限制或只允许确定性提取 |

## 7. 跨域数据流门（G2H）

| 编号 | 层级 | 前置/输入 | 操作 | 期望断言 |
|---|---|---|---|---|
| GATE-301 | 集成 | `research_to_office_enabled=false` | “查政策并做 PPT” | Research 可独立结束；Office Job 不创建；无 `ResearchBrief` 消费记录 |
| GATE-302 | 集成 | 开启 `research_to_office_enabled`，同一用户/租户 | `VERIFIED` Research Run → PPT | Office 仅收到 `ResearchBrief v1`；不收到原 HTML、Tavily 原响应、检索记忆或 Provider 凭证 |
| GATE-303 | 单元 | `ResearchBrief` 缺 `as_of`、引用或 claim/evidence 关联 | 交给 Office | G2H/G3 拒绝；Office Worker=0 |
| GATE-304 | 集成 | `PARTIALLY_VERIFIED` Brief | 生成 PPT | 每个未完全核验主张均带“待核验/截至时间”；引用页保留，不得改写为确定事实 |
| GATE-305 | 单元 | `CONFLICTING`、`NO_AUTHORITATIVE_SOURCE`、`SEARCH_UNAVAILABLE` | 请求事实性 PPT | P0 阻断下游 Office；不生成“看似事实”的汇报 |
| GATE-306 | API | `u_research_b` 的 Brief ID | `u_research_a` 请求生成 | 404；不泄露 Brief/Run 存在性 |
| GATE-307 | 集成 | `office_to_research_egress_enabled=false` + 经营 Excel | “搜索竞品” | 返回限制说明；Tavily=0；资产正文/字段不外发 |
| GATE-308 | 预留回归 | 后续开启反向出网 | 展示 Query 后未确认、确认后改 Query、确认过期 | 未确认/变更/过期均 Tavily=0；仅确认的精确 Query 可出站；资产永不出站 |
| GATE-309 | 集成 | Research → Office 完成 | 审核 Trace/审计 | 父 Workflow、子 Run、Brief、Job 的 ID 可关联；记录只含摘要哈希/来源 ID，不含原文 |

## 8. 计划、权限、确认与幂等（G3 / G4）

| 编号 | 层级 | 前置/输入 | 操作 | 期望断言 |
|---|---|---|---|---|
| GATE-401 | 单元 | 合法 `EVENT_STATUS` 问题 | 生成检索计划 | Query 数为 1–3；每条有目的、时效和早停策略；不得无限扩展 |
| GATE-402 | 单元 | 历史慢变知识命中职位，或历史 `NO_MEMORY` 记录命中价格/天气 | 生成计划 | 慢变知识仅可按策略提供实体别名/官方域名并强制新 Query；高时效记录只可继承实体等非事实槽位，旧事实、引用和证据均不可进入计划 |
| GATE-403 | 单元 | 未过期稳定事实精确命中 | 回答 | 允许按策略复用，回答标注上次核验时间与 `reuse_mode` |
| GATE-404 | 单元 | 未授权模板/不存在模板 | 创建 PPT Job | 返回权限/资源错误；不调用生成器 |
| GATE-405 | 单元 | 模型返回非法 `SlideSpec`（任意 URL、任意路径、未允许图表） | Spec 校验 | G3 拒绝；Worker=0；记录 Schema 失败码 |
| GATE-406 | 单元 | 合法 `SlideSpec` | 检查输入 | 只包含白名单版式、文本、图表、来源与 `asset_id`/Brief，不含 shell、SQL、文件路径或云凭证 |
| GATE-407 | API | 同一用户、相同资产哈希、Spec 哈希、动作 | 连续创建两次 Job | 返回同一 Job/产物或 `deduped=true`；模型和 Worker 只执行一次 |
| GATE-408 | API | 同一输入但不同模板/资产/用户 | 创建 Job | 不得错误命中幂等；各自独立 Job 和 ACL |
| GATE-409 | 单元/API | P0 生成新私有产物 | 执行 Job | `TRANSIENT_SESSION` 自动执行，无覆盖原件行为 |
| GATE-410 | API | 覆盖、外部共享、邮件/M365/WPS/数据库写回请求 | 尝试调用对应 API/工具 | P0 Feature Flag/工具白名单拒绝；没有实际写入或外发 |
| GATE-411 | 预留回归 | 后续 `HIGH_WRITE` 功能 | 无确认、确认过期、接收人/版本变化、重复确认 | 未确认不执行；确认只绑定摘要/接收方/版本/有效期；重复请求幂等 |

## 9. 运行时、异常与资源隔离（G5）

| 编号 | 层级 | 前置/输入 | 操作 | 期望断言 |
|---|---|---|---|---|
| GATE-501 | 集成 | Tavily 超时 | 执行开放检索 | `SEARCH_UNAVAILABLE` 或受控重试后失败；不返回模型臆断；Office/巡检不受影响 |
| GATE-502 | 集成 | Tavily 429/余额耗尽 | 连续检索 | 输出配额/限流状态；不无限重试、不产生额外费用事件 |
| GATE-503 | 单元 | Tavily 返回空结果 | 查询动态事实 | `NO_AUTHORITATIVE_SOURCE`，不归档可复用确定事实 |
| GATE-504 | 集成 | Worker 队列已满 | 创建 Office Job | Job 可见地 `QUEUED`/受限失败；消息入口不超时；巡检请求仍成功 |
| GATE-505 | 集成 | 同一用户已有重 Office Job | 再提交重任务 | 排队/拒绝遵循配额；不并发占用第二个重 Worker |
| GATE-506 | 集成 | 解析、生成或渲染超时 | 执行 Job | 稳定失败码、可重试状态与审计；不交付半成品 |
| GATE-507 | API | Job 处于 `QUEUED/RUNNING` | 用户取消，再尝试重试 | 取消后 Worker 收到取消信号；已完成步骤可追溯；重试遵守幂等/版本规则 |
| GATE-508 | 集成 | Office Worker 崩溃后恢复 | 重启 Worker | Job 仅重试允许的步骤；不重复生成/覆盖产物；状态机合法 |
| GATE-509 | 性能 | 40MB 合规文件、3 文件/120MB 批次 | 安全检查、提取、生成、渲染 | Web 请求流式处理；峰值资源不占巡检进程；超时/队列指标与 `workflow_id` 可关联 |
| GATE-510 | 回归 | 触发 GATE-501/504/506 | 执行既有巡检 smoke case | 巡检响应、PaaS 调用和既有 `smoke_test.py` 结果无回归 |

## 10. 证据、生成质量与失败交付（G6）

| 编号 | 层级 | 前置/输入 | 操作 | 期望断言 |
|---|---|---|---|---|
| GATE-601 | 单元 | 合格官方证据 | 生成 ResearchAnswer | 关键 claim 均关联 `evidence_id`，包含 URL、来源等级、抓取/发布时间和 `as_of` |
| GATE-602 | 单元 | 截图 Case 经“离职→荔枝”改写后，无影视官方来源，仅有小说/音频结果 | 回答 | `NO_AUTHORITATIVE_SOURCE` 或明确形态差异；结果展示改写后的检索实体；不得断言“不是电影”或“绝无影视化” |
| GATE-603 | 单元 | 两个相互冲突的合格来源 | 证据整合 | `CONFLICTING` 或明确采用更新的一手来源；不由模型静默猜选 |
| GATE-604 | 单元 | 过期 `LIVE/HOUR/DAY/EVENT/VERSION` 证据 | 回答/记忆命中 | 证据不可直接支撑当前结论，必须刷新或标注不可用 |
| GATE-605 | 单元 | 无合格证据 | 整合回答 | 无证据确定性事实主张数=0；这是发布硬指标 |
| GATE-606 | 集成 | 正常 XLSX/DOCX → PPT | 结构校验 | PPTX 可由独立库重新打开；页数、标题、图表与 `SlideSpec` 一致 |
| GATE-607 | 集成 | 含 KPI/公式源数据 | 生成 PPT | PPT/预览中的关键数值与确定性计算、源单元格/段落定位一致；模型不能自行改数 |
| GATE-608 | Worker | 正常 PPTX | LibreOffice 渲染 | 得到 PDF/PNG 预览；页数一致；无空白页、图片缺失、明显溢出/越界 |
| GATE-609 | Worker | 模拟渲染失败/字体缺失 | 交付 | 状态为 `REVIEW_REQUIRED/FAILED`；不显示正式下载；保留错误码与最小诊断 |
| GATE-610 | 集成 | 合格 ResearchBrief → PPT | 审核产物 | 每个研究事实在页脚或引用页显示来源、`as_of`、证据状态；不能删去引用 |
| GATE-611 | 单元 | 模型生成无来源或不受源支持的结论 | Spec/语义校验 | 标为待复核或拒绝；不以“生成成功”交付 |
| GATE-612 | API | `PARTIAL` Job | 请求预览/下载 | 仅展示明确降级项；正式下载仅对 `SUCCEEDED` 可用 |

## 11. 交付、记忆、删除、反馈与审计（G7）

| 编号 | 层级 | 前置/输入 | 操作 | 期望断言 |
|---|---|---|---|---|
| GATE-701 | 集成 | `u_research_a` 已完成永久、慢变和高时效三类检索 | 查看记忆 | 只返回该用户、该租户、未删除且有效的 `PERMANENT_FACT` 与未过 60 天的 `SLOW_60D` 索引；`NO_MEMORY` 为 0 命中；不含网页全文 |
| GATE-702 | 集成 | `u_research_b`/其他租户 | 查询同实体记忆 | 0 命中，不能以公共事实为由共享用户 Query/反馈/记忆 |
| GATE-703 | 单元 | `ControllableClock` 推进超过 60 天 | 再次查询 | `SLOW_60D` 不可召回，仍有效的 `PERMANENT_FACT` 可召回；动态/高时效事实一律重新检索 |
| GATE-704 | API | 用户删除自己的检索记忆 | 再次追问/直接读 ID | 索引软删除、后续不可召回/读取；删除审计存在 |
| GATE-705 | API | `u_research_a` 的 `SUCCEEDED` 产物 | A 下载/预览 | ACL 校验通过；临时链接私有、响应 `Cache-Control: private, no-store` |
| GATE-706 | API | 同一产物 | B/其他租户下载、预览、猜测 URL | 404；对象存储数据不读出 |
| GATE-707 | 集成 | 时钟推进 30 天 | 运行清理任务 | 原件、提取快照、预览和产物均删除/不可访问；关联 Job/审计保留最小元数据，不保留二进制 |
| GATE-708 | API | 资产或记忆已删除/过期 | 创建新 Job/复用 Brief | 拒绝；不从缓存或 Trace 恢复内容 |
| GATE-709 | API | `VERIFIED`、`NO_AUTHORITATIVE_SOURCE`、`SEARCH_UNAVAILABLE` 回答 | 提交不同类型反馈 | 反馈绑定正确 Run/Workflow，系统故障不被计为“内容错误” |
| GATE-710 | API | Office/PPT 交付 | 提交版式、数据、来源、文件打不开反馈 | 反馈按域分类，关联 Job/产物版本；不自动改写事实或模板 |
| GATE-711 | 集成 | 全部敏感/成功/失败链路 | 审核审计、Trace、日志和埋点 | 覆盖路由、门禁、检索、提取、生成、下载、删除、反馈；无秘密、原 Query、网页全文或文档正文 |
| GATE-712 | API | 租户管理员 | 查看效果看板 | 仅能看到脱敏聚合指标；不能读取普通用户的私有 Query、记忆或 Office 正文 |
| GATE-713 | API/集成 | `u_research_a` 有稳定与 `NO_MEMORY` 两类完成 Run | 打开“开放检索记录”列表并读取详情 | 仅返回 A 自己的 Run；列表字段、详情最终回答/引用/截至时间/已采用限长证据与原会话一致；实时记录存在于记录页但不存在 `memory_index` |
| GATE-714 | API | `u_research_b`、其他租户用户、普通租户管理员 | 访问 A 的记录列表、猜测 A 的 `run_id`、请求详情 | 列表为空或详情 404；不组装证据、不读取对象；管理员仅可读取聚合看板，不能读取个人 Query、回答、引用或证据片段 |
| GATE-715 | 单元/API | 含详情取证、`SECONDARY` 线索、最终未采用候选的 Run | 检查记录列表、详情 DTO、前端缓存和审计 | 只出现最终采用的单来源 ≤300 字净化窗口；网页全文、HTML、Tavily 原始响应、未采用候选、内部 Trace 和模型输入均不存在 |
| GATE-716 | 集成/浏览器 | 历史天气/价格/余票 `NO_MEMORY` 记录 | 在记录详情点“重新检索”，并发送“那现在呢” | 两条路径均生成新 `run_id`、`force_fresh=true` 和新的 Provider 调用；旧值/旧引用/旧 Claim 不在新模型输入、计划或答案中；旧记录仍可回看 |

## 12. 端到端业务链路与前端验收

| 编号 | 层级 | 场景 | 操作 | 期望结果 |
|---|---|---|---|---|
| E2E-001 | HTTP 烟测 | 截图同类动态事实与 Query Rewrite | 发送“《长安的离职》什么时候上映？” | Route→实体改写“离职→荔枝”→Memory→Tavily Fake→Evidence→Answer→Trace→Memory；原/改写 Query、引用与状态正确 |
| E2E-002 | HTTP 烟测 | 正常 Office 主链路 | 上传 `xlsx_normal` 与 `docx_normal`，发送“整理成管理层 PPT” | Asset→Extract→最小模型片段→SlideSpec→Generate→Render→私有下载；原件不变 |
| E2E-003 | HTTP 烟测 | 已核验研究制成 PPT | “查最新政策并做 PPT” | 先 Research、后 Brief、后 Office；PPT 含引用和截至时间；Workflow 子步骤可追溯 |
| E2E-004 | HTTP 烟测 | 反向出网阻断 | 上传内部经营 Excel，发送“搜索竞品后做 PPT” | G2H 阻断；Tavily=0；解释不能发送文档；无 Office 产物 |
| E2E-005 | HTTP 烟测 | Feature Flag 回退 | 逐个关闭 Research/Office/协同开关 | 请求按明确原因降级；既有巡检、OPEN_QA、PDF、Plan 确认均回归通过 |
| E2E-006 | 浏览器 | 检索回答状态卡 | 打开成功/冲突/无证据/服务不可用回答 | 显示状态、截至时间、引用、反馈与重试；不显示敏感 Query/内部字段 |
| E2E-007 | 浏览器 | Office Job/产物卡 | 上传、排队、失败、成功、取消 | 显示阶段/进度/错误码/预览/下载；`PARTIAL/FAILED` 不显示正式下载 |
| E2E-008 | 浏览器 | 租户/用户切换 | 查看 A 的检索/Office 结果后切换 B | 清除旧 Run、资产、预览、反馈和下载状态；不能短暂闪现 A 数据 |
| E2E-009 | 浏览器 | 权限与关闭态 | 普通用户进入配置/聚合看板、尝试外发 | 按钮和 API 均拒绝；P0 无外发入口 |
| E2E-010 | 灰度 | Tavily 效果基线 | 运行经人工标注的动态事实集 | 统计意图漏检、证据交付、无证据断言、来源可验证、P95 与 Credits；未达阈值不得扩大灰度 |
| E2E-011 | 浏览器 | 开放检索记录页 | 从完成的检索回答进入独立记录页，筛选、展开详情、回跳原会话并重新检索；随后切换租户 | 本人记录可分页回看；实时卡明确提示重检；新旧 Run 不同；切换租户后列表、详情、游标、筛选和缓存均清空，不闪现旧租户数据 |

## 13. 自动化覆盖映射与发布门槛

### 13.1 最小自动化集合

| 脚本/层级 | 最少覆盖的用例 |
|---|---|
| `agent_governance_test.py` | GATE-001–018、GATE-401、405–411、GATE-711 |
| `open_research_test.py` | GATE-017–018、GATE-101–112、GATE-401–403、GATE-501–503、GATE-601–605、GATE-701–704、709、712–716 |
| `office_agent_test.py` | GATE-201–217、GATE-404–410、GATE-504–509、GATE-606–612、GATE-705–708、710 |
| `open_research_office_smoke_test.py` | GATE-002–004、009–014、106、212–215、301–307、407–410、501/504/506/510、610、705–716、E2E-001–005 |
| 既有脚本 | `python3 web_search_test.py`、`python3 online_agent_test.py`、`python3 credential_vault_test.py`、`python3 smoke_test.py` |

### 13.2 F0–F4 发布前的测试门槛

| 关口 | 必过用例 | 放行标准 |
|---|---|---|
| F0 运行时冻结 | GATE-201–211、215、217、504–509、606–609 | Office 安全夹具、稳定版 LibreOffice 与字体镜像均通过；40MB 边界不拖垮 Web/巡检进程 |
| F1 共享底座 | GATE-001–018、401–411、711、E2E-005 | 关闭开关时原有脚本 100% 通过；所有拒绝路径有决策、零副作用和审计 |
| F2 Open Research P0.5 灰度 | GATE-017–018、101–112、501–503、601–605、701–704、709、712–716、E2E-001/010/011 | 高置信改写命中率可观测；低置信误改写=0；无证据确定性事实=0；巡检触网=0；跨用户知识/记录读取=0；实时历史追问强制新 Run；Tavily 测试集可追踪 |
| F3 Office 灰度 | GATE-201–216、404–410、504–509、606–612、705–708/710、E2E-002/007/008 | 原件不可变、跨用户下载=0、强敏感外泄=0、渲染失败不交付 |
| F4 Research → Office | GATE-301–309、610、E2E-003/004 | Brief 外字段传输=0；无引用事实进入 PPT=0；Office→Research Tavily 调用=0 |

### 13.3 执行顺序

```text
1. 先实现并跑 G0/G1/G2 的拒绝路径；每个拒绝路径先有 Spy 断言。
2. 再实现 Open Research 与 Office 各自的成功链路，并补 G3–G7 状态/质量/生命周期。
3. 两域各自通过 F2/F3 后，才启用 ResearchBrief 与 F4 协同测试。
4. 每次新增 Provider、模板、模型网关、共享或写回能力，新增对应 Gate 用例后才能开启 Feature Flag。
```

## 14. 执行记录模板与待实现项

实现进入回归阶段后，应生成并持续更新 `QA_OPEN_RESEARCH_OFFICE_P0_FUNCTIONAL_REPORT.md`。报告不得因“主链路通过”而把未执行、浏览器待测、真实 Provider 待测或生产环境待测标为通过，至少包含：

| 字段 | 要求 |
|---|---|
| 环境与版本 | Git 提交、镜像/LibreOffice/字体版本、Feature Flag、Provider 是否 Fake |
| 账号与夹具 | 使用的租户、用户、合成资产、是否含真实数据（应为否） |
| 结果 | 用例总数、通过/失败/阻塞、按 G0–G7 分组统计 |
| 缺陷 | 编号、优先级、门禁/模块、复现步骤、期望/实际、是否存在外部副作用 |
| 安全证据 | Fake 调用计数、审计脱敏抽检、跨用户下载/记忆访问结果 |
| 性能证据 | 40MB/120MB 边界、队列、Worker 峰值、渲染耗时、巡检并行回归 |
| 发布建议 | 对照 F0–F4 明确“可放行/不可放行”与剩余风险 |

在 F0 前还需补齐的工程测试资产：稳定版 LibreOffice 镜像、阿里巴巴普惠体授权与安装自检、对象存储/病毒扫描/队列 Fake 与集成环境、Office 边界文件生成器、经过人工标注的开放检索坏例集。未补齐前不得将“未执行”标记为“通过”。
