from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, main


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot_official_qq.relationship_state import (  # noqa: E402
    OfficialRelationshipStateError,
    OfficialTargetState,
    load_official_target_state,
    observe_official_relationship_event,
    official_relationship_event_to_update,
    official_target_send_allowed,
)
from services.bot_transport import BotContractError  # noqa: E402
from settings import LOCAL_TIMEZONE  # noqa: E402


OBSERVED_AT = datetime(
    2026,
    7,
    27,
    10,
    30,
    tzinfo=LOCAL_TIMEZONE,
)


class OfficialRelationshipMappingTests(TestCase):
    def test_group_events_use_group_openid_and_expected_states(self):
        mappings = {
            "GROUP_ADD_ROBOT": "joined",
            "GROUP_DEL_ROBOT": "removed",
            "GROUP_MSG_REJECT": "rejected",
            "GROUP_MSG_RECEIVE": "receivable",
        }
        for event_type, expected_status in mappings.items():
            with self.subTest(event_type=event_type):
                update = official_relationship_event_to_update(
                    event_type,
                    SimpleNamespace(group_openid="opaque-group"),
                    observed_at=OBSERVED_AT,
                )
                self.assertEqual(update.subject_kind, "group")
                self.assertEqual(update.subject_id, "opaque-group")
                self.assertEqual(update.status, expected_status)
                self.assertEqual(update.observed_at, OBSERVED_AT)

    def test_c2c_events_use_openid_and_expected_states(self):
        mappings = {
            "C2C_MSG_REJECT": "rejected",
            "C2C_MSG_RECEIVE": "receivable",
        }
        for event_type, expected_status in mappings.items():
            with self.subTest(event_type=event_type):
                update = official_relationship_event_to_update(
                    event_type,
                    SimpleNamespace(openid="opaque-user"),
                    observed_at=OBSERVED_AT,
                )
                self.assertEqual(update.subject_kind, "c2c")
                self.assertEqual(update.subject_id, "opaque-user")
                self.assertEqual(update.status, expected_status)

    def test_unsupported_event_is_ignored_and_missing_id_is_rejected(self):
        self.assertIsNone(
            official_relationship_event_to_update(
                "GROUP_AT_MESSAGE_CREATE",
                object(),
                observed_at=OBSERVED_AT,
            )
        )
        with self.assertRaises(BotContractError):
            official_relationship_event_to_update(
                "GROUP_ADD_ROBOT",
                SimpleNamespace(group_openid=""),
                observed_at=OBSERVED_AT,
            )


class OfficialTargetStateTests(TestCase):
    def test_missing_state_is_unknown_and_non_blocking(self):
        with TemporaryDirectory() as temp_dir:
            state = load_official_target_state(
                Path(temp_dir) / "missing.json",
                "opaque-target",
            )
        self.assertEqual(state.status, "unknown")
        self.assertEqual(
            official_target_send_allowed(state),
            (True, "relationship_unknown"),
        )

    def test_target_update_is_atomic_redacted_and_round_trips(self):
        target = "opaque-private-target"
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "target.json"
            update = official_relationship_event_to_update(
                "GROUP_MSG_REJECT",
                SimpleNamespace(group_openid=target),
                observed_at=OBSERVED_AT,
            )
            observed = observe_official_relationship_event(
                update,
                target,
                path,
            )
            rendered = path.read_text(encoding="utf-8")
            loaded = load_official_target_state(path, target)

        self.assertEqual(observed.status, "rejected")
        self.assertEqual(loaded, observed)
        self.assertNotIn(target, rendered)
        self.assertEqual(json.loads(rendered)["schema_version"], 1)
        self.assertEqual(
            official_target_send_allowed(loaded),
            (False, "relationship_rejected"),
        )

    def test_non_target_and_c2c_updates_do_not_write_target_state(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "target.json"
            other_group = official_relationship_event_to_update(
                "GROUP_DEL_ROBOT",
                SimpleNamespace(group_openid="other-group"),
                observed_at=OBSERVED_AT,
            )
            c2c = official_relationship_event_to_update(
                "C2C_MSG_REJECT",
                SimpleNamespace(openid="opaque-user"),
                observed_at=OBSERVED_AT,
            )
            self.assertIsNone(
                observe_official_relationship_event(
                    other_group,
                    "configured-group",
                    path,
                )
            )
            self.assertIsNone(
                observe_official_relationship_event(
                    c2c,
                    "configured-group",
                    path,
                )
            )
            self.assertFalse(path.exists())

    def test_add_and_receive_clear_only_relationship_block(self):
        states = {
            "joined": "relationship_joined",
            "receivable": "relationship_receivable",
        }
        for status, expected_reason in states.items():
            state = OfficialTargetState(
                target_hash="a" * 64,
                status=status,
                event_type="GROUP_ADD_ROBOT",
                observed_at=OBSERVED_AT,
            )
            self.assertEqual(
                official_target_send_allowed(state),
                (True, expected_reason),
            )
        removed = OfficialTargetState(
            target_hash="a" * 64,
            status="removed",
            event_type="GROUP_DEL_ROBOT",
            observed_at=OBSERVED_AT,
        )
        self.assertEqual(
            official_target_send_allowed(removed),
            (False, "robot_removed"),
        )

    def test_corrupt_or_wrong_target_state_is_rejected(self):
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "target.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(OfficialRelationshipStateError) as corrupt:
                load_official_target_state(path, "target")
            self.assertEqual(corrupt.exception.code, "state_unreadable")

            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "target_hash": "0" * 64,
                        "status": "joined",
                        "event_type": "GROUP_ADD_ROBOT",
                        "observed_at": OBSERVED_AT.isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(OfficialRelationshipStateError) as mismatch:
                load_official_target_state(path, "different-target")
            self.assertEqual(mismatch.exception.code, "state_target_mismatch")


if __name__ == "__main__":
    main()
