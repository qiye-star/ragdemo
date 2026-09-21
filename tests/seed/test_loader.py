"""种子导入：校验优先于写入，坏数据不能进库。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import psycopg
import pytest

from ragdemo.seed.loader import SeedError, load_all, load_entities
from ragdemo_core.db.migrate import migrate

MIGRATIONS = Path("db/migrations")

HEADER = (
    "entity_id,name_full,name_short,name_en,entity_type,market,tushare_code,"
    "l1_layer,l2_segment,l3_node,primary_node,hq_country,status,listed_date,"
    "currency,fiscal_year_end,notes\n"
)
GOOD_ROW = (
    "CN.688256,寒武纪-U,寒武纪,Cambricon,listed,SSE,688256.SH,"
    "算力,AI芯片,云端训练芯片|边缘推理芯片,云端训练芯片,CN,active,2020-07-20,"
    "CNY,12,\n"
)


@pytest.fixture
def db(temp_db: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(temp_db) as conn:
        migrate(conn, MIGRATIONS)
        conn.commit()
        yield conn


def _csv(tmp_path: Path, rows: str, name: str = "entity.csv") -> Path:
    p = tmp_path / name
    p.write_text(HEADER + rows, encoding="utf-8")
    return p


@pytest.mark.db
def test_loads_entity_and_node_membership(
    db: psycopg.Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    """实体的环节归属必须同时写进时点表，否则回测基准不可复现。"""
    n = load_entities(db, _csv(tmp_path, GOOD_ROW))
    entities = db.execute("SELECT count(*) FROM core.entity").fetchone()
    memberships = db.execute("SELECT count(*) FROM core.entity_node_membership").fetchone()
    assert entities is not None
    assert memberships is not None
    assert (n, entities[0], memberships[0]) == (1, 1, 2)


@pytest.mark.db
def test_membership_known_at_comes_from_listed_date(
    db: psycopg.Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    """手工数据的 known_at 取 valid_from 当日 00:00（docs/03-point-in-time.md §6.3）。

    绝不能取 now()——那样这条归属在任何历史回测中都不可见。
    """
    load_entities(db, _csv(tmp_path, GOOD_ROW))
    row = db.execute(
        "SELECT valid_from, known_at FROM core.entity_node_membership LIMIT 1"
    ).fetchone()
    assert row is not None
    valid_from, known_at = row
    assert isinstance(valid_from, date)
    assert isinstance(known_at, datetime)
    assert known_at.date() == valid_from
    assert (known_at.hour, known_at.minute) == (0, 0)


@pytest.mark.db
def test_primary_node_not_in_l3_is_rejected_before_any_write(
    db: psycopg.Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    bad = GOOD_ROW.replace(",云端训练芯片,CN,", ",先进封装,CN,")
    with pytest.raises(SeedError, match="primary_node"):
        load_entities(db, _csv(tmp_path, bad))
    row = db.execute("SELECT count(*) FROM core.entity").fetchone()
    assert row is not None
    assert row[0] == 0


@pytest.mark.db
def test_malformed_entity_id_is_rejected(
    db: psycopg.Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    bad = GOOD_ROW.replace("CN.688256,", "688256,", 1)
    with pytest.raises(SeedError, match="entity_id"):
        load_entities(db, _csv(tmp_path, bad))


@pytest.mark.db
def test_duplicate_entity_id_in_csv_is_rejected(
    db: psycopg.Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    with pytest.raises(SeedError, match="重复"):
        load_entities(db, _csv(tmp_path, GOOD_ROW + GOOD_ROW))


@pytest.mark.db
def test_unknown_l3_node_is_rejected(
    db: psycopg.Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    """环节名是五张表的事实联结键，数据库不校验它，导入器必须校验。

    一个错字不会报错，只会让该环节的基准悄悄变空、传导规则悄悄不匹配。
    """
    bad = GOOD_ROW.replace("云端训练芯片|边缘推理芯片,云端训练芯片", "云端训练芯,云端训练芯")
    with pytest.raises(SeedError, match="不在环节表"):
        load_entities(db, _csv(tmp_path, bad))


@pytest.mark.db
def test_alias_colliding_with_another_entity_name_is_rejected(
    db: psycopg.Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    """docs/02-data-model.md §10：别名不与其他实体全称冲突。

    别名一对多是允许的（「长城」），但别名等于另一家的全称一定是录入错误。
    """
    two = GOOD_ROW + (
        "CN.688041,海光信息,海光,Hygon,listed,SSE,688041.SH,"
        "算力,AI芯片,通用服务器CPU,通用服务器CPU,CN,active,2022-08-12,CNY,12,\n"
    )
    _csv(tmp_path, two)
    (tmp_path / "entity_alias.csv").write_text(
        "alias,entity_id,alias_type,source,confidence\n海光信息,CN.688256,short,manual,high\n",
        encoding="utf-8",
    )
    with pytest.raises(SeedError, match="全称"):
        load_all(db, tmp_path)


@pytest.mark.db
def test_load_all_reports_counts_per_table(
    db: psycopg.Connection[tuple[object, ...]], tmp_path: Path
) -> None:
    _csv(tmp_path, GOOD_ROW)
    (tmp_path / "entity_alias.csv").write_text(
        "alias,entity_id,alias_type,source,confidence\n寒武纪,CN.688256,short,manual,high\n",
        encoding="utf-8",
    )
    counts = load_all(db, tmp_path)
    assert counts["core.entity"] == 1
    assert counts["core.entity_alias"] == 1
