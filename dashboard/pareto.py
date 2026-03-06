import pandas as pd
import numpy as np

def compute_pareto_front(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculates the Pareto efficient configurations (minimizing cost and latency).
    Uses vectorized NumPy operations for O(N^2) worst-case, but lightning fast in C.
    """
    # 1. Aggregate the 12,000 raw logs into average performance per config
    agg_df = df.groupby(['provider', 'policy']).agg(
        avg_latency=('latency_ms', 'mean'),
        avg_cost=('cost_cents', 'mean'),
        error_rate=('error_flag', 'mean')
    ).reset_index()

    # 2. Extract arrays for vectorized comparison
    costs = agg_df['avg_cost'].values
    latencies = agg_df['avg_latency'].values

    # 3. Find non-dominated points
    is_efficient = np.ones(costs.shape[0], dtype=bool)
    for i in range(costs.shape[0]):
        if is_efficient[i]:
            # j dominates i if j is <= i on all axes AND strictly < on at least one
            dominated_by_others = np.any(
                (costs <= costs[i]) & (latencies <= latencies[i]) & 
                ((costs < costs[i]) | (latencies < latencies[i]))
            )
            if dominated_by_others:
                is_efficient[i] = False
            else:
                # Remove points that i strictly dominates to speed up future loops
                dominates_others = (
                    (costs >= costs[i]) & 
                    (latencies >= latencies[i]) & 
                    ((costs > costs[i]) | (latencies > latencies[i]))
                )
                is_efficient[dominates_others] = False
                
    # 4. Return only the optimal configurations
    pareto_front = agg_df[is_efficient].copy()
    pareto_front['is_pareto'] = True
    
    # Merge back so the UI knows which dots to highlight
    return pd.merge(agg_df, pareto_front[['provider', 'policy', 'is_pareto']], 
                    on=['provider', 'policy'], how='left').fillna({'is_pareto': False})