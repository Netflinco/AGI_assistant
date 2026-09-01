# Agent Core 大版本功能测试报告

测试日期：2026-07-16  
测试地址：`http://127.0.0.1:8000/`  
测试角色：Agent 应用测试专家  
测试结论：不建议直接全量发布。核心功能主链路可用，但存在权限放大、Manifest 安全校验绕过、语义校验不足等高优先级问题，建议修复后再做一次回归。

## 1. 测试依据

- 架构说明：`docs/AGENT_CORE_ARCHITECTURE.md`
- 历史报告：`docs/QA_AGENT_CATALOG_REGRESSION_REPORT_2026-07-15.md`
- 原始需求：`/Users/dimeng/.codex/attachments/cb4aed3b-da2e-4d6d-94c2-ab329c3dca70/pasted-text.txt`
- 当前页面：`http://127.0.0.1:8000/`
- 当前数据库：`data/agi_inspection.db`

本次测试不是只看历史报告结论，而是在当前已发布页面和服务上重新造数、重新调用接口、重新体验页面后得到的结果。

## 2. 本次测试数据

本次在当前可访问在线租户 `oppo` 下新增或使用以下 QA 数据：

| 类型 | 标识 | 结果 |
| --- | --- | --- |
| Skill Manifest | `qa_20260716_oppo_competitor_logo_check` v1.0.0 / v1.1.0 | v1.0.0 被置为 `SUPERSEDED`，v1.1.0 为 `ENABLED` |
| Tool Manifest | `qa_20260716_oppo.logo_detector` v1.0.0 | `ENABLED`，`runtime_status=registry_only` |
| 长期记忆 | `qa_20260716_oppo_door_alias` | 已落库并在页面展示 |
| 权限探针记忆 | `qa_20260716_rbac_store_escalation` | 由 `u_store` 成功写入，用于证明权限问题 |
| 知识库 | `QA OPPO 竞品 Logo 参考规范` | 已落库并在页面展示，含素材 URL |
| 对话链路 | `conv_ef7b7c83db8a` | 覆盖点位查询、快照意图、周期巡检创建 |

## 3. 测试用例规划

| 用例编号 | 模块 | 测试目标 | 结果 |
| --- | --- | --- | --- |
| AC-01 | Agent 能力中心 | 页面入口、Tab、目录统计、指标展示 | 通过 |
| AC-02 | Catalog API | `GET /api/agent/catalog` 返回内置能力、导入能力、模板、记忆、知识 | 通过 |
| AC-03 | 权限控制 | 非管理员不应管理 Agent 能力中心 | 不通过 |
| AC-04 | Skill 模板 | 创建 Skill 能力入口应使用标准模板 | 部分通过 |
| AC-05 | Tool 模板 | 注册工具入口应使用 Tool Manifest 标准模板 | 部分通过 |
| AC-06 | Manifest 结构校验 | 缺少步骤、缺少 schema、原始密钥等应被拦截 | 部分通过 |
| AC-07 | Manifest 语义校验 | 不存在工具、重复意图应被拦截或警告 | 不通过 |
| AC-08 | Manifest 导入 | 导入、版本替换、审计、租户隔离 | 通过 |
| AC-09 | 长期记忆 | 创建、展示、持久化、审计、删除、确认 | 部分通过 |
| AC-10 | 知识库 | 创建、展示、持久化、多模态素材引用 | 部分通过 |
| AC-11 | 意图识别链路 | 点位查询、快照、周期巡检意图识别合理性 | 通过 |
| AC-12 | Skill/Tool 执行链路 | 工具调用准确性、第三方接口稳定性、失败不乱判 | 通过 |
| AC-13 | 执行 Trace | 展示输入、意图、Skill、Tool、模型输出、业务复核 | 部分通过 |
| AC-14 | 租户-场-数据隔离 | OPPO / lianhe_tech 切换不串数据 | 通过 |
| AC-15 | 巡检结果定位 | 巡检 run、证据、摄像头、链路可追溯 | 通过 |
| AC-16 | 核心配置持久化 | 大模型 key、租户 appkey 等敏感数据持久化和加密 | 通过 |

## 4. 关键通过项

1. 基础回归通过  
   `python3 -m py_compile server.py smoke_test.py agent_core.py agent_skills.py online_agent.py` 通过；`python3 smoke_test.py` 通过。

2. Agent 能力中心页面可用  
   页面存在 `Agent 能力` 入口，包含 `业务能力 / 执行工具 / 意图路由 / 长期记忆 / 知识库 / Manifest 导入` Tab；OPPO 下展示本次导入 Skill、Tool、记忆、知识项。

3. Manifest 导入主链路可用  
   `qa_20260716_oppo_competitor_logo_check` v1.0.0 导入后，再导入 v1.1.0，旧版本自动 `SUPERSEDED`，新版本 `ENABLED`；审计日志有 `agent.manifest.import`。

4. 结构校验基本可用  
   缺少 `skill.execution.steps` 的 Skill Manifest 被拦截，UI 显示 `Manifest 还需要修正`。

5. 意图识别链路整体合理  
   - “帮我查一下门店监控点位列表”识别为 `QUERY_CAMERAS`，调用 `paas.camera.page`，返回 30 路在线摄像头。
   - “看一下门口监控快照有没有异常”识别为 `ANALYZE_VISUAL`，但未找到门口摄像头时进入 `CAMERA_LOCATION_NOT_FOUND`，没有使用无关画面生成判断。
   - “创建一个每天上午9点检查门口是否有竞品Logo的巡检”识别为 `CREATE_SCHEDULED_INSPECTION`，生成补槽计划。

6. 巡检结果可追溯  
   `/api/scheduled-inspections` 和 `/api/inspection-runs` 能返回周期任务、run、7 张证据、camera_id、camera_name、org_name、sha256、trace_json。Trace 包含抓图、证据归档、VLM 判断、结果落库、模型原始输出、业务规则复核。

7. 租户隔离正向通过  
   页面从 OPPO 切换到 `lianhe_tech` 后，OPPO 的 QA Skill、记忆、知识不再显示；切回 OPPO 后恢复。接口层 `oppo/lianhe_tech/arcfox` 的 catalog 数据互相隔离。

8. 核心配置持久化正向通过  
   `service_configs` 存在 `visual_model`，`encrypted_value` 为加密串，`public_metadata` 保存模型、URL、auth_scheme、max_images；`tenant_integrations` 只保存 `app_key_masked`、`encrypted_credentials`、`credential_fingerprint`，未发现明文 app_secret 字段；`data/.credential_master_key` 存在。

## 5. 缺陷与优化建议

### P0-01 在线租户模式存在权限放大

现象：使用 `u_store`、`u_frontline` 请求 `GET /api/agent/catalog`，在 `X-Tenant-Code: oppo` 下均返回 200，且 summary 与管理员一致。进一步使用 `u_store` 成功写入租户级长期记忆 `qa_20260716_rbac_store_escalation`。

影响：店长或一线人员可管理 Agent 能力中心、写入租户级记忆/知识/Manifest，存在越权配置和业务口径污染风险。

定位线索：`server.py:815-827` 中，只要命中在线租户，`user_from_request` 会把任意已存在用户覆盖为 `tenant_admin`，并赋予 `allowed_org_ids=["*"]`。

建议：在线租户切换不应覆盖用户原始角色。应建立租户用户映射或外部身份映射，并在 Agent catalog、manifest、memory、knowledge 写接口继续使用真实角色做权限判断。

### P0-02 Tool Manifest 顶层原始 api_key 可绕过校验

现象：以下 Manifest 在 API 和 UI 中均显示校验通过：

```json
{
  "kind": "tool",
  "schema_version": "tool.v1",
  "runtime": {"type": "http", "endpoint": "https://example.com/api"},
  "auth": {"type": "api_key", "api_key": "RAW-SECRET"}
}
```

对比：`runtime.auth.api_key` 能被正确拦截，但原始需求中的 Tool Manifest 示例是顶层 `auth`，当前校验只检查 `runtime.auth.api_key`。

影响：用户可以把原始密钥写入 Manifest JSON 并导入目录，绕过 `credential_ref` 机制，存在敏感信息落库风险。

定位线索：`agent_core.py:151-153` 只读取 `runtime.get("auth")`，未检查顶层 `manifest.auth`，也未做递归敏感字段扫描。

建议：统一 Tool Manifest 规范。如果支持顶层 `auth`，必须强制 `credential_ref`；如果不支持，应直接报错。建议增加递归扫描，禁止 `api_key/app_secret/token/password/secret` 等敏感字段以明文出现在 Manifest 任意位置。

### P1-01 Manifest 语义校验不足

现象：
- Skill steps 引用不存在工具 `qa.nonexistent.tool`，校验返回 `ok=true`。
- Skill intent 直接使用已有意图 `ANALYZE_VISUAL`，校验返回 `ok=true`，无冲突告警。

影响：导入目录会出现无法执行或覆盖/混淆既有路由的 Skill，后续意图识别命中率和执行稳定性不可控。

建议：在 schema 校验后增加语义校验：
- step 中的 `tool/skill` 必须存在于内置 registry 或当前租户已启用导入目录；
- `intent.name` 与内置/租户已有 intent 冲突时必须失败或至少高危警告；
- 必填槽位与执行步骤入参应做 dry-run 补槽检查；
- 权限/risk 与工具风险等级应做一致性检查。

### P1-02 UI 仍是 JSON 编辑器，不完全满足“表单化配置 + Manifest 预览”

现象：`创建 Skill 能力`、`注册工具调用` 能自动填充模板，但最终仍是 `Agent Manifest JSON` 文本框，用户可以直接修改结构。页面文案也显示“专家模式/JSON 导入”。

影响：比自由粘贴 YAML 有改善，但仍不能从产品形态上约束用户“必须按标准模板配置”，不利于字段解释、权限提示、dry-run 和错误定位。

定位线索：`static/app.js:1287-1326` 直接把模板 JSON stringify 到编辑器，再 parse JSON。

建议：
- 普通入口做表单化向导：基础信息、意图说法、槽位、执行步骤、风险、认证引用；
- 右侧只读 Manifest 预览；
- JSON 编辑器保留为管理员/专家模式，并加权限开关；
- 每个字段提供枚举、说明、风险提示和 inline 校验。

### P1-03 长期记忆缺少删除和重要记忆确认机制

现象：页面支持保存和展示长期记忆，审计也有 `agent.memory.create`；但未发现删除入口和删除接口。另，本次 `u_store` 可直接写入租户级记忆，没有二次确认。

影响：不满足原始需求中的“可查看、可删除、可审计”和“重要记忆写入需确认”。错误业务口径一旦写入，缺少前台治理闭环。

建议：增加 `DELETE /api/agent/memories/{memory_id}` 或软删除接口、页面删除按钮、审计日志；对 `scope=tenant` 或 `category=business_rule` 的重要记忆增加确认和权限校验。

### P1-04 Execution Trace 对记忆/知识召回展示不足

现象：普通对话 Trace 展示了 `意图识别 / Skill 路由 / 工具调用`；周期巡检 run 的 Trace 展示了模型原始输出和业务规则复核。但本次已创建记忆和知识后，聊天 Trace 中没有独立的 `Memory Retrieve`、`Knowledge Recall` 节点，也没有展示召回命中内容。

影响：不完全满足原始需求中的“memory、knowledge、tool IO、model raw output、business rule review、final output 全链路可观察”。尤其知识库和长期记忆是否参与推理无法评估。

建议：在 `build_agent_trace` 或执行编排层增加标准节点：`memory.retrieve`、`knowledge.retrieve`、`knowledge.citation`，并输出命中数量、命中 key/title、置信度、是否参与最终结论。

### P1-05 HIGH_WRITE 的确认要求被静默纠偏

现象：`risk.level=HIGH_WRITE` 且 `confirm_required=false` 的 Tool Manifest 返回 `ok=true`，normalized 中被改为 `confirm_required=true`，没有错误或警告。

影响：如果产品期望“严格校验”，当前行为会让用户误以为原始配置合规；如果产品期望“自动纠偏”，页面也缺少明确提示。

定位线索：`agent_core.py:70-87` 中先把 `HIGH_WRITE` 的 confirm_required 计算为 true，后续错误分支永远不会触发。

建议：改为保留用户原始值，并在 `HIGH_WRITE && confirm_required !== true` 时返回错误；或返回 warning 且 UI 明示“已自动强制开启确认”。

### P2-01 巡检 Trace 中模型状态与业务状态枚举容易混淆

现象：周期巡检 run 中，模型原始输出 `raw_output.status` 出现 `POSITIVE`，但业务复核后 `final_status/result_status` 为 `NEGATIVE`，结论是“未发现禁止出现对象”。从异常检测语义看，`POSITIVE/NEGATIVE` 容易被理解为“异常/正常”，当前 raw 与 final 含义不一致。

影响：研发、运营和客户复盘时可能误读模型是否判异常。

建议：区分 `model_detection_status`、`business_result_status`，或统一枚举为 `ANOMALY/NO_ANOMALY/PENDING`，并在 Trace UI 中显示枚举说明。

### P2-02 知识库仍偏轻量，未验证真正召回

现象：知识库支持标题、类型、模态、内容摘要、素材 URL、标签的创建和展示；但当前测试未看到知识向量化、附件上传、引用片段、召回命中进入执行 Trace。

影响：满足“资料沉淀”的基础能力，但距离“多模态知识参与巡检判断”仍有差距。

建议：补齐上传/索引/召回 API；执行链路中展示 knowledge recall；对图片/平面图/摄像头描述类知识增加引用和版本管理。

## 6. 回归建议

修复后建议优先回归以下场景：

1. 用 `u_store/u_frontline` 访问 `GET /api/agent/catalog`、`POST /api/agent/manifests`、`POST /api/agent/memories`、`POST /api/agent/knowledge`，应被拒绝。
2. Tool Manifest 在任意层级包含 `api_key/app_secret/token/password/secret` 明文字段时，应校验失败。
3. Skill step 引用不存在工具、重复 intent、风险等级不一致时，应校验失败或产生阻断级 warning。
4. 普通用户通过表单化 Skill/Tool 向导创建能力，只能选择合法字段、合法工具和合法风险等级。
5. 长期记忆支持删除、审计和重要记忆确认。
6. 对话和巡检 Trace 增加 memory/knowledge 命中节点，并能展示召回证据。
7. OPPO、lianhe_tech、arcfox 多租户切换后，能力目录、记忆、知识、巡检任务和证据均不串租户。

## 7. 附：核心测试命令

```bash
python3 -m py_compile server.py smoke_test.py agent_core.py agent_skills.py online_agent.py
python3 smoke_test.py
```

接口和页面测试均在 `http://127.0.0.1:8000/` 当前运行服务上完成，页面无前端 console error。
