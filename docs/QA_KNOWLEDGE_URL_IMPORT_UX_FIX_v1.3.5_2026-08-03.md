# 知识库 URL 图片导入交互优化与回归报告

- 版本：v1.3.5
- 日期：2026-08-03
- 范围：Agent 能力 > 知识库 > 图片素材 URL 导入

## 交互改造

URL 导入由始终展开的表单改为按需展开的独立操作区：

1. 新建知识的默认态仅显示“在线图片地址导入”说明和“通过 URL 添加”按钮，不再与本地上传区争夺空间。
2. 点击按钮后，URL 图片地址及其 SKU、视角、特征说明在整行宽度内展开；再次点击即可收起。
3. URL 已填写时显示“已填写”状态与保存说明，避免用户误以为地址会丢失或即时上传。
4. 编辑知识时标题切换为“追加在线图片”，明确不会影响已保留素材；默认同样收起，避免素材列表下方字段拥挤。
5. SKU 仍沿用既有校验：该图 SKU 或表单顶部默认 SKU 至少提供一个；视角与特征说明保持可选。
6. 小屏幕下，URL 操作按钮会独占一行，避免标题、说明和按钮相互挤压。

## 自测结果

| 用例 | 预期 | 结果 |
| --- | --- | --- |
| 新建知识默认态 | URL 地址和图片元数据收起；按钮显示“通过 URL 添加” | 通过 |
| 新建知识展开态 | 地址与 SKU 元数据可见，操作区占整行宽度 | 通过 |
| 填写 URL 地址 | 操作区显示“已填写”，并提示保存时导入 | 通过 |
| 编辑知识默认态 | 显示“追加在线图片”，不影响已保留素材，默认收起 | 通过 |
| 编辑知识展开态 | SKU、视角、特征说明字段完整显示，操作区占整行宽度 | 通过 |
| 前端语法 | `static/app.js` 可解析 | 通过 |
| 全量冒烟回归 | 现有前后端、权限、证据、审计和 SKU 合约均通过 | 通过 |
| 服务健康检查 | `http://127.0.0.1:8000/` 返回 HTTP 200 | 通过 |

执行命令：

```bash
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --check static/app.js
/Users/dimeng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 smoke_test.py
curl -sS -o /private/tmp/deepvision-url-import-home.html -w "%{http_code}" http://127.0.0.1:8000/
```
