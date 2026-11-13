"""Persist independent provider submission attempts and structured release decisions.

Revision ID: c585c20261112
Revises: f6b585a12c34
Create Date: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa

revision = "c585c20261112"
down_revision = "f6b585a12c34"
branch_labels = None
depends_on = None

_JOB_COLUMNS = {
    "ebook_submission_state": sa.String(30),
    "audio_submission_state": sa.String(30),
    "ebook_submission_reason": sa.String(100),
    "audio_submission_reason": sa.String(100),
}
_RECEIPT_COLUMNS = {
    "decision_code": sa.String(100),
    "decision_action": sa.String(40),
}


def upgrade() -> None:
    bind = op.get_bind()
    for table, columns in (("acquisition_jobs", _JOB_COLUMNS), ("acquisition_requests", _RECEIPT_COLUMNS)):
        existing = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        for name, type_ in columns.items():
            if name not in existing:
                op.add_column(table, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for table, columns in (("acquisition_requests", _RECEIPT_COLUMNS), ("acquisition_jobs", _JOB_COLUMNS)):
        existing = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        for name in reversed(tuple(columns)):
            if name in existing:
                op.drop_column(table, name)
