"""trybit orders: coin

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('trybit_orders', sa.Column('coin', sa.String(length=24), nullable=True))


def downgrade() -> None:
    op.drop_column('trybit_orders', 'coin')
