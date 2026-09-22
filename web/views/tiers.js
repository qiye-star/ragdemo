// 分档与预算：A/B/C 档路由分布、生效策略与历史、月度预算、重试队列、
// 告警。五个独立数据源，各自懒加载失败不影响其余部分。

import { getJSON } from '../lib/api.js';
import { el, clear } from '../lib/dom.js';
import { viewLink } from '../lib/state.js';
import { dataTable } from '../lib/table.js';

export const id = 'tiers';
export const title = '分档与预算';

const TIER_LABEL = { A: 'A 档（供应商结构化）', B: 'B 档（xParse 标准）', C: 'C 档（重解析）', failed: '解析失败', other: '其他' };

const TIER_COLUMNS = [
  { label: '档位', get: (t) => TIER_LABEL[t.tier] ?? t.tier },
  { label: '文档数', cls: 'num', get: (t) => t.documents },
  { label: '预算超支', cls: 'num', get: (t) => t.budget_exceeded },
  { label: '置信度均值', cls: 'num', get: (t) => (t.confidence_avg == null ? null : t.confidence_avg.toFixed(4)) },
  { label: '置信度最小值', cls: 'num', get: (t) => (t.confidence_min == null ? null : t.confidence_min.toFixed(4)) },
];

const ENGINE_COLUMNS = [
  { label: 'parse_engine', cls: 'mono', get: (e) => e.parse_engine ?? 'NULL' },
  { label: '文档数', cls: 'num', get: (e) => e.documents },
];

function tierSection(data) {
  return el(
    'section',
    { class: 'card' },
    el('h3', {}, '分档分布'),
    el('pre', { class: 'mono' }, data.tier_rule),
    dataTable({ columns: TIER_COLUMNS, rows: data.tiers }),
    el('details', {}, el('summary', {}, 'parse_engine 原始分布'), dataTable({ columns: ENGINE_COLUMNS, rows: data.by_parse_engine }))
  );
}

const POLICY_COLUMNS = [
  { label: 'doc_type', get: (p) => p.doc_type ?? '（通配）' },
  { label: 'confidence_below', cls: 'num', get: (p) => p.confidence_below },
  { label: 'closure_below', cls: 'num', get: (p) => p.closure_below },
  { label: 'monthly_cap_cny', cls: 'num mono', get: (p) => p.monthly_cap_cny },
  { label: 'enabled', get: (p) => (p.enabled ? '启用' : '停用') },
  { label: 'known_at', cls: 'mono dim', get: (p) => p.known_at.slice(0, 10) },
  { label: 'superseded_at', cls: 'mono dim', get: (p) => (p.superseded_at ? p.superseded_at.slice(0, 10) : null) },
  {
    label: '状态',
    // 此前这个判定挂在 <tr class="pass"> 上——.pass::before{content:'✓ '}
    // 是给单元格设计的伪元素，挂在行上会在所有格子之外单独吐出一个勾，
    // 不在任何列里。判定只能挂在它描述的那个单元格上。
    get: (p) => (p.active_at_as_of ? '生效中' : '历史'),
    cellCls: (p) => (p.active_at_as_of ? 'pass' : 'dim'),
  },
];

function policiesSection(data) {
  return el(
    'section',
    { class: 'card span-2' },
    el('h3', {}, 'C 档路由策略（历史）'),
    dataTable({ columns: POLICY_COLUMNS, rows: data.policies, scroll: true })
  );
}

const BUDGET_COLUMNS = [
  { label: 'doc_type', get: (b) => b.doc_type },
  { label: '页数', cls: 'num mono', get: (b) => b.pages_spent },
  { label: '原始行数', cls: 'num mono', get: (b) => b.rows },
  { label: '月度上限(元)', cls: 'num mono', get: (b) => b.cap_cny },
  {
    label: '已花(元)',
    cls: 'num mono',
    get: (b) => b.spent_cny ?? (b.cost_per_page_configured ? null : '单价未配置，无法换算金额'),
  },
];

function budgetSection(data) {
  if (data.buckets.length === 0) {
    return el(
      'section',
      { class: 'card' },
      el('h3', {}, `月度预算（${data.month_start} – ${data.month_end}）`),
      el('p', { class: 'dim' }, '这个月没有 C 档重解析记录。')
    );
  }
  return el(
    'section',
    { class: 'card' },
    el('h3', {}, `月度预算（${data.month_start} – ${data.month_end}）`),
    el('p', { class: 'dim' }, '页数汇总用裸 sum(value)，与月度预算闸门同一口径，rows>1 时说明这个月有重复写入。'),
    dataTable({ columns: BUDGET_COLUMNS, rows: data.buckets })
  );
}

const RETRY_COLUMNS = [
  { label: 'source', get: (e) => e.source },
  { label: 'provider_doc_id', cls: 'mono', get: (e) => e.provider_doc_id },
  { label: 'first_failed_at', cls: 'mono dim', get: (e) => e.first_failed_at.slice(0, 16) },
  { label: 'retry_deadline', cls: 'mono dim', get: (e) => e.retry_deadline.slice(0, 16) },
  { label: 'attempts', cls: 'num', get: (e) => e.attempts },
  {
    label: '状态',
    get: (e) => (e.overdue ? '已逾期' : '窗口内'),
    cellCls: (e) => (e.overdue ? 'fail' : 'pass'),
  },
];

function retryQueueSection(data) {
  const body = data.entries.length
    ? dataTable({ columns: RETRY_COLUMNS, rows: data.entries })
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
  return el(
    'section',
    { class: 'card span-2' },
    el('h3', {}, '解析告警'),
    data.aggregate.length ? list : el('p', { class: 'dim' }, '没有任何告警。'),
    el('div', { class: 'warning-docs' })
  );
}

export async function render(ctx) {
  const [tiers, policies, budget, retryQueue, warnings] = await Promise.all([
    getJSON('/api/parse/tiers', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/parse/policies', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/parse/budget', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/parse/retry-queue', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/parse/warnings', {}, { asOf: ctx.asOf, signal: ctx.signal }),
  ]);

  const warnCard = warningsSection(warnings, async (warning) => {
    const detail = await getJSON('/api/parse/warnings', { warning }, { asOf: ctx.asOf, signal: ctx.signal });
    const host = warnCard.querySelector('.warning-docs');
    clear(host);
    host.append(
      el(
        'ul',
        {},
        detail.documents.map((d) =>
          el(
            'li',
            {},
            viewLink('layout', { doc: d.doc_id, page: 1 }, `#${d.doc_id} ${d.title}`),
            `（${d.source}, ${d.publish_at.slice(0, 10)}）`
          )
        )
      )
    );
  });

  return el(
    'div',
    { class: 'card-grid' },
    tierSection(tiers),
    budgetSection(budget),
    retryQueueSection(retryQueue),
    policiesSection(policies),
    warnCard
  );
}
