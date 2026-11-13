"""Persist confirmed library arrivals, pairing and post-delivery scans (#585 Phase D).

Revision ID: d585c20261112
Revises: h16d20261112
Create Date: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa

revision = "d585c20261112"
down_revision = "h16d20261112"
branch_labels = None
depends_on = None

_COLUMNS = {
    "ebook_library_state": sa.String(30),
    "audio_library_state": sa.String(30),
    "ebook_library_reason": sa.String(255),
    "audio_library_reason": sa.String(255),
    "ebook_review_candidates": sa.Text(),
    "audio_review_candidates": sa.Text(),
    "pairing_state": sa.String(30),
    "pairing_reason": sa.String(255),
    "linked_abs_id": sa.String(255),
    "audio_delivered_at": sa.DateTime(),
    "scan_library_id": sa.String(50),
    "scan_scope": sa.String(500),
    "scan_job_id": sa.String(100),
    "scan_state": sa.String(30),
    "scan_reason": sa.String(255),
    "scan_attempts": sa.Integer(),
    "scan_claimed_at": sa.DateTime(),
    "scan_completed_at": sa.DateTime(),
}


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("acquisition_jobs")}
    for name, type_ in _COLUMNS.items():
        if name not in existing:
            op.add_column("acquisition_jobs", sa.Column(
                name, type_, nullable=True,
                server_default="0" if name == "scan_attempts" else None,
            ))


def downgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("acquisition_jobs")}
    for name in reversed(tuple(_COLUMNS)):
        if name in existing:
            op.drop_column("acquisition_jobs", name)
