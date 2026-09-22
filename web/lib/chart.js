// 手写 SVG 折线/柱状图，不 vendor 库——需求是几种静态图、几百个点，
// uPlot/Chart.js 的价值全用不上，却留下一个没人维护得起的压缩文件，
// 正是 ADR-0010 后果 4"技术栈必须是能扔掉的"的反面。

import { svgEl, svgText } from './svg.js';

const PAD = { top: 14, right: 64, bottom: 30, left: 56 };

// quality/checks.py:129 与 ingest/reconcile.py:91 用 -1 表示"这个窗口
// 没有样本"，不是真的 -1——画成数据点会让趋势图凭空出现一个深坑。
export const NO_SAMPLE = -1;
export const isNoSample = (hasSentinel, y) => hasSentinel && y === NO_SAMPLE;

function niceDomain(values) {
  if (!values.length) return [0, 1];
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (lo === hi) {
    const d = Math.abs(lo) * 0.1 || 0.5;
    lo -= d;
    hi += d;
  }
  const pad = (hi - lo) * 0.1;
  return [lo - pad, hi + pad];
}

/**
 * @param {object} opts
 * @param {boolean} opts.hasSentinel 该指标是否用 -1 表示"无样本"
 * @param {{x:string, y:number, passed:boolean, note:string|null}[]} opts.series
 * @param {number|null} opts.threshold null=纯观测指标，不画阈值线
 * @param {[number,number]|null} opts.yDomain 传 [0,1] 之类固定轴；不传则自动缩放
 */
export function lineChart({ hasSentinel, series, threshold = null, yDomain = null, width = 640, height = 190 }) {
  const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart', role: 'img' });
  const pw = width - PAD.left - PAD.right;
  const ph = height - PAD.top - PAD.bottom;

  const real = series.filter((p) => !isNoSample(hasSentinel, p.y));
  const domainValues = real.map((p) => p.y).concat(threshold == null ? [] : [threshold]);
  const [y0, y1] = yDomain ?? niceDomain(domainValues);
  const sx = (i) => PAD.left + (series.length < 2 ? pw / 2 : (i / (series.length - 1)) * pw);
  const sy = (v) => PAD.top + ph - ((v - y0) / (y1 - y0 || 1)) * ph;

  for (let k = 0; k <= 4; k++) {
    const v = y0 + ((y1 - y0) * k) / 4;
    const y = sy(v);
    svg.append(svgEl('line', { x1: PAD.left, y1: y, x2: width - PAD.right, y2: y, class: 'grid' }));
    svg.append(svgText({ x: PAD.left - 6, y: y + 4, class: 'tick', 'text-anchor': 'end' }, v.toFixed(3)));
  }

  if (threshold != null) {
    const y = sy(threshold);
    svg.append(svgEl('line', { x1: PAD.left, y1: y, x2: width - PAD.right, y2: y, class: 'threshold' }));
    svg.append(
      svgText({ x: width - PAD.right + 6, y: y + 4, class: 'threshold-label' }, `阈值 ${threshold}`)
    );
  }

  // 折线按"无样本处断开"分段，不跨过缺口连一条假的直线。
  let seg = [];
  const flush = () => {
    if (seg.length > 1) svg.append(svgEl('path', { class: 'line', d: seg.join(' ') }));
    seg = [];
  };
  series.forEach((p, i) => {
    if (isNoSample(hasSentinel, p.y)) {
      flush();
      return;
    }
    seg.push(`${seg.length ? 'L' : 'M'}${sx(i).toFixed(1)} ${sy(p.y).toFixed(1)}`);
  });
  flush();

  series.forEach((p, i) => {
    const x = sx(i).toFixed(1);
    if (isNoSample(hasSentinel, p.y)) {
      const rect = svgEl('rect', {
        x: (Number(x) - 3).toFixed(1),
        y: height - PAD.bottom - 4,
        width: 6,
        height: 6,
        class: 'no-sample',
      });
      const t = svgEl('title');
      t.textContent = `${p.x} · 无样本（value=-1 哨兵值）`;
      rect.append(t);
      svg.append(rect);
      return;
    }
    // passed 直接来自数据库列，这里不重算——reconcile_diff 可以
    // value≠threshold 而 passed=true（note 非空即算通过）。
    const g = svgEl('g', { class: `pt ${p.passed ? 'pt-pass' : 'pt-fail'}` });
    g.append(svgEl('circle', { cx: x, cy: sy(p.y).toFixed(1), r: p.passed ? 3 : 5 }));
    const t = svgEl('title');
    t.textContent = `${p.x} · ${p.y} · ${p.passed ? '通过' : '未通过'}` + (p.note ? ` · note: ${p.note}` : '');
    g.append(t);
    svg.append(g);
  });

  if (series.length) {
    svg.append(svgText({ x: PAD.left, y: height - 8, class: 'tick' }, series[0].x));
    if (series.length > 1) {
      svg.append(
        svgText(
          { x: width - PAD.right, y: height - 8, class: 'tick', 'text-anchor': 'end' },
          series.at(-1).x
        )
      );
    }
  }
  return svg;
}

/** 计数类指标（如 orphan_block_count）用柱状图，默认自动缩放且必须包含 0。 */
export function barChart({ bars, cap = null, width = 640, height = 190 }) {
  const svg = svgEl('svg', { viewBox: `0 0 ${width} ${height}`, class: 'chart', role: 'img' });
  const pw = width - PAD.left - PAD.right;
  const ph = height - PAD.top - PAD.bottom;
  const top = Math.max(...bars.map((b) => b.y), cap ?? 0, 1) * 1.1;
  const sy = (v) => PAD.top + ph - (v / top) * ph;
  const bw = pw / Math.max(bars.length, 1);

  bars.forEach((b, i) => {
    const g = svgEl('g', { class: `bar ${b.exceeded ? 'bar-over' : ''}` });
    const x = PAD.left + i * bw + bw * 0.15;
    const y = sy(b.y);
    g.append(
      svgEl('rect', {
        x: x.toFixed(1),
        y: y.toFixed(1),
        width: (bw * 0.7).toFixed(1),
        height: (ph - (y - PAD.top)).toFixed(1),
      })
    );
    const t = svgEl('title');
    t.textContent = `${b.x} · ${b.y}`;
    g.append(t);
    svg.append(g);
    svg.append(
      svgText({ x: (PAD.left + i * bw + bw / 2).toFixed(1), y: height - 8, class: 'tick', 'text-anchor': 'middle' }, b.x)
    );
  });

  if (cap != null) {
    const y = sy(cap);
    svg.append(svgEl('line', { x1: PAD.left, y1: y, x2: width - PAD.right, y2: y, class: 'threshold' }));
    svg.append(svgText({ x: width - PAD.right + 6, y: y + 4, class: 'threshold-label' }, `上限 ${cap}`));
  }
  return svg;
}
