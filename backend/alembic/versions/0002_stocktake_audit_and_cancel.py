"""سجل تسوية الجرد التفصيلي + سبب إلغاء الجرد

- quantity_adjustments: location_id / location_code / stocktake_id / unit_price / value_difference
  (سجل لكل منتج+موقع فيه فرق، بقيمته المالية بسعر البيع وقت لقطة الجرد). السجلات القديمة بتفضل زي ما هي (الأعمدة الجديدة NULL).
- stocktakes: cancel_reason / cancelled_by_username / cancelled_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('quantity_adjustments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('location_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('location_code', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('stocktake_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('unit_price', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=True))
        batch_op.add_column(sa.Column('value_difference', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_quantity_adjustments_location_id_locations'), 'locations', ['location_id'], ['id'])
        batch_op.create_foreign_key(batch_op.f('fk_quantity_adjustments_stocktake_id_stocktakes'), 'stocktakes', ['stocktake_id'], ['id'])
        batch_op.create_index('ix_quantity_adjustments_stocktake', ['stocktake_id'], unique=False)

    with op.batch_alter_table('stocktakes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cancel_reason', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('cancelled_by_username', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('cancelled_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('stocktakes', schema=None) as batch_op:
        batch_op.drop_column('cancelled_at')
        batch_op.drop_column('cancelled_by_username')
        batch_op.drop_column('cancel_reason')

    with op.batch_alter_table('quantity_adjustments', schema=None) as batch_op:
        # MySQL: لازم الـ FK يتشال قبل الـ index اللي بيعتمد عليه
        batch_op.drop_constraint(batch_op.f('fk_quantity_adjustments_stocktake_id_stocktakes'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('fk_quantity_adjustments_location_id_locations'), type_='foreignkey')
        batch_op.drop_index('ix_quantity_adjustments_stocktake')
        batch_op.drop_column('value_difference')
        batch_op.drop_column('unit_price')
        batch_op.drop_column('stocktake_id')
        batch_op.drop_column('location_code')
        batch_op.drop_column('location_id')
