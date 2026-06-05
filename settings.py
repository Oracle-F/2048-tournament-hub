import os
from datetime import timedelta, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"
DOCS_DIR = ROOT_DIR / "docs"
TEMP_DIR = DATA_DIR / "tmp"
PRODUCTION_DATABASE_PATH = DATA_DIR / "tournament_hub.sqlite3"
TEST_DATABASE_PATH = DATA_DIR / "testing.db"
EXPORTS_DIR = ROOT_DIR.parent / "比赛导出"
ORGANIZER_EXPORTS_DIR = EXPORTS_DIR / "主办方"
INTERIM_EXPORTS_DIR = EXPORTS_DIR / "中途排名"

def _resolve_database_path():
    override = (os.getenv("TOURNAMENT_HUB_DB_PATH") or os.getenv("TOURNAMENT_HUB_TEST_DB_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    return PRODUCTION_DATABASE_PATH


DATABASE_PATH = _resolve_database_path()
BOT_ADMINS_PATH = CONFIG_DIR / "bot_admins.json"
BOT_UPLOAD_TEMP_DIR = TEMP_DIR / "bot_uploads"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LOCAL_TIMEZONE = timezone(timedelta(hours=8))

DEFAULT_IMPORT_SOURCE = "legacy_export_import"
DEFAULT_IMPORT_STATUS = "archived"
DEFAULT_IMPORT_IS_OFFICIAL = False
DEFAULT_IMPORT_IS_RATED = False
DEFAULT_IMPORT_TAGS = ["legacy_import", "unrated"]

VERSE_BACKEND_BASE_URL = "https://backend.2048verse.com"
VERSE_API_BASE_URL = VERSE_BACKEND_BASE_URL + "/leaderboard/user"
VERSE_LEADERBOARD_API_BASE_URL = VERSE_BACKEND_BASE_URL + "/leaderboard/all"
VERSE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.124 Safari/537.36"
)
VERSE_REQUEST_TIMEOUT = 15
