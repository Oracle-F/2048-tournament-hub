import argparse
from datetime import datetime, timedelta, timezone

from services.verse_adapter import inspect_recent_game_fields


def parse_args():
    parser = argparse.ArgumentParser(description="Probe 2048verse API fields for one user")
    parser.add_argument("--username", required=True)
    parser.add_argument("--variant", default="4x4")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--days", type=int, default=3650)
    return parser.parse_args()


def main():
    args = parse_args()
    since = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=args.days)
    result = inspect_recent_game_fields(args.username, args.variant, since, max_pages=args.pages)
    print("username={}".format(args.username))
    print("variant={}".format(args.variant))
    print("game_count={}".format(result["game_count"]))
    print("keys:")
    for key, count in sorted(result["key_counts"].items(), key=lambda kv: kv[1], reverse=True):
        print("- {} ({})".format(key, count))
    if result["replay_like_keys"]:
        print("replay_like_keys={}".format(", ".join(result["replay_like_keys"])))
    else:
        print("replay_like_keys=(none)")


if __name__ == "__main__":
    main()
