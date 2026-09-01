# Agent 能力中心 UX 回归测试报告

日期：2026-07-16

## 测试范围

- Agent 能力中心信息架构：顶部引导、推荐操作、三步流程、目录导航。
- Skill/Tool 目录展示：从字段表改为能力卡片，突出触发意图、默认工具、必填信息、风险等级和管理边界。
- 目录导航优化：将“业务能力”改为“Skill 列表”，放大可点击 tab，弱化只读统计区。
- Manifest 导入流程：模板选择、导入前检查、JSON 编辑区、校验反馈。
- Manifest 编辑新版本：已导入能力应基于原始 Manifest 生成草稿，并提供显性取消返回。
- 知识库管理：支持本地图片上传与图片地址导入并存，补充知识条目删除能力。
- 响应式体验：桌面 1280px 与移动端 390px 宽度无横向溢出。
- 现有功能回归：前端契约、计划确认、权限、证据、数据分析、反馈、审计和脱敏。

## 测试命令

```bash
'/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node' --check static/app.js
python3 -m py_compile server.py smoke_test.py agent_core.py agent_skills.py
python3 smoke_test.py
```

## 浏览器验收

- 页面地址：`http://127.0.0.1:8000/`
- 默认进入 Agent 能力页后：
  - 标题为“Agent 能力”，副标题为“管理 OPPO 可被 Agent 调用的 Skill、工具与知识资产”。
  - 顶部展示“把门店巡检经验变成可执行的 Agent 能力”引导区。
  - 展示轻量“只读统计”条和 15 个 Skill 卡片。
  - 桌面宽度 `clientWidth=1280`，`scrollWidth=1280`，无横向溢出。
- 点击“创建 Skill 能力”：
  - 进入 Manifest 导入模式。
  - 自动填充 Skill 模板。
  - 显示“导入前检查”说明和 Skill 模板提示。
- 切换“工具模板”：
  - 面板状态切换为 `tool`。
  - 自动填充 Tool 模板。
  - “校验 Manifest”返回校验通过。
- 移动端 390px 宽度：
  - `clientWidth=390`，`scrollWidth=390`。
  - 顶部引导、概览卡和目录 tab 自动单列/双列收敛。
  - “创建 Skill 能力”按钮可见且可点击。
- 浏览器控制台：未发现 error 级错误。
- 切换“知识库”：
  - 新增“本地上传图片”与“图片地址导入”双入口，本地文件优先生效。
  - 本地上传支持 JPG、PNG、WebP、GIF，单文件上限 8MB。
  - 知识列表展示“删除”操作，删除前有二次确认文案。
  - 表头包含“知识 / 类型 / 模态 / 内容摘要 / 素材 / 更新时间 / 操作”。
  - 桌面宽度 `clientWidth=1280`，`scrollWidth=1280`，无横向溢出。
- 点击已导入 Skill `QA OPPO 竞品 Logo 巡检` 的“编辑新版本”：
  - 本地 sqlite 原始 `manifest_json` 已核验为 Logo 巡检链路，包含 `QA_OPPO_CHECK_COMPETITOR_LOGO`、`knowledge.retrieve`、`vlm.image.inspect`。
  - 编辑器草稿与原始 Manifest 一致，不再出现消防通道模板内容。
  - 顶部提示为“正在基于「QA OPPO 竞品 Logo 巡检」的原始 Manifest 编辑新版本”。
  - 操作区新增“取消”按钮，点击后返回 Skill 列表，Manifest 面板隐藏。
  - 浏览器控制台：未发现 error 级错误。

## 测试结论

通过。

本轮优化降低了 Agent 能力中心的理解门槛：默认先解释“能力是什么、如何创建、上线前如何保障安全”，再进入专业 Manifest 配置；Skill/Tool 列表从工程字段表转换为面向业务操作的能力卡片，同时保留 Manifest 导入、校验和目录管理能力。

补充优化：针对“只读统计误导为可点击入口”的问题，已将原大尺寸概览卡改为轻量状态条；针对管理完整性，内置 Skill/Tool/Intent 采用“复制为模板”而非直接编辑/删除，导入项展示“编辑新版本”入口。停用/删除需要后端软删除、二次确认和审计接口后再开放。

知识库补充优化：已新增 `/api/agent/knowledge-assets` 图片上传接口和 `/api/agent/knowledge/{id}` 删除接口。上传文件落到 `/static/uploads/knowledge/...`，知识删除采用软删除并记录审计，避免误删后影响历史链路追溯。

Manifest 编辑补充优化：已确认 QA Logo 记录的 mock 数据本身不是消防通道内容，偏差来自前端“模板 + 少量字段覆盖”的草稿生成策略。现在后端会随目录返回已导入项的原始 Manifest，前端点击“编辑新版本”时优先使用原文，缺失原文时才降级到模板生成。

## 残余风险

- Manifest 仍是专家入口，适合管理员或实施人员；后续可继续增加表单化创建向导。
- 第三方 Skill/工具仍保持目录注册模式，未扩展真实执行器绑定。
- 当前知识删除不物理清理已上传素材文件，后续如果需要空间治理，可补充未引用素材的定期清理任务。
- “编辑新版本”当前保留原版本号，需要配置人员在 JSON 中调整版本号后再导入；后续可增加自动建议升版。
