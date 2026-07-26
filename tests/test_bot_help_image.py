from __future__ import annotations

import sys
import shutil
from pathlib import Path
from unittest import TestCase, main

from PIL import Image, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from services import bot_private_service as bot  # noqa: E402


class BotHelpImageTests(TestCase):
    def test_help_renderer_uses_a_cjk_capable_project_font(self):
        font = bot._help_font(24)

        self.assertIsInstance(font, ImageFont.FreeTypeFont)
        self.assertGreater(font.getlength("群星杯查分"), 0)

    def test_group_help_with_stars_cup_commands_renders_a_real_png(self):
        help_dir = PROJECT_ROOT / "data" / "tmp" / "test_help_render"
        image_path = help_dir / "group_help_stars_cup.png"
        shutil.rmtree(help_dir, ignore_errors=True)
        try:
            sections = [
                {
                    "title": "比赛功能（需@bot）",
                    "items": [
                        {"cmd": "@bot 群星杯", "note": "查看群星杯队榜/队伍/个人成绩"},
                        {"cmd": "@bot 群星杯 A", "note": "查看指定队伍成绩"},
                    ],
                }
            ]
            self.assertTrue(
                bot._build_help_image(
                    image_path,
                    "QQ Bot 群星杯查分",
                    sections,
                )
            )
            with Image.open(image_path) as image:
                self.assertEqual(image.width, 900)
                self.assertGreater(image.height, 400)
                self.assertEqual(image.mode, "RGB")
        finally:
            shutil.rmtree(help_dir, ignore_errors=True)

    def test_existing_help_images_are_reused_without_rebuild(self):
        original_help_dir = bot.HELP_IMAGE_DIR
        original_build = bot._build_help_image
        original_enabled = bot.BOT_HELP_IMAGE_ENABLED
        help_dir = PROJECT_ROOT / "data" / "tmp" / "test_help_images"
        shutil.rmtree(help_dir, ignore_errors=True)
        try:
            help_dir.mkdir(parents=True, exist_ok=True)
            for file_name in ("player_help.png", "group_help_stars_cup.png", "admin_help.png"):
                (help_dir / file_name).write_bytes(b"existing image")

            bot.HELP_IMAGE_DIR = help_dir
            bot.BOT_HELP_IMAGE_ENABLED = True
            bot._build_help_image = lambda *args, **kwargs: self.fail("existing help image should be reused")

            self.assertEqual(
                "[CQ:image,file={}]".format((help_dir / "player_help.png").as_uri()),
                bot._help_image_cq(mode="player"),
            )
            self.assertEqual(
                "[CQ:image,file={}]".format((help_dir / "group_help_stars_cup.png").as_uri()),
                bot._help_image_cq(mode="group"),
            )
            self.assertEqual(
                "[CQ:image,file={}]".format((help_dir / "admin_help.png").as_uri()),
                bot._help_image_cq(mode="admin_full"),
            )
        finally:
            bot.HELP_IMAGE_DIR = original_help_dir
            bot._build_help_image = original_build
            bot.BOT_HELP_IMAGE_ENABLED = original_enabled
            shutil.rmtree(help_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
