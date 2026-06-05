SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS platforms (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        base_url TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        metadata_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS variants (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        game_family TEXT,
        shape_type TEXT,
        board_width INTEGER,
        board_height INTEGER,
        ruleset_key TEXT,
        category TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS platform_variants (
        id INTEGER PRIMARY KEY,
        platform_id INTEGER NOT NULL,
        variant_id INTEGER NOT NULL,
        platform_variant_code TEXT NOT NULL,
        platform_variant_name TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(platform_id, platform_variant_code),
        FOREIGN KEY(platform_id) REFERENCES platforms(id),
        FOREIGN KEY(variant_id) REFERENCES variants(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        notes TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS player_accounts (
        id INTEGER PRIMARY KEY,
        player_id INTEGER NOT NULL,
        platform_id INTEGER NOT NULL,
        account_key TEXT NOT NULL,
        account_name TEXT NOT NULL,
        account_display_name TEXT,
        is_primary INTEGER NOT NULL DEFAULT 1,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(platform_id, account_key),
        FOREIGN KEY(player_id) REFERENCES players(id),
        FOREIGN KEY(platform_id) REFERENCES platforms(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS identity_bindings (
        id INTEGER PRIMARY KEY,
        player_id INTEGER NOT NULL,
        binding_type TEXT NOT NULL,
        external_user_id TEXT NOT NULL,
        external_name TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        metadata_json TEXT,
        verified_at TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(binding_type, external_user_id),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seasons (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        metadata_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS communities (
        id INTEGER PRIMARY KEY,
        platform_type TEXT NOT NULL,
        external_id TEXT NOT NULL,
        name TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(platform_type, external_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rating_families (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        description TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rating_buckets (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        family_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        platform_id INTEGER,
        variant_id INTEGER,
        competition_type TEXT NOT NULL,
        target_value INTEGER,
        is_rated_by_default INTEGER NOT NULL DEFAULT 1,
        is_active INTEGER NOT NULL DEFAULT 1,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(family_id) REFERENCES rating_families(id),
        FOREIGN KEY(platform_id) REFERENCES platforms(id),
        FOREIGN KEY(variant_id) REFERENCES variants(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY,
        event_code TEXT NOT NULL UNIQUE,
        event_name TEXT NOT NULL,
        parent_event_id INTEGER,
        platform_id INTEGER NOT NULL,
        variant_id INTEGER,
        season_id INTEGER,
        community_id INTEGER,
        rating_bucket_id INTEGER,
        event_type TEXT NOT NULL,
        competition_type TEXT NOT NULL,
        status TEXT NOT NULL,
        is_official INTEGER NOT NULL DEFAULT 1,
        is_rated INTEGER NOT NULL DEFAULT 1,
        registration_open_time TEXT,
        registration_close_time TEXT,
        start_time TEXT,
        end_time TEXT,
        seal_time TEXT,
        source TEXT,
        tags_json TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(parent_event_id) REFERENCES events(id),
        FOREIGN KEY(platform_id) REFERENCES platforms(id),
        FOREIGN KEY(variant_id) REFERENCES variants(id),
        FOREIGN KEY(season_id) REFERENCES seasons(id),
        FOREIGN KEY(community_id) REFERENCES communities(id),
        FOREIGN KEY(rating_bucket_id) REFERENCES rating_buckets(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_rule_sets (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        version INTEGER NOT NULL,
        rule_type TEXT NOT NULL,
        ranking_metric TEXT NOT NULL,
        ranking_order TEXT NOT NULL,
        aggregation_method TEXT,
        validation_rule TEXT,
        tiebreakers_json TEXT,
        rule_config_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(event_id, version),
        FOREIGN KEY(event_id) REFERENCES events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS registrations (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        community_id INTEGER,
        registered_via TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        metadata_json TEXT,
        registered_at TEXT NOT NULL,
        UNIQUE(event_id, player_id),
        FOREIGN KEY(event_id) REFERENCES events(id),
        FOREIGN KEY(player_id) REFERENCES players(id),
        FOREIGN KEY(community_id) REFERENCES communities(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS timed_event_reservations (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        reserved_start_time TEXT NOT NULL,
        reserved_end_time TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'reserved',
        is_late_confirmed INTEGER NOT NULL DEFAULT 0,
        settled_at TEXT,
        settlement_payload_json TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(event_id, player_id),
        FOREIGN KEY(event_id) REFERENCES events(id),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_records (
        id INTEGER PRIMARY KEY,
        platform_id INTEGER NOT NULL,
        player_account_id INTEGER NOT NULL,
        variant_id INTEGER,
        source_record_id TEXT NOT NULL,
        record_type TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        raw_score INTEGER,
        final_score INTEGER,
        primary_time_ms INTEGER,
        target_tile_value INTEGER,
        score_before_target INTEGER,
        board_state_json TEXT,
        evidence_json TEXT,
        result_state TEXT,
        is_completed INTEGER NOT NULL DEFAULT 1,
        is_valid_source INTEGER NOT NULL DEFAULT 1,
        raw_payload_json TEXT,
        ingested_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(platform_id, source_record_id),
        FOREIGN KEY(platform_id) REFERENCES platforms(id),
        FOREIGN KEY(player_account_id) REFERENCES player_accounts(id),
        FOREIGN KEY(variant_id) REFERENCES variants(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attempt_sessions (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        registration_id INTEGER,
        session_type TEXT NOT NULL,
        status TEXT NOT NULL,
        start_command_time TEXT NOT NULL,
        lock_deadline_time TEXT,
        locked_record_id INTEGER,
        completed_time TEXT,
        cancelled_time TEXT,
        source TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(event_id) REFERENCES events(id),
        FOREIGN KEY(player_id) REFERENCES players(id),
        FOREIGN KEY(registration_id) REFERENCES registrations(id),
        FOREIGN KEY(locked_record_id) REFERENCES performance_records(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_attempt_records (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        attempt_session_id INTEGER,
        performance_record_id INTEGER NOT NULL,
        record_role TEXT NOT NULL,
        evaluation_status TEXT NOT NULL,
        derived_metric_value INTEGER,
        sort_metric_primary REAL,
        sort_metric_secondary REAL,
        derived_metric_json TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(event_id, performance_record_id),
        FOREIGN KEY(event_id) REFERENCES events(id),
        FOREIGN KEY(player_id) REFERENCES players(id),
        FOREIGN KEY(attempt_session_id) REFERENCES attempt_sessions(id),
        FOREIGN KEY(performance_record_id) REFERENCES performance_records(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_results (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        player_id INTEGER NOT NULL,
        result_status TEXT NOT NULL,
        rank_value INTEGER,
        primary_metric_type TEXT NOT NULL,
        primary_metric_value REAL,
        secondary_metric_type TEXT,
        secondary_metric_value REAL,
        tertiary_metric_type TEXT,
        tertiary_metric_value REAL,
        scoring_game_count INTEGER,
        total_full_boards INTEGER,
        best_single_score INTEGER,
        is_published INTEGER NOT NULL DEFAULT 1,
        result_payload_json TEXT,
        calculated_at TEXT NOT NULL,
        published_at TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE(event_id, player_id),
        FOREIGN KEY(event_id) REFERENCES events(id),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS result_snapshots (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        snapshot_type TEXT NOT NULL,
        snapshot_label TEXT,
        payload_json TEXT NOT NULL,
        signature TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(event_id) REFERENCES events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_runs (
        id INTEGER PRIMARY KEY,
        source_type TEXT NOT NULL,
        platform_id INTEGER,
        event_id INTEGER,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        records_seen INTEGER NOT NULL DEFAULT 0,
        records_inserted INTEGER NOT NULL DEFAULT 0,
        records_updated INTEGER NOT NULL DEFAULT 0,
        error_message TEXT,
        metadata_json TEXT,
        FOREIGN KEY(platform_id) REFERENCES platforms(id),
        FOREIGN KEY(event_id) REFERENCES events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY,
        actor_type TEXT NOT NULL,
        actor_id TEXT,
        action_type TEXT NOT NULL,
        target_table TEXT NOT NULL,
        target_id INTEGER NOT NULL,
        reason TEXT,
        before_json TEXT,
        after_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_score_submissions (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL,
        player_id INTEGER,
        submitter_platform TEXT,
        submitter_account TEXT,
        display_name TEXT,
        source_record_id TEXT,
        started_at TEXT,
        ended_at TEXT NOT NULL,
        raw_score INTEGER,
        final_score INTEGER,
        competition_score INTEGER,
        primary_time_ms INTEGER,
        target_tile_value INTEGER,
        score_before_target INTEGER,
        evidence_json TEXT,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        review_reason TEXT,
        reviewed_by TEXT,
        performance_record_id INTEGER,
        submitted_at TEXT NOT NULL,
        reviewed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(event_id) REFERENCES events(id),
        FOREIGN KEY(player_id) REFERENCES players(id),
        FOREIGN KEY(performance_record_id) REFERENCES performance_records(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_account_bindings (
        id INTEGER PRIMARY KEY,
        bot_platform TEXT NOT NULL,
        bot_user_id TEXT NOT NULL,
        game_platform TEXT NOT NULL,
        player_id INTEGER NOT NULL,
        account_key TEXT NOT NULL,
        display_name TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(bot_platform, bot_user_id, game_platform),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_player_dashboards (
        id INTEGER PRIMARY KEY,
        game_platform TEXT NOT NULL,
        player_id INTEGER NOT NULL,
        dashboard_text TEXT NOT NULL,
        line_count INTEGER NOT NULL DEFAULT 0,
        nonspace_char_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(game_platform, player_id),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS player_ratings (
        id INTEGER PRIMARY KEY,
        player_id INTEGER NOT NULL,
        rating_bucket_id INTEGER NOT NULL,
        rating_value REAL NOT NULL,
        rating_deviation REAL NOT NULL,
        rating_volatility REAL,
        event_count INTEGER NOT NULL DEFAULT 0,
        best_rating REAL,
        last_event_id INTEGER,
        last_updated_at TEXT NOT NULL,
        UNIQUE(player_id, rating_bucket_id),
        FOREIGN KEY(player_id) REFERENCES players(id),
        FOREIGN KEY(rating_bucket_id) REFERENCES rating_buckets(id),
        FOREIGN KEY(last_event_id) REFERENCES events(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rating_history (
        id INTEGER PRIMARY KEY,
        player_id INTEGER NOT NULL,
        rating_bucket_id INTEGER NOT NULL,
        event_id INTEGER NOT NULL,
        old_rating REAL NOT NULL,
        new_rating REAL NOT NULL,
        delta_rating REAL NOT NULL,
        old_deviation REAL,
        new_deviation REAL,
        placement INTEGER,
        field_size INTEGER,
        weight REAL,
        details_json TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(player_id, rating_bucket_id, event_id),
        FOREIGN KEY(player_id) REFERENCES players(id),
        FOREIGN KEY(rating_bucket_id) REFERENCES rating_buckets(id),
        FOREIGN KEY(event_id) REFERENCES events(id)
    )
    """,
]


INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_platform_variants_platform_variant ON platform_variants(platform_id, variant_id)",
    "CREATE INDEX IF NOT EXISTS idx_players_display_name ON players(display_name)",
    "CREATE INDEX IF NOT EXISTS idx_player_accounts_player_id ON player_accounts(player_id)",
    "CREATE INDEX IF NOT EXISTS idx_player_accounts_platform_name ON player_accounts(platform_id, account_name)",
    "CREATE INDEX IF NOT EXISTS idx_rating_buckets_family ON rating_buckets(family_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_platform_start ON events(platform_id, start_time)",
    "CREATE INDEX IF NOT EXISTS idx_events_season_id ON events(season_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_community_id ON events(community_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_rating_bucket_id ON events(rating_bucket_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)",
    "CREATE INDEX IF NOT EXISTS idx_event_rule_sets_event_id ON event_rule_sets(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_performance_records_player_end ON performance_records(player_account_id, ended_at)",
    "CREATE INDEX IF NOT EXISTS idx_performance_records_platform_end ON performance_records(platform_id, ended_at)",
    "CREATE INDEX IF NOT EXISTS idx_performance_records_variant_end ON performance_records(variant_id, ended_at)",
    "CREATE INDEX IF NOT EXISTS idx_attempt_sessions_event_player_status ON attempt_sessions(event_id, player_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_timed_event_reservations_event_status ON timed_event_reservations(event_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_timed_event_reservations_player_status ON timed_event_reservations(player_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_event_attempt_records_event_player ON event_attempt_records(event_id, player_id)",
    "CREATE INDEX IF NOT EXISTS idx_event_results_event_rank ON event_results(event_id, rank_value)",
    "CREATE INDEX IF NOT EXISTS idx_event_results_player_id ON event_results(player_id)",
    "CREATE INDEX IF NOT EXISTS idx_result_snapshots_event_type_created ON result_snapshots(event_id, snapshot_type, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_sync_runs_event_started ON sync_runs(event_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_sync_runs_status_started ON sync_runs(status, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_logs_target ON audit_logs(target_table, target_id)",
    "CREATE INDEX IF NOT EXISTS idx_pending_score_submissions_event_status ON pending_score_submissions(event_id, status, submitted_at)",
    "CREATE INDEX IF NOT EXISTS idx_bot_account_bindings_player_active ON bot_account_bindings(player_id, is_active)",
    "CREATE INDEX IF NOT EXISTS idx_bot_player_dashboards_platform_player ON bot_player_dashboards(game_platform, player_id)",
    "CREATE INDEX IF NOT EXISTS idx_player_ratings_bucket_rating ON player_ratings(rating_bucket_id, rating_value DESC)",
    "CREATE INDEX IF NOT EXISTS idx_rating_history_bucket_created ON rating_history(rating_bucket_id, created_at)",
]
