# EAS OVD 实时视觉辅助 QA（2026-08-27）

## 结论

EAS `pytrt_sam3` 已完成协议适配并接入实时“存在性”视觉链路。它根据用户 query 动态规划并校验开放词汇候选，而非只支持人员；合成无内容图片的真实公网调用返回成功，鉴权、请求路径、请求体和响应转换均已验证。未向 EAS 发送含人员的监控快照：这需要对具体视频证据的外发取得单独授权，因此真实人体召回效果仍待获批后验证。

## 已验证用例

| 用例 | 预期 | 结果 |
| --- | --- | --- |
| EAS 请求契约 | `inputParaJson` 含 requestID/clientID/动态校验后的 object prompts/threshold，图片为 Base64 | 通过 |
| EAS `[x,y,w,h]` | 转为边界校验后的内部 `bbox_xyxy` | 通过 |
| EAS 业务错误 | 不可伪装为空检测，且不回传供应方内部错误 | 通过 |
| 红衣人员查询 | `person` 候选框提示 VLM，VLM 输出原始 query 的定位证据 | 模拟链路通过 |
| 非人员问题 | 由受控规划器生成具体对象类别；URL、角色/系统指令等不能成为 OVD prompt | 通过 |
| 真实 EAS 合成图 | 鉴权、HTTPS 路径、`pytrt_sam3`、响应契约 | 通过；0 个检测为合理结果 |
| 既有动态视觉回归 | 红衣、背包、弱否定、完整否定与自然语言问法 | 通过 |
| 在线 Agent 回归 | 路由、媒体、槽位、脱敏与视觉既有契约 | 通过 |

## 执行命令

```bash
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B ovd_eas_integration_test.py
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B visual_dynamic_query_test.py
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B online_agent_test.py
```

## 已知限制与发布建议

- 当前环境没有 Node.js，`static/app.js` 的 Node 语法检查未执行；本次没有修改前端代码。
- 合成图成功不能替代真实监控样本的人体召回验证。取得“将指定监控快照发送至 EAS”的明确授权后，应使用已脱敏/已批准样本复跑，并核验人框是否覆盖人员。
- 建议以 EAS 环境变量启动服务；Token 仅通过部署平台的密钥注入，不写入 shell 配置、仓库、数据库或日志。
