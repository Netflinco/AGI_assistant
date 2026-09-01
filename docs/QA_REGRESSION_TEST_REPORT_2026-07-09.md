# QA 回归整改报告（2026-07-09）

## 结论

针对 `QA_AGENT_FUNCTIONAL_TEST_REPORT_2026-07-09.md` 中的 P0/P1 问题完成整改，并补充自动化回归。当前本地回归通过：

- `python3 -m py_compile online_agent.py server.py online_agent_test.py smoke_test.py`
- `python3 online_agent_test.py`
- `python3 credential_vault_test.py`
- `python3 smoke_test.py`

## 整改项

1. 巡检结论极性一致性
   - 禁止目标场景下，若结论明确包含“无散落垃圾”“无污渍”“符合清洁标准”等负向证据，不再被 `target_observed=true` 误标为异常。
   - `PROHIBITED_CONDITION + 否定结论` 输出 `NEGATIVE`，异常证据列表置空。

2. 精确摄像头匹配
   - 媒体类请求优先匹配上下文 `camera_id`、模型抽取 `camera_names`、用户文本中的精确摄像头名称。
   - 用户输入疑似完整镜头名但未命中时，不再用 VLM 语义选镜头兜底，改为等待用户确认。
   - VLM 选镜头相关性低于 `0.6` 时不自动抓图，返回候选镜头。

3. 统计排行意图路由
   - `统计/排行/趋势/Top` 类问题在规则识别为 `ANALYZE_ALARMS` 时确定性覆盖 LLM 的 `QUERY_ALARMS` 误判。
   - 对应工具链走 `paas.alarm.aggregate`。

4. 签名媒体 URL 持久化防护
   - `messages.linked_object` 写库前递归脱敏第三方签名 URL 参数。
   - 自动化断言禁止持久化 `OSSAccessKeyId`、`Signature`、`Expires`。

## 新增回归覆盖

- 结构化字段与自然语言结论冲突：正常清洁画面不能生成异常状态或异常证据。
- 精确镜头名：命中指定镜头时不调用 `vlm.camera.select`。
- 未匹配完整镜头名：等待确认，不抓取其他镜头。
- 低相关性语义选镜头：等待确认，不生成媒体结果。
- 统计排行：`近7天当前门店告警统计排行 Top10` 必须进入 `ANALYZE_ALARMS`。
- 数据安全：签名媒体 URL 不进入对话历史持久化字段。

## 补充整改：安全配置卡草稿保留

- 问题原因：聊天区后台轮询每 8 秒刷新会话与订阅状态，`renderMessages()` 会整体重建消息 DOM；安全接入表单没有本地草稿状态，导致未提交的租户名称、编码、AppKey 和 AppSecret 被新 DOM 清空。
- 漏测原因：此前回归覆盖了历史会话切换、断连提示、凭证脱敏和接入安全链路，但没有覆盖“用户正在填写嵌入式安全表单时发生后台轮询重绘”的交互时序。
- 整改结果：前端增加 `integrationSetupDrafts`，输入时即时保存草稿；重绘前捕获焦点、光标位置和最新表单值；重绘后按消息稳定 key 恢复 value、焦点和光标；提交成功后清理草稿。
- 回归加固：`smoke_test.py` 新增前端契约断言，确保草稿状态、输入监听、稳定 key、焦点恢复和租户名 value 绑定不会被后续改动删除。

## 残余风险

周期巡检第三方服务失败率治理仍属于 P2 稳定性任务，需要后续结合 DeepVision/VLM 健康检查、重试策略和失败原因分层继续加强。
