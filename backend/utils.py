"""Configuration, wire-format helpers, and turn-block condensation."""

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
LOG = logging.getLogger("ctx-engine")
DEFAULT_PROMPT = (
    "Compress one completed turn into a concise factual note. Preserve the user request, "
    "exact commands, files, identifiers, decisions, tool results and unresolved work. "
    "Output only the note. Never add tags, wrappers or metadata."
)
DEFAULTS = {
    "tokens": int(os.getenv("PI_CONTEXT_ROUTER_TOKENS", "32000")),
    "keepTurns": int(os.getenv("PI_CONTEXT_ROUTER_KEEP_TURNS", "2")),
    "maxTokens": int(os.getenv("PI_CONTEXT_ROUTER_MAX_TOKENS", "4096")),
    "model": os.getenv("PI_CONTEXT_ROUTER_CONDENSER_MODEL", "deepseek-flash"),
    "blockPrompt": os.getenv("PI_CONTEXT_ROUTER_BLOCK_PROMPT", DEFAULT_PROMPT),
}
ROUTES = json.loads(
    os.getenv(
        "PI_CONTEXT_ROUTER_ROUTES",
        '{"openai":"https://api.openai.com/v1","deepseek":"https://api.deepseek.com"}',
    )
)
UPSTREAM = os.getenv("PI_CONTEXT_ROUTER_UPSTREAM", "https://chatgpt.com/backend-api")
AUTH_PATH = Path(
    os.getenv("PI_CONTEXT_ROUTER_AUTH", "~/.pi/agent/auth.json")
).expanduser()
CONDENSER_URL = os.getenv(
    "PI_CONTEXT_ROUTER_CONDENSER_URL", "https://api.deepseek.com/chat/completions"
)
ATTEMPTS = 3
ATTEMPT_TIMEOUT = 10


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


def turns(items):
    prefix, groups = [], []
    for item in items:
        if not groups and item.get("role") in ("system", "developer"):
            prefix.append(item)
            continue
        parts = item.get("content")
        result_only = (
            isinstance(parts, list)
            and parts
            and all(
                isinstance(p, dict) and p.get("type") == "tool_result" for p in parts
            )
        )
        if not groups or (item.get("role") == "user" and not result_only):
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


# Python owns the server; the supported Pi SDK handles subscription refresh in
# this one-shot child. Payload travels over stdin, never process arguments.
LUNA_BRIDGE = r"""
import { ModelRuntime } from '/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js';
const input = JSON.parse(await Bun.stdin.text());
const runtime = await ModelRuntime.create({authPath:input.auth,modelsPath:null,refreshOnCreate:false});
const model = runtime.getModel('openai',input.model);
if (!model) throw new Error('Condenser model unavailable');
const result = await runtime.completeSimple(model, {
  systemPrompt:input.system,
  messages:[{role:'user',content:input.text,timestamp:Date.now()}]
}, {maxTokens:input.maxTokens,reasoning:'low',transport:'sse',maxRetries:0});
if (!['stop','length'].includes(result.stopReason)) throw new Error('Condenser failed');
process.stdout.write(JSON.stringify(result.content.filter(p=>p.type==='text').map(p=>p.text).join('\n')));
"""


async def condenser(client, source, config):
    target = 100 + math.ceil(tokens(source) / 10)
    system = f"{config['blockPrompt']}\n\nComplete the note at approximately {target} tokens; preserve evidence, failures, constraints and unfinished work over the length target."
    if config["model"] == "gpt-6-luna":
        bun = os.getenv("PI_CONTEXT_ROUTER_BUN", str(Path.home() / ".bun/bin/bun"))
        process = await asyncio.create_subprocess_exec(
            bun,
            "-e",
            LUNA_BRIDGE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            payload = canonical(
                {
                    "auth": str(AUTH_PATH),
                    "model": config["model"],
                    "system": system,
                    "text": source,
                    "maxTokens": config["maxTokens"],
                }
            )
            stdout, _ = await process.communicate(payload.encode())
            if process.returncode:
                raise RuntimeError("Subscription condenser failed")
            answer = json.loads(stdout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    else:
        key = os.getenv("PI_CONTEXT_ROUTER_API_KEY", "")
        if not key and AUTH_PATH.exists():
            auth = json.loads(AUTH_PATH.read_text()).get("deepseek", {})
            key = auth.get("key", auth.get("apiKey", ""))
        response = await client.post(
            CONDENSER_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": config["model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": source},
                ],
                "max_tokens": config["maxTokens"],
                "thinking": {"type": "enabled"},
                "reasoning_effort": "low",
            },
        )
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"]
    if not isinstance(answer, str):
        raise TypeError("Invalid condenser output")
    output = re.sub(r"<think>[\s\S]*?</think>", "", answer, flags=re.IGNORECASE).strip()
    if not output:
        raise ValueError("Empty condenser output")
    return output


async def ctx_man(block, config, db, client):
    key = f"{digest(block)}-{config['model']}-{config['maxTokens']}"
    hit = db.cache_get(key)
    if hit is not None:
        return hit, True
    source = transcript(block)
    for attempt in range(ATTEMPTS):
        try:
            async with asyncio.timeout(ATTEMPT_TIMEOUT):
                output = await condenser(client, source, config)
            break
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as error:  # noqa: BLE001 -- Auth, transport, and output failures all count as attempts.
            if attempt == ATTEMPTS - 1:
                LOG.critical(
                    "Condenser exhausted %s attempts; model=%s block=%s failure=%s",
                    ATTEMPTS,
                    config["model"],
                    digest(block),
                    type(error).__name__,
                )
                user = next((i for i in block if i.get("role") == "user"), {})
                raise RouterError(
                    502, f'Summarization failed for turn: "{text(user.get("content"))}"'
                ) from None
    return db.cache_put(key, output, digest(output)), False


async def compress_body(original, config, db, client):
    field = (
        "messages"
        if isinstance(original.get("messages"), list)
        else "input"
        if isinstance(original.get("input"), list)
        else None
    )
    if field is None or tokens(original) <= config["tokens"]:
        return original, [], 0
    if any(not isinstance(i, dict) for i in original[field]):
        raise RouterError(400, "messages must contain objects")
    body = copy.deepcopy(original)
    prefix, groups = turns(body[field])
    completed = max(0, len(groups) - config["keepTurns"])
    # If fixed fields and protected turns already overflow, preserve everything.
    protected = {**body, field: prefix + [i for g in groups[completed:] for i in g]}
    if tokens(protected) > config["tokens"]:
        LOG.warning(
            "Protected turns or fixed fields exceed %s tokens; forwarding intact",
            config["tokens"],
        )
        return original, [], 0

    async def replace(index, block):
        if has_image(block):
            return block, None, 0
        if (
            len(block) == 1
            and block[0].get("role") == "user"
            and db.is_summary(digest(text(block[0].get("content"))))
        ):
            return block, None, 1
        output, cached = await ctx_man(block, config, db, client)
        content = (
            output if field == "messages" else [{"type": "input_text", "text": output}]
        )
        result = [{"role": "user", "content": content}]
        return (
            result,
            {"kind": "block", "before": block, "after": result, "cached": cached},
            int(cached),
        )

    results = await asyncio.gather(
        *(replace(index, block) for index, block in enumerate(groups[:completed])),
        return_exceptions=True,
    )
    # Wait for every launched task, then fail before forwarding any partial plan.
    for result in results:
        if isinstance(result, BaseException):
            raise result
    changes, hits = [], 0
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
    return body if changes else original, changes, hits
