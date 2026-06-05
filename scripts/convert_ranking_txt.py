import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from importers.legacy_ranking_txt_parser import convert_legacy_ranking_txt, dumps_payload
from settings import ORGANIZER_EXPORTS_DIR


def parse_args():
    parser = argparse.ArgumentParser(description="Convert a legacy ranking txt file into organizer ranking json")
    parser.add_argument("input_path", help="Path to legacy 总排名.txt")
    parser.add_argument("--output", help="Output JSON path")
    return parser.parse_args()


def main():
    args = parse_args()
    input_path = Path(args.input_path).expanduser().resolve()
    payload = convert_legacy_ranking_txt(input_path)
    event_id = payload["event"]["event_id"]

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
    else:
        output_path = ORGANIZER_EXPORTS_DIR / event_id / "总排名.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(dumps_payload(payload), encoding="utf-8")
    print("Converted {} -> {}".format(input_path, output_path))


if __name__ == "__main__":
    main()
