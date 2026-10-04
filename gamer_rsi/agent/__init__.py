"""MOD-04 policy：把 ``Observation`` 变成 prompt、把 LLM 原始文本变成 ``Action``。

对外面（PRD §6 / Spec §4.7）：

- ``PolicyAgent``：``reset(ctx)`` / ``play_decision(env, obs, ctx)`` / ``prompt_bundle(obs, ctx)``；
- ``parser``：``parse_llm_output`` / ``validate_syntax`` / ``validate_schema`` / ``validate_semantic``；
- ``heuristic``：``heuristic_policy``（fallback 与 MOD-08 heuristic 基线共用）；
- ``prompts.assembler``：INV-3 的唯一 prompt 注入出口；
- ``tools.registry``：``@harness_tool`` / ``rank_actions`` 受限加载。

本包只从 ``gamer_rsi.sandbox.api`` 导入环境相关类型（INV-1：禁止 import ``gamer_rsi.env.*``）。
"""

from __future__ import annotations

from .heuristic import heuristic_policy, heuristic_policy_explained
from .parser import (ParseResult, allowed_kinds, canonical_action, parse_llm_output,
                     semantic_match, validate_schema, validate_semantic, validate_syntax)
from .policy import (BackendInferenceClient, DecisionResult, EpisodeContext, InferenceClient,
                     LLMCallMeta, MemoryReader, PolicyAgent, StepRecord, coerce_client)
from .prompts.assembler import (MemoryItem, PromptBundle, PromptContext, PromptSpec,
                                ReflectionItem, TruncationRecord, assemble_prompt,
                                load_template)
from .tools.registry import ToolRegistry, harness_tool, load_harness_tools, tool_summary

__all__ = [
    # policy
    "PolicyAgent", "DecisionResult", "StepRecord", "LLMCallMeta", "EpisodeContext",
    "InferenceClient", "MemoryReader", "BackendInferenceClient", "coerce_client",
    # parser
    "ParseResult", "parse_llm_output", "validate_syntax", "validate_schema",
    "validate_semantic", "semantic_match", "canonical_action", "allowed_kinds",
    # heuristic
    "heuristic_policy", "heuristic_policy_explained",
    # prompts
    "PromptSpec", "PromptContext", "PromptBundle", "MemoryItem", "ReflectionItem",
    "TruncationRecord", "load_template", "assemble_prompt",
    # tools
    "ToolRegistry", "harness_tool", "load_harness_tools", "tool_summary",
]
