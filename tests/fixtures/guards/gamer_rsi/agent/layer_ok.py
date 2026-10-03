"""守卫 fixture（T-09-9 / T-09-10，纳入版本控制，G2）。

- ``layer_ok.py``：agent 侧合法代码（经 SandboxEnv 触达环境）。
- ``layer_bad.py``：agent 侧直接 import ``gamer_rsi.env``（违反 INV-1）。
- ``det_ok.py``：确定性代码（用 derive_seed）。
- ``det_bad.py``：未豁免的 ``random.random()`` / ``time.time()`` / ``uuid4()``。
- ``det_exempt.py``：同上但带 ``# guard: allow ... reason=...`` 白名单注释。
- ``../infra/dep_bad.py``：infra 反向依赖其他子包（违反 NFR-09-5）。
"""

# layer_ok.py —— agent 侧合法：只经 SandboxEnv 触达环境（INV-1 允许）
