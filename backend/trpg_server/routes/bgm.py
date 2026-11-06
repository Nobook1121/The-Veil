"""服务端背景音乐库。

内置曲目位于 ``data/assets/bgm``，发布包不随包分发（版权与体积考虑）；目录缺省时
内置曲库为空，不影响服务启动。管理员上传的音乐保存在运行时目录
（``data/runtime/bgm``）。房规只保存站内相对地址（``/assets/bgm/library/<文件名>``），
因此同一首本地音乐不需要重复填写完整域名，也不会因为外链失效而静默无声。
"""

from pathlib import Path
from uuid import uuid4

from flask import Blueprint, current_app, request, send_from_directory, session

from trpg_server.responses import error_response, success_response
from trpg_server.security import normalize_filename
from trpg_server.settings import BGM_LIBRARY_DIR, BUILTIN_BGM_DIR

bp = Blueprint("bgm", __name__)

AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "ogg", "oga", "wav", "flac", "webm", "opus"}
BGM_UPLOAD_MAX_BYTES = 30 * 1024 * 1024

_BUILTIN_URL_PREFIX = "/assets/bgm/builtin/"
_LIBRARY_URL_PREFIX = "/assets/bgm/library/"
# 文件名与列表展示名之间用双下划线分隔，避免把过长原始名直接当作存储名。
_NAME_SEPARATOR = "__"


def _current_role() -> str:
    return str(session.get("role") or "USER").upper()


def _is_admin() -> bool:
    return _current_role() in {"ADMIN", "OWNER"}


def _builtin_dir() -> Path:
    return Path(current_app.config.get("BUILTIN_BGM_DIR", BUILTIN_BGM_DIR))


def _library_dir() -> Path:
    return Path(current_app.config.get("BGM_LIBRARY_DIR", BGM_LIBRARY_DIR))


def _is_audio_name(filename: str) -> bool:
    suffix = Path(filename).suffix.lstrip(".").lower()
    return suffix in AUDIO_EXTENSIONS


def _display_name(stored_name: str) -> str:
    stem = Path(stored_name).stem
    if _NAME_SEPARATOR in stem:
        stem = stem.split(_NAME_SEPARATOR, 1)[1]
    return stem.strip() or stem


def _audio_signature_matches(raw: bytes, suffix: str) -> bool:
    """按扩展名校验常见音频魔数，防止把任意文件改名后当作音乐上传。"""

    suffix = suffix.lower()
    if suffix == "mp3":
        return raw[:3] == b"ID3" or raw[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}
    if suffix in {"ogg", "oga", "opus"}:
        return raw[:4] == b"OggS"
    if suffix == "wav":
        return raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    if suffix == "flac":
        return raw[:4] == b"fLaC"
    if suffix in {"m4a", "aac"}:
        return raw[4:8] == b"ftyp"
    # webm 等容器格式没有统一短签名，交给浏览器解码判断。
    return True


def _collect_tracks(directory: Path, url_prefix: str, builtin: bool) -> list[dict]:
    if not directory.is_dir():
        return []
    tracks = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or not _is_audio_name(path.name):
            continue
        tracks.append(
            {
                "url": f"{url_prefix}{path.name}",
                "name": path.name if builtin else _display_name(path.name),
                "builtin": builtin,
                "size": path.stat().st_size,
            }
        )
    return tracks


@bp.route("/api/bgm/tracks", methods=["GET"])
def list_bgm_tracks():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    tracks = [
        *_collect_tracks(_builtin_dir(), _BUILTIN_URL_PREFIX, True),
        *_collect_tracks(_library_dir(), _LIBRARY_URL_PREFIX, False),
    ]
    return success_response({"tracks": tracks, "can_manage": _is_admin()})


@bp.route("/api/bgm/tracks", methods=["POST"])
def upload_bgm_track():
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _is_admin():
        return error_response("Only administrators can manage the music library", 403, "Forbidden")

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return error_response("Please provide a file", 400, "No file")
    if not _is_audio_name(uploaded.filename):
        return error_response("Unsupported audio file type", 400, "Invalid file type")

    raw = uploaded.read(BGM_UPLOAD_MAX_BYTES + 1)
    if len(raw) > BGM_UPLOAD_MAX_BYTES:
        return error_response("File is too large", 413, "File too large")
    suffix = Path(uploaded.filename).suffix.lstrip(".").lower()
    if not _audio_signature_matches(raw, suffix):
        return error_response("Invalid audio file", 400, "Invalid file content")

    display_name = normalize_filename(Path(uploaded.filename).stem, "bgm")
    stored_name = f"{uuid4().hex[:12]}{_NAME_SEPARATOR}{display_name}.{suffix}"
    library_dir = _library_dir()
    library_dir.mkdir(parents=True, exist_ok=True)
    (library_dir / stored_name).write_bytes(raw)
    return success_response(
        {
            "url": f"{_LIBRARY_URL_PREFIX}{stored_name}",
            "name": display_name,
            "builtin": False,
            "size": len(raw),
        },
        "Music uploaded successfully",
        201,
    )


@bp.route("/api/bgm/tracks/<path:filename>", methods=["DELETE"])
def delete_bgm_track(filename):
    if "user_id" not in session:
        return error_response("Please login first", 401, "Not logged in")
    if not _is_admin():
        return error_response("Only administrators can manage the music library", 403, "Forbidden")
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename or not _is_audio_name(safe_name):
        return error_response("Invalid file name", 400, "Invalid file name")
    target = _library_dir() / safe_name
    if not target.is_file():
        return error_response("Track not found", 404, "Not found")
    target.unlink()
    return success_response({"filename": safe_name}, "Music deleted successfully")


@bp.route("/assets/bgm/builtin/<path:filename>")
def serve_builtin_bgm(filename):
    response = send_from_directory(_builtin_dir(), filename)
    response.headers["Cache-Control"] = "private, max-age=604800"
    return response


@bp.route("/assets/bgm/library/<path:filename>")
def serve_library_bgm(filename):
    response = send_from_directory(_library_dir(), filename)
    response.headers["Cache-Control"] = "no-cache"
    return response
