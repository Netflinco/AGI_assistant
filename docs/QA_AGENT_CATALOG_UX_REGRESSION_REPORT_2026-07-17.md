# Agent 能力目录 UX 回归测试报告

日期：2026-07-17

## 本轮优化范围

- 基于当前最新实现优化，不按历史截图复刻旧状态。
- 压缩 Agent 能力目录顶部说明区：移除大块“上线前自动评估”卡片，保留单行状态摘要。
- 统一已导入 Skill / Tool 与内置 Skill / Tool 的展示形态：全部使用同一能力卡片样式，通过“已导入 / 内置”标签区分来源。
- Skill / Tool 卡片新增“查看详情”入口。
- 已导入 Skill / Tool 新增“删除”入口，后端采用 Manifest 软删除并写审计。

## 测试命令

```bash
'/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node' --check static/app.js
python3 -m py_compile server.py smoke_test.py agent_core.py agent_skills.py
python3 smoke_test.py
```

## 浏览器验收

- 页面地址：`http://127.0.0.1:8000/`
- Skill 列表：
  - 顶部摘要压缩为一行：“目录状态 / Skill / 工具 / 意图 / 记忆知识 / 当前 Tab / 上线前校验”。
  - 不再展示独立的大块 `agent-quality-card`。
  - 不再展示额外大段 `agent-section-note` 说明。
  - 已导入 `QA OPPO 竞品 Logo 巡检` 使用与内置 Skill 一致的卡片样式。
  - 已导入 Skill 展示“查看详情 / 编辑新版本 / 删除”。
  - Skill 详情面板展示来源、唯一标识、版本、状态、风险等级、关联意图、用户说法、必填信息、执行步骤和原始 Manifest。
- 工具列表：
  - 已导入工具使用与内置工具一致的卡片样式。
  - 已导入工具展示“查看详情 / 编辑新版本 / 删除”。
- 桌面宽度：
  - `clientWidth=1280`
  - `scrollWidth=1280`
  - 无横向溢出。
- 浏览器控制台：未发现 error 级错误。

## 自动化回归

- 前端契约新增校验：
  - Skill / Tool 共用 `renderAgentCapabilityCard`。
  - Skill / Tool 支持 `renderAgentCatalogDetail`。
  - 已导入 Manifest 支持 `deleteAgentManifest`。
  - Skill / Tool 不再用 `agent-extension-section` 单独渲染已导入项。
- 后端契约新增校验：
  - `DELETE /api/agent/manifests/{manifest_id}` 仅管理员可用。
  - 删除后 Manifest 状态为 `DELETED`。
  - 删除后目录 `extensions` 不再返回该 Manifest。

## 结论

通过。

当前 Agent 能力目录更符合配置型 Agent 产品的使用路径：用户先看到清晰目录和可操作卡片，再按需查看详情、编辑新版本或删除已导入项；说明性内容被压缩到状态条，不再占用主要操作空间。

## 残余风险

- 删除为软删除，不物理清理历史 Manifest 记录，便于审计追溯。
- 已导入项删除后，如果其他导入 Skill 仍引用被删除工具，当前不会主动做依赖阻断；后续可补充删除前影响分析。
