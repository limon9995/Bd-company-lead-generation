"""browser discovery sources

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("campaigns") as b:
        b.add_column(sa.Column("discovery_source", sa.String(20), nullable=False, server_default="places_api"))
        b.add_column(sa.Column("directory_urls", sa.JSON(), nullable=True))
        b.add_column(sa.Column("directory_max_pages", sa.Integer(), nullable=False, server_default="5"))
    with op.batch_alter_table("companies") as b:
        b.add_column(sa.Column("source", sa.String(20), nullable=False, server_default="places_api"))
        b.add_column(sa.Column("source_url", sa.String(1000), nullable=False, server_default=""))


def downgrade():
    with op.batch_alter_table("companies") as b:
        b.drop_column("source_url")
        b.drop_column("source")
    with op.batch_alter_table("campaigns") as b:
        b.drop_column("directory_max_pages")
        b.drop_column("directory_urls")
        b.drop_column("discovery_source")
