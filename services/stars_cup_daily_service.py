from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image

from event_hub import (
    DEFAULT_BACKGROUND,
    DEFAULT_LIVE_CACHE,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_ROSTER,
    export_rank_images,
    query_live_scores,
)
from services.bot_transport import (
    BotAttachment,
    BotReply,
    BotTransport,
    BotTransportError,
    SendReceipt,
)
from services.match_rank_image_service import validate_snapshot
from services.stars_cup_bot_service import (
    DEFAULT_LATEST_POINTER,
    LoadedStarsCupSnapshot,
    StarsCupSnapshotUnavailable,
    load_latest_stars_cup_snapshot,
)
from settings import LOCAL_TIMEZONE, TEMP_DIR


DEFAULT_STATE_ROOT = TEMP_DIR / "stars_cup_bot"
DEFAULT_RUN_STATE_ROOT = DEFAULT_STATE_ROOT / "runs"
DEFAULT_WORK_ROOT = DEFAULT_STATE_ROOT / "work"
DEFAULT_LOCK_PATH = DEFAULT_STATE_ROOT / "daily.lock"
EXPECTED_IMAGE_SIZE = (1920, 1080)
ARTIFACT_ORDER = ("total", "detail")


class StarsCupDailyError(RuntimeError):
    pass


class StarsCupDailyLocked(StarsCupDailyError):
    pass


class StarsCupDeliveryError(StarsCupDailyError):
    pass


class StarsCupDeliveryLocked(StarsCupDeliveryError):
    pass


@dataclass(frozen=True, slots=True)
class DailyRunResult:
    run_id: str
    published_at: datetime
    output_dir: Path
    snapshot_path: Path
    snapshot_sha256: str
    image_paths: dict[str, Path]
    image_sha256: dict[str, str]
    state_path: Path
    latest_pointer_path: Path
    query_summary: dict[str, Any]
    reused: bool = False
    receipts: tuple[SendReceipt, ...] = ()


def _coerce_run_time(run_time: datetime | None) -> datetime:
    value = run_time or datetime.now(LOCAL_TIMEZONE)
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TIMEZONE)
    return value.astimezone(LOCAL_TIMEZONE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StarsCupDailyError("状态文件不可读: {}".format(path)) from exc
    if not isinstance(value, dict):
        raise StarsCupDailyError("状态文件必须是 JSON object: {}".format(path))
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.{}.tmp".format(path.name, uuid4().hex))
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        "{}.{}.tmp".format(destination.name, uuid4().hex)
    )
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _exclusive_lock(path: Path, *, run_id: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    token = "{}:{}:{}".format(os.getpid(), run_id, uuid4().hex)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise StarsCupDailyLocked("群星杯每日任务已有实例运行") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
        yield
    finally:
        try:
            current = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeError):
            current = None
        if current == token:
            path.unlink(missing_ok=True)


def _next_output_dir(export_root: Path, run_time: datetime) -> Path:
    base_name = run_time.strftime("%Y%m%d_%H%M%S")
    candidate = export_root / base_name
    suffix = 2
    while candidate.exists():
        candidate = export_root / "{}_{:02d}".format(base_name, suffix)
        suffix += 1
    return candidate


def _artifact_path(raw_path: Any, *, output_dir: Path, label: str) -> Path:
    path = Path(str(raw_path or "")).expanduser().resolve()
    root = output_dir.expanduser().resolve()
    if not path.is_relative_to(root):
        raise StarsCupDailyError("{} 不在本次不可变输出目录中".format(label))
    if not path.is_file():
        raise StarsCupDailyError("{} 不存在".format(label))
    return path


def _validate_png(path: Path, label: str) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format != "PNG":
                raise StarsCupDailyError("{} 不是 PNG".format(label))
            if image.size != EXPECTED_IMAGE_SIZE:
                raise StarsCupDailyError(
                    "{} 尺寸错误: {}x{}".format(label, image.width, image.height)
                )
    except StarsCupDailyError:
        raise
    except Exception as exc:
        raise StarsCupDailyError("{} 无法验证".format(label)) from exc


def _validate_export_artifacts(
    paths: dict[str, str],
    *,
    output_dir: Path,
) -> tuple[Path, dict[str, Path]]:
    if not isinstance(paths, dict):
        raise StarsCupDailyError("榜图导出结果必须是映射")
    snapshot_path = _artifact_path(
        paths.get("snapshot"),
        output_dir=output_dir,
        label="榜图快照",
    )
    image_paths = {
        kind: _artifact_path(
            paths.get(kind),
            output_dir=output_dir,
            label="{}榜图".format("总" if kind == "total" else "明细"),
        )
        for kind in ARTIFACT_ORDER
    }
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        validate_snapshot(snapshot)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise StarsCupDailyError("榜图快照契约验证失败") from exc
    for kind in ARTIFACT_ORDER:
        _validate_png(image_paths[kind], kind)
    return snapshot_path, image_paths


def _relative_artifact(path: Path, export_root: Path) -> str:
    resolved = path.expanduser().resolve()
    root = export_root.expanduser().resolve()
    if not resolved.is_relative_to(root):
        raise StarsCupDailyError("发布产物不在导出根目录中")
    return resolved.relative_to(root).as_posix()


def _pointer_payload(
    *,
    run_id: str,
    published_at: datetime,
    snapshot_path: Path,
    snapshot_sha256: str,
    image_paths: dict[str, Path],
    image_sha256: dict[str, str],
    export_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "published_at": published_at.isoformat(),
        "snapshot": {
            "path": _relative_artifact(snapshot_path, export_root),
            "sha256": snapshot_sha256,
        },
        "images": {
            kind: {
                "path": _relative_artifact(image_paths[kind], export_root),
                "sha256": image_sha256[kind],
            }
            for kind in ARTIFACT_ORDER
        },
    }


def _result_from_loaded(
    loaded: LoadedStarsCupSnapshot,
    *,
    state_path: Path,
    latest_pointer_path: Path,
    query_summary: dict[str, Any],
    reused: bool,
) -> DailyRunResult:
    return DailyRunResult(
        run_id=loaded.run_id,
        published_at=loaded.published_at,
        output_dir=loaded.snapshot_path.parent,
        snapshot_path=loaded.snapshot_path,
        snapshot_sha256=loaded.snapshot_sha256,
        image_paths=loaded.image_paths,
        image_sha256=loaded.image_sha256,
        state_path=state_path,
        latest_pointer_path=latest_pointer_path,
        query_summary=query_summary,
        reused=reused,
    )


def run_stars_cup_daily_export(
    *,
    run_time: datetime | None = None,
    roster_path: Path = DEFAULT_ROSTER,
    live_cache_path: Path = DEFAULT_LIVE_CACHE,
    background_path: Path = DEFAULT_BACKGROUND,
    export_root: Path = DEFAULT_OUTPUT_ROOT,
    state_root: Path = DEFAULT_STATE_ROOT,
    latest_pointer_path: Path = DEFAULT_LATEST_POINTER,
    lock_path: Path | None = None,
    workers: int = 8,
    full: bool = False,
) -> DailyRunResult:
    effective_time = _coerce_run_time(run_time)
    run_id = effective_time.strftime("%Y%m%d")
    run_state_root = state_root / "runs"
    work_root = state_root / "work"
    state_path = run_state_root / "{}.json".format(run_id)
    effective_lock_path = lock_path or (state_root / "daily.lock")

    with _exclusive_lock(effective_lock_path, run_id=run_id):
        existing = _load_json_object(state_path)
        if existing.get("status") == "published":
            try:
                loaded = load_latest_stars_cup_snapshot(
                    state_path,
                    export_root=export_root,
                    current_time=effective_time,
                )
            except StarsCupSnapshotUnavailable:
                pass
            else:
                pointer = _pointer_payload(
                    run_id=loaded.run_id,
                    published_at=loaded.published_at,
                    snapshot_path=loaded.snapshot_path,
                    snapshot_sha256=loaded.snapshot_sha256,
                    image_paths=loaded.image_paths,
                    image_sha256=loaded.image_sha256,
                    export_root=export_root,
                )
                _atomic_write_json(latest_pointer_path, pointer)
                return _result_from_loaded(
                    loaded,
                    state_path=state_path,
                    latest_pointer_path=latest_pointer_path,
                    query_summary=existing.get("query_summary")
                    if isinstance(existing.get("query_summary"), dict)
                    else {},
                    reused=True,
                )

        output_dir = _next_output_dir(export_root, effective_time)
        work_dir = work_root / output_dir.name
        staged_cache_path = work_dir / "live_cache.json"
        base_state = {
            "schema_version": 1,
            "run_id": run_id,
            "run_time": effective_time.isoformat(),
            "output_dir": str(output_dir),
        }
        _atomic_write_json(state_path, {**base_state, "status": "querying"})
        try:
            work_dir.mkdir(parents=True, exist_ok=False)
            if live_cache_path.is_file():
                shutil.copy2(live_cache_path, staged_cache_path)
            query_summary = query_live_scores(
                roster_path,
                cache_path=staged_cache_path,
                workers=workers,
                full=full,
            )
            _atomic_write_json(
                state_path,
                {
                    **base_state,
                    "status": "queried",
                    "query_summary": query_summary,
                },
            )
            _atomic_write_json(
                state_path,
                {
                    **base_state,
                    "status": "rendering",
                    "query_summary": query_summary,
                },
            )
            exported = export_rank_images(
                roster_path,
                None,
                output_dir,
                background_path,
                None,
                source="live",
                live_cache_path=staged_cache_path,
            )
            snapshot_path, image_paths = _validate_export_artifacts(
                exported,
                output_dir=output_dir,
            )
            snapshot_hash = _sha256(snapshot_path)
            image_hashes = {
                kind: _sha256(image_paths[kind])
                for kind in ARTIFACT_ORDER
            }
            published_at = effective_time
            pointer = _pointer_payload(
                run_id=run_id,
                published_at=published_at,
                snapshot_path=snapshot_path,
                snapshot_sha256=snapshot_hash,
                image_paths=image_paths,
                image_sha256=image_hashes,
                export_root=export_root,
            )
            validated_state = {
                **base_state,
                **pointer,
                "status": "validated",
                "query_summary": query_summary,
            }
            _atomic_write_json(state_path, validated_state)
            _atomic_copy(staged_cache_path, live_cache_path)
            published_state = {
                **validated_state,
                "status": "published",
            }
            _atomic_write_json(state_path, published_state)
            _atomic_write_json(latest_pointer_path, pointer)
            return DailyRunResult(
                run_id=run_id,
                published_at=published_at,
                output_dir=output_dir.resolve(),
                snapshot_path=snapshot_path,
                snapshot_sha256=snapshot_hash,
                image_paths=image_paths,
                image_sha256=image_hashes,
                state_path=state_path,
                latest_pointer_path=latest_pointer_path,
                query_summary=query_summary,
            )
        except Exception as exc:
            failed_state = {
                **base_state,
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            _atomic_write_json(state_path, failed_state)
            if isinstance(exc, StarsCupDailyError):
                raise
            raise StarsCupDailyError("群星杯每日榜图生成失败: {}".format(exc)) from exc


def _receipt_payload(receipt: SendReceipt) -> dict[str, Any]:
    return {
        "transport": receipt.transport,
        "status": receipt.status,
        "platform_message_id": receipt.platform_message_id,
        "sent_at": receipt.sent_at.isoformat() if receipt.sent_at else None,
        "error_code": receipt.error_code,
        "retryable": receipt.retryable,
    }


def _target_hash(group_id: str) -> str:
    return hashlib.sha256(group_id.encode("utf-8")).hexdigest()


def _transport_name(transport: BotTransport) -> str:
    value = str(getattr(transport, "transport_name", "") or "").strip()
    return value or type(transport).__name__


def _delivery_receipt(
    *,
    transport_name: str,
    target_hash: str,
    status: str,
    error_code: str | None = None,
    retryable: bool = False,
) -> SendReceipt:
    return SendReceipt(
        transport=transport_name,
        target=target_hash,
        status=status,
        error_code=error_code,
        retryable=retryable,
    )


async def _deliver_stars_cup_daily_images_unlocked(
    result: DailyRunResult,
    transport: BotTransport,
    *,
    group_id: str,
    delivery_state_path: Path,
    dry_run: bool = True,
    attempt_time: datetime | None = None,
) -> DailyRunResult:
    target = str(group_id or "").strip()
    if not target:
        raise StarsCupDeliveryError("群目标不能为空")
    now = _coerce_run_time(attempt_time)
    transport_name = _transport_name(transport)
    target_hash = _target_hash(target)

    for kind in ARTIFACT_ORDER:
        path = result.image_paths.get(kind)
        expected = result.image_sha256.get(kind)
        if path is None or not path.is_file() or not expected or _sha256(path) != expected:
            raise StarsCupDeliveryError("{} 榜图哈希验证失败".format(kind))
        _validate_png(path, kind)

    state = _load_json_object(delivery_state_path)
    if state:
        if state.get("run_id") != result.run_id:
            raise StarsCupDeliveryError("投递状态属于其他运行")
        if state.get("target_hash") != target_hash:
            raise StarsCupDeliveryError("投递状态属于其他群目标")
        if state.get("transport") != transport_name:
            raise StarsCupDeliveryError("投递状态属于其他传输实现")
    else:
        state = {
            "schema_version": 1,
            "run_id": result.run_id,
            "transport": transport_name,
            "target_hash": target_hash,
            "images": {},
        }
    image_states = state.setdefault("images", {})
    if not isinstance(image_states, dict):
        raise StarsCupDeliveryError("投递图片状态无效")

    receipts: list[SendReceipt] = []
    for kind in ARTIFACT_ORDER:
        entry = image_states.get(kind)
        if not isinstance(entry, dict):
            entry = {
                "sha256": result.image_sha256[kind],
                "status": "pending",
            }
            image_states[kind] = entry
        if entry.get("sha256") != result.image_sha256[kind]:
            raise StarsCupDeliveryError("{} 投递状态哈希不匹配".format(kind))
        status = entry.get("status")
        if status == "sent":
            continue
        if status == "sending":
            entry["status"] = "unknown"
            entry["resolved_at"] = now.isoformat()
            entry["reason"] = "previous attempt ended before a durable receipt"
            _atomic_write_json(delivery_state_path, state)
            receipts.append(
                _delivery_receipt(
                    transport_name=transport_name,
                    target_hash=target_hash,
                    status="unknown",
                    error_code="delivery_receipt_missing",
                )
            )
            break
        if status in {"unknown", "failed_terminal"}:
            receipts.append(
                _delivery_receipt(
                    transport_name=transport_name,
                    target_hash=target_hash,
                    status="unknown" if status == "unknown" else "failed",
                    error_code=entry.get("error_code"),
                    retryable=False,
                )
            )
            break
        if dry_run:
            receipts.append(
                _delivery_receipt(
                    transport_name=transport_name,
                    target_hash=target_hash,
                    status="dry_run",
                )
            )
            continue

        entry["status"] = "sending"
        entry["attempted_at"] = now.isoformat()
        _atomic_write_json(delivery_state_path, state)
        reply = BotReply(
            attachments=(
                BotAttachment(
                    kind="image",
                    name=result.image_paths[kind].name,
                    local_path=result.image_paths[kind],
                ),
            )
        )
        try:
            sent_receipts = tuple(await transport.send_group(target, reply))
        except BotTransportError as exc:
            receipt = _delivery_receipt(
                transport_name=transport_name,
                target_hash=target_hash,
                status="failed",
                error_code=exc.code,
                retryable=exc.retryable,
            )
            entry["status"] = (
                "failed_retryable" if exc.retryable else "failed_terminal"
            )
            entry["error_code"] = exc.code
            entry["retryable"] = exc.retryable
            entry["receipt"] = _receipt_payload(receipt)
            _atomic_write_json(delivery_state_path, state)
            receipts.append(receipt)
            break
        except Exception:
            receipt = _delivery_receipt(
                transport_name=transport_name,
                target_hash=target_hash,
                status="unknown",
                error_code="transport_exception",
            )
            entry["status"] = "unknown"
            entry["error_code"] = "transport_exception"
            entry["retryable"] = False
            entry["receipt"] = _receipt_payload(receipt)
            _atomic_write_json(delivery_state_path, state)
            receipts.append(receipt)
            break

        if not sent_receipts:
            receipt = _delivery_receipt(
                transport_name=transport_name,
                target_hash=target_hash,
                status="unknown",
                error_code="empty_receipt",
            )
            entry["status"] = "unknown"
            entry["receipt"] = _receipt_payload(receipt)
            _atomic_write_json(delivery_state_path, state)
            receipts.append(receipt)
            break
        receipt = sent_receipts[-1]
        receipts.extend(sent_receipts)
        if receipt.status == "sent":
            entry["status"] = "sent"
            entry["receipt"] = _receipt_payload(receipt)
            entry["sent_at"] = (
                receipt.sent_at.isoformat()
                if receipt.sent_at
                else now.isoformat()
            )
            _atomic_write_json(delivery_state_path, state)
            continue
        entry["status"] = (
            "failed_retryable"
            if receipt.status == "failed" and receipt.retryable
            else "failed_terminal"
            if receipt.status == "failed"
            else "unknown"
        )
        entry["receipt"] = _receipt_payload(receipt)
        _atomic_write_json(delivery_state_path, state)
        break

    return replace(result, receipts=tuple(receipts))


async def deliver_stars_cup_daily_images(
    result: DailyRunResult,
    transport: BotTransport,
    *,
    group_id: str,
    delivery_state_path: Path,
    dry_run: bool = True,
    attempt_time: datetime | None = None,
) -> DailyRunResult:
    delivery_lock = delivery_state_path.with_name(
        "{}.lock".format(delivery_state_path.name)
    )
    try:
        with _exclusive_lock(delivery_lock, run_id=result.run_id):
            return await _deliver_stars_cup_daily_images_unlocked(
                result,
                transport,
                group_id=group_id,
                delivery_state_path=delivery_state_path,
                dry_run=dry_run,
                attempt_time=attempt_time,
            )
    except StarsCupDailyLocked as exc:
        raise StarsCupDeliveryLocked("群星杯榜图投递已有实例运行") from exc
