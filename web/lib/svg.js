// createElementNS 封装 + 面积/重叠计算。纯函数，不碰 DOM 之外的任何东西
// （除了 svgEl/svgText 本身创建节点）。

export const SVG_NS = 'http://www.w3.org/2000/svg';

export function svgEl(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v != null) node.setAttribute(k, String(v));
  }
  return node;
}

export function svgText(attrs, s) {
  const node = svgEl('text', attrs);
  node.textContent = s;
  return node;
}

/** bbox = [x0, y0, x1, y1]，归一化坐标，面积用于绘制顺序（大块先画）。 */
export function area([x0, y0, x1, y1]) {
  return Math.abs((x1 - x0) * (y1 - y0));
}

/**
 * 同页两两求交，返回按 IoU 降序排列的重叠对。n 通常 ≤ 数十，O(n²) 无所谓。
 * 只返回事实（哪两个块、IoU 多少），不判断"这是不是错"。
 */
export function overlaps(blocks) {
  const out = [];
  for (let i = 0; i < blocks.length; i++) {
    for (let j = i + 1; j < blocks.length; j++) {
      const a = blocks[i].bbox;
      const b = blocks[j].bbox;
      if (!a || !b) continue;
      const w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
      const h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
      if (w <= 0 || h <= 0) continue;
      const inter = w * h;
      const union = area(a) + area(b) - inter;
      out.push({ a: blocks[i], b: blocks[j], iou: union > 0 ? inter / union : 0 });
    }
  }
  return out.sort((p, q) => q.iou - p.iou);
}
