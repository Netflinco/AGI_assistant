# 万象 AGI 巡检 Agent

本仓库包含对话式 AI 巡检产品、DeepVision PaaS 在线连接器、Agent 编排器、功能级自测和可运行全栈工作台。

仓库只包含源码、产品/技术文档、前端静态资源和测试夹具。监控快照、数据库、用户上传文件、Office/PDF 导出物及本地凭证均属于运行时数据，不纳入版本控制。配置服务前可复制 `.env.example`，并通过部署环境或密钥管理服务填入真实凭证。

## 交付物

| 文件 | 说明 |
|---|---|
| `docs/TECH_ARCHITECTURE.md` | 技术可行性评估与架构设计 |
| `docs/WORKLIST.md` | P0/P1/P2 开发工作清单 |
| `docs/QA_FUNCTIONAL_TEST_CASES.md` | 功能级开发自测用例 |
| `docs/QA_FUNCTIONAL_TEST_REPORT.md` | 功能测试报告 |
| `docs/QA_REGRESSION_TEST_REPORT_2026-06-24.md` | 回归测试报告 |
| `server.py` | P0 后端 API 与静态服务 |
| `online_agent.py` | DeepVision 在线连接器、模型意图适配与只读工具编排 |
| `static/` | 独立 AI 巡检助理工作台 |
| `smoke_test.py` | 自动化业务烟测 |
| `online_agent_test.py` | 在线 Agent 合约、脱敏和路由测试 |
| `comparison_service.py` | OVD Adapter 契约、安全边界与时间窗槽位规则 |
| `docs/OVD_P0_P1_IMPLEMENTATION_AND_SELF_TEST_2026-07-31.md` | OVD P0/P1 实现说明与功能自测报告 |

## 启动

需要 Python 3.10 或更高版本。克隆仓库后执行：

```bash
cp .env.example .env  # 仅在线能力需要填写真实凭证；本地 Demo 可跳过
./scripts/setup.sh
./scripts/start.sh
```

首次启动会自动创建 `data/agi_inspection.db`、本地演示组织和测试数据，不依赖仓库中的历史数据库或监控截图。默认以本地 Demo 模式启动；配置 DeepVision、LLM、VLM、OVD 等环境变量后启用对应在线能力。

等价的手动启动方式：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py --port 8000
```

访问：

```text
http://127.0.0.1:8000
```

可通过环境变量修改端口：

```bash
PORT=8080 ./scripts/start.sh
```

Windows 用户可以在 PowerShell 中创建虚拟环境并运行：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe server.py --port 8000
```

旧的直接启动命令仍然可用：

```bash
python3 server.py --port 8000
```

`Pillow` 是样板图压缩、图片比对证据准备的运行依赖，已写入 `requirements.txt`。生产环境应使用安装完依赖的独立虚拟环境启动；若缺失 Pillow，系统只会保留巡检，不会把无法准备的图片参照伪造成精确比对。

## 重置本地数据

```bash
python3 server.py --port 8000 --reset
```

默认数据库：

```text
data/agi_inspection.db
```

## DeepVision 在线模式

设置以下环境变量后，服务自动切换为线上模式；不设置时继续运行原有本地 Demo：

```bash
export DEEPVISION_APP_KEY="<app-key>"
export DEEPVISION_APP_SECRET="<app-secret>"
export DEEPVISION_TENANT_CODE="oppo"
python3 server.py --port 8000
```

在线模式当前为只读，支持真实组织、摄像头、能力、告警、证据和告警分析。密钥只允许通过部署环境或密钥管理服务注入，禁止写入仓库或前端。

### 租户接入管理

租户管理员可在“接入管理”查看已接入租户和门店，也可向 Agent 说“接入一个新的 DeepVision 租户”。缺少凭证时 Agent 返回安全配置卡；同一条消息中已提供 tenantCode、AppKey 和 AppSecret 时直接验证并同步门店。前后端均会在显示或持久化前隐藏 AppKey/AppSecret。应用使用 Fernet 加密凭证，默认主密钥文件为 `data/.credential_master_key`并以 `0600` 权限创建。

左侧工作台使用“租户 -> 门店”两级上下文。前端使用 `X-Tenant-Code` 传递选中租户，服务端从凭证库获取该租户独立 Agent，并对会话、订阅、巡检和审计进行租户隔离。新租户的门店索引在切换时直接展示，摄像头、能力和告警按当前门店查询。

生产环境必须由密钥管理服务注入主密钥，不应依赖本地文件：

```bash
export AGI_CREDENTIAL_MASTER_KEY="<fernet-key>"
```

### 大模型意图识别

Agent 支持 OpenAI-compatible Chat Completions 接口：

```bash
export AGENT_LLM_API_KEY="<llm-api-key>"
export AGENT_LLM_MODEL="<model-name>"
export AGENT_LLM_BASE_URL="https://<gateway>/v1"
```

未配置模型凭证时，页面会明确显示“本地降级识别”；不会将降级解析伪装为模型结果。

### 多模态画面判断

画面判断使用 OpenAI-compatible Chat Completions 多模态接口，可直接对接深象统一传输层。未单独配置时会复用 `AGENT_LLM_*`；生产环境建议使用独立的视觉模型配置：

```bash
export AGENT_VLM_API_KEY="<vlm-api-key>"
export AGENT_VLM_MODEL="<multimodal-model-name>"
export AGENT_VLM_BASE_URL="https://<gateway>/v1"
# 可选：完整 Chat Completions 地址和单次候选画面上限
export AGENT_VLM_CHAT_COMPLETIONS_URL="https://<gateway>/v1/chat/completions"
export AGENT_VLM_MAX_IMAGES="8"
# PAI-EAS 原生 Token 使用 raw；标准 OpenAI 网关保持默认 Bearer
export AGENT_VLM_AUTH_SCHEME="raw"
```

未配置 VLM 时，Agent 仍会完成意图识别、自动候选镜头取帧和上下文继承，但会明确返回“未执行视觉判断”，不会要求用户选择镜头或生成猜测结论。

### OVD 比对服务（受控 POC）

`/v1` 提供 Catalog、样板、标定、槽位、比对会话与人工复核接口。只有 `PUBLISHED` 目录、已审批样板、`ACTIVE` 业态包、GREEN 标定和启用槽位可创建会话；帧不足返回 `INCONCLUSIVE`，外部 OVD 失败返回 `SYSTEM_FAILED`，不会从空检测、VLM 或单帧相似度生成合规/违规结论。

外部 OVD 仅允许由服务端读取环境变量调用。请在完成供应方响应 Schema 联调与凭证轮换后配置，禁止把 Authorization、签名 URL 或历史静态凭证写入前端、数据库或仓库：

```bash
export OVD_BASE_URL="https://<approved-ovd-host>/api/predict/..."
export OVD_ALLOWED_HOSTS="<approved-ovd-host>"
export OVD_AUTHORIZATION="<rotated-runtime-token>"
export OVD_CLIENT_ID="wanxiang-comparison-service"
export OVD_TIMEOUT_SECONDS="8"
```

#### Alibaba EAS OVD（`pytrt_sam3`）

EAS 不使用上面的通用响应格式。服务端识别到 `OVD_EAS_TOKEN` 后自动启用 EAS 适配器：使用 `POST /api/predict/<model>/ovd`、固定 JSON 请求体，并把 EAS 的 `outputInfo[].box=[x,y,w,h]` 校验后转换为内部 `bbox_xyxy`。令牌原样放入 `Authorization` 请求头；不要添加 `Bearer`，除非供应方另行变更协议。

```bash
export OVD_EAS_TOKEN="<runtime-secret-from-secret-manager>"
export OVD_EAS_ACCOUNT_ID="<eas-account-id>"
export OVD_EAS_REGION="cn-hangzhou"
export OVD_EAS_MODEL="pytrt_sam3"
export OVD_CLIENT_ID="deepvision"
export OVD_TIMEOUT_SECONDS="3"
export OVD_THRESHOLD="0.5"
# 可选；未设置时由 account_id + region 自动生成唯一白名单主机。
export OVD_ALLOWED_HOSTS="<eas-account-id>.cn-hangzhou.pai-eas.aliyuncs.com"
```

实时视觉问答会对任意“存在性”问题执行受控的对象候选规划：人员问题以 `person` 为确定性候选；其他问题由同一 VLM 将 query 作为数据转换为最多 3 个最小英文物体名词（例如 `backpack`、`bottle`），并经过长度、ASCII、URL/指令词和数量校验后再发送给 OVD。颜色、材质、款式、数量、位置和对象关系始终由 VLM 根据原始 query 判断，用户原始文本绝不直接作为 OVD prompt。

OVD 返回的框会组成“候选裁剪 + 完整画面”的单图证据板，适配只支持单图输入的 VLM 网关。候选框只提高小物体和边缘物体的召回；OVD 无返回、超时或检测到 0 个对象都不是“不存在”的证据，仍由 VLM 定位证据和完整排除证据门禁裁决。证据板、原始框和图片字节仅在本次内存中使用，持久化结果只保留脱敏的 provider、候选 prompt、每帧状态、模型版本和数量。

可用 `POST /v1/ovd/contract-test` 离线校验脱敏的 EAS 样例响应；请求传 `provider:"eas"`、`response`、`expected_prompts`、`image_width`、`image_height` 和可选 `model_version`。该接口不接收图片或 Token。

先使用 `POST /v1/ovd/contract-test` 上传脱敏供应方样例响应完成字段、坐标系与模型版本契约测试；未配置、URL 白名单/DNS 校验失败、超时或 Schema 异常都会安全失败。

## 自动化自测

```bash
python3 smoke_test.py
python3 online_agent_test.py
python3 credential_vault_test.py
```

当前通过结果：

```text
PASS smoke tests: subscription plan confirmation, idempotency, permissions, evidence, analytics, feedback, audit, redaction
PASS online agent tests: token refresh, skill routing, media, slots, pipeline, pagination, DTOs, redaction, analytics
```

## 已实现能力

1. 对话生成订阅 Plan，未确认不执行。
2. 计划卡确认后创建订阅，重复确认幂等。
3. 角色权限和组织范围在后端校验。
4. 查询事件结果并绑定证据。
5. 统计问数返回 `query_id` 和口径。
6. 误报/真警/忽略反馈更新事件并写入 badcase。
7. 订阅、统计、反馈、证据查看进入审计。
8. 摄像头响应不暴露原始流地址、密码或凭证引用。
9. OPPO 线上组织、30 路摄像头、配置能力和告警证据实时查询。
10. 模型结构化意图适配、Schema 校验、工具白名单与本地显式降级。
11. 线上只读边界：任务配置和告警反馈不会被伪造成执行成功。
