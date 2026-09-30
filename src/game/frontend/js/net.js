// net.js — 唯一的 fetch 封装
export async function api(method, path, body) {
  let res;
  try {
    res = await fetch(`/api${path}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined
    });
  } catch (e) {
    return { ok: false, error: { code: 'E_NET', message: '无法连接服务器' } };
  }
  try {
    return await res.json();
  } catch {
    return { ok: false, error: { code: 'E_NET', message: `HTTP ${res.status}` } };
  }
}
