from pathlib import Path
import sqlite3

from codex_herder.app import load_picker_groups


def test_load_picker_groups_flattens_nested_groups_in_picker_order(tmp_path: Path) -> None:
    database = tmp_path / "experiment_picker.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER,
                node_type TEXT NOT NULL,
                name TEXT NOT NULL,
                user_id TEXT,
                exp_id TEXT,
                sort_order INTEGER
            );
            INSERT INTO nodes VALUES (1, NULL, 'group', 'Experiments', NULL, NULL, 0);
            INSERT INTO nodes VALUES (2, 1, 'group', 'Visual', NULL, NULL, 0);
            INSERT INTO nodes VALUES (3, 2, 'experiment', 'exp-002', 'user-b', 'exp-002', 1);
            INSERT INTO nodes VALUES (4, 2, 'group', 'Nested', NULL, NULL, 1);
            INSERT INTO nodes VALUES (5, 4, 'experiment', 'exp-001', 'user-a', 'exp-001', 0);
            """
        )

    groups = load_picker_groups(database)

    assert [group.name for group in groups] == [
        "Experiments / Visual",
        "Experiments / Visual / Nested",
    ]
    assert [(item.exp_id, item.user_id) for item in groups[0].experiments] == [
        ("exp-002", "user-b"),
        ("exp-001", "user-a"),
    ]
