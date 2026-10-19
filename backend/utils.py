"""Configuration, wire-format helpers, and turn-block condensation."""

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
LOG = logging.getLogger("ctx-engine")
DEFAULT_PROMPT = (
    "Summarize one completed assistant activity block into a concise factual note. "
    "Preserve exact commands, files, identifiers, decisions, tool results, failures and "
    "unresolved work. The user query stays verbatim in a separate user message. "
    "Output only the assistant note. Never add tags, wrappers or metadata."
)
DEFAULTS = {
    "tokens": int(os.getenv("PI_CONTEXT_ROUTER_TOKENS", "32000")),
    "keepTurns": int(os.getenv("PI_CONTEXT_ROUTER_KEEP_TURNS", "2")),
    "maxTokens": int(os.getenv("PI_CONTEXT_ROUTER_MAX_TOKENS", "8192")),
    "model": os.getenv("PI_CONTEXT_ROUTER_CONDENSER_MODEL", "minicpm5"),
    "blockPrompt": os.getenv("PI_CONTEXT_ROUTER_BLOCK_PROMPT", DEFAULT_PROMPT),
}
ROUTES = json.loads(
    os.getenv(
        "PI_CONTEXT_ROUTER_ROUTES",
        '{"openai":"https://api.openai.com/v1","deepseek":"https://api.deepseek.com","antigravity":"https://daily-cloudcode-pa.googleapis.com"}',
    )
)
UPSTREAM = os.getenv("PI_CONTEXT_ROUTER_UPSTREAM", "https://chatgpt.com/backend-api")
AUTH_PATH = Path(
    os.getenv("PI_CONTEXT_ROUTER_AUTH", "~/.pi/agent/auth.json")
).expanduser()
LOCAL_URL = os.getenv(
    "PI_CONTEXT_ROUTER_LOCAL_URL", "http://127.0.0.1:30000/v1/chat/completions"
)
ZAI_URL = os.getenv(
    "PI_CONTEXT_ROUTER_ZAI_URL", "https://api.z.ai/api/coding/paas/v4/chat/completions"
)
CONDENSER_MODELS = {
    "minicpm5": {
        "url": LOCAL_URL,
        "model": os.getenv("PI_CONTEXT_ROUTER_MINICPM_MODEL", "minicpm5-2b-fp8"),
    },
    "zai": {
        "url": ZAI_URL,
        "model": os.getenv("PI_CONTEXT_ROUTER_ZAI_MODEL", "glm-5.3-flash"),
    },
}


def zai_key():
    key = os.getenv("ZAI_API_KEY") or os.getenv("Z_AI_API_KEY")
    if key:
        return key
    if AUTH_PATH.exists():
        auth = json.loads(AUTH_PATH.read_text()).get("zai-coding-cn", {})
        key = auth.get("key", auth.get("apiKey", ""))
        if key:
            return key
    raise RuntimeError("No Z.ai key: set ZAI_API_KEY or add auth.json:zai-coding-cn")
ATTEMPTS = 3
ATTEMPT_TIMEOUT = int(os.getenv("PI_CONTEXT_ROUTER_ATTEMPT_TIMEOUT", "30"))
COOLDOWN = int(os.getenv("PI_CONTEXT_ROUTER_CONDENSER_COOLDOWN", "120"))
_SKIP_UNTIL = {}  # condenser model -> monotonic deadline while it is out


def condenser_skipped(config):
    return time.monotonic() < _SKIP_UNTIL.get(config["model"], 0.0)


class RouterError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(
        (value if isinstance(value, str) else canonical(value)).encode()
    ).hexdigest()


def image(value):
    return isinstance(value, dict) and value.get("type") in (
        "image",
        "input_image",
        "output_image",
        "image_url",
    )


def strip_images(value):
    if image(value):
        return None
    if isinstance(value, list):
        return [strip_images(v) for v in value if not image(v)]
    if isinstance(value, dict):
        return {k: strip_images(v) for k, v in value.items()}
    return value


def has_image(value):
    if image(value):
        return True
    if isinstance(value, list):
        return any(has_image(v) for v in value)
    if isinstance(value, dict):
        return any(has_image(v) for v in value.values())
    return False


def tokens(value):
    return math.ceil(
        len(value if isinstance(value, str) else canonical(strip_images(value))) / 3.5
    )


def text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (text(v) for v in value)))
    if isinstance(value, dict):
        if image(value):
            return "[image]"
        return text(value.get("text", value.get("content", value.get("output", ""))))
    return "" if value is None else str(value)


def is_user_query(item):
    parts = item.get("content")
    result_only = (
        isinstance(parts, list)
        and bool(parts)
        and all(isinstance(p, dict) and p.get("type") == "tool_result" for p in parts)
    )
    return item.get("role") == "user" and not result_only


def concatenate_users(items, field):
    """Join only adjacent real queries, retaining every content part and text byte."""
    joined, changes = [], []
    part_type = "input_text" if field == "input" else "text"
    for item in items:
        if joined and is_user_query(joined[-1]) and is_user_query(item):
            previous = joined[-1]
            left, right = previous.get("content"), item.get("content")
            merged = copy.deepcopy(previous)
            if isinstance(left, str) and isinstance(right, str):
                merged["content"] = left + "\n" + right
            else:
                if not isinstance(left, (str, list)) or not isinstance(
                    right, (str, list)
                ):
                    raise RouterError(400, "User content must be text or content parts")
                left_parts = (
                    left
                    if isinstance(left, list)
                    else [{"type": part_type, "text": left}]
                )
                right_parts = (
                    right
                    if isinstance(right, list)
                    else [{"type": part_type, "text": right}]
                )
                merged["content"] = (
                    left_parts + [{"type": part_type, "text": "\n"}] + right_parts
                )
            joined[-1] = merged
            changes.append(
                {
                    "kind": "merge_users",
                    "before": [previous, item],
                    "after": [merged],
                    "cached": False,
                }
            )
        else:
            joined.append(item)
    return joined, changes


def turns(items):
    prefix, groups = [], []
    for item in items:
        if not groups and item.get("role") in ("system", "developer"):
            prefix.append(item)
            continue
        if not groups or is_user_query(item):
            groups.append([])
        groups[-1].append(item)
    return prefix, groups


def transcript(block):
    sections = []
    for item in block:
        kind = item.get("type")
        role = item.get("role", "assistant")
        if kind == "function_call":
            sections.append(
                f"tool_call:\n{item.get('name')}({text(item.get('arguments'))})"
            )
        elif kind == "function_call_output" or role in ("tool", "toolResult"):
            sections.append(
                "tool_output:\n" + text(item.get("output", item.get("content")))
            )
        elif kind == "reasoning":
            # Never feed opaque encrypted reasoning to the condenser.
            sections.append(
                "thinking:\n" + text(item.get("summary", item.get("content")))
            )
        else:
            parts = item.get("content")
            parts = parts if isinstance(parts, list) else [{"text": parts}]
            visible = [
                p
                for p in parts
                if not isinstance(p, dict)
                or p.get("type") not in ("tool_use", "tool_result", "thinking")
            ]
            if text(visible):
                sections.append(role + ":\n" + text(visible))
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    sections.append(
                        f"tool_call:\n{part.get('name')}({canonical(part.get('input'))})"
                    )
                elif part.get("type") == "tool_result":
                    sections.append("tool_output:\n" + text(part.get("content")))
                elif part.get("type") == "thinking":
                    sections.append(
                        "thinking:\n" + text(part.get("thinking", part.get("text")))
                    )
            for call in item.get("tool_calls", []):
                func = call.get("function", call)
                args = func.get("arguments", "")
                sections.append(
                    f"tool_call:\n{func.get('name')}({args if isinstance(args, str) else canonical(args)})"
                )
            if item.get("function_call"):
                func = item["function_call"]
                sections.append(
                    f"tool_call:\n{func.get('name')}({text(func.get('arguments'))})"
                )
            if item.get("reasoning_content") or item.get("reasoning"):
                sections.append(
                    "thinking:\n"
                    + text(item.get("reasoning_content", item.get("reasoning")))
                )
    return "\n---\n".join(sections) + "\n---------------------------"


# Python owns the server; condensation targets are the local MiniCPM and Z.ai
# GLM. Both speak the OpenAI chat-completions wire format.


async def condenser(client, source, config):
    started = time.monotonic()
    target = 100 + math.ceil(tokens(source) / 10)
    system = (
        f"{config['blockPrompt']}\n\n"
        "Summarize only the assistant activity supplied below: replies, tool calls/results "
        "and reasoning. The user query is preserved verbatim separately; do not recreate "
        "or invent it. Output one assistant note.\n\n"
        f"Complete the note at approximately {target} tokens; preserve evidence, failures, "
        "constraints and unfinished work over the length target."
    )
    spec = CONDENSER_MODELS[config["model"]]
    headers = {}
    payload = {
        "model": spec["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": source},
        ],
        "max_tokens": config["maxTokens"],
    }
    if config["model"] == "zai":
        headers["Authorization"] = f"Bearer {zai_key()}"
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    response = await client.post(spec["url"], headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage", {})
    answer = data["choices"][0]["message"]["content"]
    LOG.info(
            "Condenser finished; model=%s duration_ms=%d prompt_tokens=%s completion_tokens=%s output_tokens=%d",
            config["model"],
            round((time.monotonic() - started) * 1000),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            tokens(answer) if isinstance(answer, str) else 0,
        )
    if not isinstance(answer, str):
        raise TypeError("Invalid condenser output")
    output = re.sub(r"<think>[\s\S]*?</think>", "", answer, flags=re.IGNORECASE).strip()
    if not output:
        raise ValueError("Empty condenser output")
    return output


async def ctx_man(block, config, db, client):
    """Return a cached or condensed note, or None when the condenser is out."""
    key = f"{digest(block)}-{config['model']}-{config['maxTokens']}"
    hit = db.cache_get(key)
    if hit is not None:
        LOG.info(
            "Turn block cache hit; model=%s block=%s tokens=%d",
            config["model"],
            digest(block)[:12],
            tokens(hit),
        )
        return hit, True
    if condenser_skipped(config):
        LOG.warning(
            "Condenser cooling down; forwarding block verbatim; model=%s block=%s tokens=%d",
            config["model"],
            digest(block)[:12],
            tokens(block),
        )
        return None, False
    source = transcript(block)
    for attempt in range(ATTEMPTS):
        attempt_started = time.monotonic()
        try:
            LOG.info(
                "Condensing turn block; attempt=%d/%d model=%s block=%s input_tokens=%d timeout_s=%d",
                attempt + 1,
                ATTEMPTS,
                config["model"],
                digest(block)[:12],
                tokens(source),
                ATTEMPT_TIMEOUT,
            )
            async with asyncio.timeout(ATTEMPT_TIMEOUT):
                output = await condenser(client, source, config)
            attempt_ms = round((time.monotonic() - attempt_started) * 1000)
            LOG.info(
                "Turn block condensed; attempt=%d/%d model=%s block=%s duration_ms=%d output_tokens=%d",
                attempt + 1,
                ATTEMPTS,
                config["model"],
                digest(block)[:12],
                attempt_ms,
                tokens(output),
            )
            break
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as error:  # noqa: BLE001 -- Auth, transport, and output failures all count as attempts.
            attempt_ms = round((time.monotonic() - attempt_started) * 1000)
            LOG.warning(
                "Turn block attempt failed; attempt=%d/%d model=%s block=%s duration_ms=%d failure=%s (%s)",
                attempt + 1,
                ATTEMPTS,
                config["model"],
                digest(block)[:12],
                attempt_ms,
                type(error).__name__,
                str(error),
            )
            if attempt == ATTEMPTS - 1:
                _SKIP_UNTIL[config["model"]] = time.monotonic() + COOLDOWN
                LOG.critical(
                    "Condenser exhausted %s attempts; model=%s block=%s failure=%s; "
                    "forwarding block verbatim for %ss",
                    ATTEMPTS,
                    config["model"],
                    digest(block),
                    type(error).__name__,
                    COOLDOWN,
                )
                return None, False
    return db.cache_put(key, output, digest(output)), False


async def compress_body(original, config, db, client):
    field = (
        "messages"
        if isinstance(original.get("messages"), list)
        else "input"
        if isinstance(original.get("input"), list)
        else None
    )
    if field is None:
        return original, [], 0
    if any(not isinstance(i, dict) for i in original[field]):
        raise RouterError(400, "messages must contain objects")
    body = copy.deepcopy(original)
    body[field], changes = concatenate_users(body[field], field)
    if tokens(body) <= config["tokens"]:
        return body if changes else original, changes, 0
    prefix, groups = turns(body[field])
    completed = max(0, len(groups) - config["keepTurns"])
    # Queries are indivisible fixed context too: never summarize or clip them.
    fixed = list(prefix)
    for index, block in enumerate(groups):
        if index >= completed or not is_user_query(block[0]) or has_image(block):
            fixed.extend(block)
        else:
            fixed.append(block[0])
    protected = {**body, field: fixed}
    t_body = tokens(body)
    t_prot = tokens(protected)
    if t_prot > config["tokens"]:
        LOG.warning(
            "Verbatim queries, protected turns or fixed fields exceed %s tokens; "
            "preserving them and condensing eligible older activity",
            config["tokens"],
        )

    LOG.info(
        "Compression plan: total_tokens=%d protected_tokens=%d threshold=%d completed_blocks=%d total_turns=%d",
        t_body,
        t_prot,
        config["tokens"],
        completed,
        len(groups),
    )

    async def replace(block):
        if not is_user_query(block[0]) or len(block) == 1 or has_image(block):
            return block, None, 0
        if (
            len(block) == 2
            and block[1].get("role") == "assistant"
            and db.is_summary(digest(text(block[1].get("content"))))
        ):
            return block, None, 1
        query, activity = block[0], block[1:]
        output, cached = await ctx_man(activity, config, db, client)
        if output is None:
            return block, None, 0
        # String content is valid for assistant input in both OpenAI wire formats.
        result = [query, {"role": "assistant", "content": output}]
        before_tokens, after_tokens = tokens(block), tokens(result)
        # The threshold is a soft target, not permission to clip fixed context.
        # A verbose note must not make a request larger; preserve the original
        # block instead, while retaining the canonical cached note for reuse.
        if after_tokens >= before_tokens:
            LOG.info(
                "Keeping non-reducing activity block; tokens=%d->%d cached=%s",
                before_tokens,
                after_tokens,
                cached,
            )
            return block, None, int(cached)
        return (
            result,
            {
                "kind": "block",
                "before": block,
                "after": result,
                "cached": cached,
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
            },
            int(cached),
        )

    results = await asyncio.gather(
        *(replace(block) for block in groups[:completed]),
        return_exceptions=True,
    )
    # Wait for every launched task, then fail before forwarding any partial plan.
    for result in results:
        if isinstance(result, BaseException):
            raise result
    hits = 0
    for index, (block, change, cached) in enumerate(results):
        groups[index] = block
        hits += cached
        if change:
            changes.append(change)
    body[field] = prefix + [item for group in groups for item in group]
    if tokens(body) > config["tokens"]:
        LOG.warning(
            "Condensed context remains above %s tokens; forwarding intact blocks without dropping",
            config["tokens"],
        )
    LOG.info(
        "Compression complete: tokens=%d->%d cache_hits=%d changes=%d",
        t_body,
        tokens(body),
        hits,
        len(changes),
    )
    return body if changes else original, changes, hits
