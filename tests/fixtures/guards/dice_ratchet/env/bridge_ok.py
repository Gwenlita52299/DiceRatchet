"""沙箱层（MOD-02）合法 import 环境层：SandboxEnv 是 INV-1 的唯一桥梁。"""

from dice_ratchet.env.engine import GameEngine  # 合法：sandbox 是环境层的代理


def wrap():
    return GameEngine
