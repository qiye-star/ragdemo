// 检索诊断：ADR-0010 立项的另一半理由——同一查询下 BM25/向量/融合/重排/
// 最终五段排名并排比较。查询从 hash 里读（navigateTo('retrieval', {q})），
// 所以链接可分享、as_of 跟着走；有 q 就自动发起检索（用户裁决：搜索时
// 自动调，按 (query, as_of) 缓存，不需要额外的"执行"按钮）。
//
// 页面没有一个 <form> 元素——查询框是 input + button + Enter 键监听，
// 不是 form submit（约束 1「无写接口」的结构性保证之一）。

import { getJSON } from '../lib/api.js';
import { el } from '../lib/dom.js';
import { emptyCard } from '../lib/status.js';
import { navigateTo, viewLink } from '../lib/state.js';
import { dataTable } from '../lib/table.js';

export const id = 'retrieval';
export const title = '检索诊断';

function searchBox(initialQuery) {
  const input = el('input', {
    type: 'text',
    value: initialQuery ?? '',
    placeholder: '检索查询……',
    'aria-label': '检索查询',
    class: 'retrieval-input',
  });
  const button = el('button', { type: 'button' }, '检索');
  const submit = () => {
    const q = input.value.trim();
    if (q) navigateTo('retrieval', { q });
  };
  button.addEventListener('click', submit);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submit();
  });
  return el('div', { class: 'retrieval-search' }, input, button);
}

function modelsBanner(models, stats, coverage) {
  const lines = [
    el(
      'p',
      {},
      `嵌入模型：${models.embedder}${models.embedder_is_mock ? '（Mock）' : ''}` +
        ` · 重排模型：${models.reranker}${models.reranker_is_mock ? '（Mock）' : ''}`
    ),
  ];
  if (!models.vector_path_is_meaningful) {
    lines.push(
      el(
        'p',
        { class: 'fail' },
        '向量列由 Mock 伪向量算出，语义上无意义；BM25 列是真实排名。'
      )
    );
  }
  if (stats.degraded) {
    lines.push(el('p', { class: 'fail' }, '重排失败，已降级为 RRF 顺序。'));
  } else if (!stats.rerank_attempted) {
    lines.push(el('p', { class: 'dim' }, '本次检索未启用重排（rerank=false）。'));
  }
  lines.push(
    el(
      'p',
      { class: 'dim mono' },
      `${coverage.with_embedding}/${coverage.leaf_blocks} 叶子块带向量` +
        ` · 语料 embedding_version：${models.corpus_embedding_versions.join(', ') || '（无）'}`
    )
  );
  return el('section', { class: 'card' }, lines);
}

function stageCell(rank, score) {
  return rank == null ? '—' : `${rank} (${score.toFixed(3)})`;
}

function resultsTable(rows, vectorMeaningful) {
  const columns = [
    {
      label: '块',
      get: (r) =>
        el(
          'div',
          {},
          viewLink('layout', { doc: r.doc_id, page: r.page ?? 1 }, `#${r.block_id} ${r.doc_title}`),
          el('div', { class: 'cell-sub dim' }, `${r.section_path || '（无章节）'} · ${r.preview}`)
        ),
    },
    { label: 'BM25', cls: 'mono num', get: (r) => stageCell(r.bm25_rank, r.bm25_score) },
    {
      label: '向量',
      cls: `mono num${vectorMeaningful ? '' : ' mock-col'}`,
      get: (r) => stageCell(r.vec_rank, r.vec_score),
    },
    { label: '融合', cls: 'mono num', get: (r) => stageCell(r.fused_rank, r.fused_score) },
    { label: '重排', cls: 'mono num', get: (r) => stageCell(r.rerank_rank, r.rerank_score) },
    { label: '最终', cls: 'num', get: (r) => r.final_position },
  ];
  return dataTable({ columns, rows, scroll: true });
}

export async function render(ctx) {
  const q = ctx.params.get('q') ?? '';
  const container = el('div', {});
  container.append(searchBox(q));

  if (!q) {
    container.append(el('p', { class: 'dim' }, '输入查询词并按 Enter（或点击「检索」）。'));
    return container;
  }

  const body = await getJSON('/api/retrieval/search', { q }, { asOf: ctx.asOf, signal: ctx.signal });

  container.append(modelsBanner(body.models, body.stats, body.coverage));

  if (body.rows.length === 0) {
    container.append(
      emptyCard({
        what: `查询 ${JSON.stringify(q)} 的候选块`,
        predicate: `as_of = ${body.as_of}\nowner_tenant IS NULL AND owner_user IS NULL`,
      })
    );
    return container;
  }

  container.append(resultsTable(body.rows, body.models.vector_path_is_meaningful));
  return container;
}
