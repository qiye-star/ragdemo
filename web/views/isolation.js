// 权限隔离：固定探针矩阵 + 目录佐证。约束 6（硬编码只看公共检索空间）
// 意味着这个视图不能做成"切换身份浏览私有文档"——后端的
// /api/isolation/probes 签名里本来就没有身份参数，这里也不提供任何
// 身份选择输入框。全部 textContent，绝不 innerHTML。

import { getJSON } from '../lib/api.js';
import { el } from '../lib/dom.js';
import { dataTable } from '../lib/table.js';

export const id = 'isolation';
export const title = '权限隔离';

const STATUS_LABEL = { passed: '通过', failed: '未通过', skipped: '跳过（无演示数据）' };
const STATUS_CLASS = { passed: 'pass', failed: 'fail', skipped: 'dim' };

// 7 个聚合计数——两张转置表（docs 一张、blocks 一张）共用同一份行定义，
// 行 = 计数字段，列 = 4 个身份分支，foreign_* 行非零标红。
const COUNTER_KEYS = [
  'total',
  'public_rows',
  'user_private_rows',
  'tenant_private_rows',
  'empty_owner_rows',
  'foreign_user_rows',
  'foreign_tenant_rows',
];

function stat(label, value, cls) {
  return el(
    'div',
    { class: 'stat' },
    el('div', { class: 'stat-label dim' }, label),
    el('div', { class: `stat-value mono${cls ? ` ${cls}` : ''}` }, value)
  );
}

// 顶部状态条——只对接口已经算好的标志位计数展示，不合成任何论断句
// （约束 7）：数的是 checks[].status / error_branches[].passed /
// rls_status[].relforcerowsecurity / owner_is_superuser，不是写出来的判断。
function statStrip(catalog, probes) {
  const tally = (status) => probes.checks.filter((c) => c.status === status).length;
  const errPassed = probes.error_branches.filter((b) => b.passed).length;
  const forceOff = catalog.rls_status.filter((r) => !r.relforcerowsecurity).length;
  const superOwner = catalog.rls_status.filter((r) => r.owner_is_superuser).length;
  const failed = tally('failed');
  return el(
    'div',
    { class: 'stat-strip' },
    stat('检查·通过', tally('passed'), 'pass'),
    stat('检查·未通过', failed, failed ? 'fail' : 'dim'),
    stat('检查·跳过', tally('skipped'), 'dim'),
    stat(
      '错误分支符合预期',
      `${errPassed}/${probes.error_branches.length}`,
      errPassed === probes.error_branches.length ? 'pass' : 'fail'
    ),
    stat('relforcerowsecurity=false 的表', forceOff, forceOff ? 'fail' : 'pass'),
    stat('owner_is_superuser=true 的表', superOwner, superOwner ? 'fail' : 'pass'),
    stat('demo_seeded', String(probes.demo_seeded), probes.demo_seeded ? 'pass' : 'dim')
  );
}

function branchHeader(b) {
  return el('span', {}, b.label, el('br'), el('span', { class: 'mono dim' }, `${b.tenant ?? '—'}/${b.user ?? '—'}`));
}

function relationMatrix(branches, relation, title) {
  const columns = [
    { label: '计数', cls: 'mono', get: (key) => key },
    ...branches.map((b) => ({
      label: branchHeader(b),
      cls: 'num',
      get: (key) => b[relation][key],
      cellCls: (key, v) => (key.startsWith('foreign') && v > 0 ? 'fail' : undefined),
    })),
  ];
  return el('section', { class: 'card' }, el('h3', {}, title), dataTable({ columns, rows: COUNTER_KEYS }));
}

const RLS_COLUMNS = [
  { label: '表', get: (r) => r.relname },
  { label: 'relrowsecurity', get: (r) => String(r.relrowsecurity), cellCls: (r) => (r.relrowsecurity ? 'pass' : 'fail') },
  {
    label: 'relforcerowsecurity',
    get: (r) => String(r.relforcerowsecurity),
    cellCls: (r) => (r.relforcerowsecurity ? 'pass' : 'fail'),
  },
  { label: 'owner', get: (r) => r.owner },
  {
    label: 'owner_is_superuser',
    get: (r) => String(r.owner_is_superuser),
    cellCls: (r) => (r.owner_is_superuser ? 'fail' : 'pass'),
  },
];

const VIEW_COLUMNS = [
  { label: 'asof 视图', get: (v) => v.relname },
  { label: 'owner', get: (v) => v.owner },
  {
    label: 'owner_is_superuser',
    get: (v) => String(v.owner_is_superuser),
    cellCls: (v) => (v.owner_is_superuser ? 'fail' : 'pass'),
  },
];

const POLICY_COLUMNS = [
  { label: '表', get: (p) => p.tablename },
  { label: 'cmd', get: (p) => p.cmd },
  { label: 'policy', cls: 'mono', get: (p) => p.policyname },
  { label: 'roles', cls: 'mono', get: (p) => p.roles.join(', ') },
  { label: 'qual/with_check', get: (p) => el('pre', { class: 'mono' }, p.qual ?? p.with_check ?? '—') },
];

function identityDl(identity) {
  const pairs = [
    ['current_user', identity.current_user],
    ['session_user', identity.session_user],
    ['current_user_is_superuser', String(identity.current_user_is_superuser)],
    ['asof.doc_block SELECT', String(identity.asof_doc_block_select)],
    ['core.doc_block SELECT', String(identity.core_doc_block_select)],
    ['core.doc_block INSERT', String(identity.core_doc_block_insert)],
    ['core.document SELECT', String(identity.core_document_select)],
    ['quality.quality_metric SELECT', String(identity.quality_metric_select)],
  ];
  const dl = el('dl', { class: 'kv mono' });
  for (const [k, v] of pairs) dl.append(el('dt', {}, k), el('dd', {}, v));
  return dl;
}

// relforcerowsecurity=false 或 owner_is_superuser=true 任一成立，下面探针
// 矩阵的绿全是假的——rls_status 表因此是前置条件，留在可见区，不进
// <details>。其余 SQL 证据（连接身份、asof 视图属主、policy 原文）只是
// 佐证，默认折叠。
function catalogSection(catalog) {
  return el(
    'section',
    { class: 'card span-2' },
    el('h2', {}, '目录佐证（隔离是否真的生效的前置条件）'),
    dataTable({ columns: RLS_COLUMNS, rows: catalog.rls_status }),
    el(
      'details',
      {},
      el('summary', {}, '连接身份 / asof 视图属主 / policy 原文（SQL 证据）'),
      identityDl(catalog.connection_identity),
      dataTable({ columns: VIEW_COLUMNS, rows: catalog.asof_view_owners }),
      dataTable({ columns: POLICY_COLUMNS, rows: catalog.policies, scroll: true })
    )
  );
}

const CHECK_COLUMNS = [
  { label: '检查', cls: 'mono', get: (c) => c.name },
  { label: '结果', get: (c) => STATUS_LABEL[c.status] ?? c.status, cellCls: (c) => STATUS_CLASS[c.status] },
  { label: '说明', cls: 'dim', get: (c) => c.detail },
];

const ERROR_BRANCH_COLUMNS = [
  { label: '分支', cls: 'mono', get: (b) => b.key },
  { label: 'raised', get: (b) => String(b.raised) },
  { label: 'sqlstate', cls: 'mono', get: (b) => b.sqlstate },
  { label: 'message', get: (b) => el('pre', { class: 'mono' }, b.message_head ?? '（未抛出异常）') },
  {
    label: '判定',
    get: (b) => (b.passed ? '符合预期' : '不符合预期'),
    cellCls: (b) => (b.passed ? 'pass' : 'fail'),
  },
];

export async function render(ctx) {
  const [catalog, probes] = await Promise.all([
    getJSON('/api/isolation/catalog', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/isolation/probes', {}, { asOf: ctx.asOf, signal: ctx.signal }),
  ]);

  const container = el('div', {});
  container.append(statStrip(catalog, probes));
  container.append(catalogSection(catalog));

  container.append(
    el(
      'section',
      { class: 'card span-2' },
      el('h2', {}, '固定探针矩阵'),
      el('p', { class: 'dim' }, '身份来自服务端硬编码常量，不接受任何请求参数——这里没有身份选择输入框。'),
      el(
        'p',
        { class: probes.demo_seeded ? 'pass' : 'dim' },
        probes.demo_seeded ? '演示数据存在，以下私有相关检查真实生效' : '演示数据不存在（ragdemo db seed-isolation-demo），私有相关检查标"跳过"'
      ),
      el(
        'div',
        { class: 'card-grid' },
        relationMatrix(probes.identity_branches, 'documents', 'documents 可见行数'),
        relationMatrix(probes.identity_branches, 'blocks', 'blocks 可见行数')
      ),
      el('h3', {}, '判定'),
      dataTable({ columns: CHECK_COLUMNS, rows: probes.checks }),
      el('h3', {}, '错误分支（约束 4/5 的运行时告警器）'),
      dataTable({ columns: ERROR_BRANCH_COLUMNS, rows: probes.error_branches })
    )
  );

  return container;
}
