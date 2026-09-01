# OVD 比对服务 P0/P1 实现与功能自测报告

| 项目 | 结论 |
|---|---|
| 实现版本 | v1.3.1-P0P1 |
| 依据 | 《OVD 驱动的连锁门店目标物品出样比对技术方案 v1.3》 |
| 日期 | 2026-07-31 |
| 方案复核 | 有条件可行：可进入受控 POC，不可直接用于自动处罚或全门店自动 SKU 判罚 |
| 本次范围 | P0 契约/安全边界与 P1 目录、样板、标定、槽位、时间窗和人工复核服务闭环 |
| 自动化结论 | 通过：完整烟测、在线 Agent 回归、凭证库回归均通过 |

## 1. 复核结论与实施边界

v1.3 的分层、状态语义、SKU 主数据治理和失效安全策略正确，尤其是将 OVD 定位、身份复核、时间窗和业务裁决分开。实现中没有发现需要推翻架构的问题。

仍有三项不能被代码替代的上线前置条件：供应方必须确认真实 OVD 响应 Schema 和坐标系；历史静态凭证必须完成轮换并以运行环境密钥注入；首店独立盲测、阈值校准和客户“违规”授权必须完成。故本实现将未具备条件的路径安全地落入 `SYSTEM_FAILED`、`INCONCLUSIVE` 或 `REVIEW`，不会生成伪精确的 SKU/违规结论。

P2 的 DINOv2/Faiss/局部几何复核与按镜头/品类校准，依赖客户真实样板和盲测数据，未在没有数据与验收基线的情况下以固定阈值冒充完成；本次服务已为其保留不可变快照和受控对象证据写入边界。

## 2. 已实现功能

### 2.1 OVD Adapter P0

- 新增 `comparison_service.py`：严格规范化 `detection[]`，要求 `request_id`、模型版本、图像宽高、类别、分数和像素 `bbox_xyxy` 全部存在且合法。
- 提供 `POST /v1/ovd/contract-test`。它只返回脱敏的契约报告，不回传原始供应方报文、Authorization 或签名地址。
- 外部调用只允许服务端环境变量注入；要求 HTTPS、域名白名单、DNS 公网地址校验、8 MB 输入限制、2 MB 响应限制、超时、一次退避重试和三次失败熔断。
- 未配置、白名单/DNS 不合格、超时或 Schema 异常时，帧状态为 `SYSTEM_FAILED`，原因码如 `OVD_NOT_CONFIGURED` / `OVD_INVALID_SCHEMA`；绝不将其转换为空检测、合规或缺货。

### 2.2 Catalog Service P1

- 新增目录版本状态机：`DRAFT → PENDING_APPROVAL → PUBLISHED → RETIRED`，创建人与审批人必须不同；发布会原子退休旧的当前版本，历史版本仍可审计。
- SKU 使用稳定 `sku_id`，而不是名称作为视觉身份；支持标准名、展示名、别名、品牌、family、变体属性、外部编码、生效期与状态。
- SKU 更新采用 `If-Match` / ETag 乐观锁；目录发布校验别名和外部编码冲突。
- 提供影响分析，列出目录版本或 SKU 关联的样板和陈列槽位；所有目录、发布和审批操作写入审计日志。

### 2.3 业态包、样板、标定与槽位

- 新增业态包：明确采集模式、身份证据优先级与 OVD 受控提示词，必须审批后才启用。
- 新增归一化 ROI 标定；只有 `ACTIVE + GREEN + 未过期` 标定可被槽位和会话引用。
- 一张样板只绑定一个已发布在售 SKU；样板须审批。槽位审批前校验其所有预期 SKU 都已有已审批样板。
- 槽位绑定组织、镜头、业态包、目录快照、标定版本、归一化多边形、预期 SKU/数量、最小有效帧、质量/覆盖/遮挡阈值和自动化开关。

### 2.4 Comparison Session 与时间窗规则

- 新增幂等会话、不可变运行快照、帧级证据、槽位结论和人工复核记录。
- `POST /v1/comparison-sessions/{id}/frames` 只接受当前租户受控存储的 `evidence_id`；浏览器不能提交 SKU 身份结果。身份结果只能由服务端 Comparison Worker 写入。
- 有效帧不足返回 `INCONCLUSIVE`；相机不为 GREEN、遮挡超限、ROI 覆盖不足或画质不足不会计入缺失证据。
- OVD 或关键依赖失败返回 `SYSTEM_FAILED`；样板/策略/目录缺失返回 `REVIEW`；连续有效帧中明确禁止身份或缺失预期身份才可能成为 `SUSPECTED_VIOLATION`。
- 人工复核以追加记录方式保存 `CONFIRMED`、`OVERTURNED`、`NEEDS_SITE_CHECK`，不覆盖原始模型或规则证据。

## 3. 接口清单

| 接口 | 用途 |
|---|---|
| `POST /v1/ovd/contract-test` | 验证脱敏 OVD 样例的字段、模型版本、坐标系与提示词映射 |
| `GET/POST/PUT /v1/catalog/skus` | 读取、新建、ETag 更新 SKU；写入必须绑定草稿目录版本 |
| `POST /v1/catalog-versions` | 创建目录草稿；`/{id}/approve|publish|retire` 控制生命周期 |
| `GET /v1/catalog-impact` | 查询目录/SKU 对样板、槽位的影响 |
| `POST /v1/domain-profiles`、`/v1/calibrations`、`/v1/reference-assets`、`/v1/display-slots` | 创建并分别使用 `/{id}/approve` 启用前置配置 |
| `POST/GET /v1/comparison-sessions` | 创建并查询不可变会话快照、帧级证据和槽位决定 |
| `POST /v1/comparison-sessions/{id}/frames` | 由受控证据触发 OVD 检测；没有精确身份 Worker 时安全转复核 |
| `POST /v1/comparison-sessions/{id}/decide` | 依据当前时间窗刷新槽位决定 |
| `POST /v1/reviews/{slot_decision_id}` | 追加人工复核结论 |

所有 `/v1` 管理和会话入口执行既有租户与角色检查；调用方通过 `X-User-Id` / `X-Tenant-Code` 进入当前租户范围。生产环境应替换为统一身份认证和服务身份令牌。

## 4. OVD 运行配置

仅在供应商契约测试、凭证轮换和数据处理边界确认后，在部署环境中配置：

```bash
export OVD_BASE_URL="https://<approved-ovd-host>/api/predict/..."
export OVD_ALLOWED_HOSTS="<approved-ovd-host>"
export OVD_AUTHORIZATION="<rotated-runtime-token>"
export OVD_CLIENT_ID="wanxiang-comparison-service"
export OVD_TIMEOUT_SECONDS="8"
```

`OVD_AUTHORIZATION` 不能写入知识库、请求日志、审计数据、前端或仓库。使用 `POST /v1/ovd/contract-test` 的脱敏样例通过后，再做只读影子调用和回放评测。

## 5. 详细功能自测

执行环境：2026-07-31 16:05（Asia/Shanghai），临时 SQLite 数据库和本机回环 HTTP 服务；未调用外部 OVD，未使用真实客户图片或凭证。

| 编号 | 覆盖功能 | 验证点 | 结果 |
|---|---|---|---|
| T01 | 语法/依赖 | `server.py`、`comparison_service.py`、`smoke_test.py` 的 Python 编译通过 | 通过 |
| T02 | OVD 响应契约 | 合法 `request_id/model/image size/detection/bbox` 规范化为像素坐标；缺失字段拒绝为 `OVD_INVALID_SCHEMA` | 通过 |
| T03 | OVD 失效安全 | 非 HTTPS/私网 OVD 地址拒绝；空配置、无受控 `evidence_id`、Schema 异常不产生空检测合规结论 | 通过 |
| T04 | SKU 目录 | 草稿创建、SKU/别名/条码建档、ETag 冲突拒绝、创建人自审拒绝、异人审批、发布、已发布目录读取 | 通过 |
| T05 | 样板与槽位门禁 | 业态包、GREEN 标定、已发布 SKU、已审批样板和槽位依赖按顺序启用 | 通过 |
| T06 | 会话幂等/快照 | 同幂等键返回同一会话；快照携带目录、业态包、标定、槽位与规则版本，且不含 Authorization | 通过 |
| T07 | 时间窗合规 | 3 帧合格、无遮挡、GREEN 且均匹配允许 SKU → `COMPLIANT` | 通过 |
| T08 | 证据不足 | 未达到最小有效帧 → `INCONCLUSIVE`，不会推断缺失 | 通过 |
| T09 | 依赖故障 | 帧为 `SYSTEM_FAILED/OVD_NOT_CONFIGURED` → 槽位 `SYSTEM_FAILED` | 通过 |
| T10 | 人工闭环 | 可对槽位追加 `CONFIRMED` 复核，不覆盖原始槽位决定 | 通过 |
| T11 | HTTP 集成 | 完整走通 `/v1` 的契约测试、目录、审批、样板、标定、槽位、会话查询；非本租户证据返回 404 | 通过 |
| T12 | 既有巡检回归 | 固定 11:00 调度、知识库 SKU 标签、证据归档、权限、审计、前端契约等全部通过 | 通过 |
| T13 | 在线 Agent 回归 | token、技能路由、媒体、分页、DTO 与脱敏不回归 | 通过 |
| T14 | 凭证库回归 | 加密往返、明文脱敏、主密钥权限通过 | 通过 |

执行命令：

```bash
python3 -m py_compile server.py comparison_service.py smoke_test.py
python3 smoke_test.py
python3 online_agent_test.py
python3 credential_vault_test.py
```

实际输出：

```text
PASS smoke tests: frontend contracts, plans, grouped inspection history, permissions, evidence, analytics, feedback, audit, redaction
PASS online agent tests: token refresh, skill routing, media, slots, pipeline, pagination, DTOs, redaction, analytics
PASS credential vault tests: encrypted round-trip, plaintext redaction, key permissions
```

## 6. 下一步上线门禁

1. 用旋转后的凭证和供应方样例完成 Adapter Contract Test，确认响应字段、坐标系、模型版本、错误码、QPS 与保留策略。
2. 为首店 10–20 个可核验 identity 建立多视角样板、family/variant 可辨识性矩阵、槽位和标定，并由客户、算法、现场三方签字。
3. 接入 P2 的受控特征检索与候选局部复核 Worker；只将经校准且在可辨识矩阵允许范围内的对象结果写入 `object_evidence`。
4. 使用独立盲测集进行离线回放、影子对账与阈值校准；在指标和人工职责确认前，保持 `REVIEW` 优先，不启用自动处罚。
