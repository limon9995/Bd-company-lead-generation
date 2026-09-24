"""per-campaign browser options

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("campaigns") as b:
        b.add_column(sa.Column("browser_style", sa.String(10), nullable=False, server_default=""))
        b.add_column(sa.Column("search_provider", sa.String(20), nullable=False, server_default=""))
        b.add_column(sa.Column("on_block", sa.String(10), nullable=False, server_default="fallback"))


def downgrade():
    with op.batch_alter_table("campaigns") as b:
        b.drop_column("on_block")
        b.drop_column("search_provider")
        b.drop_column("browser_style")
