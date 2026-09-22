// 版面还原：按页把块画成彩色矩形叠在空白画布上，点击块在右侧看完整 JSON。
// ADR-0010 说 CLI 答不了的"二维问题"（块边界 × 页码 × 章节树 × 相邻重叠）
// 核心就是这个视图。
//
// 裁决 4（已获用户确认）：不做 PDF 底图。空白页画框，零新依赖，不需要
// "吐原始文件字节"的通道。右侧 JSON 面板会带出 content/table_html，
// 因此这里要看 can_show_raw——不是"不碰"，是"碰了就按它过滤"。

import { getJSON } from '../lib/api.js';
import { el, clear } from '../lib/dom.js';
import { emptyCard } from '../lib/status.js';
import { svgEl, svgText, area, overlaps } from '../lib/svg.js';
import { viewLink } from '../lib/state.js';
import { tableBlock } from '../lib/fmt.js';

export const id = 'layout';
export const title = '版面还原';

const PAGE_W = 1000;
const DISPLAY_W = 620;
const RATIOS = { 'a4-p': 1.4142, 'a4-l': 0.7071, letter: 1.2941, square: 1 };
const TYPE_LABEL = { paragraph: '段落', table: '表格', figure: '图' };

function noDocSelectedCard() {
  const card = el(
    'section',
    { class: 'card card-blocked' },
    el('h2', {}, '未选择文档'),
    el('p', {}, '从 '),
    viewLink('docs', {}, '文档列表'),
    el('p', { class: 'dim' }, ' 里点一篇文档进入版面还原。')
  );
  return card;
}

function renderPage(pageBlocks, ratio) {
  const H = Math.round(PAGE_W * ratio);
  // 显式 width/height 属性（元素属性，不是 CSS style——约束 8 禁内联
  // style）：没有它们时浏览器按内容默认尺寸（通常是 300×150）渲染 SVG，
  // 只能靠外部 CSS 硬撑容器宽度，纵向页面在一个宽矮的盒子里会被压扁、
  // 挤出大片空白边距。
  const displayH = Math.round(DISPLAY_W * ratio);
  const svg = svgEl('svg', {
    viewBox: `0 0 ${PAGE_W} ${H}`,
    width: DISPLAY_W,
    height: displayH,
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

function pageNav(pageBlockCounts, pageCount, currentPage, docId, ratioKey) {
  const total = pageCount ?? Math.max(0, ...pageBlockCounts.map((p) => p.page ?? 0));
  const byPage = new Map(pageBlockCounts.filter((p) => p.page != null).map((p) => [p.page, p.blocks]));
  const nav = el('div', { class: 'page-nav' });
  for (let p = 1; p <= total; p++) {
    const count = byPage.get(p) ?? 0;
    // ratio 一并带上——viewLink 只带显式给出的参数，此前手拼 href 时
    // 漏了它，翻页会把用户选的长宽比悄悄弹回默认值。
    const a = viewLink('layout', { doc: docId, page: p, ratio: ratioKey }, `p.${p} (${count})`);
    a.className = p === currentPage ? 'chip chip-active' : 'chip';
    // 0 块的页也列出来并标数字——空页是解析漏页的信号，藏起来等于把
    // 信号删了。
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

  container.append(pageNav(doc.page_block_counts, doc.page_count, page, docId, ratioKey));

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

  // 父块的 bbox 在解析阶段恒为 NULL（parse/tree.py 硬编码），一页如果全是
  // 父块，withBbox 就是空的——过去这里无条件调 renderPage 画一张没有任何
  // 矩形的白纸，看起来像是加载失败或漏解析了。这不是空集（layout.blocks
  // 非空），也不是错误，是"这页目前只有分组用的父块"这个具体事实。
  if (withBbox.length === 0) {
    container.append(
      el(
        'section',
        { class: 'card card-empty' },
        el('h2', {}, `本页 ${layout.blocks.length} 个块全部没有 bbox`),
        el(
          'p',
          { class: 'dim' },
          '父块的 bbox 在解析阶段恒为 NULL（parse/tree.py 建父块时硬编码），没有坐标就画不出矩形——不是加载失败，也不是漏解析。'
        )
      )
    );
    container.append(
      el(
        'details',
        { open: '' },
        el('summary', {}, `本页全部 ${noBbox.length} 个块`),
        el(
          'ul',
          { class: 'mono' },
          noBbox.map((b) => el('li', {}, `#${b.ordinal} ${b.block_type} · ${b.section_path || '（无章节）'}`))
        )
      )
    );
    return container;
  }

  const layoutRow = el('div', { class: 'page-row' });
  const svg = renderPage(withBbox, RATIOS[ratioKey] ?? RATIOS['a4-p']);
  const detail = el('div', { class: 'layout-detail page-detail' }, el('p', { class: 'dim' }, '点击块查看完整 JSON'));

  // 点击 = 固定选中：取详情是异步的，鼠标划过邻块不该在请求返回前把选中
  // 换掉。单调 token 防止后发先至的旧请求覆盖用户后点的块。
  let pick = 0;
  const showDetail = async (b, g) => {
    const token = ++pick;
    for (const other of svg.querySelectorAll('.blk.pinned')) other.classList.remove('pinned');
    g.classList.add('pinned');
    clear(detail);
    detail.append(el('p', { class: 'dim' }, '加载详情……'));

    const full = await getJSON(`/api/blocks/${b.block_id}`, {}, { asOf: ctx.asOf, signal: ctx.signal });
    if (token !== pick) return;

    // can_show_raw=false：doc 级标志同一路径反规范化到这个块，content /
    // content_desc / table_html 在序列化前置空，不把原文吐给不该看见的人。
    const shown = doc.can_show_raw
      ? full
      : { ...full, content: null, content_desc: null, table_html: null };
    const json = JSON.stringify(shown, null, 2);

    clear(detail);
    const copyBtn = el('button', { type: 'button', class: 'chip-btn' }, '复制 JSON');
    copyBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(json).then(
        () => { copyBtn.textContent = '已复制'; },
        () => { copyBtn.textContent = '复制失败（请手动选中）'; }
      );
    });
    // Element.append()（原生 DOM API，不是 lib/dom.js 里的 el()）不会跳过
    // null/false——它会把 null 字符串化成一个字面量文本节点 "null" 塞进页面。
    // el() 自己的 children 处理会跳过，但这里直接调用 append，必须自己
    // filter(Boolean)。
    detail.append(
      ...[
        el('h3', {}, `#${full.ordinal} ${TYPE_LABEL[full.block_type] ?? full.block_type}（block_id=${full.block_id}）`),
        copyBtn,
        doc.can_show_raw
          ? null
          : el('p', { class: 'dim' }, 'source_registry.can_show_raw = false：content / content_desc / table_html 已置空。'),
        doc.can_show_raw && full.table_html ? tableBlock(full.table_html) : null,
        el('pre', { class: 'mono json-dump' }, json),
      ].filter(Boolean)
    );
  };

  for (const g of svg.querySelectorAll('.blk')) {
    const b = withBbox.find((x) => String(x.block_id) === g.dataset.blockId);
    g.addEventListener('click', () => showDetail(b, g));
    g.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        showDetail(b, g);
      }
    });
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
