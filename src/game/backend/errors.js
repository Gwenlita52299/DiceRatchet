// backend/errors.js — 错误码集中登记（GAME-00 §9.1 / §9.5）

export const ERRORS = Object.freeze({
  E_API_NOT_FOUND: { status: 404, message: '接口不存在' },
  E_API_BAD_JSON: { status: 400, message: '请求体不是合法 JSON' },
  E_API_METHOD: { status: 405, message: '方法不允许' },
  E_API_BAD_REQUEST: { status: 400, message: '请求参数错误' },
  E_API_TOO_LARGE: { status: 413, message: '请求体过大' },
  E_STATE_NO_SESSION: { status: 404, message: '会话不存在或已过期' },
  E_STATE_SUSPENDED: { status: 409, message: '会话因连续非法动作被挂起' },
  E_STATE_FINISHED: { status: 409, message: '对局已结束' },
  E_ACTION_SYNTAX: { status: 400, message: '动作语法错误' },
  E_ACTION_ILLEGAL: { status: 200, message: '动作不合法' },
  E_KERNEL_EXCEPTION: { status: 500, message: '内核异常' },
  E_KERNEL_BAD_CONFIG: { status: 400, message: '配置无效' }
});

export class ApiError extends Error {
  constructor(code, message, extra = {}) {
    super(message || (ERRORS[code] && ERRORS[code].message) || code);
    this.code = code;
    this.status = (ERRORS[code] && ERRORS[code].status) || (String(code).startsWith('E_KERNEL') ? 500 : 400);
    this.extra = extra;
  }
}
