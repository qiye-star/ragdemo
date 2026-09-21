# 架构决策记录（ADR）

每篇 ADR 记录一个**难以逆转**的技术决策：为什么这么选、放弃了什么、
以及**什么情况下应该推翻它**。

最后一项是 ADR 与普通文档最大的区别。写下推翻条件，是为了让未来的人
（包括三个月后的自己）能判断「现在是不是该改了」，而不是对着一个旧决定发呆。

## 格式

```markdown
# ADR-NNNN · 标题

- 状态：已接受 | 已废弃 | 被 ADR-MMMM 取代
- 日期：YYYY-MM-DD
- 决策者：<who>

## 背景
## 备选方案
## 决策
## 后果
## 推翻条件
```

## 索引

| # | 标题 | 状态 | 日期 |
|---|---|---|---|
| [0001](0001-bm25-paradedb-pg-search.md) | 中文全文检索用 ParadeDB `pg_search` | 已接受 | 2026-09-21 |
| [0002](0002-self-hosted-paradedb-single-db.md) | 自建 ParadeDB 单库，不用托管 RDS | 已接受 | 2026-09-21 |
| [0003](0003-langgraph-with-own-orchestrator.md) | Agent 编排用 LangGraph + 自有封装 | 已接受 | 2026-09-21 |
| [0004](0004-hosted-embedding-reranker-api-1024d.md) | 嵌入与重排走托管 API，维度固定 1024 | 已接受 | 2026-09-21 |
| [0005](0005-announcement-provider-abstraction.md) | 公告数据源先定接口、不锁定供应商 | 已接受 | 2026-09-21 |
| [0006](0006-no-frontend-until-p4.md) | P1–P3 不做 Web 前端 | 已接受 | 2026-09-21 |
| [0007](0007-opinion-scoring-dual-track.md) | 观点评分双轨制，分开存不合并 | 已接受 | 2026-09-21 |
| [0008](0008-textin-xparse-document-parsing.md) | 通用文档解析用 TextIn xParse，取代 MinerU | 已接受 | 2026-09-21 |
| [0009](0009-chroma-as-vector-candidate-generator.md) | Chroma 承担向量召回，pgvector 保留为时点权威 | 已接受 | 2026-09-21 |

## 什么时候要写新 ADR

- 改动上述任一决策；
- 引入新的存储、编排框架或外部依赖；
- 改变时点语义、溯源规则或合规边界的任何部分；
- 在两个方案之间做了取舍，且三个月后的人会问「为什么不用另一个」。

不需要写 ADR 的：库的小版本升级、参数调优（那些记在评测报告里）、
纯实现细节。
