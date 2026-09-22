// 三个函数替代模板引擎：无构建链前提下手搓 DOM 的最小集合。

/** el('div', {class:'card', 'data-x':1}, child1, 'text', child2) */
export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, String(v));
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** text(s) — 显式包一层，标注"这是纯文本节点"，调用点不必记 document.createTextNode */
export function text(s) {
  return document.createTextNode(String(s ?? ''));
}

/** clear(node) — 移除全部子节点，比 innerHTML = '' 更明确不触发解析 */
export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}
