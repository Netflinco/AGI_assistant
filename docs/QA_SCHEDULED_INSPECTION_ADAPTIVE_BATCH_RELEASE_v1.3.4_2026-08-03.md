# 周期巡检自适应分批发布与回归报告

- 版本：v1.3.4
- 日期：2026-08-03
- 范围：知识库 SKU 比对、周期巡检证据标记、视觉模型候选批处理

## 本次问题与修复

1. 模型单次处理预算曾被当作巡检镜头总数上限，后续镜头有被截断的风险。
2. 结果归一化错误使用 `max_images` 截断 `selected_camera_names` 与 `anomaly_camera_names`，导致已判定风险的后续镜头没有红框。
3. 单个候选镜头模型失败时只累加失败计数，界面未说明该镜头未完成 SKU 判断，容易被误认为“未命中且未报出”。

修复后，全部已归档快照按 `candidate_batch_size` 自适应分批分析并合并结果；SKU 规则始终按镜头执行：命中任一库内 SKU 不报风险且带标签，可识别出样但未命中才报风险。每张候选快照失败后会自动重试一次；持续失败会作为“未完成判断”写入执行说明和画面依据，不被误判为风险或 SKU 未命中。

历史执行链路中可识别出该失败镜头时，前端会在对应图片上显示黄色虚线和“待复核”标签；这与红色“异常证据”和蓝色 SKU 标签互斥，避免把模型失败伪装成正常或风险结果。

## 自测结果

| 用例 | 预期 | 结果 |
| --- | --- | --- |
| 7 张快照，候选批大小 2 | 分为 4 批，7 张均纳入最终结果 | 通过 |
| 1 张 SKU 命中、6 张未命中 | 命中镜头不报风险；6 张未命中均进入风险证据 | 通过 |
| 展厅4首轮候选调用失败 | 自动重试一次后完成判断，不丢失镜头 | 通过 |
| 历史执行链路仅保存失败计数 | 从候选输出与归档快照差集识别待复核镜头 | 通过 |
| 风险镜头数量超过单次输入配置 | 风险镜头名称不再按 `max_images` 截断 | 通过 |
| Python/前端语法 | 后端与前端可解析 | 通过 |
| 全量冒烟回归 | 既有前后端、权限、证据、审计、SKU 合约通过 | 通过 |

执行命令：

```bash
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m py_compile online_agent.py server.py smoke_test.py
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --check static/app.js
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 smoke_test.py
```

## 已知运行记录说明

2026-08-03 11:00 的历史批次 `run_459aea730bfa` 共归档 17 张快照，模型完成了 16 张候选判断，`展厅13` 出现单镜头模型失败。旧版本未向界面展示该失败状态，且将风险证据截为前 8 张。新版本会从该批次保留的候选结构化输出恢复其余 5 张已判定 SKU 未命中风险，并将展厅13明确展示为“待复核”；下一次巡检则会使用全量自适应分批链路。
