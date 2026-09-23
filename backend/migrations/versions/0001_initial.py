"""initial

Revision ID: 0001
Revises:
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.create_table('employees',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('fio', sa.String(length=255), nullable=False),
    sa.Column('position', sa.String(length=255), nullable=False),
    sa.Column('department', sa.String(length=255), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('employee_id', sa.UUID(), nullable=True),
    sa.Column('login', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('employee_id'),
    sa.UniqueConstraint('login')
    )
    op.create_table('voice_profiles',
    sa.Column('employee_id', sa.UUID(), nullable=False),
    sa.Column('vector', Vector(), nullable=False),
    sa.Column('model_id', sa.String(length=255), nullable=False),
    sa.Column('model_revision', sa.String(length=255), nullable=False),
    sa.Column('dimension', sa.Integer(), nullable=False),
    sa.Column('quality_status', sa.String(length=32), nullable=False),
    sa.Column('quality_reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('speech_seconds', sa.Float(), nullable=False),
    sa.Column('consent_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('employee_id')
    )
    op.create_table('auth_sessions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('csrf_token', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_auth_sessions_user_id'), 'auth_sessions', ['user_id'], unique=False)
    op.create_table('meetings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('agenda', sa.Text(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('organizer_id', sa.UUID(), nullable=False),
    sa.Column('secretary_id', sa.UUID(), nullable=False),
    sa.Column('approval_status', sa.String(length=16), nullable=False),
    sa.Column('protocol_version', sa.Integer(), nullable=False),
    sa.Column('draft_revision', sa.Integer(), nullable=False),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('confirmed_by', sa.UUID(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('summary_edited', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['confirmed_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['organizer_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['secretary_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('meeting_participants',
    sa.Column('meeting_id', sa.UUID(), nullable=False),
    sa.Column('employee_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('meeting_id', 'employee_id')
    )
    op.create_table('recordings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('meeting_id', sa.UUID(), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('local_path', sa.String(length=1024), nullable=False),
    sa.Column('normalized_path', sa.String(length=1024), nullable=True),
    sa.Column('processing_status', sa.String(length=16), nullable=False),
    sa.Column('stage', sa.String(length=16), nullable=False),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('duration_seconds', sa.Float(), nullable=True),
    sa.Column('languages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('extraction_status', sa.String(length=16), nullable=False),
    sa.Column('extraction_error_code', sa.String(length=64), nullable=True),
    sa.Column('extraction_error_message', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('meeting_id')
    )
    op.create_table('tasks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('meeting_id', sa.UUID(), nullable=False),
    sa.Column('from_label', sa.String(length=32), nullable=True),
    sa.Column('from_employee_id', sa.UUID(), nullable=True),
    sa.Column('to_employee_id', sa.UUID(), nullable=True),
    sa.Column('task', sa.Text(), nullable=False),
    sa.Column('deadline', sa.Date(), nullable=True),
    sa.Column('deadline_source', sa.Text(), nullable=True),
    sa.Column('evidence', sa.Text(), nullable=True),
    sa.Column('source_utterance_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('execution_status', sa.String(length=16), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('needs_review', sa.Boolean(), nullable=False),
    sa.Column('review_reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('origin', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['from_employee_id'], ['employees.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['to_employee_id'], ['employees.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tasks_meeting_id'), 'tasks', ['meeting_id'], unique=False)
    op.create_index(op.f('ix_tasks_to_employee_id'), 'tasks', ['to_employee_id'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('recipient_user_id', sa.UUID(), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('meeting_id', sa.UUID(), nullable=True),
    sa.Column('task_id', sa.UUID(), nullable=True),
    sa.Column('dedup_key', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['recipient_user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedup_key')
    )
    op.create_index(op.f('ix_notifications_recipient_user_id'), 'notifications', ['recipient_user_id'], unique=False)
    op.create_table('speakers',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('meeting_id', sa.UUID(), nullable=False),
    sa.Column('recording_id', sa.UUID(), nullable=False),
    sa.Column('label', sa.String(length=32), nullable=False),
    sa.Column('proposed_employee_id', sa.UUID(), nullable=True),
    sa.Column('confirmed_employee_id', sa.UUID(), nullable=True),
    sa.Column('manually_set', sa.Boolean(), nullable=False),
    sa.Column('similarity', sa.Float(), nullable=True),
    sa.Column('second_similarity', sa.Float(), nullable=True),
    sa.Column('review_required', sa.Boolean(), nullable=False),
    sa.Column('review_reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('clean_speech_seconds', sa.Float(), nullable=False),
    sa.Column('utterance_count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['confirmed_employee_id'], ['employees.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['proposed_employee_id'], ['employees.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['recording_id'], ['recordings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recording_id', 'label')
    )
    op.create_index(op.f('ix_speakers_meeting_id'), 'speakers', ['meeting_id'], unique=False)
    op.create_table('utterances',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('recording_id', sa.UUID(), nullable=False),
    sa.Column('start', sa.Float(), nullable=False),
    sa.Column('end', sa.Float(), nullable=False),
    sa.Column('speaker_label', sa.String(length=32), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('language', sa.String(length=8), nullable=True),
    sa.Column('uncertain', sa.Boolean(), nullable=False),
    sa.Column('uncertain_reasons', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['recording_id'], ['recordings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_utterances_recording_id'), 'utterances', ['recording_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_utterances_recording_id'), table_name='utterances')
    op.drop_table('utterances')
    op.drop_index(op.f('ix_speakers_meeting_id'), table_name='speakers')
    op.drop_table('speakers')
    op.drop_index(op.f('ix_notifications_recipient_user_id'), table_name='notifications')
    op.drop_table('notifications')
    op.drop_index(op.f('ix_tasks_to_employee_id'), table_name='tasks')
    op.drop_index(op.f('ix_tasks_meeting_id'), table_name='tasks')
    op.drop_table('tasks')
    op.drop_table('recordings')
    op.drop_table('meeting_participants')
    op.drop_table('meetings')
    op.drop_index(op.f('ix_auth_sessions_user_id'), table_name='auth_sessions')
    op.drop_table('auth_sessions')
    op.drop_table('voice_profiles')
    op.drop_table('users')
    op.drop_table('employees')
