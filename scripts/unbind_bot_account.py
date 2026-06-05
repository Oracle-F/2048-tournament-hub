import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from db import connect, transaction
from services.bot_admin_service import write_audit_log
from services.bot_binding_service import deactivate_bot_binding, find_active_bot_binding
from settings import DATABASE_PATH


def parse_args():
    parser = argparse.ArgumentParser(description="Manually unbind one QQ <-> Verse account relation")
    parser.add_argument("--bot-platform", default="qq", help="Bot platform code, default qq")
    parser.add_argument("--bot-user-id", help="QQ user id bound to the Verse account")
    parser.add_argument("--user", help="Verse account key to unbind")
    parser.add_argument("--platform", default="2048verse", help="Game platform code, default 2048verse")
    parser.add_argument("--reason", default="manual unbind in hub CLI", help="Audit reason")
    args = parser.parse_args()
    if not args.bot_user_id and not args.user:
        parser.error("Either --bot-user-id or --user is required.")
    return args


def main():
    args = parse_args()
    if not DATABASE_PATH.exists():
        print("Database file not found: {}".format(DATABASE_PATH))
        print("Run scripts\\init_db.py first.")
        return

    connection = connect(DATABASE_PATH)
    try:
        with transaction(connection):
            binding = find_active_bot_binding(
                connection,
                bot_platform=args.bot_platform,
                bot_user_id=args.bot_user_id,
                game_platform=args.platform,
                account_key=args.user,
            )
            if binding is None:
                print("No active binding found.")
                return
            before = dict(binding)
            deactivate_bot_binding(connection, binding_id=binding["id"])
            after = dict(binding)
            after["is_active"] = 0
            write_audit_log(
                connection,
                actor_type="cli",
                actor_id="unbind_bot_account",
                action_type="unbind_bot_account",
                target_table="bot_account_bindings",
                target_id=binding["id"],
                reason=args.reason,
                before=before,
                after=after,
            )
        print(
            "Unbound {game_platform} account {account_key} from bot_user_id={bot_user_id}".format(
                game_platform=binding["game_platform"],
                account_key=binding["account_key"],
                bot_user_id=binding["bot_user_id"],
            )
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
