"""Retain semantic field identity and exact source offsets across SQL retrieval."""
from alembic import op
import sqlalchemy as sa

revision = '0008_extraction_provenance'
down_revision = '0007_preference_memory'
branch_labels = None
depends_on = None


def upgrade():
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('research_requirements')}
    if 'extraction_provenance' not in columns:
        op.add_column('research_requirements', sa.Column('extraction_provenance', sa.JSON(),
                      nullable=False, server_default=sa.text("'{}'")))


def downgrade():
    op.drop_column('research_requirements', 'extraction_provenance')
