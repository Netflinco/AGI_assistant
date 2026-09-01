# Agent 功能专项测试报告（2026-07-09）

## 结论

本次对 `http://127.0.0.1:8000/` 进行了自动化回归、浏览器页面交互、接口级端到端和 SQLite 持久化核验。基础功能整体可用：本地 demo 查询、周期巡检计划生成、租户隔离、权限校验、凭证加密持久化、线上只读摄像头/能力查询均通过。

但存在 3 个需要优先修复的问题：

1. **P0：巡检结果异常状态与结论文案矛盾**，会导致正常画面被标为异常，并错误定位异常证据。
2. **P1：精确摄像头快照请求被语义选镜头覆盖，实际抓错镜头**。
3. **P1：统计排行类问题被识别为告警明细查询，未走聚合分析 Skill**。

另有 2 个安全与稳定性优化项：对话历史中持久化了第三方签名媒体 URL；周期巡检历史失败率较高，需要补充重试、熔断和配置状态前置检查。

## 测试范围

- 意图识别：摄像头查询、告警查询、告警统计、监控快照、点位歧义确认、周期巡检。
- Skill 链路：`paas.camera.page`、`paas.capability.configured`、`paas.alarm.query`、`paas.media.snapshot`、`vlm.camera.select`、`scheduler.plan.validate`。
- 页面回归：接入管理、操作记录、租户切换、门店列表收敛、历史对话展示。
- 数据正确性：租户-门店隔离、摄像头/能力/告警查询、巡检 run 聚合、异常证据定位。
- 持久化：租户 AppKey/AppSecret、大模型 key、主密钥文件权限、对话历史敏感信息。

## 执行记录

- 服务启动：`python3 server.py --port 8000`
- 自动化回归：
  - `python3 smoke_test.py`：通过
  - `python3 online_agent_test.py`：通过
  - `python3 credential_vault_test.py`：通过
- 当前数据：
  - 租户接入：`lianhe_tech` 45 家门店、`oppo` 2 家门店，均 `CONNECTED`
  - 大模型配置：`visual_model` 已持久化，密文长度 184，公开元数据不含 API key
  - 主密钥文件：`data/.credential_master_key` 权限 `0600`

## 通过项

1. 本地 demo 意图链路正常：
   - `查看广州悦汇城离线摄像头` -> `CAMERA_SEARCH`，返回 1 路离线摄像头。
   - `昨天广州悦汇城离岗超过 5 分钟有哪些告警` -> `RESULT_QUERY`，返回 2 条事件，均带证据。

2. 周期巡检计划链路正常：
   - 首轮请求 `每隔3h...为期一周...` -> `CREATE_SCHEDULED_INSPECTION`，缺少 `daily_window`。
   - 补充 `按门店营业时间执行` 后 -> `READY_FOR_CONFIRM`，间隔 180 分钟，覆盖 2 路在线摄像头。
   - 测试未确认计划，未创建真实新任务。

3. 权限与租户隔离正常：
   - 门店负责人跨范围查深圳前海店 -> `403 TENANT_SCOPE_DENIED`。
   - 一线人员创建订阅 -> `403 PERMISSION_DENIED`。
   - 页面切换到 OPPO 后，门店列表收敛到 2 家，不再显示 `lianhe_tech` 门店。

4. 线上只读查询基本稳定：
   - `lianhe_tech/sxjdytc` 摄像头状态查询：302 路，299 在线、3 离线，耗时 2.889s。
   - 能力查询：13 个已配置能力，耗时 2.822s。
   - 点位歧义：`盒马超市门口` 未直接抓图，返回 `CAMERA_LOCATION_DISAMBIGUATION`，候选 `永辉超市门口`，行为合理。

5. 核心凭证持久化符合预期：
   - `tenant_integrations` 返回不含 `encrypted_credentials`，仅展示脱敏 AppKey。
   - `service_configs.public_metadata` 不含模型 API key。
   - 对话中出现的 AppSecret 文本已被替换为 `[已隐藏，请使用安全配置卡]`。

## 缺陷与优化建议

### P0：巡检结果状态与结论矛盾

**复现证据**

- 接口：`GET /api/inspection-runs?page=1&page_size=10`，请求头 `X-Tenant-Code: oppo`
- 典型 run：
  - `run_bb4aa4135e15`
  - `result_status=POSITIVE`
  - `conclusion=地面无散落垃圾或污渍，符合清洁标准。`
  - `business_reason=观察到禁止出现的目标，判定为异常。`
  - `anomaly_evidence_count=1`

当前库中至少 3 条 run 存在同类矛盾：`run_bb4aa4135e15`、`run_ab972e8fd52e`、`run_4882c6d30cc7`。

**影响**

- 正常画面会被运营误判为异常。
- 异常图定位错误，`anomaly_reason` 也可能是正常结论。
- 影响巡检结果展示、处置优先级、后续统计准确性。

**建议**

- 在 `online_agent.py` 的 `VisualReasoner.apply_business_policy()` 增加结构化字段与结论文案一致性校验。
- 对禁止目标场景补充负向词：`无散落垃圾`、`无污渍`、`符合清洁标准` 等。
- 当 `target_observed=true` 但 conclusion 明显否定时，降级为 `UNCERTAIN` 或以结论/证据类型二次校验，不应直接标 `POSITIVE`。
- 增加回归用例：`PROHIBITED_CONDITION + 否定结论` 必须输出 `NEGATIVE`，且 `anomaly_evidence_ids=[]`。

### P1：精确摄像头快照请求抓错镜头

**复现证据**

- 会话：`conv_b2bca4c4aadb`
- 用户输入：`获取 jk-JK-305#-BF-周真真门口朝向永辉超市门口 当前监控画面`
- 实际回复：选择了 `216#JK-297#-F1-6号门3.4.5号客梯保安岗亭`
- Agent 链路：`CAPTURE_SNAPSHOT` -> `paas.camera.page` -> `paas.media.snapshot` -> `vlm.camera.select`
- `camera_selection.relevance=0.0` 仍被接受为最终镜头。

**影响**

- 用户明确指定镜头时仍可能抓错图。
- 后续视觉判断、证据定位、审计记录都会绑定错误点位。

**建议**

- `_resolve_media_camera()` 优先使用 `analysis.camera_names`、上下文 `camera_id` 和精确/模糊名称匹配。
- `vlm.camera.select` 只作为兜底，且必须设置最低相关性阈值，例如 `<0.6` 返回候选确认，不允许自动抓图。
- 如果用户输入含有疑似完整镜头名但未匹配，应返回“未找到该镜头”，而不是改用语义最高图。

### P1：统计排行意图被路由为告警明细查询

**复现证据**

- 会话：`conv_b2bca4c4aadb`
- 用户输入：`近7天当前门店告警统计排行 Top10`
- 实际意图：`QUERY_ALARMS`
- 实际工具：`paas.alarm.query`
- 期望意图：`ANALYZE_ALARMS`
- 期望工具：`paas.alarm.aggregate`

**影响**

- 用户要“排行/统计”时返回明细列表而不是聚合结论。
- 数据分析页与 Agent 问数口径不一致。

**建议**

- `IntentAnalyzer._validate()` 中将 `ANALYZE_ALARMS` 纳入确定性覆盖，或当规则识别为分析类且用户含 `统计/排行/趋势/Top` 时覆盖 LLM 输出。
- 为 `统计排行 Top10`、`趋势`、`最多门店` 增加线上 Agent 合约测试。

### P1：对话历史持久化第三方签名媒体 URL

**证据**

- `messages.linked_object` 中有 40 条记录包含第三方签名 URL 特征，如 `OSSAccessKeyId`、`Signature`、`Expires`。
- 当前快照/视觉结果会将 `snapshot_url` 放入 `artifact.media` 或 `artifact.mediaGallery`。

**影响**

- 即使 URL 有时效，也属于可访问媒体资源凭据，长期落在对话历史中会增加泄露面。

**建议**

- 与周期巡检证据保持一致，持久化内部 `evidence_id/media_id`，前端通过后端代理或短期 access token 拉取。
- 对历史 `linked_object` 做迁移清理或过期脱敏。

### P2：周期巡检第三方稳定性需要治理

**现状**

- `inspection_runs` 共 36 条：20 条成功、16 条失败。
- 失败原因分布：
  - `无法连接 DeepVision 在线服务`：8
  - `Remote end closed connection without response`：6
  - `视觉分析服务尚未配置`：1
  - `所有定时巡检摄像头抓图均失败`：1

**建议**

- 增加 PaaS/VLM 健康检查与配置状态提示。
- 对快照抓取和 VLM 请求增加分层重试、超时分档、失败原因标准化。
- 页面上区分“第三方不可用/模型未配置/抓图失败/证据不足”，不要只给运营一个泛化失败态。

## 回归建议

1. 修复后优先回归 4 条链路：
   - 精确镜头名抓图必须命中指定镜头。
   - 相关性为 0 的 VLM 选镜头必须等待确认或失败。
   - `统计/排行/Top` 必须进入 `ANALYZE_ALARMS`。
   - 负向清洁结论必须对应 `NEGATIVE`，且不能产生异常证据。

2. 建议新增自动化：
   - `online_agent_test.py` 增加精确 camera name、低相关性阈值、统计意图覆盖。
   - `smoke_test.py` 增加巡检 run 一致性断言：`result_status`、`conclusion`、`business_reason`、`anomaly_evidence_ids` 必须语义一致。
   - 增加数据库安全断言：`messages.linked_object` 不允许持久化第三方签名 URL。

## 测试副作用

- 创建了测试会话：
  - `conv_b27ab7e5de03`：本地意图/周期巡检计划测试。
  - `conv_b2bca4c4aadb`：线上只读 Agent 测试。
- 创建了未确认计划，不会执行真实订阅或新定时任务。
- 未提交告警反馈，未确认任何线上写操作。
