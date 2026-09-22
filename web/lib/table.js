// 7 处几乎一样的手搓 <table class="grid-table"> 的公因式。抽出来是为了
// 分档与预算、权限隔离两个视图的重新设计（挤成一团的根因之一就是每张表
// 都是独立手写的，改列结构要动好几处）。

import { el } from './dom.js';

/**
 * @param {object} opts
 * @param {{label: string, get(row): unknown, cls?: string, cellCls?(row, value): string|undefined}[]} opts.columns
 * @param {unknown[]} opts.rows
 * @param {boolean} [opts.scroll] 包一层 .table-scroll（宽表格需要横向滚动时用）
 *
 * `get(row)` 返回 null/undefined 时单元格显示「—」，与既有视图的既定约定
 * 一致（例如 docs.js 里 `page_count == null ? '—' : ...`）。返回 DOM 节点
 * （比如 viewLink() 产出的 <a>）时原样放进单元格——el() 自己会区分节点和
 * 文本，这里不需要额外分支。
 */
export function dataTable({ columns, rows, scroll = false }) {
  const table = el(
    'table',
    { class: 'grid-table' },
    el(
      'thead',
      {},
      el(
        'tr',
        {},
        columns.map((c) => el('th', { class: c.cls }, c.label))
      )
    ),
    el(
      'tbody',
      {},
      rows.map((row) =>
        el(
          'tr',
          {},
          columns.map((c) => {
            const v = c.get(row);
            const cellCls = [c.cls, c.cellCls?.(row, v)].filter(Boolean).join(' ') || undefined;
            return el('td', { class: cellCls }, v == null ? '—' : v);
          })
        )
      )
    )
  );
  return scroll ? el('div', { class: 'table-scroll' }, table) : table;
}
