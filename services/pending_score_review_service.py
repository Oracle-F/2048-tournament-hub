import json
from datetime import datetime

from services.ingest_service import ensure_player
from services.raw_score_import_service import add_manual_score, lookup_event


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _json_or_none(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _parse_json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def submit_pending_score(
    connection,
    event_code,
    *,
    username,
    display_name=None,
    submitter_platform="qqbot",
    submitter_account=None,
    source_record_id=None,
    started_at=None,
    ended_at=None,
    raw_score=None,
    final_score=None,
    competition_score=None,
    primary_time_ms=None,
    target_tile_value=None,
    score_before_target=None,
    evidence=None,
    payload=None,
):
    if ended_at is None:
        raise ValueError("ended_at is required")
    if raw_score is None and final_score is None and competition_score is None and primary_time_ms is None:
        raise ValueError("At least one score field is required")
    event = lookup_event(connection, event_code)
    resolved_display_name = display_name or username
    player_id = None
    if username:
        player_id, _inserted = ensure_player(
            connection,
            display_name=resolved_display_name,
            username=username,
            platform_id=event["platform_id"],
        )

    submitted_at = now_iso()
    cursor = connection.execute(
        """
        INSERT INTO pending_score_submissions (
            event_id, player_id, submitter_platform, submitter_account, display_name,
            source_record_id, started_at, ended_at, raw_score, final_score,
            competition_score, primary_time_ms, target_tile_value, score_before_target,
            evidence_json, payload_json, status, submitted_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
            event["id"],
            player_id,
            submitter_platform,
            submitter_account,
            resolved_display_name,
            source_record_id,
            started_at,
            ended_at,
            raw_score,
            final_score,
            competition_score,
            primary_time_ms,
            target_tile_value,
            score_before_target,
            _json_or_none(evidence),
            _json_or_none(payload),
            submitted_at,
            submitted_at,
            submitted_at,
        ),
    )
    return {
        "submission_id": cursor.lastrowid,
        "event_code": event_code,
        "username": username,
        "display_name": resolved_display_name,
    }


def list_pending_scores(connection, event_code, *, status=None):
    event = lookup_event(connection, event_code)
    query = """
        SELECT
            pss.id,
            pss.player_id,
            pss.submitter_platform,
            pss.submitter_account,
            pss.display_name,
            pss.source_record_id,
            pss.started_at,
            pss.ended_at,
            pss.raw_score,
            pss.final_score,
            pss.competition_score,
            pss.primary_time_ms,
            pss.target_tile_value,
            pss.score_before_target,
            pss.evidence_json,
            pss.payload_json,
            pss.status,
            pss.review_reason,
            pss.reviewed_by,
            pss.performance_record_id,
            pss.submitted_at,
            pss.reviewed_at,
            pa.account_key AS username
        FROM pending_score_submissions pss
        LEFT JOIN player_accounts pa
            ON pa.player_id = pss.player_id
           AND pa.platform_id = ?
           AND pa.is_primary = 1
        WHERE pss.event_id = ?
    """
    params = [event["platform_id"], event["id"]]
    if status:
        query += " AND pss.status = ?"
        params.append(status)
    query += " ORDER BY pss.submitted_at ASC, pss.id ASC"
    rows = connection.execute(query, tuple(params)).fetchall()
    result = []
    for row in rows:
        result.append(
            {
                **dict(row),
                "evidence": _parse_json(row["evidence_json"]),
                "payload": _parse_json(row["payload_json"]),
            }
        )
    return {
        "event": event,
        "rows": result,
    }


def _load_submission(connection, submission_id):
    row = connection.execute(
        """
        SELECT
            pss.*,
            e.event_code,
            pa.account_key AS username
        FROM pending_score_submissions pss
        JOIN events e ON e.id = pss.event_id
        LEFT JOIN player_accounts pa ON pa.player_id = pss.player_id AND pa.is_primary = 1
        WHERE pss.id = ?
        """,
        (submission_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Pending score submission not found: {}".format(submission_id))
    return row


def approve_pending_score(
    connection,
    submission_id,
    *,
    reviewer="organizer_event_hub",
    actor_type="organizer_cli",
):
    submission = _load_submission(connection, submission_id)
    if submission["status"] != "pending":
        raise ValueError("Only pending submissions can be approved")

    evidence = _parse_json(submission["evidence_json"]) or {}
    payload = _parse_json(submission["payload_json"]) or {}
    note_parts = ["approved from pending submission #{}".format(submission_id)]
    if submission["submitter_platform"] or submission["submitter_account"]:
        note_parts.append(
            "submitter={}{}".format(
                submission["submitter_platform"] or "-",
                ":{}".format(submission["submitter_account"]) if submission["submitter_account"] else "",
            )
        )
    if evidence.get("note"):
        note_parts.append("evidence={}".format(evidence["note"]))
    evidence_note = " | ".join(note_parts)
    result = add_manual_score(
        connection,
        submission["event_code"],
        username=submission["username"] or submission["display_name"],
        display_name=submission["display_name"] or submission["username"],
        source_record_id=submission["source_record_id"],
        started_at=submission["started_at"],
        ended_at=submission["ended_at"],
        raw_score=submission["raw_score"],
        final_score=submission["final_score"],
        competition_score=submission["competition_score"],
        primary_time_ms=submission["primary_time_ms"],
        target_tile_value=submission["target_tile_value"],
        score_before_target=submission["score_before_target"],
        evidence_note=evidence_note,
    )
    reviewed_at = now_iso()
    connection.execute(
        """
        UPDATE pending_score_submissions
        SET status = 'approved',
            review_reason = ?,
            reviewed_by = ?,
            performance_record_id = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            "approved",
            reviewer,
            result["performance_record_id"],
            reviewed_at,
            reviewed_at,
            submission_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_type, actor_id, action_type, target_table, target_id,
            reason, before_json, after_json, created_at
        )
        VALUES (?, ?, 'approve_pending_score', 'pending_score_submissions', ?, ?, ?, ?, ?)
        """,
        (
            actor_type,
            reviewer,
            submission_id,
            "approve pending score",
            json.dumps({"status": "pending"}, ensure_ascii=False),
            json.dumps({"status": "approved", "performance_record_id": result["performance_record_id"]}, ensure_ascii=False),
            reviewed_at,
        ),
    )
    return {
        "submission_id": submission_id,
        "event_code": submission["event_code"],
        "source_record_id": result["source_record_id"],
        "performance_record_id": result["performance_record_id"],
    }


def reject_pending_score(
    connection,
    submission_id,
    *,
    reason,
    reviewer="organizer_event_hub",
    actor_type="organizer_cli",
):
    submission = _load_submission(connection, submission_id)
    if submission["status"] != "pending":
        raise ValueError("Only pending submissions can be rejected")
    if not reason or not reason.strip():
        raise ValueError("Reject reason is required")

    reviewed_at = now_iso()
    connection.execute(
        """
        UPDATE pending_score_submissions
        SET status = 'rejected',
            review_reason = ?,
            reviewed_by = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            reason.strip(),
            reviewer,
            reviewed_at,
            reviewed_at,
            submission_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_type, actor_id, action_type, target_table, target_id,
            reason, before_json, after_json, created_at
        )
        VALUES (?, ?, 'reject_pending_score', 'pending_score_submissions', ?, ?, ?, ?, ?)
        """,
        (
            actor_type,
            reviewer,
            submission_id,
            reason.strip(),
            json.dumps({"status": "pending"}, ensure_ascii=False),
            json.dumps({"status": "rejected", "review_reason": reason.strip()}, ensure_ascii=False),
            reviewed_at,
        ),
    )
    return {
        "submission_id": submission_id,
        "event_code": submission["event_code"],
        "reason": reason.strip(),
    }
