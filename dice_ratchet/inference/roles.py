"""模型角色与角色装配（Issue #1 §2 / 验收标准 A）。

**三个角色**（``policy`` 游戏决策 / ``reflection`` 局末反思 / ``proposer`` harness 修改
提案）都只依赖统一的 ``InferenceClient`` 契约（``client.py``），不接触具体后端实现、
API SDK、HTTP 客户端或 MLX/llama.cpp 细节。角色 → 具体模型的绑定**全部在配置层**：

优先级（高 → 低）::

    experiment.roles.<role>            # 手工覆盖（configs/default.yaml 的 experiment.roles）
    experiment.arms[experiment.arm]    # 实验臂差分（A0/A1/B0/B1/C0/C1）
    models.<role>                      # 角色绑定（configs/default.yaml 的 models 段）
    inference.model_id                 # 兜底：CLI --model 仍一次覆盖全部角色

**fail-closed**：未知角色、未知臂、空 model_id、注册表里不存在的模型、``provider=api``
但没登记 key 环境变量 —— 一律显式报错，绝不静默降级到别的模型（验收标准 A 第 5 条）。

**provenance**：``run_provenance()`` 让每个 run 都能还原三个角色各自用的
model_id / provider / backend / model_revision / online，以及臂与 config_hash。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Mapping

from ..infra.errors import E, InfraError
from .backend import BackendManager, InfraBridge, InfraFacade, NullInfra
from .client import BackendInferenceClient
from .errors import InferenceError
from .registry import ModelRegistry, ModelSpec, load_registry
from .secrets import env_file_path, lookup_api_key

__all__ = [
    "ModelRole", "ROLE_NAMES", "RoleBinding", "RoleModelProvider",
    "role_model_ids", "resolve_role_bindings", "run_provenance", "PROVENANCE_FILENAME",
]

ModelRole = Literal["policy", "reflection", "proposer"]

#: 三个角色的规范顺序（报告与 provenance 的稳定键序）。
ROLE_NAMES: Final[tuple[ModelRole, ...]] = ("policy", "reflection", "proposer")

#: run 目录下的角色 provenance 文件名（``runs/<run_id>/models_provenance.json``）。
PROVENANCE_FILENAME: Final[str] = "models_provenance.json"


def _clean(value: Any) -> str:
    """配置里的角色绑定值 → 去空白字符串（空串表示「未绑定，继续向下继承」）。"""
    return str(value or "").strip()


def _arm_spec(arms: Mapping[str, Any], arm: str) -> dict[str, Any]:
    """取该臂的角色差分；臂存在但不是对象 → 显式报配置错误（不静默当成空差分）。"""
    if not arm:
        return {}
    spec = arms.get(arm)
    if spec is None:
        return {}
    if not isinstance(spec, Mapping):
        raise InfraError(E.E_IO_CONFIG_INVALID,
                         f"experiment.arms.{arm} 必须是对象，实际是 {type(spec).__name__}")
    return dict(spec)


# =====================================================================
# 角色 → model_id 解析（纯配置，不需要注册表）
# =====================================================================

def _resolve_role(cfg: Any, role: str) -> tuple[str, str]:
    """``(model_id, 来源层)`` —— **唯一的优先级实现**（下面两个入口共用，避免漂移）。

    优先级（高 → 低）::

        experiment.roles.<role>  >  experiment.arms[arm].<role>  >  models.<role>
                                 >  inference.model_id

    空字符串 = 未绑定，继续向下继承。未知臂在这里 fail-closed（不静默当空差分）。
    """
    exp = getattr(cfg, "experiment", None)
    models = getattr(cfg, "models", None)
    default_id = _clean(getattr(getattr(cfg, "inference", None), "model_id", ""))
    arm = _clean(getattr(exp, "arm", ""))
    arms = dict(getattr(exp, "arms", {}) or {}) if exp is not None else {}
    if arm and arm not in arms:
        raise InfraError(
            E.E_IO_CONFIG_INVALID,
            f"experiment.arm={arm!r} 不在 experiment.arms 内",
            hint=f"合法臂：{', '.join(sorted(arms))}（空字符串 = 不选臂）")
    overrides = dict(getattr(exp, "roles", {}) or {}) if exp is not None else {}
    arm_roles = _arm_spec(arms, arm)
    layers: tuple[tuple[Any, str], ...] = (
        (overrides.get(role), "experiment.roles"),
        (arm_roles.get(role), f"experiment.arms.{arm}"),
        (getattr(models, role, None) if models is not None else None, "models"),
    )
    for candidate, source in layers:
        if _clean(candidate):
            return _clean(candidate), source
    return default_id, "inference.model_id"


def role_model_ids(cfg: Any) -> dict[str, str]:
    """三角色最终生效的 ``model_id``（只读配置，不碰注册表，不校验模型是否存在）。

    给 MOD-08 归档（``model_hash`` / ``model_backend``）与埋点用：它们只需要「配置说要
    用哪个 id」，真正的可用性由 MOD-03 在加载时 fail-closed。
    """
    return {role: _resolve_role(cfg, role)[0] for role in ROLE_NAMES}


def _binding_source(cfg: Any, role: str) -> str:
    """记录该角色绑定来自哪一层（provenance 要能说清楚「谁定的」）。"""
    return _resolve_role(cfg, role)[1]


@dataclass(frozen=True)
class RoleBinding:
    """一个角色最终绑定的模型（含 run provenance 需要的全部描述字段）。"""

    role: str
    model_id: str
    provider: str # local | api
    backend: str # mlx | llama_cpp | llama_server | mock | deepseek
    online: bool
    model_revision: str
    api_model: str
    api_key_env: str
    source: str
    context_length: int
    supports_grammar: bool
    supports_json_mode: bool
    #: key 是否可用（环境变量或本地密钥文件）；只记布尔，绝不落盘 key 本身
    api_key_present: bool = False
    #: key 来源：``environment`` / ``env_file`` / ``missing``（同样是「在哪」，不是 key）
    api_key_source: str = "missing"

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model_id": self.model_id,
            "provider": self.provider,
            "backend": self.backend,
            "online": bool(self.online),
            "model_revision": self.model_revision,
            "api_model": self.api_model,
            "api_key_env": self.api_key_env,
            "api_key_present": bool(self.api_key_present),
            "api_key_source": str(self.api_key_source),
            "source": self.source,
            "context_length": int(self.context_length),
            "supports_grammar": bool(self.supports_grammar),
            "supports_json_mode": bool(self.supports_json_mode),
        }


def _binding_for(role: str, model_id: str, spec: ModelSpec, source: str, *,
                 api_key_source: str) -> RoleBinding:
    return RoleBinding(
        role=role, model_id=model_id, provider=spec.provider, backend=spec.backend,
        online=bool(spec.online), model_revision=spec.model_revision,
        api_model=spec.api_model or model_id, api_key_env=spec.api_key_env,
        source=source, context_length=int(spec.context_length),
        supports_grammar=bool(spec.supports_grammar),
        supports_json_mode=bool(spec.supports_json_mode),
        api_key_present=(api_key_source != "missing"), api_key_source=api_key_source)


def resolve_role_bindings(cfg: Any, *, registry: ModelRegistry | None = None,
                          registry_path: str | Path | None = None
                          ) -> dict[str, RoleBinding]:
    """解析三角色的完整绑定；任何缺失/未知都 fail-closed（不静默换模型）。"""
    ids = role_model_ids(cfg)
    path = registry_path
    if path is None:
        path = str(getattr(getattr(cfg, "inference", None), "registry_path",
                           "configs/models.yaml"))
    reg = registry if registry is not None else load_registry(path)
    file_path = env_file_path(getattr(cfg, "inference", None))
    out: dict[str, RoleBinding] = {}
    for role in ROLE_NAMES:
        model_id = ids[role]
        if not model_id:
            raise InferenceError(
                E.E_LLM_UNAVAILABLE,
                f"角色 {role!r} 没有可用的 model_id（models.{role} 与 inference.model_id 都为空）",
                hint="在 configs/default.yaml 的 models 段显式绑定，或设置 inference.model_id",
                retryable=False)
        spec = reg.spec(model_id) # 未知 id → E_LLM_UNAVAILABLE（fail-closed）
        key_source = "missing"
        if spec.provider == "api":
            if not spec.api_key_env:
                raise InferenceError(
                    E.E_LLM_UNAVAILABLE,
                    f"模型 {model_id!r} 的 provider=api，但注册表没有登记 api_key_env",
                    hint="在 configs/models.yaml 该模型下补 api_key_env（只登记变量名）",
                    retryable=False)
            # 只查注册表声明的那个名字；真实环境变量优先，其次 inference.api_key_file。
            ref = lookup_api_key(spec.api_key_env, env_file=file_path)
            key_source = ref.source
        out[role] = _binding_for(role, model_id, spec, _binding_source(cfg, role),
                                 api_key_source=key_source)
    return out


# =====================================================================
# run provenance（验收标准 A 第 3 条）
# =====================================================================

def run_provenance(cfg: Any, bindings: Mapping[str, RoleBinding], *, run_id: str,
                   extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """还原「哪个角色用了哪个 provider/backend/模型/版本」的 provenance 段。

    API key 本体绝不进这里：只记变量名与「是否已设置」的布尔值。
    """
    exp = getattr(cfg, "experiment", None)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": str(run_id),
        "arm": _clean(getattr(exp, "arm", "")) or None,
        "harness_mode": (str(exp.harness_mode()) if hasattr(exp, "harness_mode") else None),
        "config_hash": _safe_config_hash(cfg),
        "registry_path": str(getattr(getattr(cfg, "inference", None), "registry_path", "")),
        "api_mode": str(getattr(getattr(cfg, "inference", None), "api_mode", "")),
        "roles": {role: bindings[role].to_json() for role in ROLE_NAMES if role in bindings},
    }
    if extra:
        payload.update({str(k): v for k, v in extra.items()})
    return payload


def _safe_config_hash(cfg: Any) -> str | None:
    """``config_hash``（换臂即换 run-id 族）；非 AppConfig 形状的对象返回 ``None``。"""
    try:
        from ..infra.config import AppConfig, config_hash

        if not isinstance(cfg, AppConfig):
            return None
        return config_hash(cfg)
    except Exception: # noqa: BLE001 - provenance 不是判定路径，缺 hash 不足以中断
        return None


# =====================================================================
# RoleModelProvider（验收标准 A 第 1 条）
# =====================================================================

class RoleModelProvider:
    """按角色返回统一 ``InferenceClient`` 的装配件（MOD-03 拥有，组合根构造）。

    只构造**一个** ``BackendManager``：所有角色共享加载生命周期、并发限流与内存预算，
    因此「policy 用本地 8B + proposer 用 DeepSeek」不会把预算绕过去。
    """

    def __init__(self, cfg: Any, *, run_id: str = "", runs_root: Path | str = "runs",
                 registry: ModelRegistry | None = None,
                 manager: BackendManager | None = None,
                 infra: InfraFacade | None = None,
                 mode: str | None = None,
                 record_sink: Any = None) -> None:
        self.cfg = cfg
        self.run_id = str(run_id)
        self.runs_root = Path(runs_root)
        inference = getattr(cfg, "inference", None)
        self.registry = registry if registry is not None else load_registry(
            str(getattr(inference, "registry_path", "configs/models.yaml")))
        self.bindings = resolve_role_bindings(cfg, registry=self.registry)
        self._clients: dict[str, BackendInferenceClient] = {}
        self._manager = manager
        self._infra: InfraFacade = infra if infra is not None else (
            InfraBridge(run_id=self.run_id or None, runs_root=self.runs_root,
                        debug_dump_raw=bool(getattr(inference, "debug_dump_raw", False)))
            if inference is not None else NullInfra())
        self._mode = str(mode or getattr(inference, "mode", "train") or "train")
        self._record_sink = record_sink
        self.provenance_path: Path | None = None

    # ---- 装配 -------------------------------------------------------------------
    @property
    def manager(self) -> BackendManager:
        """共享的 ``BackendManager``（惰性构造；测试可传入自己的实例）。"""
        if self._manager is None:
            inference = getattr(self.cfg, "inference", None)
            self._manager = BackendManager(
                str(getattr(inference, "registry_path", "configs/models.yaml")),
                mode=self._mode, # type: ignore[arg-type]
                ram_budget_gb=float(getattr(inference, "ram_budget_gb", 20.0)),
                infra=self._infra, config=inference, registry=self.registry,
                record_sink=self._record_sink,
                # API 后端（deepseek）的审计工件必须落在本 run 的目录里
                runs_root=self.runs_root)
        return self._manager

    def binding(self, role: str) -> RoleBinding:
        return self._checked(role)

    def model_id_for(self, role: str) -> str:
        return self._checked(role).model_id

    def client_for(self, role: str) -> BackendInferenceClient:
        """按角色返回同步客户端（同一角色只构造一次）。"""
        binding = self._checked(role)
        client = self._clients.get(role)
        if client is None:
            inference = getattr(self.cfg, "inference", None)
            client = BackendInferenceClient(
                self.manager, model_id=binding.model_id, role=role,
                total_timeout_ms=int(getattr(inference, "total_timeout_ms", 120_000)),
                max_attempts=int(getattr(inference, "max_attempts", 3)))
            self._clients[role] = client
        return client

    def provenance(self) -> dict[str, Any]:
        """本次 run 的角色 provenance（含臂、config_hash、三角色各自的后端/版本）。"""
        return run_provenance(self.cfg, self.bindings, run_id=self.run_id,
                              extra={"registry_ids": list(self.registry.ids())})

    def write_provenance(self, path: Path | str | None = None) -> Path | None:
        """落 ``runs/<run_id>/models_provenance.json``（原子写；key 不落盘）。"""
        target = Path(path) if path is not None else (
            self.runs_root / self.run_id / PROVENANCE_FILENAME if self.run_id else None)
        if target is None:
            return None
        from ..infra.io import write_json_atomic

        write_json_atomic(target, self.provenance())
        self.provenance_path = target
        return target

    # ---- 内部 -------------------------------------------------------------------
    def _checked(self, role: str) -> RoleBinding:
        if role not in ROLE_NAMES:
            raise InferenceError(
                E.E_LLM_UNAVAILABLE, f"未知模型角色 {role!r}",
                hint=f"合法角色：{', '.join(ROLE_NAMES)}", retryable=False)
        return self.bindings[role]
