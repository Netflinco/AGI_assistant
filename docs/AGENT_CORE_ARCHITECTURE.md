# 深象万象 Agent Core 架构说明

更新时间：2026-07-15

## 目标

在不改变当前产品功能和前端交互的前提下，将 AGI 巡检助手的底层能力标准化为 Agent 分层架构，降低后续新增意图、Skill、工具和第三方扩展的耦合成本。

## 分层契约

1. 输入层：`agent_core.InputEnvelope`
   - 统一承载文本、多模态附件、当前租户/门店上下文和历史对话摘要。
   - 当前版本先接入文本输入，保留附件扩展位。

2. 意图识别层：`agent_core.IntentDefinition` + `IntentRegistry`
   - 管理标准意图、别名、相似意图关联、默认 Skill 和默认工具。
   - 当前规则/模型识别仍由 `online_agent.IntentAnalyzer` 和 `agent_skills.infer_intent()` 执行，识别结果会进入标准路由目录。

3. Skill 库：`agent_core.SkillDefinition` + `SkillRegistry`
   - 管理 Skill 名称、业务标签、风险级别、必填槽位和默认工具。
   - 当前 Skill 来源为 `agent_skills.SKILL_CATALOG`，支持继续注册第三方 Skill。

4. 工具箱：`agent_core.ToolDefinition` + `ToolRegistry`
   - 管理工具名称、调用说明、输入输出契约、风险级别和安装来源。
   - 当前工具包括 DeepVision PaaS 查询、抓图、直播、订阅、调度、视觉模型等。

5. 执行层：`agent_core.RouteDecision` + `agent_core.ExecutionStep`
   - 根据识别意图路由到 Skill 和默认工具。
   - 当前业务执行仍沿用 `online_agent.OnlineInspectionAgent.handle_message()` 的既有分支，避免影响现有能力。

6. 输出层：`server.build_agent_trace()`
   - 输出执行结果，并在链路中记录输入、过程依据、工具/Skill 输出和规则复核结果。
   - 新增标准路由结果进入 `agent_meta.route`，历史数据仍兼容旧 `agent_meta.skill/tool_calls`。

## 当前落地范围

- 新增 `agent_core.py`，提供输入、意图、Skill、工具、路由和执行节点的标准数据结构。
- 新增 `validate_agent_manifest()`，统一校验 Skill/Tool 轻量 Manifest 的 Schema、风险级别、执行步骤和密钥安全约束。
- `agent_skills.standard_agent_catalog()` 将现有 `SKILL_CATALOG` 转换为标准目录。
- `online_agent.py` 每轮对话记录 `catalog_version` 和标准 `route`。
- `server.py` 在执行链路中展示标准路由的 Skill、工具、风险和槽位，并提供 Agent 能力目录 API：
  - `GET /api/agent/catalog`：查看当前租户的内置意图、Skill、工具、导入扩展和评估指标。
  - `POST /api/agent/manifests/validate`：仅校验 Manifest，不写入数据库。
  - `POST /api/agent/manifests`：校验通过后导入当前租户目录，并写入审计日志。
- 新增 `agent_manifest_imports` 表，按租户保存导入 Manifest 的版本、风险、校验结果和状态；同名同类新版本会将旧版本标记为 `SUPERSEDED`。
- 新增 `agent_memories` 表，按租户沉淀用户偏好、别名、业务判断口径和对话习惯。
- 新增 `agent_knowledge_items` 表，按租户沉淀 SOP、品牌规范、参考物料、门店平面图和管理制度。
- `static/index.html` / `static/app.js` 新增“Agent 能力”页面，可查看 Skill 库、工具箱、意图图谱、长期记忆、知识库和 Manifest 导入结果。
- `smoke_test.py` 增加 Agent Core、能力中心 API、Manifest 校验/导入、长期记忆、知识库、前端入口契约回归，确保旧 Skill 列表格式不变。

## Skill 与工具的边界

- Skill 是业务能力：面向“要做什么”，声明关联意图、别名、相似意图、槽位、风险、执行步骤和输出目标。
- 工具是执行能力：面向“怎么调用”，声明 runtime、输入 Schema、输出 Schema、权限和超时等调用契约。
- 一个 Skill 可以编排多个工具；一个工具可以被多个 Skill 复用。
- 当前导入的第三方 Skill/工具先进入 `registry_only` 状态，只参与目录展示、意图关联和方案评估，不会绕过现有安全链路直接执行。

## 轻量 Manifest 导入流程

1. 管理员进入“Agent 能力”页面，选择 Skill 模板或工具模板。
2. 填写 JSON Manifest，先点击“校验 Manifest”。
3. 服务端执行统一校验：
   - `kind` 只能为 `skill` 或 `tool`。
   - `schema_version` 必须匹配 `skill.v1` 或 `tool.v1`。
   - `metadata.name` 必须稳定、可版本化。
   - Skill 必须声明 `intent.name` 和至少一个执行步骤。
   - Tool 必须声明 runtime、输入 Schema、输出 Schema。
   - 原始 `api_key` 不允许写入 Manifest，必须使用 `credential_ref`。
   - `HIGH_WRITE` 必须要求确认。
4. 校验通过后导入当前租户目录，写入 `agent_manifest_imports` 和 `audit_logs`。
5. 页面刷新目录，可看到导入扩展、风险等级、运行状态、槽位和关联意图。

## 长期记忆模块

当前版本已落地最小闭环：

- `GET /api/agent/memories`：查看当前租户可用长期记忆。
- `POST /api/agent/memories`：创建长期记忆，并写入审计日志。
- 支持记忆类别：`alias`、`preference`、`business_rule`、`conversation_style`。
- 支持作用范围：`tenant`、`user`、`store`。
- 前端“Agent 能力 > 长期记忆”支持表单创建、列表查看、别名和置信度展示。

设计边界：

- 长期记忆当前进入 Agent 能力中心和目录上下文，可观测、可审计。
- 为避免影响现有业务判断，当前不自动覆盖视觉模型结论；后续接入执行器时应以“检索引用 + 节点输入输出展示”的方式进入执行链路。

## 多模态知识库模块

当前版本已落地最小闭环：

- `GET /api/agent/knowledge`：查看当前租户知识内容。
- `POST /api/agent/knowledge`：创建知识条目，并写入审计日志。
- 支持知识类型：`sop`、`brand_standard`、`reference_material`、`floor_plan`、`policy`。
- 支持模态：`text`、`image`、`document`、`video`、`floor_plan`。
- 前端“Agent 能力 > 知识库”支持导入内容摘要、标签、素材地址和列表查看。

设计边界：

- 当前支持文本、图片/文档/视频/平面图素材地址登记，不直接上传大文件。
- 后续应增加向量索引、图片特征索引、知识版本管理和执行链路引用证据展示。

## 可观测与可评估

- 目录级可观测：能力中心展示内置/导入的意图、Skill、工具和路由关系。
- 执行级可观测：对话执行链路展示节点输入、输出、摘要和业务复核。
- 导入级可观测：Manifest 校验结果、错误、警告、版本、风险和审计日志可追踪。
- 当前评估指标口径：
  - `intent_hit_rate`：意图命中率。
  - `slot_completion_rate`：槽位补全率。
  - `tool_success_rate`：工具调用成功率。
  - `model_confidence`：模型置信度。
  - `business_review_result`：业务规则复核结果。
  - `memory_hit_rate`：执行链路命中的长期记忆比例。
  - `knowledge_recall_rate`：执行链路检索到有效知识的比例。

## 待落地模块建议

- Skill/工具运行器：当前第三方扩展为目录注册模式；后续可增加 `SkillExecutor` 和 `ToolAdapter`，在管理员启用后接入真实执行。
- 记忆/知识检索器：当前 P2 已完成登记、查看、审计和目录上下文；后续需要在执行层引入 `MemoryRetriever` 和 `KnowledgeRetriever`，并在链路节点中展示命中内容。
- 评测集管理：建议按意图、Skill、工具建立样例问法、期望槽位、期望输出和视觉样本，支持回归评分。

## 后续演进建议

- 将 `IntentAnalyzer` 的模型识别、规则兜底、相似意图确认拆入独立 `intent_pipeline`。
- 将长分支执行逐步迁移为 `SkillExecutor`，每个 Skill 拥有独立输入槽、工具调用和输出适配器。
- 将工具调用统一封装为 `ToolAdapter`，补充幂等键、超时、重试、审计和脱敏策略。
- 将执行链路持久化为独立表，便于后续在右侧面板按节点查看完整过程数据。
