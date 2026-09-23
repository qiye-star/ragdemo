// 文档列表——最小闭环，验证三态：as_of 早于全部文档发布之前 → 空集卡；
// 从 URL 删掉 as_of → 阻断卡（app.js 层面处理）；数据库不可用 → 错误卡
// 带 SQLSTATE（app.js 层面处理，这里只管正常路径与空集）。

import { getJSON } from '../lib/api.js';
import { el } from '../lib/dom.js';
import { emptyCard } from '../lib/status.js';
import { viewLink } from '../lib/state.js';
import { dataTable } from '../lib/table.js';

export const id = 'docs';
export const title = '文档';

function warningBadges(warnings) {
  if (!warnings.length) return null;
  return el(
    'span',
    { class: 'chips' },
    warnings.map((w) => el('span', { class: 'chip chip-out' }, w))
  );
}

const COLUMNS = [
  { label: 'doc_id', cls: 'num', get: (d) => String(d.doc_id) },
  {
    label: '标题',
    // 数组会被 el() 展开成多个子节点——viewLink 与徽标（可能是 null）
    // 各自独立，不需要额外包一层 <span>。
    get: (d) => [viewLink('layout', { doc: d.doc_id, page: 1 }, d.title), warningBadges(d.parse_warnings)],
  },
  { label: 'doc_type', get: (d) => d.doc_type },
  { label: 'source', get: (d) => d.source },
  { label: 'publish_at', get: (d) => (d.publish_at || '').slice(0, 10) },
  { label: 'pages', cls: 'num', get: (d) => d.page_count },
  { label: 'parse_engine', cls: 'mono', get: (d) => d.parse_engine },
  {
    label: '叶子/总块数',
    cls: 'num',
    // 全站第一个也是唯一一个块树入口——此前是纯文本，块树视图只能靠手敲
    // hash 到达。
    get: (d) => viewLink('blocks', { doc: d.doc_id }, `${d.leaf_count}/${d.block_count}`),
  },
  {
    label: 'confidence',
    cls: 'num',
    get: (d) => (d.parse_confidence == null ? 'NULL（未评分）' : d.parse_confidence.toFixed(4)),
  },
];

// 只读导入指引——诊断界面本身没有、也不会有上传接口（ADR-0010：GET-only，
// 界面出现任何写操作即触发推翻条件）。真正导入文档走既有 CLI，这里只是
// 把命令抄给操作者看，不调用任何接口。
function importGuide() {
  return el(
    'details',
    { class: 'card' },
    el('summary', {}, '如何导入新文档'),
    el(
      'p',
      { class: 'dim' },
      '本界面只读，没有上传入口。导入新文档走命令行：'
    ),
    el(
      'pre',
      { class: 'mono' },
      'ragdemo docs ingest --manifest <manifest.jsonl 路径> --entity-ref <如 688041.SH> --yes --parser textin'
    ),
    el(
      'p',
      { class: 'dim' },
      '默认是 dry-run，加 --yes 才会真正调用解析器并写库；--max-pages 可以限制单次解析的页数。'
    )
  );
}

export async function render(ctx) {
  const body = await getJSON('/api/documents', {}, { asOf: ctx.asOf, signal: ctx.signal });

  if (body.documents.length === 0) {
    return el(
      'div',
      {},
      importGuide(),
      emptyCard({
        what: '文档',
        predicate:
          `as_of = ${ctx.asOf}\n` +
          '谓词: known_at <= as_of AND (superseded_at IS NULL OR superseded_at > as_of)\n' +
          '      AND owner_tenant IS NULL AND owner_user IS NULL',
      })
    );
  }

  return el(
    'div',
    { class: 'table-wrap' },
    importGuide(),
    el('p', { class: 'dim' }, `${body.documents.length} 份文档（as_of = ${body.as_of}）`),
    dataTable({ columns: COLUMNS, rows: body.documents })
  );
}
