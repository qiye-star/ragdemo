// 块树：按 section_path 分组，不按 parent_block_id 建树——28 个表格叶子
// parent_block_id IS NULL 但有 section_path，按小节分组它们自然归位，
// 不必塞进"其他"桶或伪造父块。组内排序用最小叶子 ordinal（父块 ordinal
// 全排在叶子前 0..61，不表达文档位置，tree.py 模块 docstring 明说）。

import { getJSON } from '../lib/api.js';
import { el, clear } from '../lib/dom.js';
import { emptyCard } from '../lib/status.js';
import { svgEl } from '../lib/svg.js';
import { overlapLen, sanitizeTable } from '../lib/fmt.js';
import { viewLink } from '../lib/state.js';

export const id = 'blocks';
export const title = '块树';

const MIN_LEN = 200;
const MAX_LEN = 400;
const SCALE = 700;
const TYPE_LABEL = { paragraph: '段落', table: '表格', figure: '图' };

function rangeOf(b) {
  // 200–400 的区间判定只对 paragraph 叶子成立——与 quality/checks.py::
  // leaf_length_compliance_check 的 WHERE 条件一致。表格/图块走"区间不
  // 适用"，不参与越界判定，否则会跟门禁自己的口径互相矛盾。
  if (!b.is_leaf || b.block_type !== 'paragraph' || b.char_len == null) return 'na';
  if (b.char_len < MIN_LEN) return 'under';
  if (b.char_len > MAX_LEN) return 'over';
  return 'in';
}

function lenBar(b) {
  const W = 110;
  const H = 14;
  const range = rangeOf(b);
  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: 'len-bar-svg' });
  if (range === 'na') {
    svg.append(svgEl('rect', { x: 0, y: 0, width: W, height: H, rx: 2, class: 'len-bar-na' }));
    return svg;
  }
  svg.append(svgEl('rect', { x: 0, y: 0, width: W, height: H, rx: 2, class: 'len-bar-bg' }));
  const bx = (MIN_LEN / SCALE) * W;
  const bw = ((MAX_LEN - MIN_LEN) / SCALE) * W;
  svg.append(svgEl('rect', { x: bx.toFixed(1), y: 0, width: bw.toFixed(1), height: H, class: 'len-bar-band' }));
  const fw = Math.min(W, (b.char_len / SCALE) * W);
  svg.append(
    svgEl('rect', { x: 0, y: 4, width: fw.toFixed(1), height: 6, class: `len-bar-fill-${range}` })
  );
  return svg;
}

function groupBySection(blocks) {
  const groups = new Map();
  for (const b of blocks) {
    const key = b.section_path || '（无章节）';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(b);
  }
  const entries = [...groups.entries()].map(([path, items]) => {
    const leaves = items.filter((b) => b.is_leaf).sort((a, b) => a.ordinal - b.ordinal);
    const parents = items.filter((b) => !b.is_leaf);
    const minOrdinal = leaves.length ? leaves[0].ordinal : Math.min(...items.map((b) => b.ordinal));
    return { path, leaves, parents, minOrdinal };
  });
  entries.sort((a, b) => a.minOrdinal - b.minOrdinal);
  return entries;
}

function crumbs(path) {
  const parts = path.split(' > ').filter(Boolean);
  if (parts.length === 0) return el('span', { class: 'crumbs' }, '（无章节）');
  const node = el('span', { class: 'crumbs' });
  parts.forEach((p, i) => {
    if (i > 0) node.append(el('b', { class: 'sep' }, '›'));
    node.append(el('span', { class: 'crumb' }, p));
  });
  return node;
}

function sectionHeader(group) {
  const pages = group.leaves.map((b) => b.page).filter((p) => p != null);
  const pageRange = pages.length ? (Math.min(...pages) === Math.max(...pages) ? `p.${pages[0]}` : `p.${Math.min(...pages)}–${Math.max(...pages)}`) : 'p.?';
  const paragraphCount = group.leaves.filter((b) => b.block_type === 'paragraph').length;
  const tableCount = group.leaves.filter((b) => b.block_type === 'table').length;
  const outOfRange = group.leaves.filter((b) => rangeOf(b) === 'under' || rangeOf(b) === 'over').length;

  const chips = el(
    'span',
    { class: 'chips' },
    el('span', { class: 'chip' }, `叶子 ${group.leaves.length}`),
    el('span', { class: 'chip' }, pageRange),
    el('span', { class: 'chip' }, `段落 ${paragraphCount} / 表格 ${tableCount}`)
  );
  if (outOfRange > 0) chips.append(el('span', { class: 'chip chip-out' }, `越界 ${outOfRange}`));

  return el('summary', { class: 'sec-head' }, crumbs(group.path), chips);
}

function leafRow(b, onPick) {
  const row = el(
    'li',
    { class: 'leaf', 'data-range': rangeOf(b), tabindex: 0 },
    el('span', { class: 'ord mono' }, `#${b.ordinal}`),
    el('span', { class: 'mono dim' }, b.page == null ? '—' : `p.${b.page}`),
    el('span', {}, TYPE_LABEL[b.block_type] ?? b.block_type),
    lenBar(b),
    el('span', { class: 'mono' }, b.char_len ?? '—'),
    el('span', { class: 'snip' }, b.preview)
  );
  row.addEventListener('click', () => onPick(b));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onPick(b);
    }
  });
  return row;
}

function parentRow(p) {
  return el(
    'details',
    { class: 'parent-block' },
    el('summary', {}, `父块 #${p.ordinal} · is_leaf=false · char_len=${p.char_len ?? 'NULL'} · ${p.page == null ? '无页码' : `p.${p.page}`} · 无 bbox（设计如此）`),
    el('p', { class: 'snip' }, p.preview)
  );
}

async function showLeafDetail(detailEl, block, allLeaves, asOf, signal) {
  clear(detailEl);
  detailEl.append(el('p', { class: 'dim' }, '加载详情……'));

  const idx = allLeaves.findIndex((x) => x.block_id === block.block_id);
  const prevLeaf = idx > 0 ? allLeaves[idx - 1] : null;
  const nextLeaf = idx >= 0 && idx < allLeaves.length - 1 ? allLeaves[idx + 1] : null;

  const [full, prevFull, nextFull] = await Promise.all([
    getJSON(`/api/blocks/${block.block_id}`, {}, { asOf, signal }),
    prevLeaf ? getJSON(`/api/blocks/${prevLeaf.block_id}`, {}, { asOf, signal }) : Promise.resolve(null),
    nextLeaf ? getJSON(`/api/blocks/${nextLeaf.block_id}`, {}, { asOf, signal }) : Promise.resolve(null),
  ]);

  clear(detailEl);
  detailEl.append(
    el('h3', {}, `#${full.ordinal} ${TYPE_LABEL[full.block_type] ?? full.block_type}（block_id=${full.block_id}）`),
    el('p', { class: 'mono dim' }, full.section_path || '（无章节）')
  );

  if (full.table_html) {
    const table = sanitizeTable(full.table_html);
    if (table) {
      const wrap = el('div', { class: 'table-scroll' });
      wrap.append(table);
      detailEl.append(el('p', { class: 'dim' }, 'table_html（结构化形态）：'), wrap);
    }
  }

  detailEl.append(el('pre', { class: 'content' }, full.content));

  if (prevFull) {
    const n = overlapLen(prevFull.content, full.content);
    detailEl.append(
      el(
        'p',
        { class: 'mono dim' },
        `与上一叶子（#${prevFull.ordinal}）重叠 ${n} 字（量出来的实际值，不是切块参数里写死的数字）`
      )
    );
  }
  if (nextFull) {
    const n = overlapLen(full.content, nextFull.content);
    detailEl.append(el('p', { class: 'mono dim' }, `与下一叶子（#${nextFull.ordinal}）重叠 ${n} 字`));
  }
}

export async function render(ctx) {
  const docId = ctx.params.get('doc');
  if (!docId) {
    return el(
      'section',
      { class: 'card card-blocked' },
      el('h2', {}, '未选择文档'),
      el('p', {}, '从 '),
      viewLink('docs', {}, '文档列表'),
      el('p', { class: 'dim' }, ' 里点一篇文档进入块树。')
    );
  }

  const body = await getJSON(`/api/documents/${docId}/blocks`, {}, { asOf: ctx.asOf, signal: ctx.signal });
  if (body.blocks.length === 0) {
    return emptyCard({
      what: `文档 ${docId} 的块`,
      predicate: `doc_id=${docId}，owner_tenant IS NULL AND owner_user IS NULL`,
    });
  }

  const allLeaves = body.blocks.filter((b) => b.is_leaf).sort((a, b) => a.ordinal - b.ordinal);
  const groups = groupBySection(body.blocks);

  const container = el('div', { class: 'layout-row' });
  const treeCol = el('div', { class: 'tree-col' });
  const detail = el('div', { class: 'layout-detail' }, el('p', { class: 'dim' }, '点一个叶子块查看全文与相邻重叠'));

  const list = el('ol', { class: 'tree' });
  for (const g of groups) {
    const details = el('details', { open: groups.length <= 3 ? '' : null });
    details.append(sectionHeader(g));
    for (const p of g.parents) details.append(parentRow(p));
    const leavesList = el('ul', { class: 'leaves' });
    for (const b of g.leaves) {
      leavesList.append(leafRow(b, (block) => showLeafDetail(detail, block, allLeaves, ctx.asOf, ctx.signal)));
    }
    details.append(leavesList);
    list.append(el('li', { class: 'sec' }, details));
  }
  treeCol.append(
    el('p', { class: 'dim' }, `${body.blocks.length} 块（${allLeaves.length} 叶子，${groups.length} 个小节）`),
    list
  );

  container.append(treeCol, detail);
  return container;
}
