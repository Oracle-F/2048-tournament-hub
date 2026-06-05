import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from services.ingest_service import finish_sync_run, ingest_organizer_json_file, record_sync_run_start
from settings import DATABASE_PATH, ORGANIZER_EXPORTS_DIR


def discover_ranking_json_files():
    if not ORGANIZER_EXPORTS_DIR.exists():
        return []
    return sorted(ORGANIZER_EXPORTS_DIR.rglob("总排名.json"))


def main():
    files = discover_ranking_json_files()
    if not files:
        print("No organizer ranking JSON files found under: {}".format(ORGANIZER_EXPORTS_DIR))
        return

    connection = connect(DATABASE_PATH)
    sync_run_id = record_sync_run_start(
        connection,
        source_type="organizer_json_import",
        metadata={"root": str(ORGANIZER_EXPORTS_DIR)},
    )
    connection.commit()

    seen_count = 0
    inserted_count = 0
    updated_count = 0

    try:
        for file_path in files:
            seen_count += 1
            with transaction(connection):
                result = ingest_organizer_json_file(connection, file_path)
            inserted_count += result["result_count"]
            print(
                "Imported {} -> event_id={}, results={}, new_players={}".format(
                    file_path,
                    result["event_id"],
                    result["result_count"],
                    result["new_players"],
                )
            )

        finish_sync_run(connection, sync_run_id, "completed", seen_count, inserted_count, updated_count)
        connection.commit()
        print("Import completed. files={}, results={}".format(seen_count, inserted_count))
    except Exception as error:
        finish_sync_run(connection, sync_run_id, "failed", seen_count, inserted_count, updated_count, str(error))
        connection.commit()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()

