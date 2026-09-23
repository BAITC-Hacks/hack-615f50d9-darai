"""users.must_change_password, users.password_changed_at

Existing accounts keep must_change_password = false.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('must_change_password', sa.Boolean(), nullable=False,
                                     server_default=sa.text('false')))
    op.add_column('users', sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'password_changed_at')
    op.drop_column('users', 'must_change_password')
