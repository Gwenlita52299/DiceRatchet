"""MOD-04 prompt 素材与组装（``task.md`` / ``decision_fallback.md`` / ``fixed_v1.md``）。"""

from __future__ import annotations

from .assembler import (DEFAULT_TEMPLATE_PATH, FIXED_TEMPLATE_PATH, KNOWN_PLACEHOLDERS,
                        PLACEHOLDER_ORDER, REQUIRED_PLACEHOLDERS, TASK_PATH, MemoryItem,
                        PromptBundle, PromptContext, PromptSpec, PromptTemplateError,
                        ReflectionItem, TruncationRecord, assemble_prompt,
                        check_placeholders, estimate_tokens, load_task_text, load_template,
                        render_memory, render_reflections, resolve_path, sha256_of)
from .cache_layout import (CacheLayoutError, DYNAMIC_SECTIONS, FINAL_REMINDER,
                           FINAL_SECTION, REQUIRED_SECTIONS, STATIC_SECTIONS, is_reordered,
                           reorder_for_prefix_cache)

__all__ = [
    "DEFAULT_TEMPLATE_PATH", "FIXED_TEMPLATE_PATH", "TASK_PATH",
    "PLACEHOLDER_ORDER", "KNOWN_PLACEHOLDERS", "REQUIRED_PLACEHOLDERS",
    "MemoryItem", "ReflectionItem", "PromptSpec", "PromptContext", "PromptBundle",
    "TruncationRecord", "PromptTemplateError", "load_template", "check_placeholders",
    "assemble_prompt", "estimate_tokens", "sha256_of", "render_memory",
    "render_reflections", "load_task_text", "resolve_path",
    "STATIC_SECTIONS", "DYNAMIC_SECTIONS", "REQUIRED_SECTIONS", "FINAL_SECTION",
    "FINAL_REMINDER", "CacheLayoutError", "reorder_for_prefix_cache", "is_reordered",
]
