# 同租户跨门店多轮上下文 V2 回归报告

| 项目 | 结果 |
| --- | --- |
| 日期 | 2026-08-27；2026-08-31 旧会话坏例补充回归 |
| 总结论 | PASS |
| 范围 | 会话决策、路由、门店范围、权限、证据、VLM/OVD、持久化、并发、跨域隔离、前端展示 |
| 发布结论 | P0 核心链路可交付本地验收 |

## 1. 节点结果

| 节点 | 验证内容 | 结果 |
| --- | --- | --- |
| N1 Context Load/Recover | 优先读取同 conversation + tenant + user 的 ACTIVE revision；无 ACTIVE 时只在高置信续问下从过期 revision/旧视觉消息恢复语义；TTL 过期强制重抓 | PASS |
| N2 Continuation Resolver | 新任务/续问/歧义；KEEP、EXPAND、NARROW、RETURN_PAGE、PREVIOUS、COMPARE；`在帮我`窄纠错 | PASS |
| N3 Domain/Plan Gate | 视觉续问先于 OpenQA；天气/PPT/上映等新问题不继承视觉上下文；待补槽 Plan 不抢占跨域新问题 | PASS |
| N4 Permission Gate | 服务端实时 `authorized_org_ids`；撤权/越权在摄像头、抓图、OVD/VLM 前拒绝 | PASS |
| N5 Scope Resolver | 页面 A/实际 B 分离；其他门店排除活动范围；上一家恢复；两店比较合并；“这家”歧义不猜测 | PASS |
| N6 Evidence Resolver | 明确同帧才读取归档证据；租户/权限/受控路径/SHA-256/大小校验；内部 data URL 不落库不返前端 | PASS |
| N7 Capture/Coverage | “现在/再看”沿用范围抓最新帧；跨店/返回页面店重新取证；未指定点位的对象查找覆盖目标店全部在线镜头，不受单批模型上限截断 | PASS |
| N8 VLM/OVD | 动态问法合并；人员枚举；背包、行李箱、水瓶、椅子、桌子、垃圾、手机、二维码、出口标识等开放词汇候选；弱阴性降级不确定 | PASS |
| N9 Persistence | 不可变 revision、supersede、作用域历史、服务重连恢复、同版并发只允许一个写入者激活 | PASS |
| N10 Response/UI/Audit | `conversationScope` 显示页面店、实际范围、范围来源、证据策略、版本；执行 trace 含 recover/context/permission/scope/evidence 节点 | PASS |

## 2. 2026-08-31 坏例闭环

| 缺陷 | 优先级 | 期望 | 实际 | 修复结果 |
| --- | --- | --- | --- | --- |
| CTX-LEGACY-001 | P0 | 旧会话中视觉分析后输入“在帮我找一个灰色的沙发”，继承门店视觉任务并重抓画面 | `conversation_contexts` 无 ACTIVE 记录，直接进入 OpenQA 输出购物建议 | 新增旧视觉消息懒恢复、`在→再`窄纠错、证据过期强制重抓和 `conversation.context.recover` Trace；已通过 |
| VIS-COVERAGE-001 | P0 | “看东莞店当前镜头画面，找一个黑色沙发”对当前店所有在线镜头做视觉查找 | 模型误判 `CAPTURE_SNAPSHOT`，并将“东莞店当前镜头”误抽为设备名，触发 `vlm.camera.select` 后只抓 1 张，未调用 `vlm.image.inspect` | 增加开放谓词 Intent Guard、摄像头台账校验和 Camera Coverage Planner；17 路模拟门店实际抓取 17 张、5 批分析后命中第 17 路；已通过 |
| VIS-COVERAGE-002 | P0 | 未覆盖所有可用镜头时不输出确定性“未发现” | 1 路抓图失败时，其余画面的批内否定可能被合并为全店否定 | `coverage_status=PARTIAL`，否定状态强制降级 `UNCERTAIN`，返回计划/成功镜头数和失败明细；已通过 |
| VIS-COVERAGE-003 | P0 | 在包含旧“展厅3”结果的同一历史会话重复全店查找，仍应检查全部在线镜头 | 意图已是 `ANALYZE_VISUAL`，但 LLM 从近期历史返回真实存在的 `camera_names=[“展厅3”]`；旧逻辑只查台账存在性，因而仍只抓 1 张 | 新增当前 utterance grounding；真实页面在原会话 `conv_8eefdae85872` 重放原 query，得到 `camera_names=[]`、候选 17、抓图 17、VLM 输入 17、3 批合并、覆盖 `FULL`；已通过 |
| UI-COVERAGE-001 | P1 | 执行范围和点位推理必须与后端覆盖数一致 | 视觉链路已处理 17 路，但前端将 `evidence_mode=NONE` 误译为“无需视觉证据”，并把 `CAMERA_COVERAGE` 当成点位匹配显示“0路” | `NONE` 改为“不复用历史证据”；`CAMERA_COVERAGE` 使用 `eligible/captured` 独立渲染；前端契约与 JS 语法检查通过 |

真实会话 `conv_8eefdae85872` 只读决策复核：`CONTINUE / VISUAL_INSPECTION / KEEP_SCOPE / RECAPTURE_RESOLVED_SCOPE`，恢复范围为 `kuka00003 / LAZBOY乐至宝东莞红星综合店`，规范化文本为“再帮我找一个灰色的沙发”。

## 3. 关键对话回归

1. 页面 A 店查 B 店红色沙发：实际只抓 B 店，页面范围不变。
2. 续问“灰色的呢”：继承 B 店和上一任务语义，抓 B 店最新帧。
3. 续问“这张图里有几个”：不调快照接口，使用通过完整性校验的归档帧。
4. 页面 A、任务 B 时问“这家呢”：返回 `NEED_CLARIFICATION`，不抓图、不改写上下文。
5. “当前门店呢”：切回 A 店并重新取证。
6. “其他门店也看一下”：进入多店编排，排除当前活动任务范围。
7. “上一家呢”：从 `scope_history` 恢复而不是重复当前店。
8. “这两家对比一下”：合并当前和上一范围，生成多店计划。
9. 多店 Plan 待补槽后问天气：进入 OpenQA，不被 Plan 抢占，视觉 context 保留供以后显式恢复。
10. 无权查 B 店：快照调用计数不变，不泄露受限证据。
11. 两个并发请求基于同一 revision：第二个返回 `STALE_CONTEXT`。
12. 其他用户/其他租户读取该会话 context：返回空。
13. context 上线前的旧视觉会话续问灰色沙发：恢复任务语义和门店范围，不进入 OpenQA，因旧证据超时而重抓当前画面。
14. 相同旧视觉会话后问天气：不激活视觉恢复，仍进入 OpenQA。
15. 开放物体查找即使被意图模型误标为快照，也由门禁升级为视觉分析；不调用单镜头语义选择。
16. 17 路在线镜头、单批上限 4：抓取 17 张，分 5 批分析，末批命中可汇总为肯定结论。
17. 17 路中 1 路抓图失败：成功分析 16 路，全店否定结论降级为待复核。
18. 17 路分 5 批均未命中：仅当 5 批全部返回 `coverage=FULL` 时合并为全店否定，核验对象数按批次求和为 17。
19. 同一历史会话已出现“展厅3”后重复全店黑色沙发查找：模型即使返回合法台账镜头槽位，本轮未明示就清除，覆盖全部 17 路。
20. 用户本轮明确说“展厅3”的对照用例：槽位保留，只抓取台账唯一匹配的展厅3，不影响定点查询。

## 4. 自动化执行记录

| 测试 | 结果 |
| --- | --- |
| `conversation_context_test.py` | PASS：resolver、scope、permission、evidence、routing、persistence、concurrency、UI artifact |
| `visual_dynamic_query_test.py` | PASS：人员局部化、任意属性、阴性证据门禁 |
| `ovd_eas_integration_test.py` | PASS：EAS 协议、动态对象 prompt、裁剪板 + 全图 VLM |
| `online_agent_test.py` | PASS：在线 Agent 路由、媒体、分页、DTO、脱敏、分析 |
| `agent_governance_test.py` | PASS：门禁、能力开关、路由、审计 |
| `open_research_test.py` + `web_search_test.py` | PASS：公开检索证据和秘钥/引用边界 |
| `open_research_office_smoke_test.py` | PASS：Research/Office/巡检跨域隔离与 ACL |
| `smoke_test.py` | PASS：HTTP、前端契约、Plan、权限、证据、审计、OpenQA PDF |
| `node --check static/app.js` | PASS |
| 真实本地页面 + PaaS/VLM 同会话重放 | PASS：`2026-08-31 15:26:30`提交，`15:29:40`完成；17 路候选/17 张快照/17 张模型输入/3 批聚合，`coverage_status=FULL` |

## 5. 明确降级边界

- 指定历史时刻但未接入录像抽帧服务时，链路返回 `BLOCKED` + `media.frame.extract:unavailable`，不使用当前快照代替。
- OVD 无候选、VLM 证据覆盖不足、抓图失败时返回 `UNCERTAIN/BLOCKED`，不把“未检测到”当成“不存在”。
