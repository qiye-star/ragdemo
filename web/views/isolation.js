// 权限隔离：固定探针矩阵 + 目录佐证。约束 6（硬编码只看公共检索空间）
// 意味着这个视图不能做成"切换身份浏览私有文档"——后端的
// /api/isolation/probes 签名里本来就没有身份参数，这里也不提供任何
// 身份选择输入框。全部 textContent，绝不 innerHTML。

import { getJSON } from '../lib/api.js';
import { el } from '../lib/dom.js';

export const id = 'isolation';
export const title = '权限隔离';

const STATUS_LABEL = { passed: '通过', failed: '未通过', skipped: '跳过（无演示数据）' };
const STATUS_CLASS = { passed: 'pass', failed: 'fail', skipped: 'dim' };

function catalogSection(catalog) {
  const identity = catalog.connection_identity;
  const rlsRows = catalog.rls_status.map((r) =>
    el(
      'tr',
      {},
      el('td', {}, r.relname),
      el('td', { class: r.relrowsecurity ? 'pass' : 'fail' }, String(r.relrowsecurity)),
      el('td', { class: r.relforcerowsecurity ? 'pass' : 'fail' }, String(r.relforcerowsecurity)),
      el('td', {}, r.owner),
      el('td', { class: r.owner_is_superuser ? 'fail' : 'pass' }, String(r.owner_is_superuser))
    )
  );
  const viewRows = catalog.asof_view_owners.map((v) =>
    el('tr', {}, el('td', {}, v.relname), el('td', {}, v.owner), el('td', { class: v.owner_is_superuser ? 'fail' : 'pass' }, String(v.owner_is_superuser)))
  );

  // relforcerowsecurity=false 或 owner_is_superuser=true 任一成立，
  // 下面探针矩阵的绿全是假的——这两行必须放在矩阵上方，是前置条件
  // 不是补充说明。
  return el(
    'section',
    { class: 'card' },
    el('h2', {}, '目录佐证（隔离是否真的生效的前置条件）'),
    el(
      'p',
      { class: 'mono dim' },
      `连接身份: current_user=${identity.current_user} session_user=${identity.session_user} ` +
        `current_user_is_superuser=${identity.current_user_is_superuser}`
    ),
    el(
      'p',
      { class: 'mono dim' },
      `asof.doc_block SELECT=${identity.asof_doc_block_select} · core.doc_block SELECT=${identity.core_doc_block_select} ` +
        `core.doc_block INSERT=${identity.core_doc_block_insert} · core.document SELECT=${identity.core_document_select} ` +
        `quality.quality_metric SELECT=${identity.quality_metric_select}`
    ),
    el(
      'table',
      { class: 'grid-table' },
      el('thead', {}, el('tr', {}, el('th', {}, '表'), el('th', {}, 'relrowsecurity'), el('th', {}, 'relforcerowsecurity'), el('th', {}, 'owner'), el('th', {}, 'owner_is_superuser'))),
      el('tbody', {}, rlsRows)
    ),
    el(
      'table',
      { class: 'grid-table' },
      el('thead', {}, el('tr', {}, el('th', {}, 'asof 视图'), el('th', {}, 'owner'), el('th', {}, 'owner_is_superuser'))),
      el('tbody', {}, viewRows)
    ),
    el(
      'table',
      { class: 'grid-table' },
      el('thead', {}, el('tr', {}, el('th', {}, '表'), el('th', {}, 'cmd'), el('th', {}, 'policy'), el('th', {}, 'roles'), el('th', {}, 'qual/with_check'))),
      el(
        'tbody',
        {},
        catalog.policies.map((p) =>
          el(
            'tr',
            {},
            el('td', {}, p.tablename),
            el('td', {}, p.cmd),
            el('td', { class: 'mono' }, p.policyname),
            el('td', { class: 'mono' }, p.roles.join(', ')),
            el('td', {}, el('pre', { class: 'mono' }, p.qual ?? p.with_check ?? '—'))
          )
        )
      )
    )
  );
}

function branchTable(branches) {
  const cols = ['total', 'public_rows', 'user_private_rows', 'tenant_private_rows', 'empty_owner_rows', 'foreign_user_rows', 'foreign_tenant_rows'];
  const header = el('tr', {}, el('th', {}, '分支'), el('th', {}, 'tenant'), el('th', {}, 'user'), ...cols.map((c) => el('th', {}, `docs.${c}`)), ...cols.map((c) => el('th', {}, `blocks.${c}`)));
  const rows = branches.map((b) =>
    el(
      'tr',
      {},
      el('td', {}, b.label),
      el('td', { class: 'mono dim' }, b.tenant ?? '—'),
      el('td', { class: 'mono dim' }, b.user ?? '—'),
      ...cols.map((c) => el('td', { class: c.startsWith('foreign') && b.documents[c] > 0 ? 'num fail' : 'num' }, b.documents[c])),
      ...cols.map((c) => el('td', { class: c.startsWith('foreign') && b.blocks[c] > 0 ? 'num fail' : 'num' }, b.blocks[c]))
    )
  );
  return el('table', { class: 'grid-table' }, el('thead', {}, header), el('tbody', {}, rows));
}

function checksSection(checks) {
  return el(
    'table',
    { class: 'grid-table' },
    el('thead', {}, el('tr', {}, el('th', {}, '检查'), el('th', {}, '结果'), el('th', {}, '说明'))),
    el(
      'tbody',
      {},
      checks.map((c) => el('tr', {}, el('td', { class: 'mono' }, c.name), el('td', { class: STATUS_CLASS[c.status] }, STATUS_LABEL[c.status] ?? c.status), el('td', { class: 'dim' }, c.detail)))
    )
  );
}

function errorBranchesSection(branches) {
  return el(
    'table',
    { class: 'grid-table' },
    el('thead', {}, el('tr', {}, el('th', {}, '分支'), el('th', {}, 'raised'), el('th', {}, 'sqlstate'), el('th', {}, 'message'), el('th', {}, '判定'))),
    el(
      'tbody',
      {},
      branches.map((b) =>
        el(
          'tr',
          {},
          el('td', { class: 'mono' }, b.key),
          el('td', {}, String(b.raised)),
          el('td', { class: 'mono' }, b.sqlstate ?? '—'),
          el('td', {}, el('pre', { class: 'mono' }, b.message_head ?? '（未抛出异常）')),
          el('td', { class: b.passed ? 'pass' : 'fail' }, b.passed ? '符合预期' : '不符合预期')
        )
      )
    )
  );
}

export async function render(ctx) {
  const [catalog, probes] = await Promise.all([
    getJSON('/api/isolation/catalog', {}, { asOf: ctx.asOf, signal: ctx.signal }),
    getJSON('/api/isolation/probes', {}, { asOf: ctx.asOf, signal: ctx.signal }),
  ]);

  const container = el('div', {});
  container.append(catalogSection(catalog));

  container.append(
    el(
      'section',
      { class: 'card' },
      el('h2', {}, '固定探针矩阵'),
      el('p', { class: 'dim' }, '身份来自服务端硬编码常量，不接受任何请求参数——这里没有身份选择输入框。'),
      el('p', { class: probes.demo_seeded ? 'pass' : 'dim' }, probes.demo_seeded ? '演示数据存在，以下私有相关检查真实生效' : '演示数据不存在（ragdemo db seed-isolation-demo），私有相关检查标"跳过"'),
      branchTable(probes.identity_branches),
      el('h3', {}, '判定'),
      checksSection(probes.checks),
      el('h3', {}, '错误分支（约束 4/5 的运行时告警器）'),
      errorBranchesSection(probes.error_branches)
    )
  );

  return container;
}
