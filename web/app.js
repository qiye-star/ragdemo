// 路由入口：读 hash → 同步 as_of 条 → 渲染当前视图。
//
// 导航是数据驱动的（从 VIEWS 生成，不是 index.html 里写死的 <a> 列表）：
// 加一个新视图只需要在下面的 import 列表和 VIEWS 数组里各加一行。

import { readRoute, bindAsOfBar, syncAsOfBar, hashFor } from './lib/state.js';
import { asOfBlockedCard, errorCard, loading } from './lib/status.js';
import { el } from './lib/dom.js';
import * as docs from './views/docs.js';
import * as layout from './views/layout.js';
import * as blocks from './views/blocks.js';
import * as quality from './views/quality.js';
import * as tiers from './views/tiers.js';
import * as isolation from './views/isolation.js';
import * as retrieval from './views/retrieval.js';

// 尚未接入的视图占位——route.view 落在这几个 key 上时用它兜底，
// 不是错误状态，只是"这个视图还没做"。
const PLACEHOLDER = {
  id: 'placeholder',
  title: '诊断界面',
  async render() {
    return el('p', { class: 'dim' }, '视图正在接入中……');
  },
};

const VIEWS = [docs, layout, blocks, quality, tiers, isolation, retrieval];
const BY_ID = new Map(VIEWS.map((v) => [v.id, v]));

const nav = document.getElementById('nav');
for (const v of VIEWS) nav.append(el('a', { 'data-view': v.id }, v.title));

const mount = document.getElementById('view');
let inflight = null;

async function render() {
  inflight?.abort();
  inflight = new AbortController();
  const route = readRoute();
  const view = BY_ID.get(route.view) ?? PLACEHOLDER;

  syncAsOfBar(route, inflight.signal);
  for (const a of nav.querySelectorAll('a')) {
    a.classList.toggle('active', a.dataset.view === view.id);
    // 每次渲染都把当前 as_of 写回每个导航项的 href——否则切视图会静默
    // 丢掉已经选好的时点，用户不得不重新选一遍。
    a.href = hashFor(a.dataset.view, {});
  }
  document.title = `${view.title} · ${route.asOf || '未选时点'} · 内部诊断工具（非产品界面）`;

  // as_of 未选择时不发请求：后端对未设时点的读取是报错而不是空集，
  // 前端替它兜一个 now() 正是 docs/03-point-in-time.md 警告的那种
  // 静默错误。
  if (!route.asOf) {
    mount.replaceChildren(asOfBlockedCard());
    return;
  }

  mount.replaceChildren(loading());
  try {
    const node = await view.render({ ...route, signal: inflight.signal });
    mount.replaceChildren(node);
  } catch (e) {
    if (e.name === 'AbortError') return;
    mount.replaceChildren(errorCard(e, render));
  }
}

bindAsOfBar(render);
addEventListener('hashchange', render);
