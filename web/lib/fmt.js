// 文本/HTML 处理的小工具，纯函数。

import { el } from './dom.js';

/**
 * 前块后缀 == 后块前缀的最长长度（封顶 cap）。量出的实际值才是
 * "切块参数有没有真的生效"的证据，不写死 chunker.py 的 overlap_chars。
 */
export function overlapLen(prev, next, cap = 256) {
  const max = Math.min(cap, prev.length, next.length);
  for (let n = max; n > 0; n--) {
    if (prev.endsWith(next.slice(0, n))) return n;
  }
  return 0;
}

const ALLOWED_TAGS = new Set(['TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TH', 'TD', 'CAPTION']);
const ALLOWED_ATTRS = new Set(['colspan', 'rowspan']);

/**
 * `table_html` 是供应商返回的内容，与这个只读 API 同源——绝不能
 * `innerHTML` 直接塞进页面。用 DOMParser 解析后按白名单标签/属性重建，
 * 文本节点原样保留，其余一律丢弃。返回 null 表示解析不出一个 <table>。
 */
export function sanitizeTable(html) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const table = doc.body.querySelector('table');
  return table ? rebuild(table) : null;
}

/**
 * `table_html` → 包了 `.table-scroll` 的可读表格，`sanitizeTable` 解析不出
 * `<table>` 时返回 `null`（调用方据此跳过，不渲染空壳）。抽出来是因为
 * blocks.js 与 layout.js 的详情面板要渲染同一件事。
 */
export function tableBlock(html) {
  const table = sanitizeTable(html);
  return table ? el('div', { class: 'table-scroll' }, table) : null;
}

function rebuild(node) {
  const out = document.createElement(node.tagName.toLowerCase());
  for (const attr of node.attributes) {
    if (ALLOWED_ATTRS.has(attr.name.toLowerCase())) out.setAttribute(attr.name, attr.value);
  }
  for (const child of node.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) {
      out.append(document.createTextNode(child.data));
    } else if (child.nodeType === Node.ELEMENT_NODE && ALLOWED_TAGS.has(child.tagName)) {
      out.append(rebuild(child));
    }
  }
  return out;
}
