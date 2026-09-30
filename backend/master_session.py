#!/usr/bin/env python3
"""Build and incrementally condense a project's active Pi session context."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import socket
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import ctx_engine_utils as utils

MARKER = utils.COMPACTED_TRACE
MAX_REQUEST = 1024 * 1024
CHUNK_CHARS = 100_000
ALLOWED = {"role", "content", "name", "tool_calls", "tool_call_id", "function_call", "type", "id", "call_id", "arguments", "input", "output"}


def encoded_project(project: str) -> str:
    return "--" + project.lstrip("/").replace("/", "-") + "--"


def atomic_json(path: Path, value: object) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def safe_tree_files(root: Path):
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(current) / d).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if name.endswith(".jsonl") and name != "mastersession.jsonl" and not path.is_symlink():
                yield path


def read_stable(path: Path, snapshot: tuple[int, int] | None = None) -> list[dict]:
    before = path.stat(follow_symlinks=False)
    records = []
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"source changed while opening: {path}")
        if snapshot is not None and (before.st_size, before.st_mtime_ns) != snapshot:
            raise ValueError(f"source changed since snapshot: {path}")
        for number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(f"malformed JSON at {path}:{number}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"non-object record at {path}:{number}")
            records.append(record)
    after = path.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError(f"source changed while reading: {path}")
    return records


def materialize(project: str, bucket: Path, output: Path) -> Path:
    if output.resolve() == bucket.resolve() or bucket.resolve() in output.resolve().parents or output.resolve() in bucket.resolve().parents:
        raise ValueError("output directory must not overlap the session input directory")
    raw_path = output / "master-raw.jsonl"
    output.mkdir(parents=True, exist_ok=True)
    if not bucket.is_dir():
        raise ValueError(f"session bucket does not exist: {bucket}")
    sources = sorted(safe_tree_files(bucket), key=lambda p: p.relative_to(bucket).as_posix())
    previous = set()
    if raw_path.exists():
        with raw_path.open("r", encoding="utf-8") as old:
            for line in old:
                previous.add(json.loads(line)["source"])
    snapshots = {}
    for source in sources:
        stat = source.stat(follow_symlinks=False)
        snapshots[source] = (stat.st_size, stat.st_mtime_ns)
    fd, temp = tempfile.mkstemp(prefix=".master-raw.", dir=output)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as raw:
            included = set()
            for source in sources:
                records = read_stable(source, snapshots[source])
                header = records[0] if records and records[0].get("type") == "session" else None
                if not header or not isinstance(header.get("cwd"), str) or not os.path.isabs(header["cwd"]):
                    continue
                if os.path.realpath(header["cwd"]) != project:
                    continue
                rel = source.relative_to(bucket).as_posix()
                included.add(rel)
                raw.write(json.dumps({"source": rel, "entries": records}, ensure_ascii=False, separators=(",", ":")) + "\n")
            if previous - included:
                raise ValueError(f"source sessions disappeared: {sorted(previous - included)[:3]}")
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temp, raw_path)
        dfd = os.open(output, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return raw_path


def active_entries(entries: list[dict]) -> list[dict]:
    # Pi journals form parentId-linked trees; the last record determines the active leaf.
    by_id = {}
    for entry in entries:
        ident = entry.get("id")
        if isinstance(ident, str):
            if ident in by_id:
                raise ValueError(f"duplicate entry id {ident!r} in session tree")
            by_id[ident] = entry
    leaf = next((e for e in reversed(entries) if isinstance(e.get("id"), str)), None)
    if leaf is None:
        raise ValueError("session tree has no entry IDs")
    chain, seen = [], set()
    node = leaf
    while node:
        ident = node.get("id")
        if ident in seen:
            raise ValueError("cycle in session parentId chain")
        seen.add(ident)
        chain.append(node)
        parent = node.get("parentId")
        if parent is None:
            break
        if parent not in by_id:
            raise ValueError(f"missing parent {parent!r} in session tree")
        node = by_id[parent]
    return list(reversed(chain))


def entry_message(entry: dict) -> dict | None:
    typ = entry.get("type")
    if typ != "message":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role == "system":
        return None
    if role not in {"user", "assistant", "tool", "toolResult", "developer"}:
        return None
    return {k: v for k, v in message.items() if k in ALLOWED}


def native_note(entry: dict) -> dict:
    summary = entry.get("summary")
    if summary is None:
        summary = (entry.get("data") or {}).get("summary") if isinstance(entry.get("data"), dict) else None
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("native compaction/branch summary has no summary text")
    return {"role": "user", "content": MARKER + summary}


def blocks_for(entries: list[dict]) -> list[list[dict]]:
    active = active_entries(entries)
    replacements, removed = {}, set()
    for edit in active:
        if edit.get("type") != "context_edit":
            continue
        target = edit.get("targetId")
        if not isinstance(target, str) or target not in {e.get("id") for e in active}:
            raise ValueError("context_edit target is missing from active session tree")
        value = edit.get("replacement")
        if isinstance(value, dict) and set(value) == {"content"}:
            value = value["content"]
        if value is None:
            removed.add(target)
        elif isinstance(value, (str, list)):
            original = next(e for e in active if e.get("id") == target)
            if original.get("type") not in {"message", "custom_message"}:
                raise ValueError("context_edit target is not a message")
            if original.get("type") == "custom_message":
                replacements[target] = {**original, "content": value}
            else:
                replacement_message = dict(original["message"])
                if replacement_message.get("role") in {"assistant", "toolResult"} and isinstance(value, str):
                    value = [{"type": "text", "text": value}]
                replacement_message["content"] = value
                replacements[target] = {**original, "message": replacement_message}
        else:
            raise ValueError("invalid context_edit replacement")
    active = [replacements.get(e.get("id"), e) for e in active if e.get("id") not in removed and e.get("type") != "context_edit"]
    latest_compaction = None
    for index, e in enumerate(active):
        if e.get("type") == "compaction":
            latest_compaction = index
    if latest_compaction is not None:
        compact = active[latest_compaction]
        boundary = compact.get("firstKeptEntryId")
        if not isinstance(boundary, str):
            raise ValueError("invalid firstKeptEntryId in active session tree")
        positions = {e.get("id"): i for i, e in enumerate(active)}
        start = positions.get(boundary)
        if start is None:
            raise ValueError("invalid firstKeptEntryId in active session tree")
        if start is not None and start > latest_compaction:
            raise ValueError("firstKeptEntryId follows its compaction")
        active = [compact] + [e for e in active[start:latest_compaction] if e.get("type") != "compaction"] + active[latest_compaction + 1:]
    stream = []
    for entry in active:
        typ = entry.get("type")
        if typ in {"compaction", "branch_summary"}:
            stream.append(native_note(entry))
        elif typ == "custom_message":
            text = entry.get("content", entry.get("text"))
            if isinstance(text, (str, list)):
                stream.append({"role": "user", "content": text})
        else:
            msg = entry_message(entry)
            if msg is not None:
                stream.append(msg)
    # A persisted native note is already compacted and must not be sent to the router.
    _prefix, blocks = utils.split_message_blocks(stream)
    return blocks


def tagged_note(note: object) -> bool:
    if not isinstance(note, str) or not note.startswith(MARKER):
        return False
    return True


def validate_existing(project: str, document: object) -> dict:
    if not isinstance(document, dict) or document.get("project") != project or not isinstance(document.get("notes"), dict):
        raise ValueError("existing master.json has invalid project or notes")
    for key, item in document["notes"].items():
        if not isinstance(item, dict) or not tagged_note(item.get("note")) or not re.fullmatch(r"[0-9a-f]{16}", str(item.get("id", ""))):
            raise ValueError(f"invalid saved note: {key}")
        match = re.search(r":([0-9a-f]{16})(?::chunk-[0-9]+-([0-9a-f]{16}))?$", key)
        expected = match.group(2) if match and match.group(2) else (match.group(1) if match else None)
        if expected != item["id"]:
            raise ValueError(f"saved note digest does not match key: {key}")
    return document


def request_router(url: str, block: list[dict]) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password or parsed.query or parsed.path not in {"", "/"} or parsed.fragment:
        raise ValueError("router must be a plain HTTP localhost URL without path, userinfo, query or fragment")
    payload = json.dumps({"block": block}, ensure_ascii=False, separators=(",", ":")).encode()
    host = parsed.hostname
    conn = http.client.HTTPConnection(host, parsed.port or 80, timeout=120)
    try:
        conn.request("POST", "/master/condense", payload, {"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read()
        if response.status != 200:
            raise RuntimeError(f"router returned HTTP {response.status}")
        result = json.loads(raw)
        digest = utils.block_id(block)
        if not isinstance(result, dict) or result.get("id") != digest or not tagged_note(result.get("note")) or not isinstance(result.get("prompt_hash"), str):
            raise ValueError("router response id, note marker, or prompt_hash is invalid")
        return result
    finally:
        conn.close()


def export_session(path: Path, project: str, ordered: list[tuple[str, str, str]]) -> None:
    from datetime import datetime, timezone
    session_id = hashlib.sha256(project.encode()).hexdigest()[:16]
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as existing:
                header = json.loads(next(existing))
            old_timestamp = header.get("timestamp")
            if isinstance(old_timestamp, str):
                datetime.fromisoformat(old_timestamp.replace("Z", "+00:00"))
                timestamp = old_timestamp
        except (OSError, StopIteration, json.JSONDecodeError, ValueError):
            pass
    rows = [{"type": "session", "version": 3, "id": session_id, "timestamp": timestamp, "cwd": project}]
    rows.append({"type": "session_info", "id": "master-info", "parentId": None, "timestamp": timestamp, "name": "master"})
    parent = "master-info"
    for key, digest, note in ordered:
        ident = "master-" + hashlib.sha256(key.encode()).hexdigest()[:20]
        rows.append({"type": "message", "id": ident, "parentId": parent, "timestamp": timestamp, "message": {"role": "user", "content": note}})
        parent = ident
    atomic_bytes(path, ("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)).encode())


def raw_sessions(raw_path: Path):
    with raw_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            envelope = json.loads(line)
            yield envelope["source"], envelope["entries"]


def iter_blocks(raw_path: Path):
    for source, entries in raw_sessions(raw_path):
        for index, block in enumerate(blocks_for(entries)):
            digest = utils.block_id(block)
            yield f"{source}#{index}:{digest}", source, digest, block


def parts_for(block: list[dict], digest: str):
    """Keep a finished note intact; split only oversized uncondensed input."""
    if utils.compacted_note(block) is not None or len(json.dumps({"block": block}, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_REQUEST:
        yield "", block
        return
    transcript = utils.block_transcript(block)
    if not transcript:
        raise ValueError("oversized block has no text to condense")
    count = (len(transcript) + CHUNK_CHARS - 1) // CHUNK_CHARS
    for i in range(count):
        chunk = transcript[i * CHUNK_CHARS:(i + 1) * CHUNK_CHARS]
        part = [{"role": "user", "content": f"Source block {digest}; chunk {i + 1}/{count}\n{chunk}"}]
        yield f":chunk-{i + 1}-{utils.block_id(part)}", part


def process(project: str, bucket: Path, output: Path, router: str, run: bool, limit: int | None) -> tuple[int, int]:
    output.mkdir(parents=True, exist_ok=True)
    # ponytail: one lock per project output; only revisit if concurrent writers become necessary.
    with (output / ".master.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("a master run is already in progress") from exc
        return _process(project, bucket, output, router, run, limit)


def _process(project: str, bucket: Path, output: Path, router: str, run: bool, limit: int | None) -> tuple[int, int]:
    raw_path = materialize(project, bucket, output)
    master_path = output / "master.json"
    if master_path.exists():
        master = validate_existing(project, json.loads(master_path.read_text(encoding="utf-8")))
    else:
        master = {"project": project, "notes": {}}
    total = already = 0
    for key, _source, _digest, _block in iter_blocks(raw_path):
        total += 1
        if key in master["notes"]:
            already += 1
    pending_count = total - already
    print(f"estimated blocks: {total}; already done: {already}; pending: {pending_count}")
    if not run:
        return already, pending_count
    calls = 0
    for key, source, digest, block in iter_blocks(raw_path):
        for suffix, part in parts_for(block, digest):
            subdigest = utils.block_id(part)
            part_key = key + suffix
            if part_key in master["notes"]:
                continue
            if len(part) == 1 and isinstance(part[0].get("content"), str) and part[0]["content"].startswith(MARKER):
                result_note, prompt_hash = part[0]["content"], ""
            else:
                if limit is not None and calls >= limit:
                    print(f"limit reached; {max(0, pending_count - calls)} original blocks remain resumable")
                    return already, max(0, pending_count - calls)
                result = request_router(router, part)
                result_note, prompt_hash = result["note"], result["prompt_hash"]
                calls += 1
            master["notes"][part_key] = {"source": source, "id": subdigest, "note": result_note, "prompt_hash": prompt_hash}
            atomic_json(master_path, master)
    ordered = []
    complete = True
    for key, _source, _digest, block in iter_blocks(raw_path):
        for suffix, _part in parts_for(block, _digest):
            note_key = key + suffix
            value = master["notes"].get(note_key)
            if value is None:
                complete = False
                break
            ordered.append((note_key, value["id"], value["note"]))
    if complete:
        export_session(output / "mastersession.jsonl", project, ordered)
    return already + calls, max(0, pending_count - calls)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("--session-root", default="/home/ubuntu/.pi/agent/sessions")
    parser.add_argument("--output", default=None)
    parser.add_argument("--router", default="http://127.0.0.1:4141")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        if args.limit is not None and args.limit < 0:
            raise ValueError("--limit must be nonnegative")
        if not os.path.isabs(args.project) or not Path(args.project).is_dir():
            raise ValueError("PROJECT must be an existing absolute directory")
        project = os.path.realpath(args.project)
        bucket = Path(args.session_root).expanduser().resolve() / encoded_project(project)
        output = Path(args.output).expanduser() if args.output else Path.home() / ".pi/agent/masters" / encoded_project(project)
        output = output.resolve()
        process(project, bucket, output, args.router, args.run, args.limit)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"master_session: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
