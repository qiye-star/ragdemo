// 版面还原：按页把块画成彩色矩形叠在空白画布上。ADR-0010 说 CLI 答不了
// 的"二维问题"（块边界 × 页码 × 章节树 × 相邻重叠）核心就是这个视图。
//
// 裁决 4（已获用户确认）：不做 PDF 底图。空白页画框，零新依赖，
// 不需要"吐原始文件字节"的通道，不碰 can_show_raw。块的相对位置与
// 重叠关系完整保留，足以回答"切块边界对不对"。

import { getJSON } from '../lib/api.js';
import { el, clear } from '../lib/dom.js';
import { emptyCard } from '../lib/status.js';
import { svgEl, svgText, area, overlaps } from '../lib/svg.js';
import { navigateTo } from '../lib/state.js';

export const id = 'layout';
export const title = '版面还原';

const PAGE_W = 1000;
const RATIOS = { 'a4-p': 1.4142, 'a4-l': 0.7071, letter: 1.2941, square: 1 };
const TYPE_LABEL = { paragraph: '段落', table: '表格', figure: '图' };

function noDocSelectedCard() {
  const card = el(
    'section',
    { class: 'card' },
    el('h2', {}, '未选择文档'),
    el('p', {}, '从 '),
    el('a', { href: '#/docs' }, '文档列表'),
    el('p', { class: 'dim' }, ' 里点一篇文档进入版面还原。')
  );
  return card;
}

function renderPage(pageBlocks, ratio) {
  const H = Math.round(PAGE_W * ratio);
  const svg = svgEl('svg', {
    viewBox: `0 0 ${PAGE_W} ${H}`,
    class: 'page-canvas',
    role: 'img',
    'aria-label': `本页 ${pageBlocks.length} 个带坐标的块`,
  });
  svg.append(svgEl('rect', { x: 0, y: 0, width: PAGE_W, height: H, class: 'page-paper' }));

  // SVG 没有 z-index，只有文档顺序——按面积降序画：大块先画，落在它
  // 内部的小块后画，否则一个整页宽的段落块会把页内所有小块的点击区吃掉。
  const ordered = pageBlocks.slice().sort((a, b) => area(b.bbox) - area(a.bbox));

  for (const b of ordered) {
    const [x0, y0, x1, y1] = b.bbox;
    const px = x0 * PAGE_W;
    const py = y0 * H;
    // 下限 1.5：单行段落的归一化高度可以很小，但零高度/零宽度的退化
    // bbox 会画出看不见也点不到的矩形。
    const pw = Math.max(1.5, (x1 - x0) * PAGE_W);
    const ph = Math.max(1.5, (y1 - y0) * H);

    const g = svgEl('g', { class: `blk blk-${b.block_type}`, tabindex: 0 });
    g.append(
      svgEl('rect', {
        x: px.toFixed(2),
        y: py.toFixed(2),
        width: pw.toFixed(2),
        height: ph.toFixed(2),
        rx: 2,
      })
    );
    if (ph >= 16 && pw >= 26) {
      g.append(svgText({ x: (px + 3).toFixed(2), y: (py + 12).toFixed(2), class: 'blk-ord' }, `#${b.ordinal}`));
    }
    const t = svgEl('title');
    t.textContent = `#${b.ordinal} ${TYPE_LABEL[b.block_type] ?? b.block_type} · ${b.char_len ?? '?'} 字 · ${b.section_path || '（无章节）'}`;
    g.append(t);
    g.dataset.blockId = b.block_id;
    svg.append(g);
  }
  return svg;
}

function pageNav(pageBlockCounts, pageCount, currentPage, docId, asOf) {
  const total = pageCount ?? Math.max(0, ...pageBlockCounts.map((p) => p.page ?? 0));
  const byPage = new Map(pageBlockCounts.filter((p) => p.page != null).map((p) => [p.page, p.blocks]));
  const nav = el('div', { class: 'page-nav' });
  for (let p = 1; p <= total; p++) {
    const count = byPage.get(p) ?? 0;
    const a = el(
      'a',
      { href: `#/layout?doc=${docId}&page=${p}${asOf ? `&as_of=${encodeURIComponent(asOf)}` : ''}`, class: p === currentPage ? 'chip chip-active' : 'chip' },
      `p.${p} (${count})`
    );
    // 0 块的页也列出来并标数字——空页是解析漏页的信号，藏起来等于把
    // 信号删了。
    a.addEventListener('click', (e) => {
      e.preventDefault();
      navigateTo('layout', { doc: docId, page: p });
    });
    nav.append(a);
  }
  return nav;
}

function overlapPanel(blocksWithBbox) {
  const pairs = overlaps(blocksWithBbox);
  if (pairs.length === 0) return el('p', { class: 'dim' }, '本页没有检测到 bbox 重叠。');
  return el(
    'details',
    {},
    el('summary', {}, `重叠 ${pairs.length} 对（按 IoU 降序）`),
    el(
      'ol',
      { class: 'mono' },
      pairs
        .slice(0, 30)
        .map((p) => el('li', {}, `#${p.a.ordinal} × #${p.b.ordinal} — IoU=${p.iou.toFixed(3)}`))
    )
  );
}

export async function render(ctx) {
  const docId = ctx.params.get('doc');
  if (!docId) return noDocSelectedCard();

  const page = Number(ctx.params.get('page') ?? '1') || 1;
  const ratioKey = ctx.params.get('ratio') ?? 'a4-p';

  const doc = await getJSON(`/api/documents/${docId}`, {}, { asOf: ctx.asOf, signal: ctx.signal });
  const layout = await getJSON(
    `/api/documents/${docId}/pages/${page}/layout`,
    {},
    { asOf: ctx.asOf, signal: ctx.signal }
  );

  const container = el('div', {});
  container.append(
    el('h2', {}, `${doc.title}（doc_id=${docId}）`),
    el(
      'p',
      { class: 'dim' },
      `${doc.page_count ?? '?'} 页 · ${doc.block_count} 块（${doc.leaf_count} 叶子）`
    )
  );

  container.append(pageNav(doc.page_block_counts, doc.page_count, page, docId, ctx.asOf));

  const withBbox = layout.blocks.filter((b) => b.bbox != null);
  const malformed = layout.blocks.filter((b) => b.bbox_malformed);
  const noBbox = layout.blocks.filter((b) => b.bbox == null && !b.bbox_malformed);

  container.append(
    el(
      'p',
      { class: 'dim mono' },
      `长宽比为假设值（数据库未存页面尺寸；bbox 已归一化，相对位置与重叠关系不受比例影响，只有形状会被拉伸）。` +
        `本页 ${layout.blocks.length} 块 / 带坐标 ${withBbox.length} / 无坐标 ${noBbox.length}` +
        (malformed.length ? ` / 坐标异常 ${malformed.length}` : '')
    )
  );

  if (layout.blocks.length === 0) {
    container.append(
      emptyCard({
        what: `文档 ${docId} 第 ${page} 页的块`,
        predicate: `doc_id=${docId} AND page=${page}，owner_tenant IS NULL AND owner_user IS NULL`,
      })
    );
    return container;
  }

  const layoutRow = el('div', { class: 'layout-row' });
  const svg = renderPage(withBbox, RATIOS[ratioKey] ?? RATIOS['a4-p']);
  const detail = el('div', { class: 'layout-detail' }, el('p', { class: 'dim' }, '悬停或点击块查看详情'));

  const showDetail = (b) => {
    clear(detail);
    detail.append(
      el('h3', {}, `#${b.ordinal} ${TYPE_LABEL[b.block_type] ?? b.block_type}`),
      el('p', { class: 'mono dim' }, b.section_path || '（无章节）'),
      el('p', { class: 'mono dim' }, `char_len=${b.char_len ?? 'NULL'} · confidence=${b.parse_confidence ?? 'NULL（未评分）'}`),
      el('p', { class: 'snip' }, b.preview)
    );
  };

  for (const g of svg.querySelectorAll('.blk')) {
    const b = withBbox.find((x) => String(x.block_id) === g.dataset.blockId);
    g.addEventListener('mouseenter', () => showDetail(b));
    g.addEventListener('click', () => showDetail(b));
  }

  layoutRow.append(svg, detail);
  container.append(layoutRow);

  if (noBbox.length) {
    container.append(
      el(
        'details',
        {},
        el('summary', {}, `无坐标的块（${noBbox.length}）——父块恒无 bbox，是设计如此`),
        el(
          'ul',
          { class: 'mono' },
          noBbox.map((b) => el('li', {}, `#${b.ordinal} ${b.block_type} · ${b.section_path || '（无章节）'}`))
        )
      )
    );
  }

  container.append(overlapPanel(withBbox));
  return container;
}
