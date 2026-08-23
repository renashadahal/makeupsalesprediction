from datetime import date

from src.database import (
    db_claim_sunday_training_run,
    db_finish_sunday_training_run,
    get_db,
    init_db,
)


SUNDAY = date(2026, 8, 23)


def test_weekly_training_claim_is_sunday_only_and_runs_once(tmp_path):
    db_path = str(tmp_path / 'weekly_training.db')
    init_db(db_path)

    assert db_claim_sunday_training_run('staff1', 'S001', date(2026, 8, 22), db_path) is False
    assert db_claim_sunday_training_run('staff1', 'S001', SUNDAY, db_path) is True
    assert db_claim_sunday_training_run('staff2', 'S002', SUNDAY, db_path) is False

    db_finish_sunday_training_run(
        True,
        metrics={'training_records': 42, 'r2': 0.7, 'mae': 2.0, 'rmse': 3.0},
        today=SUNDAY,
        db_path=db_path,
    )
    with get_db(db_path) as conn:
        run = conn.execute('SELECT * FROM model_training_runs;').fetchone()
    assert run['status'] == 'SUCCEEDED'
    assert run['training_records'] == 42


def test_failed_sunday_training_can_be_retried(tmp_path):
    db_path = str(tmp_path / 'weekly_retry.db')
    init_db(db_path)

    assert db_claim_sunday_training_run('staff1', 'S001', SUNDAY, db_path) is True
    db_finish_sunday_training_run(False, error_message='temporary error', today=SUNDAY, db_path=db_path)
    assert db_claim_sunday_training_run('staff2', 'S002', SUNDAY, db_path) is True
