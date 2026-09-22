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

export async function render(ctx) {
  const body = await getJSON('/api/documents', {}, { asOf: ctx.asOf, signal: ctx.signal });

  if (body.documents.length === 0) {
    return emptyCard({
      what: '文档',
      predicate:
        `as_of = ${ctx.asOf}\n` +
        '谓词: known_at <= as_of AND (superseded_at IS NULL OR superseded_at > as_of)\n' +
        '      AND owner_tenant IS NULL AND owner_user IS NULL',
    });
  }

  return el(
    'div',
    { class: 'table-wrap' },
    el('p', { class: 'dim' }, `${body.documents.length} 份文档（as_of = ${body.as_of}）`),
    dataTable({ columns: COLUMNS, rows: body.documents })
  );
}
