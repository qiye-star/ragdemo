"""文档入库：反规范化列一致、整份回滚、重解析不改 known_at。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg
import pytest

from ragdemo.adapters.announcements import NormalizedBlock, NormalizedDocument
from ragdemo.adapters.base import FetchContext
from ragdemo.adapters.mock.announcements import MockAnnouncementProvider
from ragdemo.ingest.documents import DocumentWriter, MetadataInvalid, SourceNotRegistered
from ragdemo.parse.chunker import Chunk, chunk_document
from ragdemo.parse.config import ChunkConfig
from ragdemo.parse.describe import MockTableDescriber
from ragdemo.parse.tree import build_tree
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")
CFG = ChunkConfig()


def _docs() -> list[object]:
    p = MockAnnouncementProvider()
    ctx = FetchContext(ingest_run_id="t", partition_date=datetime.now(UTC).date())
    return [
        p.normalize(raw)
        for raw in p.list_documents(
            ctx, since=datetime(2024, 1, 1, tzinfo=UTC), until=datetime.now(UTC)
        )
    ]


def _prepare(doc: object) -> tuple[list[Chunk], dict[int, str]]:
    chunks = build_tree(chunk_document(doc, CFG))  # type: ignore[arg-type]
    describer = MockTableDescriber()
    descriptions = {
        c.ordinal: describer.describe(c.content, title="t", section_path=c.section_path)
        for c in chunks
        if c.block_type == "table"
    }
    return chunks, descriptions


@pytest.fixture()
def writer(temp_db: str) -> DocumentWriter:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.commit()
    return DocumentWriter(conn, ingest_run_id="r1", source="mock-announcements")


@pytest.mark.db
def test_document_and_blocks_are_written(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    assert result.doc_id > 0
    assert len(result.block_ids) == len(chunks)


@pytest.mark.db
def test_denormalised_columns_match_the_document(writer: DocumentWriter) -> None:
    """02 §5.3 的反规范化列必须与 document 完全一致，否则检索过滤会错。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    (mismatches,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block b JOIN core.document d USING (doc_id) "
        " WHERE b.entity_id IS DISTINCT FROM d.entity_id"
        "    OR b.doc_type IS DISTINCT FROM d.doc_type"
        "    OR b.known_at IS DISTINCT FROM d.known_at"
        "    OR b.publish_at IS DISTINCT FROM d.publish_at"
    ).fetchone()  # type: ignore[misc]
    assert mismatches == 0


@pytest.mark.db
def test_known_at_applies_disclosure_lag(temp_db: str) -> None:
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.execute(
        "INSERT INTO core.source_registry (source_id, vendor, layer, can_cache,"
        " can_show_raw, can_vectorize, time_precision) "
        "VALUES ('mock','mock','filing',true,true,true,'second')"
    )
    conn.commit()
    w = DocumentWriter(conn, ingest_run_id="r1", source="mock", disclosure_lag=timedelta(days=1))
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    w.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    publish_at, known_at = conn.execute("SELECT publish_at, known_at FROM core.document").fetchone()  # type: ignore[misc]
    assert known_at - publish_at == timedelta(days=1)


@pytest.mark.db
def test_duplicate_content_hash_is_skipped(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    second = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    assert second.skipped is True
    (n,) = writer.conn.execute("SELECT count(*) FROM core.document").fetchone()  # type: ignore[misc]
    assert n == 1


@pytest.mark.db
def test_invalid_metadata_rolls_back_the_whole_document(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    # 去掉第一块（build_tree 输出「父块在前」，ordinal 0 是某个父块）：
    # 剩余块的 ordinal 集合变成 {1,...,N-1}，长度 N-1，不再是 range(N-1)，
    # 触发「ordinal 不连续」。去掉最后一块反而测不出问题——build_tree 的
    # 输出天然以最大 ordinal 结尾，截掉它只是把序列缩短成仍然连续的
    # range(N-1)，validate_chunks 检测不到任何异常。
    broken = [*chunks[1:]]
    with pytest.raises(MetadataInvalid):
        writer.write_document(doc, broken, desc)  # type: ignore[arg-type]
    (docs, blocks) = writer.conn.execute(
        "SELECT (SELECT count(*) FROM core.document), (SELECT count(*) FROM core.doc_block)"
    ).fetchone()  # type: ignore[misc]
    assert (docs, blocks) == (0, 0)


@pytest.mark.db
def test_reparse_keeps_original_known_at(writer: DocumentWriter) -> None:
    """重解析不改变这份文档在现实中何时可知——改了它就会从历史回测中消失。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    (original_known_at,) = writer.conn.execute(
        "SELECT known_at FROM core.document WHERE doc_id = %s", (first.doc_id,)
    ).fetchone()  # type: ignore[misc]

    second = writer.reparse_document(
        doc,  # type: ignore[arg-type]
        chunks,
        desc,
        supersedes_doc_id=first.doc_id,
    )
    new_known_at, version_group, supersedes = writer.conn.execute(
        "SELECT known_at, version_group_id, supersedes_doc_id FROM core.document WHERE doc_id = %s",
        (second.doc_id,),
    ).fetchone()  # type: ignore[misc]

    assert new_known_at == original_known_at
    assert version_group == first.doc_id
    assert supersedes == first.doc_id


@pytest.mark.db
def test_reparse_marks_old_blocks_superseded_but_keeps_them(writer: DocumentWriter) -> None:
    """旧块必须保留——历史观点的 evidence_blocks 引用着它们。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    writer.reparse_document(doc, chunks, desc, supersedes_doc_id=first.doc_id)  # type: ignore[arg-type]

    (old_alive,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE doc_id = %s", (first.doc_id,)
    ).fetchone()  # type: ignore[misc]
    (old_superseded,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE doc_id = %s AND superseded_at IS NOT NULL",
        (first.doc_id,),
    ).fetchone()  # type: ignore[misc]
    assert old_alive == old_superseded > 0


# --- 提交必须真的发生，不能只是"同一个连接自己能看见自己的写入" ------------


@pytest.mark.db
def test_write_document_commits_and_is_visible_from_another_connection(
    writer: DocumentWriter, temp_db: str
) -> None:
    """`write_document` 在 `_live_doc_id` 那次 SELECT 时就已经隐式开了外层
    事务，内部的 `with self.conn.transaction()` 因此只是 SAVEPOINT——没有
    显式 commit 的话，写进去的文档与块只有在同一个连接上读得到，换一个
    连接就什么都看不到，进程一崩溃就真的全丢。所有读回断言都在 writer.conn
    这同一个连接上做的话，测不出这个问题（同一事务内自己当然能看见自己
    未提交的写入）——必须换一个独立连接。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    other_conn = psycopg.connect(temp_db)
    try:
        (doc_count,) = other_conn.execute(
            "SELECT count(*) FROM core.document WHERE doc_id = %s", (result.doc_id,)
        ).fetchone()  # type: ignore[misc]
        assert doc_count == 1
        (block_count,) = other_conn.execute(
            "SELECT count(*) FROM core.doc_block WHERE doc_id = %s", (result.doc_id,)
        ).fetchone()  # type: ignore[misc]
        assert block_count == len(result.block_ids) > 0
    finally:
        other_conn.close()


@pytest.mark.db
def test_reparse_document_commits_and_is_visible_from_another_connection(
    writer: DocumentWriter, temp_db: str
) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    second = writer.reparse_document(
        doc,  # type: ignore[arg-type]
        chunks,
        desc,
        supersedes_doc_id=first.doc_id,
    )

    other_conn = psycopg.connect(temp_db)
    try:
        (superseded_at,) = other_conn.execute(
            "SELECT superseded_at FROM core.document WHERE doc_id = %s", (first.doc_id,)
        ).fetchone()  # type: ignore[misc]
        assert superseded_at is not None
        (new_doc_count,) = other_conn.execute(
            "SELECT count(*) FROM core.document WHERE doc_id = %s", (second.doc_id,)
        ).fetchone()  # type: ignore[misc]
        assert new_doc_count == 1
    finally:
        other_conn.close()


# --- Finding C: 去重检查要只认活着的行 -------------------------------------


@pytest.mark.db
def test_reingest_after_reparse_returns_the_live_doc_id(writer: DocumentWriter) -> None:
    """重解析后，同一个 (source, content_hash) 对应两行：被取代的旧行与新的
    活跃行——原始字节没变，变的只是解析结果。再次摄入同一份原始文档时，
    去重检查必须只认 superseded_at IS NULL 的那一行；否则可能把旧行的
    doc_id 当作「已存在，跳过」返回——那个 doc_id 在 asof.document 里不可见，
    调用方一旦信了 DocumentWriteResult.doc_id 就会引用一份死文档。
    """
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    reparsed = writer.reparse_document(
        doc,  # type: ignore[arg-type]
        chunks,
        desc,
        supersedes_doc_id=first.doc_id,
    )

    again = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    assert again.skipped is True
    assert again.doc_id == reparsed.doc_id
    assert again.doc_id != first.doc_id


# --- Finding D: 并发首次写入要降级为 skipped，不能崩溃 ----------------------


@pytest.mark.db
def test_concurrent_first_write_degrades_to_skipped(
    writer: DocumentWriter, temp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两份从未见过的文档并发写入：两条连接都在各自的存在性检查里判定
    "不存在"，然后都去插入——分区唯一索引 document_dedup_uk
    （008_document_dedup_partial.sql）保证数据不会重复，但落败的一方原本
    会拿到未处理的 UniqueViolation，直接把整个 run 炸掉（CLAUDE.md §1.6：
    并发摄入同一条新公告是增量触发模型下的正常路径，不是例外）。

    用真实的第二个连接重放这个竞态窗口，而不是起线程：真正让两条连接在
    INSERT 语句上互相阻塞、再解开需要真的并发（线程/协程）；这里改成对
    时序的确定性重放——先在 conn_b 上做一次真实的存在性检查（确认此刻
    确实不存在），再让 conn_a 完整写入并提交（赢家），然后把 conn_b
    "写入前的那次"存在性检查结果钉在刚才真实观察到的"不存在"，逼它跟
    真並发场景一样往下走到 INSERT。except 分支里恢复用的第二次存在性
    检查不打补丁——用真实、当下的数据，因为落败方必须能看到赢家刚提交
    的活跃行，这也是为什么本测试特意不用 REPEATABLE READ 事务隔离去模拟
    竞态：那样会让 except 里的重新查询也困在旧快照里，读不到赢家的提交，
    而这个模块实际使用的连接从未设置过非默认隔离级别（一律是 Postgres
    默认的 READ COMMITTED），钉住存在性检查的返回值比改变隔离级别更贴近
    真实的并发场景。
    """
    doc = _docs()[0]
    chunks, desc = _prepare(doc)

    content_hash = cast(NormalizedDocument, doc).content_hash

    conn_b = psycopg.connect(temp_db)
    writer_b = DocumentWriter(conn_b, ingest_run_id="r2", source="mock-announcements")

    # conn_b 此刻真实检查一次：这份文档确实还不存在。
    assert writer_b._live_doc_id(content_hash) is None

    # conn_a（赢家）完整写入并提交。
    winner = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]
    writer.conn.commit()
    assert winner.skipped is False

    # 把 conn_b 第一次存在性检查的结果钉在刚才观察到的"不存在"，模拟它的
    # 检查发生在赢家提交之前；之后（异常处理里恢复用的那次）放行到真实实现。
    real_live_doc_id = writer_b._live_doc_id
    call_count = 0

    def _stale_first_check(content_hash: str) -> int | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        return real_live_doc_id(content_hash)

    monkeypatch.setattr(writer_b, "_live_doc_id", _stale_first_check)

    loser = writer_b.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    assert loser.skipped is True
    assert loser.doc_id == winner.doc_id
    assert loser.block_ids == []
    (n,) = writer.conn.execute("SELECT count(*) FROM core.document").fetchone()  # type: ignore[misc]
    assert n == 1


# --- Finding E: 实体解析失败要留痕，不能悄无声息 ----------------------------


@pytest.mark.db
def test_unresolved_entity_ref_still_writes_and_warns(
    writer: DocumentWriter, caplog: pytest.LogCaptureFixture
) -> None:
    """entity_ref 非空但在 core.entity 里找不到匹配行（典型场景：新上市实体
    还没有回填代码列）：文档必须照常写入——拒绝会在增量触发模型下
    （CLAUDE.md §1.6）一直挡住这个实体的全部公告，直到有人手工建好
    core.entity 行。但也不能像"政策/宏观文档没有 entity_ref"那样悄无声息，
    这是数据质量问题，需要有人能看到。
    """
    doc = replace(
        cast(NormalizedDocument, _docs()[0]),
        entity_ref="NOT-A-REAL-CODE",
        content_hash="unresolved-entity-hash",
    )
    chunks, desc = _prepare(doc)

    with caplog.at_level(logging.WARNING, logger="ragdemo.ingest.documents"):
        result = writer.write_document(doc, chunks, desc)

    assert result.skipped is False
    (entity_id,) = writer.conn.execute(
        "SELECT entity_id FROM core.document WHERE doc_id = %s", (result.doc_id,)
    ).fetchone()  # type: ignore[misc]
    assert entity_id is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("entity_ref" in r.getMessage() for r in warnings)
    assert any(getattr(r, "entity_ref", None) == "NOT-A-REAL-CODE" for r in warnings)
    assert any(getattr(r, "ingest_run_id", None) == "r1" for r in warnings)


# --- Finding 2：owner_user 必须真正落到 document 与 doc_block 两张表 -------
#
# 以前 _insert_document / _insert_blocks 的 INSERT 列清单里压根没有
# owner_tenant / owner_user——db/migrations/006_asof_views_and_roles.sql 的
# 整套行级隔离都建立在这两列上，写库这一步把它们悄悄丢了，
# TEXTIN_ALLOW_PRIVATE 一旦打开，私有材料就会被存成公共行，直接违反
# CLAUDE.md §0"用户上传材料私有隔离：不得进入公共检索空间"。


@pytest.mark.db
def test_owner_user_is_persisted_on_both_document_and_doc_block(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc, owner_user="u1")  # type: ignore[arg-type]

    (doc_owner,) = writer.conn.execute(
        "SELECT owner_user FROM core.document WHERE doc_id = %s", (result.doc_id,)
    ).fetchone()  # type: ignore[misc]
    assert doc_owner == "u1"

    block_owners = {
        r[0]
        for r in writer.conn.execute(
            "SELECT owner_user FROM core.doc_block WHERE doc_id = %s", (result.doc_id,)
        ).fetchall()
    }
    assert block_owners == {"u1"}


@pytest.mark.db
def test_ordinary_public_path_is_unchanged(writer: DocumentWriter) -> None:
    """不传 owner_user——绝大多数既有调用方的写法——必须继续落公共行
    （owner_user 列为 NULL）：这条修复不能悄悄改变既有行为。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    (doc_owner,) = writer.conn.execute(
        "SELECT owner_user FROM core.document WHERE doc_id = %s", (result.doc_id,)
    ).fetchone()  # type: ignore[misc]
    assert doc_owner is None

    block_owners = {
        r[0]
        for r in writer.conn.execute(
            "SELECT owner_user FROM core.doc_block WHERE doc_id = %s", (result.doc_id,)
        ).fetchall()
    }
    assert block_owners == {None}


@pytest.mark.db
def test_reparse_preserves_the_original_documents_owner(writer: DocumentWriter) -> None:
    """重解析是同一份文档换了个解析结果，不是换了归属——不能借道
    reparse_document 把一份私有文档的新版本悄悄写成公共行（也不能反过来）。
    与 known_at 的处理方式一致：沿用被取代那份文档的值，不接受调用方传入。"""
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    first = writer.write_document(doc, chunks, desc, owner_user="u1")  # type: ignore[arg-type]

    second = writer.reparse_document(
        doc,  # type: ignore[arg-type]
        chunks,
        desc,
        supersedes_doc_id=first.doc_id,
    )

    (doc_owner,) = writer.conn.execute(
        "SELECT owner_user FROM core.document WHERE doc_id = %s", (second.doc_id,)
    ).fetchone()  # type: ignore[misc]
    assert doc_owner == "u1"


@pytest.mark.db
def test_private_document_is_invisible_to_other_users_through_rls(
    writer: DocumentWriter, temp_db: str
) -> None:
    """只看 owner_user 列的值还不够证明"私有材料不进入公共检索空间"——
    db/migrations/006_asof_views_and_roles.sql 的 doc_visibility /
    block_visibility 策略才是真正决定"谁能看见"的地方：

        (owner_tenant IS NULL AND owner_user IS NULL)                  -- 公共
     OR (owner_tenant = current_setting('app.tenant', true)
         AND owner_user IS NULL)                                       -- 租户私有
     OR (owner_user = current_setting('app.user', true))               -- 用户私有

    这里起一个真实的 app_read 角色，通过 RLS 验证：u2 经 asof.document
    读不到 u1 的私有文档，只看得到公共文档；u1 自己两者都看得到。
    （tests/db/test_asof_layer.py 已经用原始 SQL 验证过这套策略本身生效；
    这里验证的是 DocumentWriter.write_document 写出来的行确实落在
    这套策略预期的位置上——即 Finding 2 修的那条写入路径。）
    """
    docs = _docs()
    public_doc = cast(NormalizedDocument, docs[0])
    private_doc = cast(NormalizedDocument, docs[1])
    public_chunks, public_desc = _prepare(public_doc)
    private_chunks, private_desc = _prepare(private_doc)

    writer.write_document(public_doc, public_chunks, public_desc)
    writer.write_document(private_doc, private_chunks, private_desc, owner_user="u1")

    app_read_user = f"apptest_{uuid.uuid4().hex[:10]}"
    password = "not-a-real-secret"  # 测试库里的一次性口令，不是任何环境的真实凭据
    writer.conn.execute(f"CREATE USER \"{app_read_user}\" PASSWORD '{password}'")
    writer.conn.execute(f'GRANT app_read TO "{app_read_user}"')
    writer.conn.commit()
    try:

        def _titles_visible_to(user: str) -> set[str]:
            with (
                psycopg.connect(temp_db, user=app_read_user, password=password) as c,
                c.transaction(),
            ):
                c.execute("SELECT set_config('app.as_of', '2025-06-01T00:00:00+00:00', true)")
                c.execute("SELECT set_config('app.user', %s, true)", (user,))
                rows = c.execute("SELECT title FROM asof.document").fetchall()
            return {r[0] for r in rows}

        assert _titles_visible_to("u2") == {public_doc.title}
        assert _titles_visible_to("u1") == {public_doc.title, private_doc.title}
    finally:
        writer.conn.execute(f'DROP OWNED BY "{app_read_user}"')
        writer.conn.execute(f'DROP USER IF EXISTS "{app_read_user}"')
        writer.conn.commit()


# --- 四条款：source_registry（阶段 B） ---------------------------------------


@pytest.mark.db
def test_unregistered_source_refuses_construction(temp_db: str) -> None:
    """四条款没有默认值可猜——猜错任何一条都是合规问题，不是工程小瑕疵。
    构造 DocumentWriter 那一刻就该失败，而不是等到第一次 write_document。"""
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.commit()
    with pytest.raises(SourceNotRegistered, match="从未登记的来源"):
        DocumentWriter(conn, ingest_run_id="r1", source="从未登记的来源")


@pytest.mark.db
def test_can_show_raw_inherits_from_registry_to_document_and_block(temp_db: str) -> None:
    """继承链 source_registry → document → doc_block，块上的这一列不该
    需要 JOIN 回 document 才能判定（02 §5.3 反规范化列的同一个理由）。"""
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.execute(
        "INSERT INTO core.source_registry (source_id, vendor, layer, can_cache,"
        " can_show_raw, can_vectorize, time_precision) "
        "VALUES ('licensed-vendor','some-vendor','filing',true,false,true,'second')"
    )
    conn.commit()
    writer = DocumentWriter(conn, ingest_run_id="r1", source="licensed-vendor")
    assert writer.can_show_raw is False

    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    (doc_flag,) = conn.execute("SELECT can_show_raw FROM core.document").fetchone()  # type: ignore[misc]
    assert doc_flag is False
    (block_mismatches,) = conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE can_show_raw IS DISTINCT FROM false"
    ).fetchone()  # type: ignore[misc]
    assert block_mismatches == 0


@pytest.mark.db
def test_day_precision_known_at_is_conservative_day_end(temp_db: str) -> None:
    """`time_precision='day'` 的来源报不出发布时刻的具体时分秒：known_at 取
    该来源自己时区下的当日 23:59:59.999999（保守上界），不是 publish_at 原样。"""
    conn = psycopg.connect(temp_db)
    migrate(conn, MIGRATIONS)
    conn.execute(
        "INSERT INTO core.entity (entity_id, name_full, entity_type, l1_layer,"
        " l2_segment, l3_node, primary_node, tushare_code) "
        "VALUES ('CN.688256','寒武纪-U','listed','算力','AI芯片',"
        " ARRAY['云端训练芯片'],'云端训练芯片','688256.SH')"
    )
    conn.execute(
        "INSERT INTO core.source_registry (source_id, vendor, layer, can_cache,"
        " can_show_raw, can_vectorize, time_precision) "
        "VALUES ('day-precision-vendor','some-vendor','filing',true,true,true,'day')"
    )
    conn.commit()
    writer = DocumentWriter(conn, ingest_run_id="r1", source="day-precision-vendor")

    doc = _docs()[0]  # publish_at = 2024-10-28T18:32:00+08:00（MockAnnouncementProvider）
    chunks, desc = _prepare(doc)
    writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    (known_at,) = conn.execute("SELECT known_at FROM core.document").fetchone()  # type: ignore[misc]
    assert known_at != doc.publish_at, "day 精度不该原样相信 publish_at 里的具体时刻"
    # 转到 publish_at 自己的时区，验证落在同一个日历日的 23:59:59
    local = known_at.astimezone(doc.publish_at.tzinfo)
    assert (local.hour, local.minute, local.second) == (23, 59, 59)
    assert local.date() == doc.publish_at.date()


# --- 块级元数据（阶段 C：char_len / chunking_version / parse_confidence / table_html） ---


def _doc_with_table(table_html: str, *, content_hash: str = "tbl-hash-1") -> NormalizedDocument:
    blocks = [
        NormalizedBlock(ordinal=0, block_type="title", section_path="", content="第一节", level=0),
        NormalizedBlock(
            ordinal=1,
            block_type="table",
            section_path="第一节",
            content="| 甲 | 乙 |\n|---|---|\n| 1 | 2 |",
            page=1,
            table_html=table_html,
        ),
    ]
    return NormalizedDocument(
        provider_doc_id=f"P-{content_hash}",
        entity_ref="688256.SH",
        doc_type="annual_report",
        title="含表格的文档",
        period="2024",
        publish_at=datetime(2024, 10, 28, 18, 32, tzinfo=UTC),
        language="zh",
        source_url=None,
        raw_bytes_ref=None,
        content_hash=content_hash,
        is_correction=False,
        supersedes_provider_doc_id=None,
        page_count=1,
        blocks=blocks,
    )


@pytest.mark.db
def test_char_len_and_chunking_version_are_populated(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc, chunking_version=CFG.version)  # type: ignore[arg-type]

    rows = writer.conn.execute(
        "SELECT char_len, chunking_version FROM core.doc_block WHERE block_id = ANY(%s)",
        (result.block_ids,),
    ).fetchall()
    assert rows
    assert all(r[0] is not None and r[0] > 0 for r in rows)
    assert all(r[1] == CFG.version for r in rows)


@pytest.mark.db
def test_parse_confidence_is_populated_on_document_and_blocks(writer: DocumentWriter) -> None:
    doc = _docs()[0]
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    (doc_confidence,) = writer.conn.execute(
        "SELECT parse_confidence FROM core.document WHERE doc_id = %s", (result.doc_id,)
    ).fetchone()  # type: ignore[misc]
    assert doc_confidence is not None
    assert 0.0 <= float(doc_confidence) <= 1.0

    block_confidences = [
        r[0]
        for r in writer.conn.execute(
            "SELECT parse_confidence FROM core.doc_block WHERE block_id = ANY(%s)",
            (result.block_ids,),
        ).fetchall()
    ]
    assert block_confidences
    assert all(c is not None for c in block_confidences)


@pytest.mark.db
def test_table_html_is_written_only_for_table_blocks(writer: DocumentWriter) -> None:
    doc = _doc_with_table("<table><tr><td>1</td><td>2</td></tr></table>")
    chunks, desc = _prepare(doc)
    result = writer.write_document(doc, chunks, desc)  # type: ignore[arg-type]

    (table_html,) = writer.conn.execute(
        "SELECT table_html FROM core.doc_block WHERE doc_id = %s AND block_type = 'table'",
        (result.doc_id,),
    ).fetchone()  # type: ignore[misc]
    assert table_html == "<table><tr><td>1</td><td>2</td></tr></table>"

    (non_table_with_html,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block"
        " WHERE doc_id = %s AND block_type != 'table' AND table_html IS NOT NULL",
        (result.doc_id,),
    ).fetchone()  # type: ignore[misc]
    assert non_table_with_html == 0


@pytest.mark.db
def test_rechunk_with_new_chunking_version_keeps_old_blocks_queryable(
    writer: DocumentWriter,
) -> None:
    """重切走既有的 reparse_document 机制（新 doc_id，supersedes_doc_id 指向
    旧文档）：旧块永远不被删除或修改，chunking_version 只是让"这批块用的是
    哪版参数切出来的"可查（数据底座方案阶段 C 的 C5）。"""
    doc = _docs()[0]
    old_cfg = ChunkConfig(leaf_max_chars=300)
    old_chunks = build_tree(chunk_document(doc, old_cfg))
    old_desc = {c.ordinal: "d" for c in old_chunks if c.block_type == "table"}
    first = writer.write_document(doc, old_chunks, old_desc, chunking_version=old_cfg.version)  # type: ignore[arg-type]

    new_cfg = ChunkConfig(leaf_max_chars=350)
    assert new_cfg.version != old_cfg.version, "前置条件：换了参数指纹应该不同"
    new_chunks = build_tree(chunk_document(doc, new_cfg))
    new_desc = {c.ordinal: "d" for c in new_chunks if c.block_type == "table"}
    second = writer.reparse_document(
        doc,
        new_chunks,
        new_desc,
        supersedes_doc_id=first.doc_id,
        chunking_version=new_cfg.version,
    )  # type: ignore[arg-type]

    assert second.doc_id != first.doc_id

    old_versions = {
        r[0]
        for r in writer.conn.execute(
            "SELECT DISTINCT chunking_version FROM core.doc_block WHERE doc_id = %s",
            (first.doc_id,),
        ).fetchall()
    }
    new_versions = {
        r[0]
        for r in writer.conn.execute(
            "SELECT DISTINCT chunking_version FROM core.doc_block WHERE doc_id = %s",
            (second.doc_id,),
        ).fetchall()
    }
    assert old_versions == {old_cfg.version}
    assert new_versions == {new_cfg.version}

    # 旧块仍然可以直接按 block_id 查到——即便 doc_id 已经被 supersede。
    (old_block_count,) = writer.conn.execute(
        "SELECT count(*) FROM core.doc_block WHERE block_id = ANY(%s)",
        (first.block_ids,),
    ).fetchone()  # type: ignore[misc]
    assert old_block_count == len(first.block_ids)
