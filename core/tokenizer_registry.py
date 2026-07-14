"""
    Unified tokenizer registry for Qwen / LLaMA / DeepSeek / GPT.

    Usage:
        from tokenizer_registry import estimate_tokens
        n = estimate_tokens("hello, world", tokenizer_type="qwen1.5b")

    Workflow
        Names containing "qwen" use the Qwen tokenizer path.
        Names containing "llama" use the LLaMA tokenizer path.
        Names containing "deepseek" use the DeepSeek tokenizer path.
        Names starting with "gpt" or containing "gpt-3"/"gpt-4" use GPT via tiktoken.

        If transformers is unavailable, skip Hugging Face tokenizers.
        If tiktoken is unavailable, skip GPT-specific logic.
        If all paths fail, fall back to len(text) without raising.
"""
import json
import os
# from __future__ import annotations
from functools import lru_cache
from typing import Optional
# from CacheRoute.util.timer import timing

# These libraries are optional, so guard imports with try/except.
try:
    from transformers import AutoTokenizer
except ImportError:
    AutoTokenizer = None  # type: ignore

try:
    import tiktoken  # for GPT-family models
except ImportError:
    tiktoken = None  # type: ignore


class TokenizerRegistry:
    """
    Manage tokenizers across model families and route by model name.
    FAST_MODE enables approximate token counting when True; False uses normal tokenizers.
    """
    FAST_MODE = False

    # Extend this mapping when additional naming conventions are needed.
    # Keys are model families; values are default Hugging Face model names.
    HF_FAMILY_DEFAULTS = {
        "qwen": "Qwen/Qwen2-1.5B",
        "llama": "meta-llama/Meta-Llama-3-70B",
        "deepseek": "deepseek-ai/deepseek-moe-16b",  # Replace with a preferred default if needed.
    }

    @classmethod
    def _is_local_model_dir(cls, name: str) -> bool:
        """
        Return whether name is a local model directory rather than a Hugging Face repo id.
        A local directory should exist and contain at least one tokenizer marker file.
        """
        if not name:
            return False
        if not os.path.isdir(name):
            return False

        # Common tokenizer marker files include tokenizer.json, tokenizer.model, vocab.json, merges.txt, and special_tokens_map.json.
        markers = [
            "tokenizer.json",
            "tokenizer.model",
            "vocab.json",
            "merges.txt",
            "special_tokens_map.json",
            "tokenizer_config.json",
        ]
        return any(os.path.exists(os.path.join(name, m)) for m in markers)

    @classmethod
    def detect_family(cls, tokenizer_type: str) -> str:
        """
        Infer the tokenizer family from the model name or type: qwen, llama, deepseek, gpt, or other.
        """
        name = tokenizer_type.lower()

        if "qwen" in name:
            return "qwen"
        if "llama" in name or "lLaMA".lower() in name:
            return "llama"
        if "deepseek" in name:
            return "deepseek"
        if name.startswith("gpt") or "gpt-3" in name or "gpt-4" in name:
            return "gpt"

        # Fall back to the generic other family.
        return "other"

    @classmethod
    def _resolve_tokenizer_source(cls, model_name: str) -> str:
        """
        Resolve served model names, repo ids, and local paths into tokenizer loading sources.
        - Return local directories directly.
        - Return mapped local directories when SCHEDULER_TOKENIZER_MAP contains the name.
        - Otherwise return the original value, which may be a repo id.
        """
        # 1) Return local directories directly.
        if cls._is_local_model_dir(model_name):
            return model_name

        # 2) Apply served-name mapping.
        raw = os.getenv("SCHEDULER_TOKENIZER_MAP", "").strip()
        if raw:
            try:
                mp = json.loads(raw)
                if isinstance(mp, dict) and model_name in mp:
                    return mp[model_name]
            except Exception:
                pass

        # 3) Fallback: return the original value.
        return model_name

    # ---------------- GPT family (tiktoken) ----------------

    @classmethod
    @lru_cache(maxsize=None)
    def _get_gpt_encoding(cls, model_name: str):
        if tiktoken is None:
            return None

        try:
            return tiktoken.encoding_for_model(model_name)
        except Exception:
            # If the concrete model is unknown, use the generic cl100k_base encoding.
            try:
                return tiktoken.get_encoding("cl100k_base")
            except Exception:
                return None

    @classmethod
    def _gpt_count_tokens(cls, text: str, model_name: str) -> Optional[int]:
        enc = cls._get_gpt_encoding(model_name)
        if enc is None:
            return None
        # tiktoken returns list[int].
        return len(enc.encode(text))

    # ---------------- HF families (Qwen / LLaMA / DeepSeek) ----------------


    @classmethod
    # @timing
    @lru_cache(maxsize=None)
    def _get_hf_tokenizer(cls, hf_model_name: str):
        if AutoTokenizer is None:
            return None

        try:
            is_local = cls._is_local_model_dir(hf_model_name)

            # Local directories: force local loading and avoid network probes.
            return AutoTokenizer.from_pretrained(
                hf_model_name,
                trust_remote_code=True,
                use_fast=True,
                local_files_only=is_local,
            )
        except Exception:
            return None

    @classmethod
    # @timing
    def _hf_count_tokens(cls, text: str, family: str, model_name: str) -> Optional[int]:
        """
        Count tokens for Hugging Face models. Prefer local tokenizer directories, then repo ids or family defaults.
        """
        model_name = cls._resolve_tokenizer_source(model_name)

        # 1) Prefer local directories.
        if cls._is_local_model_dir(model_name):
            hf_name = model_name
        else:
            # 2) If it looks like a repo id, use it directly.
            if "/" in model_name:
                hf_name = model_name
            else:
                # 3) Otherwise use the family default.
                hf_name = cls.HF_FAMILY_DEFAULTS.get(family)

        if hf_name is None:
            return None

        tok = cls._get_hf_tokenizer(hf_name)
        if tok is None:
            return None

        try:
            return len(tok.encode(text, add_special_tokens=False))
        except Exception:
            return None

    # ---------------- Public API ----------------

    @classmethod
    def estimate_tokens(cls, text: str, model_name: str) -> int:
        """
        Select a tokenizer by model name and return the token count.
        If transformers/tiktoken is unavailable or tokenization fails, fall back to len(text).

        :param text: Input text.
        :param model_name: Model name or type, for example:
                           'qwen1.5b' / 'Qwen/Qwen2-1.5B'
                           'llama3-8b'
                           'deepseek-v2'
                           'gpt-4o-mini'
        """
        family = cls.detect_family(model_name)

        # --- Fast approximate mode: skip slower tokenizer initialization entirely. ---
        if cls.FAST_MODE:
            print("use Fast tokenizer")
            # Family-specific scaling factors can be tuned later.
            rough_ratio = {
                "deepseek": 1.0,
                "qwen": 1.0,
                "llama": 0.8,
                "gpt": 0.75,
            }.get(family, 1.0)
            return int(len(text) * rough_ratio)

        # 1) GPT family -> tiktoken.
        if family == "gpt":
            print(f"[TokenizerRegistry] user tokenizer {model_name} ")
            n = cls._gpt_count_tokens(text, model_name)
            if n is not None:
                return n

        # 2) Qwen / LLaMA / DeepSeek -> Hugging Face tokenizer.
        if family in {"qwen", "llama", "deepseek"}:
            print(f"[TokenizerRegistry] user tokenizer {model_name} ")
            n = cls._hf_count_tokens(text, family, model_name)
            if n is not None:
                return n+1

        # 3) Other families or failures -> estimate by character count.
        return len(text)

    @classmethod
    def warmup_tokenizers(cls, model_name: str):
        """
        Preload the tokenizer for a model so later estimate_tokens calls avoid cold start.
        Local directories warm up the local tokenizer with local_files_only semantics.
        """
        model_name = cls._resolve_tokenizer_source(model_name)

        # Warm up local directories first.
        if cls._is_local_model_dir(model_name):
            cls._get_hf_tokenizer(model_name)
            return

        family = cls.detect_family(model_name)

        if family == "gpt":
            cls._get_gpt_encoding(model_name)
            return

        if family in {"qwen", "llama", "deepseek"}:
            if "/" in model_name:
                hf_name = model_name
            else:
                hf_name = cls.HF_FAMILY_DEFAULTS.get(family)

            if hf_name is not None:
                cls._get_hf_tokenizer(hf_name)


# Expose a top-level helper for convenience.
def estimate_tokens(text: str, model_name: str) -> int:
    """
    Estimate the token count for text under the specified model.
    """
    return TokenizerRegistry.estimate_tokens(text, model_name)

