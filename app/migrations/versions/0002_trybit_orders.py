"""trybit orders

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09 18:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('trybit_orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('telegram_id', sa.BigInteger(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('plan_id', sa.Integer(), nullable=False),
    sa.Column('redemption_id', sa.Integer(), nullable=True),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('invoice_uuid', sa.String(length=32), nullable=True),
    sa.Column('link', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('partial_notified', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['plan_id'], ['plans.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('invoice_uuid')
    )
    op.create_index(op.f('ix_trybit_orders_telegram_id'), 'trybit_orders', ['telegram_id'], unique=False)
    op.create_index('ix_trybit_orders_status', 'trybit_orders', ['status', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_trybit_orders_status', table_name='trybit_orders')
    op.drop_index(op.f('ix_trybit_orders_telegram_id'), table_name='trybit_orders')
    op.drop_table('trybit_orders')
