# DiceRatchet 模块可视化讲解（docs/html）

> 这不是 PRD 的镜像，而是**按当前代码重画的理解路径**：讲结构、画流程、指出现场状态与已知边界。
> 规范文本仍以 [模块拆分 Spec](../模块拆分Spec-DiceRatchet.md) 与 [PRD](../prd/) 为准。

## 页面

| 页面 | 内容 |
|---|---|
| [index.html](index.html) | 总览：闭环、分层、模块地图、2026-10-17 的协议纠正 |
| [game.html](game.html) | GAME 游戏本体：三层结构、回合顺序、配表、与训练链路的关系 |
| [mod-01.html](mod-01.html) | MOD-01 env-rules：双内核对拍、reduce、确定性三件套 |
| [mod-02.html](mod-02.html) | MOD-02 env-sandbox：投影 / 隔离 / 审计三道防线、INV-5 拦截点 |
| [mod-03.html](mod-03.html) | MOD-03 inference：多后端、三角色绑定、三级 JSON 修复、预算 |
| [mod-04.html](mod-04.html) | MOD-04 policy：prompt 四来源、三级校验、retry/fallback |
| [mod-05.html](mod-05.html) | MOD-05 memory：写入/检索管线、四个 INV-5 拦截点、逻辑时钟 |
| [mod-06.html](mod-06.html) | MOD-06 loop：双层循环、起跑三道闸、`loop baseline`、产物 |
| [mod-07.html](mod-07.html) | MOD-07 evolve：候选流水线、C0–C5、play 面指纹、版本语义 |
| [mod-08.html](mod-08.html) | MOD-08 evals：seed 协议、测量落盘、判据 1/2、坍缩检测 |
| [mod-09.html](mod-09.html) | MOD-09 infra：配置五层与 config_hash、run 目录、退出码、守卫 |

## 设计约束

- **纯静态、零依赖**：共享 `assets/style.css` + `assets/app.js`，图形全部是内联 SVG，不引 CDN、不需要构建。
- **可离线打开**：直接 `open docs/html/index.html` 即可；所有链接都是相对路径。
- **不是事实来源**：页面里的数字与行为都以代码/PRD 为准；改动判据、配置或产物路径时，请同步改页面。
- **来源标注**：每页页脚记录源 commit 与生成日期；本批内容对应 `879ea49`（2026-10-17）。

## 维护方式

1. 先改 Spec / PRD / 代码与用例；
2. 再按同一份 ground truth 改对应页面（重点是「当前状态」「配置」「错误码」「产物路径」四类）；
3. 用下面的检查确认没有坏链接、没有未定义的 SVG marker：

```bash
python3 - <<'PY'
from pathlib import Path
import re
for p in sorted(Path('docs/html').glob('*.html')):
    s = p.read_text()
    defined = set(re.findall(r'<marker id="([^"]+)"', s))
    used = set(re.findall(r'url\(#([^)]+)\)', s))
    assert not (used - defined), (p, used - defined)
print('ok')
PY
```

> 历史教训：2026-10-14 曾删除过一套手工维护的 `docs/html` PRD 镜像（F72），原因是「无生成器、无 CI、长期落后」。
> 本套页面的定位不同——它是**讲解**而非规范文本，并且每页都锚定到具体代码路径与 PRD；如果将来要长期维护，建议把「当前状态」段落做成可复算的构建步骤。
