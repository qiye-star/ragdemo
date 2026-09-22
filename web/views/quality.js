// 质量门禁看板：quality.quality_metric 不在 asof schema 里（约束 5 对它
// 不可满足，运维表不是时点事实表），接口已经自曝口径（source_table/
// as_of_filter/note），这里原样展示这行免责声明，不隐藏。

import { getJSON } from '../lib/api.js';
import { el, clear } from '../lib/dom.js';
import { lineChart, barChart } from '../lib/chart.js';

export const id = 'quality';
export const title = '质量门禁';

// 表现层的粗略分类，只影响默认坐标轴——不是新的业务判定。真正的
// passed/threshold/方向语义完全来自后端 registry，这里只是"这个数字
// 长得像不像一个比例"的展示决定。
const RATE_SUFFIXES = ['_rate', '_p50'];
const isRateLike = (metric) => RATE_SUFFIXES.some((s) => metric.endsWith(s));

function specBadges(spec) {
  const chips = el('span', { class: 'chips' });
  chips.append(el('span', { class: spec.is_gate ? 'chip' : 'chip chip-out' }, spec.is_gate ? '门禁' : '观测'));
  if (spec.blocking === true) chips.append(el('span', { class: 'chip' }, '阻断'));
  if (spec.blocking === false) chips.append(el('span', { class: 'chip' }, '告警'));
  if (spec.direction) chips.append(el('span', { class: 'chip' }, spec.direction === 'higher_is_better' ? '越大越好' : '越小越好'));
  if (spec.has_no_sample_sentinel) chips.append(el('span', { class: 'chip' }, 'value=-1 为无样本'));
  return chips;
}

function latestRow(point) {
  return el(
    'tr',
    {},
    el('td', {}, point.source_id ?? '（全局）'),
    el('td', { class: 'num mono' }, point.value),
    el('td', { class: 'num mono' }, point.threshold ?? '—'),
    el('td', { class: point.passed ? 'pass' : 'fail' }, point.passed ? '通过' : '未通过'),
    el('td', { class: 'dim' }, point.note ?? '—'),
    el('td', { class: 'mono dim' }, point.partition_date)
  );
}

async function trendPanel(metric, spec, sourceId, asOf, signal) {
  const history = await getJSON(
    `/api/quality/metrics/${metric}/history`,
    { days: 60, source_id: sourceId },
    { asOf, signal }
  );
  const series = history.points
    .slice()
    .reverse()
    .map((p) => ({ x: p.partition_date, y: p.value, passed: p.passed, note: p.note }));

  if (series.length === 0) return el('p', { class: 'dim' }, '这个窗口没有历史点。');

  const threshold = series.some((p) => p.y !== -1) ? (history.points[0].threshold ?? null) : null;
  const rateLike = isRateLike(metric);
  const chart = lineChart({
    hasSentinel: spec.has_no_sample_sentinel,
    series,
    threshold,
    yDomain: rateLike ? [0, 1] : null,
  });
  return el('div', {}, chart, rateLike ? el('p', { class: 'dim' }, '固定 0–1 轴（比率类指标默认）') : null);
}

function metricCard(metric, points, spec, ctx) {
  const bySource = new Map();
  for (const p of points) {
    const key = p.source_id ?? '';
    if (!bySource.has(key) || bySource.get(key).computed_at < p.computed_at) bySource.set(key, p);
  }
  const rows = [...bySource.values()].sort((a, b) => (a.source_id ?? '').localeCompare(b.source_id ?? ''));

  const card = el(
    'section',
    { class: 'card' },
    el('h3', {}, metric),
    specBadges(spec ?? { is_gate: false, blocking: null, direction: null, has_no_sample_sentinel: false }),
    el('p', { class: 'dim' }, spec?.description ?? ''),
    el(
      'table',
      { class: 'grid-table' },
      el(
        'thead',
        {},
        el('tr', {}, el('th', {}, 'source_id'), el('th', {}, 'value'), el('th', {}, 'threshold'), el('th', {}, '状态'), el('th', {}, 'note'), el('th', {}, 'partition_date'))
      ),
      el('tbody', {}, rows.map(latestRow))
    )
  );
  const trendHost = el('div', {}, el('p', { class: 'dim' }, '点下面任一 source_id 查看近 60 天趋势'));
  const buttons = el(
    'div',
    { class: 'chips' },
    rows.map((r) =>
      el('button', { type: 'button', class: 'chip-btn' }, r.source_id ?? '（全局）')
    )
  );
  buttons.querySelectorAll('button').forEach((btn, i) => {
    btn.addEventListener('click', async () => {
      clear(trendHost);
      trendHost.append(el('p', { class: 'dim' }, '加载中…'));
      const node = await trendPanel(metric, spec, rows[i].source_id, ctx.asOf, ctx.signal);
      clear(trendHost);
      trendHost.append(node);
    });
  });
  card.append(buttons, trendHost);
  return card;
}

export async function render(ctx) {
  const [registry, dashboard] = await Promise.all([
    getJSON('/api/quality/registry', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/quality/dashboard', {}, { asOf: ctx.asOf, signal: ctx.signal }),
  ]);
  const specByMetric = new Map(registry.map((s) => [s.metric, s]));

  const byMetric = new Map();
  for (const p of dashboard.metrics) {
    if (!byMetric.has(p.metric)) byMetric.set(p.metric, []);
    byMetric.get(p.metric).push(p);
  }

  const container = el('div', {});
  container.append(
    el('p', { class: 'dim mono' }, `${dashboard.source_table} · ${dashboard.as_of_filter} · ${dashboard.note}`)
  );

  if (byMetric.size === 0) {
    container.append(el('p', { class: 'dim' }, '这个时点下没有任何质量指标记录。'));
    return container;
  }

  // 注册表里没有数据的指标（还没跑过）也列出来，标"无记录"——藏起来
  // 等于假装它不存在。
  const allMetrics = new Set([...specByMetric.keys(), ...byMetric.keys()]);
  for (const metric of allMetrics) {
    const points = byMetric.get(metric);
    if (!points) {
      container.append(
        el('section', { class: 'card' }, el('h3', {}, metric), specBadges(specByMetric.get(metric)), el('p', { class: 'dim' }, '这个时点下没有记录。'))
      );
      continue;
    }
    container.append(metricCard(metric, points, specByMetric.get(metric), ctx));
  }
  return container;
}
