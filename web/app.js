// 路由入口：读 hash → 同步 as_of 条 → 渲染当前视图。
//
// 视图注册表目前只有一个占位视图——先把外壳（横幅常驻、as_of 条常驻、
// 三态渲染）跑通再做真正的视图，是刻意的顺序：如果先做视图，as_of 的
// 强制性会被"临时先给个 now() 方便调试"腐蚀掉，而那个临时 hack 一定
// 会留下来。真正的视图在后续任务里逐个接入 BY_ID。

import { readRoute, bindAsOfBar, syncAsOfBar } from './lib/state.js';
import { asOfBlockedCard, errorCard, loading } from './lib/status.js';
import { el } from './lib/dom.js';
import * as docs from './views/docs.js';
import * as layout from './views/layout.js';
import * as blocks from './views/blocks.js';
import * as quality from './views/quality.js';
import * as tiers from './views/tiers.js';
import * as isolation from './views/isolation.js';

// 尚未接入的视图占位——route.view 落在这几个 key 上时用它兜底，
// 不是错误状态，只是"这个视图还没做"。
const PLACEHOLDER = {
  id: 'placeholder',
  title: '诊断界面',
  async render() {
    return el('p', { class: 'dim' }, '视图正在接入中……');
  },
};

const BY_ID = new Map([docs, layout, blocks, quality, tiers, isolation].map((v) => [v.id, v]));

const mount = document.getElementById('view');
let inflight = null;

async function render() {
  inflight?.abort();
  inflight = new AbortController();
  const route = readRoute();
  const view = BY_ID.get(route.view) ?? PLACEHOLDER;

  syncAsOfBar(route, inflight.signal);
  for (const a of document.querySelectorAll('#tabs a')) {
    a.classList.toggle('active', a.dataset.view === view.id);
    // 每次渲染都把当前 as_of 写回每个 tab 的 href——否则点标签页会静默
    // 丢掉已经选好的时点，用户不得不重新选一遍。
    a.href = route.asOf ? `#/${a.dataset.view}?as_of=${encodeURIComponent(route.asOf)}` : `#/${a.dataset.view}`;
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
