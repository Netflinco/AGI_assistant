# EAS OVD 动态对象候选回归（2026-08-27）

## 结论

实时视觉链路已从固定人体候选扩展到受控动态物体候选。真实 EAS `pytrt_sam3` 与已配置的 `Qwen3-VL-8B-Instruct-FP8` 在无人物、无品牌的合成测试图中正确识别背包、水瓶、椅子和桌子；动态对象规划、裁剪放大证据板、VLM 证据契约与瞬态数据清理均通过。

## 真实 EAS 回归

测试夹具：[ovd_object_scene_2026-08-27.png](/Users/dimeng/dimeng_Codex/深象AGI助手/test_fixtures/ovd_object_scene_2026-08-27.png)。图中只有红色背包、灰色椅子、透明水瓶和木桌，不含人物、品牌或监控内容。

| Prompt | EAS score | `bbox_xyxy`（原图 1402×1122） | 结果 |
| --- | ---: | --- | --- |
| `backpack` | 0.963 | `[80,504,399,951]` | 通过 |
| `bottle` | 0.962 | `[1029,322,1095,536]` | 通过 |
| `chair` | 0.981 | `[276,232,799,977]` | 通过 |
| `table` | 0.977 | `[848,508,1276,990]` | 通过 |

端到端受控回归使用动态提示词 `backpack/bottle/chair/table`：EAS 返回 4 个候选框，生成了“裁剪放大 + 完整画面”单图证据板，并在返回前清除了 `_ovd_candidates` 与 `_ovd_candidate_board_url`。该受控回归用确定性 VLM 契约桩验证候选板传递与证据聚合。

真实模型联合回归向同一张合成图提问“画面中是否有红色背包、透明水瓶、灰色椅子和木桌？”，得到：

- `business_policy=OBSERVATION_ONLY`、`status=POSITIVE`、`target_observed=true`；
- 结论仅回答被询问对象：“画面中存在红色背包、透明水瓶、灰色椅子和木桌。”；
- `target_evidence` 返回 4 项带位置和 `bbox_1000` 的证据，分别对应背包、水瓶、椅子、桌子。

首轮真实回归曾出现模型把未被询问的服务行为混入结论、并给出 `target_observed=true/status=NEGATIVE` 的矛盾结果。已修复为：业务策略仅由用户 query 中的明确语义确定；纯事实查询强制为 `OBSERVATION_ONLY`，其状态由可复核的目标证据统一。修复后已按上述真实模型联合回归复测通过。

## 本地回归

`ovd_eas_integration_test.py` 覆盖以下十种 query 形态：人员红衣、背包、行李箱、水瓶、椅子、桌子、地面垃圾、手机、二维码、出口标识。另验证 URL、角色/系统指令等非法提示词不会发往 OVD；人员与物体候选均保持“空检测不等于不存在”。

```bash
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B ovd_eas_integration_test.py
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B visual_dynamic_query_test.py
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B online_agent_test.py
```

## 风险与建议

- 该合成图验证的是接口、检测框和链路，不替代门店真实场景中的召回率评估。
- 门店画面需要在独立的外发授权、脱敏和抽样计划下评估，特别是小物体、遮挡、逆光和远距离目标。
- 模型规划失败时系统回退 VLM-only；模型/检测器失败或无框不能输出目标不存在。
