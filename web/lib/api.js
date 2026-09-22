// fetch 封装：每个请求都带 as_of，统一把非 2xx / 网络失败翻成 ApiError，
// 按完整 URL 缓存——as_of 是查询参数，天然按时点分区，不需要额外的
// 失效逻辑。

const cache = new Map();
const MAX_ENTRIES = 200;

export class ApiError extends Error {
  constructor({ httpStatus, code, sqlstate, message, url }) {
    super(message);
    this.name = 'ApiError';
    this.httpStatus = httpStatus;
    this.code = code;
    this.sqlstate = sqlstate ?? null;
    this.url = url;
  }
}

/**
 * @param {string} path 形如 "/api/documents"
 * @param {Record<string, string|number|undefined|null>} params
 * @param {{asOf: string, signal?: AbortSignal}} opts
 */
export async function getJSON(path, params, { asOf, signal }) {
  if (!asOf) {
    throw new ApiError({
      httpStatus: 0,
      code: 'AS_OF_NOT_SET',
      url: path,
      message:
        'as_of 未选择。后端无默认值：未设时点的读取会报错（ERRCODE invalid_parameter_value），' +
        '不会返回空集。前端不替它兜一个 now()。',
    });
  }

  const url = new URL(path, document.baseURI);
  url.searchParams.set('as_of', asOf);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
  }
  const key = url.toString();
  if (cache.has(key)) return cache.get(key);

  let res;
  try {
    res = await fetch(key, { signal, headers: { accept: 'application/json' } });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError({ httpStatus: 0, code: 'NETWORK', message: String(e), url: key });
  }

  const raw = await res.text();
  let json = null;
  try {
    json = raw ? JSON.parse(raw) : null;
  } catch {
    // 非 JSON 响应：下面按原文展示，不假装解析成功。
  }

  if (!res.ok) {
    const detail = json?.error ?? json ?? {};
    throw new ApiError({
      httpStatus: res.status,
      code: detail.code ?? `HTTP_${res.status}`,
      sqlstate: detail.sqlstate ?? null,
      message: detail.message ?? (typeof detail === 'string' ? detail : raw.slice(0, 2000)),
      url: key,
    });
  }

  if (cache.size >= MAX_ENTRIES) cache.delete(cache.keys().next().value);
  cache.set(key, json);
  return json;
}
