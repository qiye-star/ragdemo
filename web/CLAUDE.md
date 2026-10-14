# web/ — 内部只读诊断界面（非产品前端）

依据 ADR-0010（内部诊断界面提前到 P1，收窄 ADR-0006）。面向用户的产品前端仍在 P4，**这里不是它**。

## 这个目录里有什么

| 路径 | 用途 |
|---|---|
| `index.html` | 页面骨架：固定顶部横幅「内部诊断工具，非产品界面」、`as_of` 栏；CSP 为 `default-src 'none'`、`form-action 'none'`，且没有任何 `<form>` |
| `app.js` | hash 路由；导航由 `VIEWS` 数组驱动 |
| `views/` | 视图：`docs` `layout` `blocks` `quality` `tiers` `isolation` `retrieval` |
| `lib/` | 公共库：`api.js`（只发 GET）、`state.js`（as_of/路由）、`dom.js` `status.js` `fmt.js` `table.js` `chart.js` `svg.js` |
| `style.css` | 样式 |

后端是 `packages/ragdemo/src/ragdemo/api/`（FastAPI，只读 GET，`app.py` 里把本目录静态挂在 `/`，位置必须放最后）。

## 怎么用

```bash
set -a; . ./.env; set +a
make up && uv run ragdemo db migrate
# 需要 RAGDEMO_API_DSN（只读角色，见 `ragdemo db grant-api-read`），或开发逃生口 RAGDEMO_API_ALLOW_PRIVILEGED_DSN=1（见 .env.example）
make serve                       # http://127.0.0.1:8088（RAGDEMO_API_HOST/PORT 可改）
make seed-isolation-demo         # 给隔离探针矩阵造演示数据（仅回环库）
```

静态文件无构建步骤，改完刷新浏览器即可。

## 硬规则

- **只读**：不得出现写操作（录评测、确认观点、编辑块）；否则它已不是诊断工具，须走 W8.1b 的完整权限/审计（ADR-0010 推翻条件 1）。标注入口仍是 CLI。
- **强制 as_of**：所有查询带 `as_of`，没有默认值；as_of 被拦时显示 `asOfBlockedCard`，不得绕开。
- **仅内网、不给团队之外的人用**；横幅不得移除。不出具任何结论/评级/买卖建议（CLAUDE.md §0）。
- **不引入 Node 构建链、不依赖 CDN**（CSP 在浏览器层强制）；引入 npm 须另写 ADR。
- 不展示未过 `asof` 视图的数据；用户上传私有材料不得出现在公共检索视图（隔离视图只用于探针演示）。
- 维护成本应接近 0：超过阶段工时 10% 就砍掉退回 CLI。

## 常见坑

- `app.js` 里的 `placeholder` 视图只是占位，不代表缺失 bug。
- `web_root` 可由 `RAGDEMO_API_WEB_ROOT` 覆盖，默认是相对路径 `web`，所以 `serve` 必须在仓库根启动。
- 端口 8088 避开 5433/8001/8080–8086/3000，不要撞。
