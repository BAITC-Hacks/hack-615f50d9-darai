"""live recording sessions; meetings.meeting_url

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('meetings', sa.Column('meeting_url', sa.String(length=2000), nullable=True))
    op.create_table(
        'live_sessions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('meeting_id', sa.UUID(), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('mime_type', sa.String(length=128), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('local_path', sa.String(length=1024), nullable=False),
        sa.Column('next_sequence', sa.Integer(), nullable=False),
        sa.Column('received_bytes', sa.Integer(), nullable=False),
        sa.Column('chunk_hashes', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('preview_status', sa.String(length=16), nullable=False),
        sa.Column('preview_error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('preview_utterances', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('preview_bytes', sa.Integer(), nullable=False),
        sa.Column('preview_state', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('processed_until_seconds', sa.Float(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('recording_id', sa.UUID(), nullable=True),
        sa.Column('error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('last_chunk_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_live_sessions_meeting_id', 'live_sessions', ['meeting_id'])
    # one recording session per meeting at a time
    op.create_index('uq_live_sessions_one_recording', 'live_sessions', ['meeting_id'], unique=True,
                    postgresql_where=sa.text("state = 'recording'"))


def downgrade() -> None:
    op.drop_index('uq_live_sessions_one_recording', table_name='live_sessions')
    op.drop_index('ix_live_sessions_meeting_id', table_name='live_sessions')
    op.drop_table('live_sessions')
    op.drop_column('meetings', 'meeting_url')
