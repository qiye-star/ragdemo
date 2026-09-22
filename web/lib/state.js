// hash ⇄ {view, asOf, params}。as_of 只从 URL 读写，刻意不用
// localStorage——一个记住了但看不见的时点，正是 docs/03-point-in-time.md
// 警告的那种"静默得出错误结论"的来源。链接可分享、可复现、后退键有效。

import { getJSON } from './api.js';
import { el } from './dom.js';

export function readRoute() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs] = raw.split('?');
  const params = new URLSearchParams(qs ?? '');
  return { view: path || 'docs', asOf: params.get('as_of') ?? '', params };
}

/** 保留当前 view 与其余查询参数，只替换/写入 as_of，触发 hashchange。 */
export function setAsOf(iso) {
  const r = readRoute();
  r.params.set('as_of', iso);
  location.hash = `#/${r.view}?${r.params}`;
}

/**
 * 全仓库**唯一**允许拼 hash 的地方。as_of 始终带上；没显式给的参数一律丢弃。
 *
 * 不做隐式继承是刻意的：当前 URL 里的 `doc=3` 继承进 `#/tiers` 只会得到一个
 * 无意义的参数。要带什么，调用点自己写明。
 */
export function hashFor(view, params) {
  const p = new URLSearchParams();
  const { asOf } = readRoute();
  if (asOf) p.set('as_of', asOf);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v != null && v !== '') p.set(k, String(v));
  }
  const qs = p.toString();
  return qs ? `#/${view}?${qs}` : `#/${view}`;
}

/** 切视图时把 as_of（与显式给出的参数）带过去，而不是丢光重新开始。 */
export function navigateTo(view, extraParams) {
  location.hash = hashFor(view, extraParams);
}

/**
 * 视图内的跳转链接。href 与点击行为由同一处产出——**只挂 click handler 是
 * 修不好这件事的**：中键、Ctrl/Cmd+点击、"复制链接地址"、状态栏预览走的都是
 * href 本身，handler 根本不参与。历史上 docs.js 就是只挂了 handler，
 * 于是新标签页打开的链接全都丢了时点、落进阻断卡。
 */
export function viewLink(view, params, ...children) {
  const a = el('a', { href: hashFor(view, params) }, ...children);
  a.addEventListener('click', (e) => {
    // 带修饰键或非左键时交还给浏览器去开新标签页——href 已经带了 as_of，
    // 不需要也不应该拦截。
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigateTo(view, params);
  });
  return a;
}

function toUtcIso(localValue) {
  // <input type="datetime-local"> 给的是 naive 本地时间字符串（无时区），
  // ragdemo_core.db.session.as_of_session 对 naive datetime 直接拒绝——
  // 这里补一个显式的 Z，声明"就当它是 UTC"，而不是让用户猜时区怎么填。
  const withSeconds = localValue.length === 16 ? `${localValue}:00` : localValue;
  return `${withSeconds}Z`;
}

/** 绑定 as_of 条上的输入框/按钮；onApply 在用户点了"应用时点"之后调用。 */
export function bindAsOfBar(onApply) {
  const input = document.getElementById('asof-input');
  const apply = () => {
    if (!input.value) return;
    setAsOf(toUtcIso(input.value));
  };
  document.getElementById('asof-apply').addEventListener('click', apply);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') apply();
  });
  for (const btn of document.querySelectorAll('[data-preset]')) {
    btn.addEventListener('click', () => {
      const iso = btn.dataset.preset === 'now' ? new Date().toISOString() : btn.dataset.presetValue;
      setAsOf(iso);
    });
  }
  onApply();
}

// as_of 选择器最大的痛点：不选一个时点就看不到任何东西，但不看到东西
// 又不知道该选哪个时点。这里用一个单调递增的 token 而不是复用
// AbortController——即使两次调用共享同一个 render() 的 signal（未被
// abort），只要第二次调用已经发出，第一次的响应回来时也要认输，避免
// 网络时序颠倒导致提示区显示了一个更早、已经过时的 as_of 的范围。
let hintToken = 0;

function clearAsOfHint() {
  hintToken += 1;
  const hint = document.getElementById('asof-hint');
  const earliestBtn = document.getElementById('asof-jump-earliest');
  const latestBtn = document.getElementById('asof-jump-latest');
  if (hint) hint.textContent = '';
  if (earliestBtn) earliestBtn.hidden = true;
  if (latestBtn) latestBtn.hidden = true;
}

async function updateAsOfHint(asOf, signal) {
  const token = (hintToken += 1);
  const hint = document.getElementById('asof-hint');
  const earliestBtn = document.getElementById('asof-jump-earliest');
  const latestBtn = document.getElementById('asof-jump-latest');
  if (hint) hint.textContent = '查询已知数据范围…';

  let meta;
  try {
    meta = await getJSON('/api/meta', {}, { asOf, signal });
  } catch (e) {
    if (e.name === 'AbortError' || token !== hintToken) return;
    if (hint) hint.textContent = ''; // 主视图自己的错误卡已经报过这次失败，提示区不重复报
    if (earliestBtn) earliestBtn.hidden = true;
    if (latestBtn) latestBtn.hidden = true;
    return;
  }
  if (token !== hintToken) return;

  const { earliest, latest } = meta.known_at_range;
  if (earliest && latest) {
    // 提示区只做到分钟精度——按钮 dataset 里存的仍是完整 ISO 值，
    // 跳转不损失精度，缩短只是为了不把 as_of 条撑成两行。
    if (hint) {
      hint.textContent =
        `已知公开文档 known_at：${earliest.slice(0, 16)} ~ ${latest.slice(0, 16)}` +
        `（可见 ${meta.visible.documents} 篇）`;
    }
    if (earliestBtn) {
      earliestBtn.hidden = false;
      earliestBtn.dataset.presetValue = earliest;
    }
    if (latestBtn) {
      latestBtn.hidden = false;
      latestBtn.dataset.presetValue = latest;
    }
  } else {
    if (hint) hint.textContent = '当前时点下没有任何公开文档可见——试试点击"现在"把时点往后调';
    if (earliestBtn) earliestBtn.hidden = true;
    if (latestBtn) latestBtn.hidden = true;
  }
}

/** 每次路由变化后，把地址栏里的 as_of 同步回时点条的三处显示。 */
export function syncAsOfBar(route, signal) {
  const echo = document.getElementById('asof-echo');
  echo.textContent = route.asOf || '未选择';

  // 直接带 as_of 打开一个深链（书签、刷新页面、从别的视图跳转过来）时，
  // 输入框也要跟着回填，不能只更新回显文字——否则用户会看到"已应用"
  // 但输入框却是空的，误以为状态没生效。往返关系与 bindAsOfBar 里
  // toUtcIso 的编码方式对称：这里只是把 Z/时区偏移剥掉，取前 19 个字符。
  const input = document.getElementById('asof-input');
  if (route.asOf && document.activeElement !== input) {
    const local = route.asOf.replace(/([+-]\d{2}:\d{2}|Z)$/, '').slice(0, 19);
    input.value = local;
  }

  if (route.asOf) {
    updateAsOfHint(route.asOf, signal);
  } else {
    clearAsOfHint();
  }
}
