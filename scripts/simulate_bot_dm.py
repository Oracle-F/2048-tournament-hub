import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False

load_dotenv(ROOT_DIR / ".env")

from db import connect, initialize_schema, transaction
from services.bot_private_service import handle_private_message
from settings import DATABASE_PATH


def build_parser():
    parser = argparse.ArgumentParser(description="Simulate one private bot DM command")
    parser.add_argument("--bot-platform", default="qq")
    parser.add_argument("--bot-user-id", required=True)
    parser.add_argument("message", nargs="+", help="Private message text")
    return parser


def main():
    args = build_parser().parse_args()
    message = " ".join(args.message)
    connection = connect(DATABASE_PATH)
    initialize_schema(connection)
    try:
        with transaction(connection):
            reply = handle_private_message(
                connection,
                bot_platform=args.bot_platform,
                bot_user_id=args.bot_user_id,
                text=message,
            )
        print(reply)
    except Exception as exc:
        print("ERROR: {}".format(exc))


if __name__ == "__main__":
    main()
