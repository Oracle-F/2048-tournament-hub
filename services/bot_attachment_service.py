import os
import shutil
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname, urlopen

from settings import BOT_UPLOAD_TEMP_DIR


def _sanitize_filename(name, fallback="upload.bin"):
    raw = (name or "").strip()
    if not raw:
        raw = fallback
    safe = "".join(char if char.isalnum() or char in {".", "-", "_"} else "_" for char in raw)
    return safe or fallback


def _extract_candidate_value(data, *keys):
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def extract_file_segments(message_segments):
    results = []
    for segment in message_segments or []:
        segment_type = str(segment.get("type") or "").lower()
        data = segment.get("data") or {}
        if segment_type not in {"file", "record", "video"} and not any(
            key in data for key in ("path", "file", "url", "file_url", "local_path")
        ):
            continue
        results.append({"type": segment_type, "data": dict(data)})
    return results


def _copy_existing_local_file(local_value):
    source_text = str(local_value or "").strip()
    if not source_text:
        return None

    parsed = urlparse(source_text)
    if parsed.scheme == "file":
        local_path = url2pathname(unquote(parsed.path or ""))
        if local_path.startswith("/") and len(local_path) >= 3 and local_path[2] == ":":
            # Windows file URI: /C:/path -> C:/path
            local_path = local_path[1:]
        source = Path(local_path).expanduser()
    else:
        source = Path(source_text).expanduser()

    if not source.exists() or not source.is_file():
        return None
    BOT_UPLOAD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    target = BOT_UPLOAD_TEMP_DIR / _sanitize_filename(source.name)
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while target.exists():
            target = BOT_UPLOAD_TEMP_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
    shutil.copy2(source, target)
    return target.resolve()


def _download_to_temp(url, filename_hint=None):
    BOT_UPLOAD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    filename = filename_hint or Path(parsed.path).name or "upload.bin"
    target = BOT_UPLOAD_TEMP_DIR / _sanitize_filename(filename)
    if target.exists():
        stem = target.stem
        suffix = target.suffix
        counter = 1
        while target.exists():
            target = BOT_UPLOAD_TEMP_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
    with urlopen(url, timeout=20) as response, target.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    return target.resolve()


def materialize_segment_file(segment):
    data = segment.get("data") or {}
    local_value = _extract_candidate_value(data, "path", "local_path", "file")
    if local_value:
        local_text = str(local_value).strip()
        parsed_local = urlparse(local_text)
        if parsed_local.scheme in {"http", "https"}:
            filename_hint = _extract_candidate_value(data, "name", "file_name", "filename")
            return _download_to_temp(local_text, filename_hint=filename_hint)
        copied = _copy_existing_local_file(local_value)
        if copied is not None:
            return copied

    url = _extract_candidate_value(data, "url", "file_url")
    if url:
        copied = _copy_existing_local_file(url)
        if copied is not None:
            return copied
        parsed_url = urlparse(str(url).strip())
        if parsed_url.scheme not in {"http", "https"}:
            raise ValueError("Unsupported file url scheme: {}".format(parsed_url.scheme or "(empty)"))
        filename_hint = _extract_candidate_value(data, "name", "file_name", "filename")
        return _download_to_temp(url, filename_hint=filename_hint)

    available_keys = ",".join(sorted(str(key) for key in data.keys()))
    raise ValueError("Unable to access the uploaded file from the message event (keys={})".format(available_keys))


def describe_segment_file(segment):
    data = segment.get("data") or {}
    name = _extract_candidate_value(data, "name", "file_name", "filename")
    if name:
        return os.path.basename(str(name))
    local_value = _extract_candidate_value(data, "path", "local_path", "file")
    if local_value:
        return Path(str(local_value)).name
    url = _extract_candidate_value(data, "url", "file_url")
    if url:
        return Path(urlparse(url).path).name or "uploaded file"
    return "uploaded file"
