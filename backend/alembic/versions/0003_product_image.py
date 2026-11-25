"""صورة المنتج: products.image_key

- مفتاح الصورة في مخزن الوسائط (المسار الفعلي: MEDIA_DIR/products/<2 حروف>/<key>/{main,thumb}.webp). الصور نفسها مش بتتخزن في الـ DB.
- المنتجات القديمة بتفضل من غير صورة (NULL). مفيش تغيير في أي جدول تاني.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-12 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('image_key', sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('image_key')
