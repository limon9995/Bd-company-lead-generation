"""directory AI agent flag

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("campaigns") as b:
        b.add_column(sa.Column("directory_agent", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table("campaigns") as b:
        b.drop_column("directory_agent")
