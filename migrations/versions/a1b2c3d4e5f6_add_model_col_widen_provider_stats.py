"""add_model_col_widen_provider_stats

Revision ID: a1b2c3d4e5f6
Revises: 0596718f021b
Create Date: 2026-03-03 22:43:00.000000

Fixes:
  1. inference_logs missing 'model' TEXT column (telemetry INSERT failed with UndefinedColumnError)
  2. provider_stats EMA columns overflow at DECIMAL(12,6) — latency_ms values >999999 cause
     NumericValueOutOfRangeError. Widened to DECIMAL(18,6) to handle realistic EMA values.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '0596718f021b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add missing 'model' column to inference_logs
    op.add_column(
        'inference_logs',
        sa.Column('model', sa.Text(), nullable=True),
    )

    # 2. Widen provider_stats EMA columns from DECIMAL(12,6) to DECIMAL(18,6)
    #    to prevent overflow when EMA latency accumulates large values.
    for col in ('ema_latency_mu', 'ema_latency_var',
                'ema_cost_mu',    'ema_cost_var',
                'ema_quality_mu', 'ema_quality_var'):
        op.alter_column(
            'provider_stats', col,
            type_=sa.Numeric(precision=18, scale=6),
            existing_type=sa.Numeric(precision=12, scale=6),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Restore narrow precision (may lose data if large values are stored)
    for col in ('ema_latency_mu', 'ema_latency_var',
                'ema_cost_mu',    'ema_cost_var',
                'ema_quality_mu', 'ema_quality_var'):
        op.alter_column(
            'provider_stats', col,
            type_=sa.Numeric(precision=12, scale=6),
            existing_type=sa.Numeric(precision=18, scale=6),
            existing_nullable=False,
        )

    op.drop_column('inference_logs', 'model')
