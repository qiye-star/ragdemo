// 分档与预算：A/B/C 档路由分布、生效策略与历史、月度预算、重试队列、
// 告警。五个独立数据源，各自懒加载失败不影响其余部分。

import { getJSON } from '../lib/api.js';
import { el, clear } from '../lib/dom.js';
import { barChart } from '../lib/chart.js';

export const id = 'tiers';
export const title = '分档与预算';

const TIER_LABEL = { A: 'A 档（供应商结构化）', B: 'B 档（xParse 标准）', C: 'C 档（重解析）', failed: '解析失败', other: '其他' };

function tierSection(data) {
  const bars = data.tiers.map((t) => ({ x: t.tier, y: t.documents, exceeded: t.tier === 'failed' && t.documents > 0 }));
  const chart = barChart({ bars });
  const table = el(
    'table',
    { class: 'grid-table' },
    el('thead', {}, el('tr', {}, el('th', {}, '档位'), el('th', {}, '文档数'), el('th', {}, '预算超支'), el('th', {}, '置信度均值'), el('th', {}, '置信度最小值'))),
    el(
      'tbody',
      {},
      data.tiers.map((t) =>
        el(
          'tr',
          {},
          el('td', {}, TIER_LABEL[t.tier] ?? t.tier),
          el('td', { class: 'num' }, t.documents),
          el('td', { class: 'num' }, t.budget_exceeded),
          el('td', { class: 'num' }, t.confidence_avg == null ? '—' : t.confidence_avg.toFixed(4)),
          el('td', { class: 'num' }, t.confidence_min == null ? '—' : t.confidence_min.toFixed(4))
        )
      )
    )
  );
  const engineTable = el(
    'table',
    { class: 'grid-table' },
    el('thead', {}, el('tr', {}, el('th', {}, 'parse_engine'), el('th', {}, '文档数'))),
    el('tbody', {}, data.by_parse_engine.map((e) => el('tr', {}, el('td', { class: 'mono' }, e.parse_engine ?? 'NULL'), el('td', { class: 'num' }, e.documents))))
  );
  return el(
    'section',
    { class: 'card' },
    el('h3', {}, '分档分布'),
    el('pre', { class: 'mono' }, data.tier_rule),
    chart,
    table,
    el('details', {}, el('summary', {}, 'parse_engine 原始分布'), engineTable)
  );
}

function policiesSection(data) {
  const rows = data.policies.map((p) =>
    el(
      'tr',
      { class: p.active_at_as_of ? 'pass' : undefined },
      el('td', {}, p.doc_type ?? '（通配）'),
      el('td', { class: 'num' }, p.confidence_below ?? '—'),
      el('td', { class: 'num' }, p.closure_below ?? '—'),
      el('td', { class: 'num mono' }, p.monthly_cap_cny),
      el('td', {}, p.enabled ? '启用' : '停用'),
      el('td', { class: 'mono dim' }, p.known_at.slice(0, 10)),
      el('td', { class: 'mono dim' }, p.superseded_at ? p.superseded_at.slice(0, 10) : '—'),
      el('td', {}, p.active_at_as_of ? '生效中' : '历史')
    )
  );
  return el(
    'section',
    { class: 'card' },
    el('h3', {}, 'C 档路由策略（历史）'),
    el(
      'table',
      { class: 'grid-table' },
      el('thead', {}, el('tr', {}, el('th', {}, 'doc_type'), el('th', {}, 'confidence_below'), el('th', {}, 'closure_below'), el('th', {}, 'monthly_cap_cny'), el('th', {}, 'enabled'), el('th', {}, 'known_at'), el('th', {}, 'superseded_at'), el('th', {}, '状态'))),
      el('tbody', {}, rows)
    )
  );
}

function budgetSection(data) {
  if (data.buckets.length === 0) {
    return el('section', { class: 'card' }, el('h3', {}, `月度预算（${data.month_start} – ${data.month_end}）`), el('p', { class: 'dim' }, '这个月没有 C 档重解析记录。'));
  }
  const rows = data.buckets.map((b) =>
    el(
      'tr',
      {},
      el('td', {}, b.doc_type),
      el('td', { class: 'num mono' }, b.pages_spent),
      el('td', { class: 'num mono' }, b.rows),
      el('td', { class: 'num mono' }, b.cap_cny ?? '—'),
      el('td', { class: 'num mono' }, b.spent_cny ?? (b.cost_per_page_configured ? '—' : '单价未配置，无法换算金额'))
    )
  );
  return el(
    'section',
    { class: 'card' },
    el('h3', {}, `月度预算（${data.month_start} – ${data.month_end}）`),
    el('p', { class: 'dim' }, '页数汇总用裸 sum(value)，与月度预算闸门同一口径，rows>1 时说明这个月有重复写入。'),
    el(
      'table',
      { class: 'grid-table' },
      el('thead', {}, el('tr', {}, el('th', {}, 'doc_type'), el('th', {}, '页数'), el('th', {}, '原始行数'), el('th', {}, '月度上限(元)'), el('th', {}, '已花(元)'))),
      el('tbody', {}, rows)
    )
  );
}

function retryQueueSection(data) {
  const body = data.entries.length
    ? el(
        'table',
        { class: 'grid-table' },
        el('thead', {}, el('tr', {}, el('th', {}, 'source'), el('th', {}, 'provider_doc_id'), el('th', {}, 'first_failed_at'), el('th', {}, 'retry_deadline'), el('th', {}, 'attempts'), el('th', {}, '状态'))),
        el(
          'tbody',
          {},
          data.entries.map((e) =>
            el(
              'tr',
              {},
              el('td', {}, e.source),
              el('td', { class: 'mono' }, e.provider_doc_id),
              el('td', { class: 'mono dim' }, e.first_failed_at.slice(0, 16)),
              el('td', { class: 'mono dim' }, e.retry_deadline.slice(0, 16)),
              el('td', { class: 'num' }, e.attempts),
              el('td', { class: e.overdue ? 'fail' : 'pass' }, e.overdue ? '已逾期' : '窗口内')
            )
          )
        )
      )
    : el('p', { class: 'dim' }, '重试队列为空。');
  return el(
    'section',
    { class: 'card' },
    el('h3', {}, '重试队列'),
    el('p', { class: 'dim mono' }, `point_in_time: ${data.point_in_time} — ${data.reason}`),
    body
  );
}

function warningsSection(data, onPick) {
  const list = el(
    'ul',
    {},
    data.aggregate.map((a) => {
      const li = el('li', {}, el('button', { type: 'button', class: 'chip-btn' }, `${a.warning}（${a.documents}）`));
      li.querySelector('button').addEventListener('click', () => onPick(a.warning));
      return li;
    })
  );
  return el('section', { class: 'card' }, el('h3', {}, '解析告警'), data.aggregate.length ? list : el('p', { class: 'dim' }, '没有任何告警。'), el('div', { class: 'warning-docs' }));
}

export async function render(ctx) {
  const [tiers, policies, budget, retryQueue, warnings] = await Promise.all([
    getJSON('/api/parse/tiers', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/parse/policies', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/parse/budget', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/parse/retry-queue', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/parse/warnings', {}, { asOf: ctx.asOf, signal: ctx.signal }),
  ]);

  const container = el('div', {});
  container.append(tierSection(tiers), policiesSection(policies), budgetSection(budget), retryQueueSection(retryQueue));

  const warnCard = warningsSection(warnings, async (warning) => {
    const detail = await getJSON('/api/parse/warnings', { warning }, { asOf: ctx.asOf, signal: ctx.signal });
    const host = warnCard.querySelector('.warning-docs');
    clear(host);
    host.append(
      el(
        'ul',
        {},
        detail.documents.map((d) => el('li', {}, `#${d.doc_id} ${d.title}（${d.source}, ${d.publish_at.slice(0, 10)}）`))
      )
    );
  });
  container.append(warnCard);
  return container;
}
