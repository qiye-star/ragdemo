// 文档列表——最小闭环，验证三态：as_of 早于全部文档发布之前 → 空集卡；
// 从 URL 删掉 as_of → 阻断卡（app.js 层面处理）；数据库不可用 → 错误卡
// 带 SQLSTATE（app.js 层面处理，这里只管正常路径与空集）。

import { getJSON } from '../lib/api.js';
import { el } from '../lib/dom.js';
import { emptyCard } from '../lib/status.js';
import { navigateTo } from '../lib/state.js';

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

function row(doc) {
  const tr = el(
    'tr',
    { class: 'doc-row' },
    el('td', { class: 'num' }, String(doc.doc_id)),
    el(
      'td',
      {},
      el('a', { href: `#/layout?doc=${doc.doc_id}`, title: '查看版面还原' }, doc.title),
      warningBadges(doc.parse_warnings)
    ),
    el('td', {}, doc.doc_type),
    el('td', {}, doc.source),
    el('td', {}, (doc.publish_at || '').slice(0, 10)),
    el('td', { class: 'num' }, doc.page_count == null ? '—' : String(doc.page_count)),
    el('td', { class: 'mono' }, doc.parse_engine ?? '—'),
    el('td', { class: 'num' }, `${doc.leaf_count}/${doc.block_count}`),
    el(
      'td',
      { class: 'num' },
      doc.parse_confidence == null ? 'NULL（未评分）' : doc.parse_confidence.toFixed(4)
    )
  );
  tr.querySelector('a').addEventListener('click', (e) => {
    e.preventDefault();
    navigateTo('layout', { doc: doc.doc_id, page: 1 });
  });
  return tr;
}

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
    el(
      'table',
      { class: 'grid-table' },
      el(
        'thead',
        {},
        el(
          'tr',
          {},
          el('th', {}, 'doc_id'),
          el('th', {}, '标题'),
          el('th', {}, 'doc_type'),
          el('th', {}, 'source'),
          el('th', {}, 'publish_at'),
          el('th', {}, 'pages'),
          el('th', {}, 'parse_engine'),
          el('th', {}, '叶子/总块数'),
          el('th', {}, 'confidence')
        )
      ),
      el('tbody', {}, body.documents.map(row))
    )
  );
}
