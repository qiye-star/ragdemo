# ADR-0009 · Chroma 承担向量召回，pgvector 保留为系统真相与时点权威

- 状态：已接受
- 日期：2026-09-21
- 决策者：创始团队
- 影响：ADR-0002（自建单库）、ADR-0004（1024 维）

## 背景

ADR-0002 决定自建 ParadeDB 单库，向量随之落在库内 pgvector
（`core.doc_block.embedding vector(1024)` + HNSW，见 `02-data-model.md` §5.4）。
P1b 的嵌入管线已经在写这一列，P1c 的向量一路原本直接查它。

现在要求向量检索改用 Chroma。这不是一次纯粹的实现替换：它把向量从
**事务库内**挪到了**独立进程**，而本项目的最高优先级约束是时点一致
（`CLAUDE.md` §1.1、`03-point-in-time.md`）。

## 备选方案

**A. Chroma 完全取代 pgvector**
删除 `doc_block.embedding` 列与 HNSW 索引，向量只存 Chroma，
时点谓词靠 Chroma 的元数据过滤（`known_at` / `superseded_at` 存成 epoch 数值）。

**B. Chroma 做候选生成器，pgvector 保留为系统真相**
向量双写：`doc_block.embedding` 仍是持久真相，Chroma 存一份用于近邻召回。
Chroma 返回候选 `block_id` 后，**必须回穿 `asof.doc_block` 视图**做权威过滤。

**C. 不改，继续用 pgvector**

## 决策

**选 B。**

否决 A 的理由是时点一致性无法跨引擎保证。`known_at` / `superseded_at`
在 PostgreSQL 里是事务性的：一次更正入库会在同一个事务内插入新行、
把旧行标 `superseded_at`。Chroma 不参与这个事务。两者之间必然存在一个窗口——
以及任何一次同步失败造成的永久偏移——在这个窗口里，**PG 里已被撤回的块
在 Chroma 里仍然可召回**。这正是本项目要防的那一类错误：把事后才知道的
（或已被更正推翻的）信息泄漏进某个 `as_of` 时点的检索结果。

把 Chroma 当作**候选生成器**而不是**真相来源**，这个风险就被结构性地消除了：
Chroma 召回多了无妨（PG 会滤掉），Chroma 漏召回则由过采样与一致性核对兜住。
`CLAUDE.md` §1.1「Agent 与应用层只允许查询 `asof` 安全视图」因此仍然成立——
向量近邻只是候选排序信号，时点判定始终由 PG 做。

否决 C 是因为这是产品方的明确指令，且 B 保住了 C 的全部正确性属性。

### 边界

- **Chroma 只能本地自建**（`PersistentClient` 或自建容器），不得使用
  Chroma Cloud 或任何境外托管实例——`CLAUDE.md` §0「数据境内存储」。
- **用户上传材料按 `owner_user` 物理隔离到独立 collection**，不靠元数据过滤
  兜底——`CLAUDE.md` §0「用户上传材料私有隔离」。过滤条件写错是一行代码的事，
  collection 选错则会在写入时就失败。
- 向量维度仍固定 1024，Chroma collection 用余弦距离，写入前仍须 L2 归一化
  （ADR-0004 后果 3 不变）。

## 后果

1. **多了一个有状态服务要备份**。ADR-0002 的「单库」优势被削弱：
   P1c Task 11 的备份恢复必须同时覆盖 PG 与 Chroma，且两者的快照要能对齐到
   同一时点，否则恢复后会出现召回集与事实集不一致。
2. **必须有 PG ↔ Chroma 一致性核对**，并入 P1c Task 10 的双跑一致性检查：
   PG 中每个未被 supersede、有 embedding 的叶子块都应在 Chroma 中存在，反之亦然。
   偏移量是需要监控的指标，不是一次性测试。
3. **向量召回要过采样**。Chroma 返回的 top-k 经 PG 时点过滤后会缩水，
   命中数不足时需按倍数迭代加采（等价于 pgvector 0.8 的 iterative index scan
   要解决的同一个问题）。过采样倍数是评测参数，改动要跑评测。
4. **`VectorIndex` 协议保留 pgvector 实现**。除了 ADR-0005 的可替换性原则，
   它还是 Task 10 双跑一致性的对照组——没有第二个实现就没法判断
   是 Chroma 漏了还是查询本身就该返回这些。
5. 嵌入回填（`05-document-pipeline.md` §7.3 的双列灰度）现在要同时回填 Chroma，
   且灰度期间两个 collection 并存。

## 推翻条件

- PG ↔ Chroma 一致性偏移在稳定运行下无法收敛到 0 → 退回纯 pgvector（方案 C）；
- Chroma 的召回质量或延迟在评测中不优于 pgvector HNSW → 没有理由多养一个服务，
  退回方案 C；
- 反之，若 Chroma 在数百万块规模上显著优于 pgvector，且一致性核对长期为 0 偏移，
  可重新评估方案 A——但那需要先把时点判定的权威性问题解决掉，
  而不是假设它不存在。
