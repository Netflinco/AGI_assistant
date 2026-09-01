# 门店视觉合规巡检对意图识别与 Skill 链路影响分析

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 日期 | 2026-07-10 |
| 关联文档 | `AGI巡检对话式产品PRD.md`、`门店视觉合规巡检产品方案.md` |
| 分析目标 | 判断新增“门店视觉合规巡检”场景对当前意图识别模块和 Skill/能力执行链路的影响 |

## 1. 总体结论

新增极狐和 OPPO 这类“门店视觉合规巡检”场景，对当前产品核心架构 **没有破坏性影响**，不需要改变“对话入口 -> Plan DSL -> 应用订阅 -> 工作流执行 -> 结果中心 -> 反馈/badcase”的主链路。

但它对两个模块有明确扩展影响：

1. **意图识别模块：中高影响**  
   现有 `SUBSCRIPTION_CREATE`、`RESULT_QUERY`、`DATA_STATS`、`FEEDBACK_CREATE` 可以继续复用，但需要新增视觉合规领域的子意图、槽位、业务词典和实体消歧能力，尤其要支持“对象包创建/修改”“禁用对象追加”“参考素材补充”等新型写操作。

2. **Skill/能力执行链路：中高影响**  
   不需要新增客户专属工作流，但需要新增一个通用 Skill 模板：`visual_compliance_inspection`。该 Skill 内部组合对象包检索、开放词汇检测、Logo/OCR、图片相似度、多模态复核、规则引擎和证据生成。

一句话判断：

> 当前架构可以承接新增场景，但必须把“视觉合规”从一个普通巡检能力升级为“对象包驱动的参数化 Skill”，并同步扩展意图、槽位、工具和评测集。

## 2. 当前产品功能遍历与影响判断

| 当前产品模块 | 当前能力 | 新增场景影响 | 结论 |
|---|---|---|---|
| 独立 AI 巡检助理工作台 | 对话创建订阅、查询结果、统计分析 | 需要新增“视觉合规巡检”快捷入口和示例问题 | 低影响，补入口即可 |
| 嵌入式 Chat Widget | 在应用订阅、结果中心等页面内对话 | 需要识别当前页面是否处于视觉合规订阅/对象包/结果详情 | 中影响，需上下文扩展 |
| 意图识别 | 支持订阅、结果、统计、反馈、摄像头查询 | 需要新增对象包、禁用对象、参考素材、视觉合规查询意图 | 中高影响 |
| Plan DSL | 承接写操作计划、确认、执行 | 需要表达对象包、规则模板、参考素材、对象包版本策略 | 中影响，扩展 schema |
| 应用广场 | 展示可订阅模型应用 | 新增“门店视觉合规巡检”能力卡 | 低影响 |
| 应用编排画布 | 数据源、小模型、大模型、工具库组合 | 不暴露给普通客户；内部新增通用工作流模板 | 中影响，新增模板而非改链路 |
| 应用订阅 | 选择应用、摄像头、布防时间、阈值 | 新增对象包、规则模板、版本策略、证据策略配置 | 中影响，扩展订阅表单 |
| 摄像头管理 | 摄像头查询、快照、点位、标定 | 需要按对象推荐摄像头点位，如电视、海报区、展厅全景 | 中影响，P1 增强 |
| 结果中心 | 事件列表、详情、证据、反馈 | 新增缺失、禁止出现、风格不符、内容不符等事件类型 | 中影响，扩展事件类型和证据展示 |
| 数据统计 | 事件趋势、排行、误报率、覆盖率 | 新增合规率、问题门店数、禁用对象出现次数、对象包覆盖率 | 中影响，扩展指标语义层 |
| 知识库/业务词典 | 能力同义词、SOP | 需要对象别名、品牌同义词、竞品品牌库、物料词典 | 中高影响 |
| 反馈/badcase | 误报、漏报、标注问题 | 新增“加入正样本/负样本”“调整对象包”“调整规则” | 中影响 |
| AI 配置中心 | 意图、槽位、工具 allowlist、风险策略 | 需要新增视觉合规意图和工具权限策略 | 中影响 |
| 审计日志 | 写操作审计、证据查看审计 | 对象包变更、素材上传、版本发布必须审计 | 中影响 |
| 嵌入 SDK | 上下文注入、打开对象、接收计划事件 | 需要支持 object_pack、visual_rule、reference_asset 类型 | 低中影响 |

## 3. 对意图识别模块的影响

### 3.1 现有意图是否够用

当前 PRD 中 P0 意图包括：

1. `SUBSCRIPTION_CREATE`
2. `SUBSCRIPTION_QUERY`
3. `RESULT_QUERY`
4. `EVIDENCE_VIEW`
5. `DATA_STATS`
6. `FEEDBACK_CREATE`
7. `CAMERA_SEARCH`
8. `HELP`
9. `WORKFLOW_CREATE`
10. `INSPECTION_RUN_NOW`

对于“创建一个视觉合规巡检订阅”，现有 `SUBSCRIPTION_CREATE` 可以承接；但对于“新增一个竞品 Logo”“把新的立牌样式加入对象包”“不再检测电视广告”这类需求，现有意图不够。

因此建议采用两层策略：

1. **保留通用意图**：订阅、查询、统计、反馈仍走现有通用意图。
2. **新增视觉合规子意图**：对象包和规则变化独立为新意图。

### 3.2 建议新增意图

| 意图编码 | 名称 | 风险 | 说明 |
|---|---|---|---|
| `VISUAL_COMPLIANCE_SUBSCRIPTION_CREATE` | 创建视觉合规订阅 | 高 | 可作为 `SUBSCRIPTION_CREATE` 的细分意图 |
| `VISUAL_COMPLIANCE_SUBSCRIPTION_UPDATE` | 修改视觉合规订阅 | 高 | 修改对象包、规则、摄像头范围、生效时间 |
| `OBJECT_PACK_CREATE` | 创建对象包 | 中高 | 新建客户对象包 |
| `OBJECT_PACK_UPDATE` | 修改对象包 | 高 | 新增/删除/替换巡检对象 |
| `OBJECT_PACK_VERSION_APPLY` | 应用对象包新版本 | 高 | 将新版本应用到订阅 |
| `REFERENCE_ASSET_ADD` | 添加参考素材 | 中 | 上传或从结果中加入图片、Logo、海报 |
| `REFERENCE_ASSET_REMOVE` | 移除参考素材 | 中高 | 影响识别效果，需要确认 |
| `VISUAL_RULE_UPDATE` | 修改视觉合规规则 | 高 | 修改必须出现、禁止出现、阈值、数量等 |
| `VISUAL_COMPLIANCE_RESULT_QUERY` | 查询视觉合规结果 | 中 | 可作为 `RESULT_QUERY` 细分意图 |
| `VISUAL_COMPLIANCE_STATS_QUERY` | 查询视觉合规统计 | 中 | 可作为 `DATA_STATS` 细分意图 |
| `OBJECT_FEEDBACK_TO_SAMPLE` | 反馈结果加入样本 | 中 | 误报加入负样本、真例加入正样本 |
| `CAMERA_RECOMMEND_FOR_OBJECTS` | 按巡检对象推荐摄像头 | 中 | P1 能力 |

### 3.3 需要新增的槽位

| 槽位 | 类型 | 示例 | 是否关键 |
|---|---|---|---|
| `visual_compliance_scene` | 场景 | 汽车展厅、手机门店、柜台、海报区 | 是 |
| `object_pack_id` | 对象包 | 极狐展厅标准物料对象包 | 条件关键 |
| `object_pack_version` | 版本 | v1.2 | 条件关键 |
| `target_objects` | 必须出现对象 | 统一座椅、立牌、指定广告 | 条件关键 |
| `forbidden_objects` | 禁止出现对象 | 竞品 Logo、其他品牌汽车 | 条件关键 |
| `object_type` | 对象类型 | 物体、Logo、海报、屏幕内容、车辆品牌 | 是 |
| `brand_scope` | 品牌范围 | OPPO 以外品牌、vivo/华为/小米 | 条件关键 |
| `reference_assets` | 参考素材 | 广告图片、Logo 图、立牌照片 | 条件关键 |
| `check_mode` | 检查方式 | 必须出现、禁止出现、内容匹配、风格匹配 | 是 |
| `rule_template_id` | 规则模板 | 禁止对象不得出现 | 条件关键 |
| `match_threshold` | 阈值 | 0.75、相似度 80% | 否 |
| `min_count/max_count` | 数量 | 至少 4 把座椅 | 否 |
| `applicable_zones` | 区域 | 接待区、电视区域、入口 | 否 |
| `camera_point_preference` | 推荐点位 | 展厅全景、墙面、柜台、电视 | 否 |
| `object_pack_update_policy` | 更新策略 | 固定版本、跟随最新版、审批后更新 | 是 |
| `sample_polarity` | 样本类型 | 正样本、负样本 | 条件关键 |

### 3.4 业务词典需要扩展

| 词典类型 | 示例 | 作用 |
|---|---|---|
| 物料同义词 | 立牌、展架、易拉宝、台卡、海报 | 统一映射对象类型 |
| 品牌词典 | OPPO、vivo、华为、小米、极狐、问界、理想 | 品牌识别和禁用品牌配置 |
| 行业场景词典 | 展厅、门店、柜台、休息区、电视区、海报区 | 场景和摄像头推荐 |
| 检查动词 | 看一下、检查、有没有、是否出现、是否播放 | 意图识别 |
| 合规词 | 指定、统一、标准、符合要求、其他品牌、竞品 | 规则识别 |
| 屏幕内容词 | 电视、广告、播放、画面、电子屏 | 屏幕检测链路 |
| 车辆品牌词 | 其他品牌汽车、竞品车辆、非本品牌车辆 | 汽车展厅场景 |

### 3.5 意图识别风险变化

| 风险 | 表现 | 影响 | 应对 |
|---|---|---|---|
| “检查对象”与“检查结果”混淆 | “看一下有没有竞品 Logo”可能是建订阅，也可能是查结果 | 高 | 根据页面上下文和时间表达判断；不确定时追问 |
| 必须出现/禁止出现反向理解 | “有没有其他品牌”是禁止对象，不是必须对象 | 高 | 增加 check_mode 分类器 |
| 对象与品牌混淆 | “极狐门店检查其他品牌汽车”中极狐是租户/授权品牌，其他品牌是禁用对象 | 中高 | 引入 brand_scope 和 authorized_brand |
| 屏幕内容判断缺素材 | “电视是否播放指定广告”但没有广告图 | 中 | 缺 reference_assets 时追问上传 |
| 对象包定位失败 | 用户说“加入新的立牌”但没有说哪个对象包 | 中 | 根据租户、页面、最近订阅推断；低置信追问 |
| 频繁对象变更影响订阅 | 对象包变更可能影响多个订阅 | 高 | 计划卡展示影响范围，默认审批后更新 |

## 4. 对 Skill/能力执行链路的影响

### 4.1 当前 Skill 链路是否需要改变

不需要改变主链路，但需要新增一个通用 Skill 模板：

```text
visual_compliance_inspection
  -> load_object_pack
  -> select_camera_frames
  -> detect_candidates
  -> recognize_logo_or_text
  -> compare_reference_assets
  -> multimodal_verify
  -> apply_compliance_rules
  -> generate_evidence
  -> emit_inspection_event
```

它不是客户专属 Skill，而是通用 Skill。极狐和 OPPO 的差异只体现在对象包和规则参数上。

### 4.2 Skill 链路分解

| Step | Skill/工具 | 输入 | 输出 | 是否新增 |
|---|---|---|---|---|
| 1 | `load_object_pack` | object_pack_id/version | 对象、规则、素材、阈值 | 新增 |
| 2 | `select_camera_frames` | camera_scope、schedule、point_label | 抽帧任务 | 复用/增强 |
| 3 | `detect_open_vocabulary_objects` | frame、visual_prompt | 候选物体框 | 新增或接入 |
| 4 | `detect_logo` | frame、brand_library | Logo 候选 | 新增 |
| 5 | `ocr_extract_text` | frame/region | 文本内容 | 新增/复用 |
| 6 | `compare_reference_image` | candidate、reference_assets | 相似度分数 | 新增 |
| 7 | `detect_screen_region` | frame | 屏幕区域 | P1 新增 |
| 8 | `multimodal_verify` | 候选区域、规则、参考素材 | 复核结论 | 复用大模型节点，需提示词模板 |
| 9 | `apply_compliance_rules` | 候选结果、对象规则 | 合规/不合规/待确认 | 新增 |
| 10 | `generate_visual_evidence` | frame、bbox、rule_result | 证据对象 | 增强 |
| 11 | `emit_visual_compliance_event` | rule_result、evidence | 事件 | 增强 |
| 12 | `feedback_to_sample` | event、feedback | 正/负样本 | 新增 |

### 4.3 对现有工作流节点的影响

| 节点 | 当前定位 | 新增场景影响 |
|---|---|---|
| 数据源节点 | 视频帧/视频流 | 视觉合规更适合视频帧抽样；视频流可作为 P1/P2 增强 |
| 小模型能力节点 | 目标检测、场景检测等 | 需要支持开放词汇检测、Logo 候选、屏幕区域候选 |
| 大模型能力节点 | 多模态判断 | 需要支持“基于对象包和规则复核”，不能直接自由判断 |
| 工具库节点 | 服装库、人员库等 | 需要新增对象库、品牌库、OCR、图片相似度工具 |
| 结果输出 | 事件生成 | 需要新增视觉合规事件类型和证据结构 |

结论：

1. 画布结构不变。
2. 节点类型不一定增加。
3. 工具库和节点参数需要扩展。
4. 客户默认不进入画布，只在订阅中选择对象包。

### 4.4 Skill 入参建议

```json
{
  "skill": "visual_compliance_inspection",
  "tenant_id": "tenant_oppo",
  "subscription_id": "sub_001",
  "object_pack": {
    "object_pack_id": "pack_oppo_competitor_brand",
    "version": "v1.2"
  },
  "camera_scope": {
    "camera_ids": ["cam_001", "cam_002"],
    "point_labels": ["门店全景", "墙面", "柜台"]
  },
  "schedule": {
    "mode": "business_hours"
  },
  "rules": [
    {
      "rule_type": "FORBIDDEN_OBJECT_APPEAR",
      "object_type": "brand_logo",
      "objects": ["vivo", "xiaomi", "huawei"],
      "threshold": 0.75
    }
  ],
  "evidence_policy": {
    "require_bbox": true,
    "require_reference_compare": true,
    "low_confidence_to_pending": true
  }
}
```

### 4.5 Skill 输出建议

```json
{
  "event_type": "VISUAL_FORBIDDEN_OBJECT_FOUND",
  "object_name": "vivo Logo",
  "object_type": "brand_logo",
  "rule_type": "FORBIDDEN_OBJECT_APPEAR",
  "status": "PENDING_CONFIRM",
  "confidence": 0.82,
  "camera_id": "cam_001",
  "captured_at": "2026-07-10T10:15:00+08:00",
  "evidence": {
    "image_url": "oss://...",
    "bbox": [420, 188, 530, 244],
    "reference_asset_id": "asset_vivo_logo_001"
  },
  "object_pack": {
    "object_pack_id": "pack_oppo_competitor_brand",
    "version": "v1.2"
  },
  "model_trace": {
    "detector": "logo_detector_v1",
    "ocr": "ocr_v1",
    "verifier": "multimodal_llm_v1"
  }
}
```

## 5. 对 Plan DSL 的影响

### 5.1 需要新增 Plan action

| Action | 说明 | 风险 |
|---|---|---|
| `visual_compliance.subscription.create` | 创建视觉合规订阅 | 高 |
| `object_pack.create` | 创建对象包 | 中高 |
| `object_pack.update` | 修改对象包 | 高 |
| `object_pack.apply_version` | 将对象包新版本应用到订阅 | 高 |
| `reference_asset.add` | 添加参考素材 | 中 |
| `reference_asset.remove` | 删除参考素材 | 中高 |
| `visual_rule.update` | 修改规则 | 高 |
| `sample.add_from_feedback` | 从反馈加入样本 | 中 |
| `camera.recommend_for_objects` | 按对象推荐摄像头 | 中 |

### 5.2 计划卡必须新增展示项

| 字段 | 说明 |
|---|---|
| 对象包 | 使用哪个对象包和版本 |
| 巡检对象 | 必须出现对象、禁止出现对象 |
| 规则 | 必须出现、禁止出现、风格匹配、内容匹配 |
| 参考素材状态 | 已上传、缺失、素材需完善 |
| 影响订阅 | 对象包修改会影响哪些订阅 |
| 摄像头推荐 | 推荐摄像头数和待确认摄像头 |
| 置信策略 | 低置信是否进入待确认 |
| 证据策略 | 是否需要检测框、参考图对比、全景图 |

## 6. 对结果中心和统计的影响

### 6.1 新增事件类型

| 事件类型 | 说明 |
|---|---|
| `VISUAL_REQUIRED_OBJECT_MISSING` | 必须对象缺失 |
| `VISUAL_FORBIDDEN_OBJECT_FOUND` | 禁止对象出现 |
| `VISUAL_STYLE_MISMATCH` | 风格不符 |
| `VISUAL_CONTENT_MISMATCH` | 屏幕/海报内容不符 |
| `VISUAL_LOW_CONFIDENCE_PENDING` | 低置信待确认 |

### 6.2 新增统计维度

1. 对象包。
2. 对象名称。
3. 对象类型。
4. 规则类型。
5. 品牌。
6. 素材版本。
7. 对象包版本。
8. 摄像头点位。

### 6.3 新增指标

1. 视觉合规率。
2. 问题门店数。
3. 禁止对象出现次数。
4. 必须对象缺失次数。
5. 待确认事件数。
6. 视觉合规误报率。
7. 对象包覆盖率。
8. 参考素材缺失率。

## 7. 对知识库和词典的影响

### 7.1 需要新增的知识资产

| 资产 | 说明 |
|---|---|
| 对象包库 | 客户定义的对象和素材 |
| 品牌库 | 授权品牌、禁用品牌、竞品品牌 |
| 物料词典 | 立牌、展架、海报、台卡等 |
| 场景点位词典 | 展厅全景、电视区、柜台、墙面等 |
| 规则模板库 | 必须出现、禁止出现、内容匹配等 |
| 样本库 | 正样本、负样本、badcase |

### 7.2 RAG 边界

视觉合规场景不应让 RAG 直接决定“是否违规”。RAG 只用于：

1. 检索客户 SOP。
2. 解释品牌/物料规范。
3. 推荐对象包和规则模板。
4. 生成配置草稿。

是否违规必须由 Skill 链路基于对象包、规则、检测结果和证据输出。

## 8. 对评测集的影响

### 8.1 意图评测集新增样本

需要新增至少 150 到 300 条视觉合规语料，覆盖：

1. 创建视觉合规订阅。
2. 修改对象包。
3. 添加禁用品牌。
4. 上传/添加参考素材。
5. 查询视觉合规结果。
6. 统计合规率。
7. 将误报加入负样本。
8. 摄像头推荐。

示例：

| 用户话术 | 目标意图 |
|---|---|
| 帮我检查 OPPO 门店有没有其他品牌 Logo | `VISUAL_COMPLIANCE_SUBSCRIPTION_CREATE` |
| 把小米 Logo 加入禁用品牌 | `OBJECT_PACK_UPDATE` |
| 这张立牌是新版本，加入极狐对象包 | `REFERENCE_ASSET_ADD` |
| 上周哪些门店电视广告不合规 | `VISUAL_COMPLIANCE_RESULT_QUERY` |
| 统计华东区竞品 Logo 出现次数 | `VISUAL_COMPLIANCE_STATS_QUERY` |

### 8.2 Skill 评测集新增样本

需要按对象类型建立视觉样本：

1. Logo 正负样本。
2. 海报正负样本。
3. 屏幕广告匹配样本。
4. 统一座椅/展架样式样本。
5. 其他品牌车辆样本。
6. 摄像头角度不佳样本。
7. 遮挡、反光、模糊样本。

## 9. 分阶段落地建议

### 9.1 P0：最小可用扩展

必须做：

1. 新增 `VISUAL_COMPLIANCE_SUBSCRIPTION_CREATE` 或在 `SUBSCRIPTION_CREATE` 下增加视觉合规子类型。
2. 新增 `OBJECT_PACK_CREATE`、`OBJECT_PACK_UPDATE`、`REFERENCE_ASSET_ADD`。
3. Plan DSL 支持对象包、规则模板、参考素材。
4. 新增通用 Skill：`visual_compliance_inspection`。
5. 工具库新增对象包检索、Logo/OCR、图片相似度、规则引擎。
6. 结果中心新增视觉合规事件类型。
7. 反馈支持加入正/负样本。

可以暂缓：

1. AI 自动推荐摄像头。
2. 自动标定候选。
3. 对象包批量导入。
4. 行业模板市场。
5. 复杂空间布局判断。

### 9.2 P1：提升灵活性和准确性

1. `CAMERA_RECOMMEND_FOR_OBJECTS`。
2. 对象包版本影响分析。
3. 屏幕区域检测。
4. 品牌库模板。
5. 对象包批量导入。
6. 低置信人工复核队列。

### 9.3 P2：产品化扩展

1. 汽车展厅、手机门店、零售陈列行业模板。
2. 客户自助评测。
3. 高频对象轻量模型微调。
4. 多客户对象包市场。

## 10. 研发改造清单

### 10.1 意图识别

1. 新增视觉合规意图分类。
2. 新增对象、品牌、素材、规则槽位抽取。
3. 增加 must-have / forbidden / match 三类规则判断。
4. 增加对象包实体解析。
5. 增加对象包影响范围追问。

### 10.2 Plan DSL

1. 扩展 `slots.visual_compliance`。
2. 扩展 `actions.object_pack.*`。
3. 扩展 `actions.reference_asset.*`。
4. 扩展 `validators.object_pack_version_check`。
5. 扩展 `validators.reference_asset_required_check`。

### 10.3 工具与 Skill

1. 新增对象包读取工具。
2. 新增品牌库读取工具。
3. 新增 OCR 工具。
4. 新增 Logo 检测工具。
5. 新增参考图相似度工具。
6. 新增规则引擎工具。
7. 新增证据生成工具增强。

### 10.4 前端页面

1. 应用订阅页新增视觉合规配置区。
2. 新增对象包管理页面。
3. 结果详情新增参考图对比。
4. 计划卡新增对象包和素材状态。
5. 反馈入口新增“加入正样本/负样本”。

### 10.5 数据模型

1. 新增 `ObjectPack`。
2. 新增 `ObjectDefinition`。
3. 新增 `ReferenceAsset`。
4. 新增 `VisualRuleTemplate`。
5. 扩展 `Subscription`。
6. 扩展 `InspectionEvent`。
7. 扩展 `Feedback`。

## 11. 最终判断

新增“门店视觉合规巡检”场景对当前产品能力的影响可以总结为：

1. **不改变主架构**：继续复用对话入口、Plan DSL、应用订阅、工作流、结果中心、反馈链路。
2. **需要扩展意图体系**：尤其是对象包创建/修改、参考素材补充、规则变更、视觉合规查询和统计。
3. **需要新增通用 Skill 模板**：`visual_compliance_inspection`，但不需要为极狐、OPPO 分别做 Skill。
4. **需要引入对象包作为核心业务实体**：对象包是满足通用性和灵活性的关键。
5. **需要扩展工具库**：对象包检索、品牌库、OCR、Logo 检测、图片相似度、规则引擎。
6. **需要扩展评测集**：否则意图识别和视觉识别都会在真实客户表达中不稳定。

建议优先级：

> P0 先做“对象包 + 视觉合规订阅 + 通用 Skill + 结果证据 + 对话创建/修改”，P1 再做摄像头推荐、自动标定、行业模板和对象包批量导入。
