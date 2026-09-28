"""由 `docs/csv`（创始人维护的产业链底稿）生成 `db/seed/*.csv`。离线、确定性。

输入：
- `docs/csv/AI产业链_01..04_*.csv`：公司名单、公司关系、传导规则、指标字典（人读格式）；
- `db/seed/sources/`：把人读格式落成入库格式所需的整理结果——
  `entity_curation.csv`（每家公司的实体 ID、市场、环节、币种……）、
  `relation_names.csv`（关系表里的别名与泛称）、`metric_curation.csv`、`rule_curation.csv`、
  `kept_relations.csv`（原种子里 docs/csv 没有表达的关系，含不在名单里但保留的 4 家的关系）、
  `listing_dates.csv`（供应商取的上市日期）、
  `docs_csv_version.csv`（底稿编制日期）；
- `db/seed/` 现有的 `entity_alias.csv` / `entity_relation.csv` / `node_metric.csv`（沿用已有行）。

几条会悄悄出错、因此写死的规则：
1. 关系表里的名字必须能解析：是名单里的简称、登记过的别名，或登记过的泛称。
   解析不了就整个构建失败，不静默丢关系。
2. 泛称（"北美云厂"）不是实体，不建关系；写进另一方的备注，信息不丢。
3. 关系表没有日期。新关系的 `valid_from` 取底稿编制日期——不把"行业共识"回溯成
   早就已知（docs/03 §1.1）。与现有种子同一 (from, to, 类型) 的关系沿用现有行；
   原种子里 docs/csv 没有表达的关系（同向或供货 / 依赖互为反向都没有）放在
   `kept_relations.csv` 里原样保留——那是整理过的知识，不因换底稿而丢。
4. 传导规则第 2、11 行正文里有没加引号的逗号，列会错位：多出的字段并回"传导机制"。
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ragdemo.seed.listing_dates import ListingDate, read_listing_dates
from ragdemo.seed.taxonomy import load_taxonomy

DOCS_CSV_DIR = Path("docs/csv")
SEED_DIR = Path("db/seed")
SOURCES_DIR = SEED_DIR / "sources"

COMPANIES = "AI产业链_01_公司名单.csv"
RELATIONS = "AI产业链_02_公司关系.csv"
RULES = "AI产业链_03_传导规则.csv"
METRICS = "AI产业链_04_指标字典.csv"

ENTITY_FIELDS = (
    "entity_id", "name_full", "name_short", "name_en", "entity_type", "market", "tushare_code",
    "l1_layer", "l2_segment", "l3_node", "primary_node", "hq_country", "status", "listed_date",
    "valid_from", "currency", "fiscal_year_end", "notes",
)  # fmt: skip
ALIAS_FIELDS = ("alias", "entity_id", "alias_type", "source", "confidence")
RELATION_FIELDS = (
    "from_entity", "to_entity", "relation_type", "strength", "share_estimate", "direction_note",
    "evidence_type", "valid_from", "confidence",
)  # fmt: skip
METRIC_FIELDS = (
    "metric_id", "l3_node", "metric_name", "metric_role", "frequency", "source_type",
    "extraction_hint", "direction", "definition", "unit",
)  # fmt: skip
RULE_FIELDS = (
    "trigger_node", "trigger_event", "affected_node", "direction", "lag_days", "mechanism",
    "weight_field", "confidence", "author",
)  # fmt: skip

RELATION_TYPES: Mapping[str, tuple[str, ...]] = {
    "供货": ("supplies_to",),
    "竞争": ("competes_with",),
    "自研替代": ("substitutes",),
    # 源表的"合作"都是博通 / Marvell / 世芯给云厂做定制芯片设计——实质是供货。
    "合作": ("supplies_to",),
    "投资/供货": ("invests_in", "supplies_to"),
}
EVIDENCE: Mapping[str, tuple[str, str]] = {  # 证据强度 → (evidence_type, confidence)
    "公开披露": ("filing", "high"),
    "行业共识": ("expert", "medium"),
    "媒体报道": ("news", "low"),
}
RULE_CONFIDENCE = {"强": "high", "中强": "medium", "中": "low"}
METRIC_ROLE = {"领先": "leading", "验证": "confirming"}
_QUARTERS = re.compile(r"(\d+)(?:\s*-\s*\d+)?\s*个季度")
DAYS_PER_QUARTER = 90


class BuildError(ValueError):
    """底稿与整理表对不上，构建不能继续（宁可失败，不静默丢数据）。"""


@dataclass
class BuiltSeed:
    files: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _read_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def repair_rule_row(row: Sequence[str], *, expected: int) -> list[str]:
    """正文里的裸逗号把"传导机制"（第 7 列）切开了：把多出的字段并回去，后三列不动。"""
    if len(row) == expected:
        return list(row)
    if len(row) < expected:
        raise BuildError(f"传导规则第 {row[0]} 条只有 {len(row)} 列，期望 {expected}")
    tail = expected - 7  # 机制之后的列数（强度、验证点、失效条件）
    return [*row[:6], "，".join(row[6 : len(row) - tail]), *row[len(row) - tail :]]


def parse_lag_days(text: str) -> int:
    """ "1-2个季度" → 90（取下限：最早可能起效的时点）；"同期" / "即时…" → 0。"""
    text = text.strip()
    if text.startswith(("同期", "即时")):
        return 0
    match = _QUARTERS.search(text)
    if match is None:
        raise BuildError(f"看不懂的时滞写法：{text!r}")
    return int(match.group(1)) * DAYS_PER_QUARTER


def _direction(text: str) -> int:
    if text.startswith("利好"):
        return 1
    if text.startswith("利空"):
        return -1
    if text.startswith("结构性"):
        return 0
    raise BuildError(f"看不懂的传导方向：{text!r}")


def build_seed(docs_dir: Path, sources_dir: Path, seed_dir: Path) -> BuiltSeed:
    built = BuiltSeed()
    compiled_on = {r["key"]: r["value"] for r in _read(sources_dir / "docs_csv_version.csv")}[
        "compiled_on"
    ]
    curation = _read(sources_dir / "entity_curation.csv")
    by_key = {r["key"]: r for r in curation}
    if len(by_key) != len(curation):
        raise BuildError("entity_curation.csv 里有重复的 key")
    companies = {r["简称"]: r for r in _read(docs_dir / COMPANIES)}
    missing = sorted(set(companies) - set(by_key))
    if missing:
        raise BuildError(f"公司名单里这些公司没有整理行：{missing}")
    listing = read_listing_dates(sources_dir / "listing_dates.csv")

    relations, groups = _relations(docs_dir, sources_dir, seed_dir, by_key, compiled_on, built)
    built.files["entity.csv"] = [
        _entity(
            row,
            companies.get(row["key"]),
            listing,
            groups.get(row["entity_id"], []),
            compiled_on,
        )
        for row in curation
    ]
    built.files["entity_alias.csv"] = _aliases(
        seed_dir, sources_dir, built.files["entity.csv"], curation, companies, built
    )
    built.files["entity_relation.csv"] = relations
    built.files["node_metric.csv"] = _metrics(docs_dir, sources_dir, seed_dir)
    built.files["propagation_rule.csv"] = _rules(docs_dir, sources_dir)
    _check_taxonomy(seed_dir, built)
    return built


def _entity(
    row: Mapping[str, str],
    company: Mapping[str, str] | None,
    listing: Mapping[str, ListingDate],
    groups: Sequence[str],
    compiled_on: str,
) -> dict[str, str]:
    eid = row["entity_id"]
    listed = row["entity_type"] == "listed"
    listed_on = ""
    if listed:
        found = listing.get(eid)
        if found is None:
            raise BuildError(
                f"{eid} 是上市公司但 listing_dates.csv 里没有它（先跑 seed-listing-dates）"
            )
        listed_on = found.listed_date.isoformat()
    notes = []
    if company is not None:
        notes.append(f"环节：{company['所属环节']}（{company['产业链层级']}）")
        notes.append(f"AI业务强度：{company['AI业务强度(定性)']}")
        notes.append(
            f"AI收入占比：{company['AI相关收入占比']}（核实来源建议：{company['占比核实来源建议']}）"
        )
    if row["curation_note"]:
        notes.append(row["curation_note"])
    if groups:
        notes.append("泛称关系（未建实体关系）：" + "；".join(groups))
    return {
        "entity_id": eid,
        "name_full": row["name_full"] or (company or {}).get("公司全称", ""),
        "name_short": row["key"],
        "name_en": row["name_en"],
        "entity_type": row["entity_type"],
        "market": row["market"],
        "tushare_code": row["tushare_code"],
        "l1_layer": row["l1_layer"],
        "l2_segment": row["l2_segment"],
        "l3_node": row["l3_node"],
        "primary_node": row["primary_node"],
        "hq_country": row["hq_country"],
        "status": row["status"],
        "listed_date": listed_on,
        # 环节归属的起点：上市公司取上市日期（种子既有约定），非上市取底稿编制日期
        "valid_from": listed_on or compiled_on,
        "currency": row["currency"],
        "fiscal_year_end": row["fiscal_year_end"],
        "notes": "；".join(notes),
    }


def _resolver(
    sources_dir: Path, by_key: Mapping[str, Mapping[str, str]]
) -> tuple[dict[str, str], set[str]]:
    aliases: dict[str, str] = {}
    groups: set[str] = set()
    for r in _read(sources_dir / "relation_names.csv"):
        if r["kind"] == "group":
            groups.add(r["name"])
        elif r["kind"] == "alias":
            if r["key"] not in by_key:
                raise BuildError(f"relation_names.csv: {r['name']} 指向不存在的 key {r['key']}")
            aliases[r["name"]] = str(by_key[r["key"]]["entity_id"])
        else:
            raise BuildError(f"relation_names.csv: 未知 kind {r['kind']!r}")
    for key, row in by_key.items():
        aliases.setdefault(key, str(row["entity_id"]))
    return aliases, groups


def _relations(
    docs_dir: Path,
    sources_dir: Path,
    seed_dir: Path,
    by_key: Mapping[str, Mapping[str, str]],
    compiled_on: str,
    built: BuiltSeed,
) -> tuple[list[dict[str, str]], dict[str, list[str]]]:
    aliases, group_names = _resolver(sources_dir, by_key)
    previous = {
        (r["from_entity"], r["to_entity"], r["relation_type"]): r
        for r in _read(seed_dir / "entity_relation.csv")
    }
    out: dict[tuple[str, str, str], dict[str, str]] = {}
    groups: dict[str, list[str]] = {}

    for r in _read(docs_dir / RELATIONS):
        seq, subject, obj = r["序号"], r["主体公司"], r["客体公司"]
        for name in (subject, obj):
            if name not in aliases and name not in group_names:
                raise BuildError(
                    f"关系 #{seq} 里的 {name!r} 既不是名单公司，也没有登记为别名或泛称"
                )
        if r["关系类型"] not in RELATION_TYPES:
            raise BuildError(f"关系 #{seq} 的关系类型 {r['关系类型']!r} 没有映射")
        if r["证据强度"] not in EVIDENCE:
            raise BuildError(f"关系 #{seq} 的证据强度 {r['证据强度']!r} 没有映射")
        if subject in group_names or obj in group_names:
            owner, other = (subject, obj) if obj in group_names else (obj, subject)
            if owner in group_names:
                built.warnings.append(f"关系 #{seq} 两端都是泛称，丢弃")
                continue
            groups.setdefault(aliases[owner], []).append(
                f"{r['关系类型']}→{other}（{r['关系说明']}，#{seq}）"
                if owner == subject
                else f"{other}→{r['关系类型']}（{r['关系说明']}，#{seq}）"
            )
            continue
        src, dst = aliases[subject], aliases[obj]
        if src == dst:
            built.warnings.append(f"关系 #{seq} {subject}→{obj} 合并别名后是同一实体，丢弃")
            continue
        evidence_type, confidence = EVIDENCE[r["证据强度"]]
        note = f"{r['关系说明']}（docs/csv#{seq}；{r['所属环节']}；核实：{r['核实来源建议']}）"
        for rel_type in RELATION_TYPES[r["关系类型"]]:
            key = (src, dst, rel_type)
            if key in out:
                built.warnings.append(
                    f"关系 #{seq} 与前面的关系重复（{src} {rel_type} {dst}），合并"
                )
                continue
            out[key] = previous.get(key) or {
                "from_entity": src,
                "to_entity": dst,
                "relation_type": rel_type,
                "strength": "",
                "share_estimate": "",
                "direction_note": note,
                "evidence_type": evidence_type,
                "valid_from": compiled_on,
                "confidence": confidence,
            }

    for r in _read(sources_dir / "kept_relations.csv"):
        out.setdefault((r["from_entity"], r["to_entity"], r["relation_type"]), dict(r))
    return [dict(v) for v in out.values()], groups


def _aliases(
    seed_dir: Path,
    sources_dir: Path,
    entities: Sequence[Mapping[str, str]],
    curation: Sequence[Mapping[str, str]],
    companies: Mapping[str, Mapping[str, str]],
    built: BuiltSeed,
) -> list[dict[str, str]]:
    ids = {e["entity_id"] for e in entities}
    full_names = {e["name_full"]: e["entity_id"] for e in entities}
    rows: dict[tuple[str, str], dict[str, str]] = {}

    def add(alias: str, eid: str, alias_type: str, source: str) -> None:
        alias = alias.strip()
        if not alias or (alias, eid) in rows:
            return
        owner = full_names.get(alias)
        if owner is not None and owner != eid:
            built.warnings.append(f"别名 {alias!r} 是 {owner} 的全称，不挂到 {eid}")
            return
        rows[(alias, eid)] = {
            "alias": alias,
            "entity_id": eid,
            "alias_type": alias_type,
            "source": source,
            "confidence": "high",
        }

    for r in _read(seed_dir / "entity_alias.csv"):
        if r["entity_id"] in ids:
            rows.setdefault((r["alias"], r["entity_id"]), dict(r))
    by_id = {e["entity_id"]: e for e in entities}
    for c in curation:
        eid = c["entity_id"]
        add(by_id[eid]["name_full"], eid, "full", "manual:curation")
        add(c["key"], eid, "short", "manual:curation")
        add(c["name_en"], eid, "en", "manual:curation")
        add(c["tushare_code"], eid, "ticker", "manual:curation")
        add(c["wind_code"], eid, "ticker", "manual:curation")
        company = companies.get(c["key"])
        if company is not None:
            add(company["公司全称"], eid, "full", "docs/csv:01")
            for code in company["证券代码"].split("/"):
                if code.strip() and code.strip() != "非上市":
                    add(code, eid, "ticker", "docs/csv:01")
    for r in _read(sources_dir / "relation_names.csv"):
        if r["kind"] == "alias":
            eid = next(c["entity_id"] for c in curation if c["key"] == r["key"])
            add(r["name"], eid, "nickname", "docs/csv:02")
    return list(rows.values())


def _metrics(docs_dir: Path, sources_dir: Path, seed_dir: Path) -> list[dict[str, str]]:
    financial = [r for r in _read(seed_dir / "node_metric.csv") if not r["l3_node"]]
    curation = {r["seq"]: r for r in _read(sources_dir / "metric_curation.csv")}
    out = [dict(r) for r in financial]
    for r in _read(docs_dir / METRICS):
        cur = curation.get(r["序号"])
        if cur is None:
            raise BuildError(f"指标字典第 {r['序号']} 条没有整理行")
        if r["指标类型"] not in METRIC_ROLE:
            raise BuildError(f"指标 #{r['序号']} 的指标类型 {r['指标类型']!r} 没有映射")
        out.append(
            {
                "metric_id": cur["metric_id"],
                "l3_node": cur["l3_node"],
                "metric_name": r["指标名称"],
                "metric_role": METRIC_ROLE[r["指标类型"]],
                "frequency": cur["frequency"],
                "source_type": cur["source_type"],
                "extraction_hint": (
                    f"来源：{r['数据来源文件']}；位置：{r['抽取章节/位置']}；"
                    f"数据商字段：{r['数据商字段对应(建议)']}；注意：{r['备注与陷阱']}"
                ),
                "direction": cur["direction"],
                "definition": r["指标定义"],
                "unit": cur["unit"],
            }
        )
    ids = [m["metric_id"] for m in out]
    if len(ids) != len(set(ids)):
        raise BuildError(f"指标 ID 重复：{[i for i in ids if ids.count(i) > 1]}")
    return out


def _rules(docs_dir: Path, sources_dir: Path) -> list[dict[str, str]]:
    curation = {r["seq"]: r for r in _read(sources_dir / "rule_curation.csv")}
    raw = _read_rows(docs_dir / RULES)
    header, body = raw[0], raw[1:]
    out: list[dict[str, str]] = []
    for row in body:
        fixed = dict(zip(header, repair_rule_row(row, expected=len(header)), strict=True))
        seq = fixed["序号"]
        cur = curation.get(seq)
        if cur is None:
            raise BuildError(f"传导规则第 {seq} 条没有整理行")
        if fixed["强度"] not in RULE_CONFIDENCE:
            raise BuildError(f"传导规则 #{seq} 的强度 {fixed['强度']!r} 没有映射")
        extra = f"；{cur['curation_note']}" if cur["curation_note"] else ""
        mechanism = (
            f"{fixed['传导机制']}（{fixed['触发事件']}；时滞：{fixed['预计时滞']}；"
            f"验证点：{fixed['可观测验证点']}；失效条件：{fixed['失效条件']}{extra}）"
        )
        for trigger in _split(cur["trigger_nodes"]):
            for affected in _split(cur["affected_nodes"]):
                out.append(
                    {
                        "trigger_node": trigger,
                        "trigger_event": cur["trigger_event"],
                        "affected_node": affected,
                        "direction": str(_direction(fixed["传导方向"])),
                        "lag_days": str(parse_lag_days(fixed["预计时滞"])),
                        "mechanism": mechanism,
                        "weight_field": "",
                        "confidence": RULE_CONFIDENCE[fixed["强度"]],
                        "author": f"docs/csv#{seq}",
                    }
                )
    return out


def _split(raw: str) -> list[str]:
    return [p.strip() for p in raw.split("|") if p.strip()]


def _check_taxonomy(seed_dir: Path, built: BuiltSeed) -> None:
    tax = load_taxonomy(seed_dir / "taxonomy.csv")
    bad: list[str] = []
    for e in built.files["entity.csv"]:
        bad += tax.unknown([*_split(e["l3_node"]), e["primary_node"]])
        if e["primary_node"] not in _split(e["l3_node"]):
            raise BuildError(f"{e['entity_id']} 的 primary_node 不在 l3_node 里")
    for r in built.files["propagation_rule.csv"]:
        bad += tax.unknown([r["trigger_node"], r["affected_node"]])
    for m in built.files["node_metric.csv"]:
        bad += tax.unknown([m["l3_node"]])
    if bad:
        raise BuildError(f"这些环节不在 taxonomy.csv 里：{sorted(set(bad))}")


_FIELDS: Mapping[str, Sequence[str]] = {
    "entity.csv": ENTITY_FIELDS,
    "entity_alias.csv": ALIAS_FIELDS,
    "entity_relation.csv": RELATION_FIELDS,
    "node_metric.csv": METRIC_FIELDS,
    "propagation_rule.csv": RULE_FIELDS,
}


def write_seed(built: BuiltSeed, seed_dir: Path) -> None:
    for name, rows in built.files.items():
        _write(seed_dir / name, _FIELDS[name], rows)


def _write(path: Path, fields: Sequence[str], rows: Iterable[Mapping[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})
