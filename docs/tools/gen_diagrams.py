#!/usr/bin/env python3
"""生成 docs/assets/module-deps.svg 与 docs/assets/schedule-gantt.svg，并打印排期表（Markdown）。

零依赖：纯标准库手工布局。数据来源：
- 依赖关系：Spec §6 依赖矩阵
- 工作量：各 MOD PRD §15（人日）
- 排期假设：见 docs/项目排期-DiceRatchet.md §1

用法：python3 docs/tools/gen_diagrams.py            # 写 SVG 并打印排期表
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
FONT = "PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Microsoft YaHei, sans-serif"

LAYER_COLOR = {  # 填充, 描边
    "GAME": ("#e8f5e9", "#43a047"),
    "L0": ("#e3f2fd", "#1e88e5"),
    "L1": ("#f3e5f5", "#8e24aa"),
    "L2": ("#fff3e0", "#fb8c00"),
    "L3": ("#eceff1", "#546e7a"),
    "GOAL": ("#fffde7", "#f9a825"),
    "OPT": ("#fafafa", "#9e9e9e"),
}

# ---------------------------------------------------------------- 模块依赖图
W, H = 150, 66
COLX = [40, 235, 430, 625, 820, 1015, 1210]
NODES = {
    # id: (col, y_center, 标题, 副标题, 层, 人日, 里程碑)
    "GAME":   (0, 205, "GAME 网页游戏", "内核 + 配表 + golden", "GAME", "已完成 ✓", "G0"),
    "MOD-01": (1, 150, "MOD-01 env-rules", "JS 内核的 Python 移植", "L0", "7", "M0"),
    "MOD-03": (1, 330, "MOD-03 inference", "本地推理 MLX", "L1", "14", "M1"),
    "MOD-02": (2, 150, "MOD-02 env-sandbox", "投影/隔离/审计", "L0", "18", "M0"),
    "MOD-05": (2, 440, "MOD-05 memory", "反思记忆库", "L1", "12", "M2"),
    "MOD-08": (3, 50, "MOD-08 evals", "基线/指标/消融", "L3", "18", "M1–M3"),
    "MOD-04": (3, 290, "MOD-04 policy", "决策计划/校验/fallback", "L1", "13.5", "M1"),
    "MOD-06": (4, 290, "MOD-06 loop", "闭环编排/续跑", "L2", "14", "M2"),
    "MOD-07": (5, 170, "MOD-07 evolve", "harness 自进化", "L2", "10", "M2–M3"),
    "M3":     (6, 170, "M3 验收", "held-out/消融/test", "GOAL", "", ""),
    "MOD-10": (6, 330, "MOD-10 distill", "STaR + LoRA（预留）", "OPT", "—", "M4 可选"),
}
EDGES = [  # (上游, 下游, 样式[, 源锚点, 目标锚点]) 样式: crit=关键路径, norm, dash；锚点 r/l/t/b
    ("GAME", "MOD-01", "crit"),
    ("MOD-01", "MOD-02", "crit"),
    ("MOD-02", "MOD-04", "crit"),
    ("MOD-03", "MOD-04", "crit"),
    ("MOD-05", "MOD-04", "norm"),
    ("MOD-04", "MOD-06", "crit"),
    ("MOD-02", "MOD-06", "norm", "r", "t"),
    ("MOD-05", "MOD-06", "norm", "r", "b"),
    ("MOD-01", "MOD-08", "norm", "t", "l"),
    ("MOD-02", "MOD-08", "norm"),
    ("MOD-06", "MOD-07", "crit"),
    ("MOD-08", "MOD-07", "norm"),
    ("MOD-07", "M3", "crit"),
    ("MOD-08", "M3", "norm"),
    ("MOD-07", "MOD-10", "dash"),
]


def box(nid: str) -> tuple[float, float, float, float]:
    col, yc, *_ = NODES[nid]
    x = COLX[col]
    return x, yc - H / 2, x + W, yc + H / 2


def anchor(nid: str, side: str, pad: float = 0) -> tuple[float, float, float, float]:
    """返回锚点坐标与出射方向单位向量。"""
    x0, y0, x1, y1 = box(nid)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return {"r": (x1 + pad, cy, 1, 0), "l": (x0 - pad, cy, -1, 0),
            "t": (cx, y0 - pad, 0, -1), "b": (cx, y1 + pad, 0, 1)}[side]


def edge_path(a: str, b: str, sa: str = "r", ta: str = "l") -> str:
    sx, sy, sdx, sdy = anchor(a, sa)
    tx, ty, tdx, tdy = anchor(b, ta, pad=3)
    k = max(50.0, abs(tx - sx) * 0.5, abs(ty - sy) * 0.5)
    c1 = (sx + sdx * k, sy + sdy * k)
    c2 = (tx + tdx * k, ty + tdy * k)
    return f"M{sx:.0f},{sy:.0f} C{c1[0]:.0f},{c1[1]:.0f} {c2[0]:.0f},{c2[1]:.0f} {tx:.0f},{ty:.0f}"


def module_deps_svg() -> str:
    width, height = 1400, 720
    o: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="{FONT}">',
        "<defs>",
        '<marker id="a-crit" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#e53935"/></marker>',
        '<marker id="a-norm" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#78909c"/></marker>',
        '<marker id="a-dash" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="11" markerHeight="11" markerUnits="userSpaceOnUse" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#bdbdbd"/></marker>',
        '<filter id="sh" x="-5%" y="-5%" width="110%" height="120%"><feDropShadow dx="0" dy="1.5" stdDeviation="1.5" flood-opacity="0.18"/></filter>',
        "</defs>",
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        '<g transform="translate(0,90)">',
    ]
    # 标题
    o.append(f'<text x="40" y="-52" font-size="22" font-weight="700" fill="#263238">DiceRatchet 模块依赖与关键路径</text>')
    o.append(f'<text x="40" y="-28" font-size="13" fill="#607d8b">箭头：上游 → 下游（下游依赖上游）。数据源：Spec v1.4 §6 依赖矩阵、各 PRD §15 工作量（人日）</text>')
    # 边（先画，节点盖在上面）
    for a, b, st, *anc in EDGES:
        color, sw, dash, mk = {
            "crit": ("#e53935", 3, "", "a-crit"),
            "norm": ("#78909c", 1.6, "", "a-norm"),
            "dash": ("#bdbdbd", 1.6, ' stroke-dasharray="6 5"', "a-dash"),
        }[st]
        o.append(f'<path d="EDGE_D" fill="none" stroke="{color}" stroke-width="{sw}"{dash} marker-end="url(#{mk})"/>'.replace("EDGE_D", edge_path(a, b, *anc)))
    # 节点
    for nid, (col, yc, title, sub, layer, pd, ms) in NODES.items():
        x0, y0, x1, y1 = box(nid)
        fill, stroke = LAYER_COLOR[layer]
        dash = ' stroke-dasharray="6 4"' if layer == "OPT" else ""
        o.append(f'<rect x="{x0}" y="{y0}" width="{W}" height="{H}" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="2"{dash} filter="url(#sh)"/>')
        o.append(f'<text x="{x0 + 10}" y="{y0 + 22}" font-size="14" font-weight="700" fill="#263238">{escape(title)}</text>')
        o.append(f'<text x="{x0 + 10}" y="{y0 + 40}" font-size="11.5" fill="#455a64">{escape(sub)}</text>')
        meta = " · ".join(t for t in (f"{pd} 人日" if pd and pd[0].isdigit() else pd, ms) if t)
        if meta:
            o.append(f'<text x="{x0 + 10}" y="{y0 + 57}" font-size="11" fill="{stroke}" font-weight="600">{escape(meta)}</text>')
    # MOD-09 横切带
    by = 520
    fill, stroke = LAYER_COLOR["L3"]
    o.append(f'<rect x="{COLX[1]}" y="{by}" width="{COLX[5] + W - COLX[1]}" height="52" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="2" stroke-dasharray="8 4"/>')
    o.append(f'<text x="{COLX[1] + 14}" y="{by + 22}" font-size="14" font-weight="700" fill="#263238">MOD-09 infra（横切，第一天启动 · 7 人日 · M0–M3）</text>')
    o.append(f'<text x="{COLX[1] + 14}" y="{by + 41}" font-size="11.5" fill="#455a64">配置/run_id · events.jsonl 事件注册 · 错误码/退出码 · CLI · import 门禁与确定性守卫 · git tag harness-v{{N}} —— 所有 MOD 均依赖</text>')
    for col in range(1, 6):
        cx = COLX[col] + W / 2
        o.append(f'<path d="M{cx},{by} L{cx},{by - 22}" stroke="#90a4ae" stroke-width="1.4" stroke-dasharray="3 3" marker-end="url(#a-norm)"/>')
    # 图例
    ly = 600
    items = [("GAME", "游戏本体（已完成）"), ("L0", "L0 环境"), ("L1", "L1 智能体"), ("L2", "L2 进化"), ("L3", "L3 测量与工程"), ("OPT", "预留/可选")]
    x = 40
    for layer, label in items:
        f_, s_ = LAYER_COLOR[layer]
        o.append(f'<rect x="{x}" y="{ly - 12}" width="18" height="14" rx="3" fill="{f_}" stroke="{s_}" stroke-width="1.6"/>')
        o.append(f'<text x="{x + 24}" y="{ly}" font-size="12" fill="#37474f">{label}</text>')
        x += 24 + 13 * len(label) + 22
    o.append(f'<path d="M{x},{ly - 5} L{x + 36},{ly - 5}" stroke="#e53935" stroke-width="3" marker-end="url(#a-crit)"/>')
    o.append(f'<text x="{x + 44}" y="{ly}" font-size="12" fill="#37474f">关键路径 GAME→01→02→(03)→04→06→07→M3</text>')
    o.append("</g></svg>")
    return "\n".join(s for s in o if s)


# ---------------------------------------------------------------- 排期
START = dt.date(2026, 10, 8)  # 国庆后首个工作日
HOLIDAYS = {dt.date(2027, 1, 1)} | {dt.date(2027, 2, 5) + dt.timedelta(d) for d in range(8)}  # 元旦、春节（假设）
BUFFER = 0.10  # 每个里程碑 +10% 缓冲，单列为「缓冲/验收」任务

# (里程碑, 任务, 模块, 人日, 轨道) 轨道: dev=编码（串行，单人） compute=模型实跑（后台，与 dev 并行）
TASKS = [
    ("M0", "骨架：config/run_id/JsonlWriter/错误码", "MOD-09", 2.0, "dev"),
    ("M0", "守卫：AST 门禁 + 确定性引导", "MOD-09", 1.5, "dev"),
    ("M0", "JS 内核 Python 移植 + golden 对拍", "MOD-01", 7.0, "dev"),
    ("M0", "投影 + SandboxEnv 门面", "MOD-02", 5.5, "dev"),
    ("M0", "guard/ToolWorker + 审计 + 蜜罐", "MOD-02", 10.5, "dev"),
    ("M0", "集成 + CI 门禁接线", "MOD-02", 2.0, "dev"),
    ("M0", "M0 验收：5 组 golden + 200 局差分 + heuristic ≤3pp", "—", None, "dev"),
    ("M1", "推理后端 MLX + Mock + GBNF/修复", "MOD-03", 14.0, "dev"),
    ("M1", "决策计划/三层校验/fallback/heuristic", "MOD-04", 13.5, "dev"),
    ("M1", "三基线 + Wilson CI + eval report 骨架", "MOD-08", 5.0, "dev"),
    ("M1", "CLI 子命令树 + 轨迹 schema 校验", "MOD-09", 1.5, "dev"),
    ("M1", "三基线 heldout 实跑（已归档 n=10 旧四位协议；H1 40 局的 fixed_prompt 仍未跑）", "—", 0.5, "compute"),
    ("M1", "M1 验收：基线入档 + JSON 鲁棒性报告", "—", None, "dev"),
    ("M2", "记忆库（逻辑时钟/检索/INV-5 拦截）", "MOD-05", 12.0, "dev"),
    ("M2", "闭环编排 + 续跑 + 成本闸门", "MOD-06", 14.0, "dev"),
    ("M2", "proposer/versioning/gate/judge", "MOD-07", 7.0, "dev"),
    ("M2", "评估执行器 + report schema + generations.csv", "MOD-08", 6.0, "dev"),
    ("M2", "git_ops + redaction + 原子写", "MOD-09", 1.0, "dev"),
    ("M2", "闭环冒烟：1 代实跑（C1/DeepSeek medium×6：公式 114 局 / D-M3-7 起实际 74 局 ≈¥62–65）", "—", 1.0, "compute"),
    ("M2", "M2 验收：≥1 轮演化 + diff + 决策记录", "—", None, "dev"),
    ("M3", "evaluate_candidate 编排 + 安全用例", "MOD-07", 3.0, "dev"),
    ("M3", "多样性/坍缩 + 消融 + test 终检", "MOD-08", 7.0, "dev"),
    ("M3", "绘图 + 报告汇总脚本", "MOD-09", 1.0, "dev"),
    ("M3", "正式实验：主体已跑完（C1 382 局 / 19h50m / 实扣 ¥197.67 / 0 accept ⇒ 判据 1 ok=false）；消融 + test 未跑", "—", 3.0, "compute"),
    ("M3", "结果分析 + 实验报告/作品集", "—", 4.0, "dev"),
    ("M3", "M3 验收：计划 §6 三项", "—", None, "dev"),
]


def is_workday(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def workdays(n: int) -> list[dt.date]:
    out, d = [], START
    while len(out) < n:
        if is_workday(d):
            out.append(d)
        d += dt.timedelta(1)
    return out


def schedule():
    """单人串行排期，半天粒度：游标 c 以工作日为单位，任务占用 [c, c+pd)。

    实跑（compute）同样串行：M1/M2 验收与 M3 分析都依赖实跑结果；实跑期间人可做文档，但不计为进度。
    """
    ms_dev: dict[str, float] = {}
    for ms, _, _, pd, track in TASKS:
        if track == "dev" and pd:
            ms_dev[ms] = ms_dev.get(ms, 0) + pd
    days = workdays(400)
    rows, c = [], 0.0
    for ms, name, mod, pd, track in TASKS:
        if pd is None:  # 验收 = 该里程碑编码量 × BUFFER，取半天整
            pd = max(1.0, round(ms_dev[ms] * BUFFER * 2) / 2)
            track = "gate"
        s, e = days[int(c)], days[int(-(-(c + pd) // 1)) - 1]
        rows.append((ms, name, mod, pd, track, s, e))
        c += pd
    return rows


def gantt_svg(rows) -> str:
    left, top, rowh, dayw = 420, 70, 24, 5.2
    first = rows[0][5]
    last = max(r[6] for r in rows)
    ndays = (last - first).days + 14
    width = int(left + ndays * dayw + 40)
    height = int(top + rowh * (len(rows) + 4) + 40)
    col = {"dev": "#42a5f5", "compute": "#ab47bc", "gate": "#ffb300"}
    o = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="{FONT}">',
        f'<rect width="{width}" height="{height}" fill="#fff"/>',
        f'<text x="20" y="30" font-size="20" font-weight="700" fill="#263238">DiceRatchet 项目排期（单人 · 起始 {START} · 生成自 docs/tools/gen_diagrams.py）</text>',
    ]

    def xof(d: dt.date) -> float:
        return left + (d - first).days * dayw

    # 月份网格
    d = dt.date(first.year, first.month, 1)
    while d <= last + dt.timedelta(14):
        x = xof(max(d, first))
        o.append(f'<line x1="{x:.1f}" y1="{top - 18}" x2="{x:.1f}" y2="{top + rowh * len(rows) + 6}" stroke="#eceff1"/>')
        o.append(f'<text x="{x + 3:.1f}" y="{top - 6}" font-size="11" fill="#78909c">{d.year}-{d.month:02d}</text>')
        d = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    # 假期
    for h in sorted(HOLIDAYS):
        if first <= h <= last:
            o.append(f'<rect x="{xof(h):.1f}" y="{top - 2}" width="{dayw:.1f}" height="{rowh * len(rows) + 6}" fill="#ffebee"/>')
    prev_ms = None
    for i, (ms, name, mod, pd, track, s, e) in enumerate(rows):
        y = top + i * rowh
        if ms != prev_ms:
            o.append(f'<line x1="10" y1="{y - 1}" x2="{width - 10}" y2="{y - 1}" stroke="#cfd8dc"/>')
            prev_ms = ms
        label = f"{ms} · {mod + ' · ' if mod != '—' else ''}{name}"
        o.append(f'<text x="14" y="{y + 16}" font-size="11.5" fill="#37474f">{escape(label[:40] + ("…" if len(label) > 40 else ""))}</text>')
        x0, x1 = xof(s), xof(e) + dayw
        o.append(f'<rect x="{x0:.1f}" y="{y + 5}" width="{max(x1 - x0, 3):.1f}" height="{rowh - 10}" rx="3" fill="{col[track]}"/>')
        o.append(f'<text x="{x1 + 4:.1f}" y="{y + 16}" font-size="10" fill="#607d8b">{s:%m-%d}→{e:%m-%d}</text>')
    # 里程碑菱形
    ys = top + rowh * len(rows) + 22
    for ms in ("M0", "M1", "M2", "M3"):
        e = max(r[6] for r in rows if r[0] == ms)
        x = xof(e) + dayw
        o.append(f'<path d="M{x:.1f},{ys - 8} l7,8 l-7,8 l-7,-8 z" fill="#e53935"/>')
        o.append(f'<text x="{x + 10:.1f}" y="{ys + 4}" font-size="11" fill="#c62828" font-weight="700">{ms} {e:%m-%d}</text>')
    # 图例
    ly = ys + 34
    for i, (k, t) in enumerate((("dev", "编码/联调"), ("compute", "模型实跑（结果阻塞后续）"), ("gate", "缓冲 + 里程碑验收"))):
        x = 20 + i * 200
        o.append(f'<rect x="{x}" y="{ly - 11}" width="18" height="12" rx="2" fill="{col[k]}"/><text x="{x + 24}" y="{ly}" font-size="12" fill="#37474f">{t}</text>')
    o.append(f'<rect x="{20 + 3 * 200}" y="{ly - 11}" width="18" height="12" fill="#ffebee"/><text x="{20 + 3 * 200 + 24}" y="{ly}" font-size="12" fill="#37474f">节假日（元旦、春节）</text>')
    o.append("</svg>")
    return "\n".join(o)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "module-deps.svg").write_text(module_deps_svg(), encoding="utf-8")
    rows = schedule()
    (ASSETS / "schedule-gantt.svg").write_text(gantt_svg(rows), encoding="utf-8")
    print("| 里程碑 | 任务 | 模块 | 人日 | 轨道 | 开始 | 结束 |")
    print("|---|---|---|---|---|---|---|")
    tname = {"dev": "编码", "compute": "实跑", "gate": "验收"}
    for ms, name, mod, pd, track, s, e in rows:
        print(f"| {ms} | {name} | {mod} | {pd:g} | {tname[track]} | {s} | {e} |")
    for ms in ("M0", "M1", "M2", "M3"):
        rs = [r for r in rows if r[0] == ms]
        dev = sum(r[3] for r in rs)
        print(f"# {ms}: {min(r[5] for r in rs)} → {max(r[6] for r in rs)}，{dev:g} 工作日")
    print("# 合计:", sum(r[3] for r in rows), "工作日")


if __name__ == "__main__":
    main()
