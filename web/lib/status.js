// 三态严格区分：未选时点=阻断卡（不发请求）/ 空集=HTTP 200 空集卡 /
// 错误=错误卡（回显 HTTP 码 + code + SQLSTATE + message 原文 + URL）。
// 绝不把「未设 as_of 的报错」显示成「暂无数据」——这正是
// docs/03-point-in-time.md 警告的静默错误。

import { el } from './dom.js';

export function loading() {
  return el('p', { class: 'loading' }, '加载中…');
}

export function asOfBlockedCard() {
  return el(
    'section',
    { class: 'card card-blocked' },
    el('h2', {}, '未选择 as_of'),
    el('p', {}, '这个界面的每一个请求都必须带 as_of。后端没有默认值：'),
    el(
      'pre',
      { class: 'mono' },
      "asof.current_as_of()：\n" +
        "  app.as_of 未设置 → RAISE EXCEPTION\n" +
        "  'app.as_of is not set; every read must declare a point in time'\n" +
        '  ERRCODE = invalid_parameter_value（SQLSTATE 22023）'
    ),
    el(
      'p',
      { class: 'dim' },
      '返回空集才是最危险的行为——它会让人误以为回测得出的是真实结论。' +
        '所以这里既不发请求，也不显示「无数据」，请先在上方选择时点：' +
        '点击「现在」快速开始，应用后时点条会显示当前已知数据的' +
        'known_at 范围，供你据此精确调整。'
    )
  );
}

export function emptyCard({ what, predicate }) {
  return el(
    'section',
    { class: 'card card-empty' },
    el('h2', {}, `该时点下 ${what} 0 行`),
    predicate ? el('pre', { class: 'mono' }, predicate) : null,
    el('p', { class: 'dim' }, '这是空集，不是错误：请求返回 HTTP 200。')
  );
}

export function errorCard(err, retry) {
  const card = el(
    'section',
    { class: 'card card-error' },
    el('h2', {}, '请求失败'),
    el(
      'dl',
      { class: 'kv mono' },
      el('dt', {}, 'HTTP'),
      el('dd', {}, String(err.httpStatus ?? '—')),
      el('dt', {}, 'code'),
      el('dd', {}, err.code ?? '—'),
      el('dt', {}, 'sqlstate'),
      el('dd', {}, err.sqlstate ?? '—'),
      el('dt', {}, 'url'),
      el('dd', {}, err.url ?? '—')
    ),
    // message 原样展示，不做任何改写、归类或安抚性措辞。
    el('pre', { class: 'mono err-msg' }, err.message ?? '')
  );
  if (retry) {
    const btn = el('button', { type: 'button' }, '重试');
    btn.addEventListener('click', retry);
    card.append(btn);
  }
  return card;
}
