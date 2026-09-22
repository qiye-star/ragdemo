// hash ⇄ {view, asOf, params}。as_of 只从 URL 读写，刻意不用
// localStorage——一个记住了但看不见的时点，正是 docs/03-point-in-time.md
// 警告的那种"静默得出错误结论"的来源。链接可分享、可复现、后退键有效。

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

/** 切视图时把 as_of（与其余共享参数）带过去，而不是丢光重新开始。 */
export function navigateTo(view, extraParams) {
  const r = readRoute();
  const params = new URLSearchParams();
  if (r.asOf) params.set('as_of', r.asOf);
  for (const [k, v] of Object.entries(extraParams ?? {})) {
    if (v != null && v !== '') params.set(k, String(v));
  }
  location.hash = `#/${view}?${params}`;
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

/** 每次路由变化后，把地址栏里的 as_of 同步回时点条的三处显示。 */
export function syncAsOfBar(route) {
  const echo = document.getElementById('asof-echo');
  echo.textContent = route.asOf || '未选择';
}
