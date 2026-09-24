"""admin feedback on decision makers

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("people") as b:
        b.add_column(sa.Column("feedback", sa.String(10), nullable=False, server_default=""))


def downgrade():
    with op.batch_alter_table("people") as b:
        b.drop_column("feedback")
