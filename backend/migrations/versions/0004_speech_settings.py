"""Per-meeting recognition settings and compatibility with early live schema."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("meetings", sa.Column("asr_language", sa.String(8), nullable=False, server_default="auto"))
    op.add_column("meetings", sa.Column("asr_profile", sa.String(16), nullable=False, server_default="refined"))
    # Early installed 0003 predates the incremental preview_state field.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("live_sessions")}
    if "preview_state" not in columns:
        from sqlalchemy.dialects.postgresql import JSONB
        op.add_column("live_sessions", sa.Column("preview_state", JSONB(), nullable=True))


def downgrade():
    op.drop_column("meetings", "asr_profile")
    op.drop_column("meetings", "asr_language")
