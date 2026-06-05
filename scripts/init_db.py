import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bootstrap import bootstrap_all
from db import connect, ensure_parent_dir, initialize_schema, transaction
from settings import DATABASE_PATH


def main():
    ensure_parent_dir(DATABASE_PATH)
    connection = connect(DATABASE_PATH)
    with transaction(connection):
        initialize_schema(connection)
        bootstrap_all(connection)
    connection.close()
    print("Database initialized: {}".format(DATABASE_PATH))


if __name__ == "__main__":
    main()

