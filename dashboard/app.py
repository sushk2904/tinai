import streamlit as st
import pandas as pd
import psycopg2
from psycopg2 import pool
import altair as alt
import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# --- CONFIGURATION & THEMING ---
st.set_page_config(
    page_title="TINAI Control Center",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
/* Add a slightly darker premium feel */
[data-testid="stAppViewContainer"] {
    background-color: #0b0c10;
    color: #c5c6c7;
}
[data-testid="stSidebar"] {
    background-color: #1f2833;
}
.stMetric {
    background-color: #1f2833;
    padding: 10px;
    border-radius: 8px;
    border: 1px solid #45a29e;
}
[data-testid="stMetricValue"] {
    font-size: 1.6rem !important;
}
[data-testid="stMetricValue"] > div {
    white-space: normal !important;
    word-break: break-word !important;
}
[data-testid="stMetricLabel"] p {
    font-size: 0.85rem !important;
    white-space: normal !important;
    word-break: break-word !important;
}
</style>
""", unsafe_allow_html=True)

def get_status_color(val, is_failure=True):
    """Returns CSS color based on health thresholds."""
    if is_failure:
        # Flipped logic for failure: Low is Good (Green)
        if val < 10: return "#00ff7f" # Spring Green
        if val < 40: return "#ffd700" # Gold/Yellow
        return "#ff4b4b" # Red
    else:
        # Success logic: High is Good (Green)
        if val > 90: return "#00ff7f"
        if val > 60: return "#ffd700"
        return "#ff4b4b"

st.title("🛡️ TINAI Control Center")
st.markdown("### Multi-Armed Bandit & Chaos Engineering Observability")
st.write("")

# --- DATABASE CONNECTION ---
@st.cache_resource
def get_connection_pool():
    try:
        # Respect injected Docker DSN or fallback to host execution DSN
        default_dsn = f"postgresql://{os.getenv('POSTGRES_USER', 'tinai_user')}:{os.getenv('POSTGRES_PASSWORD', 'changeme_strong_password')}@127.0.0.1:{os.getenv('POSTGRES_PORT', '5432')}/{os.getenv('POSTGRES_DB', 'tinai_db')}"
        db_url = os.getenv("DATABASE_URL", default_dsn)
        
        return psycopg2.pool.SimpleConnectionPool(
            1, 20,
            dsn=db_url
        )
    except Exception as e:
        st.error(f"Error connecting to Postgres: {e}")
        return None

@st.cache_data(ttl=5) # Cache the query results for 5 seconds
def fetch_inference_logs():
    pool_obj = get_connection_pool()
    if not pool_obj:
        return pd.DataFrame()
    
    conn = pool_obj.getconn()
    try:
        # We query the latest 15,000 logs for robust rendering
        query = "SELECT * FROM inference_logs ORDER BY created_at DESC LIMIT 15000"
        df = pd.read_sql(query, conn)
        return df
    finally:
        pool_obj.putconn(conn)

from pareto import compute_pareto_front

# --- LOAD DATA ---
df = fetch_inference_logs()

if df.empty:
    st.warning("No data found in `inference_logs` table. Run k6 load-tests to populate.")
    st.stop()

# Ensure timestamp parsing
df['created_at'] = pd.to_datetime(df['created_at'])

# --- LAYOUT ---
cols = st.columns([1, 3])

# 1. SIDEBAR / CONTROL PANEL (Top Left)
top_left_cell = cols[0].container(border=True)
with top_left_cell:
    st.write("#### System Controls")
    
    providers_list = df['provider'].unique().tolist()
    policies_list = df['policy'].unique().tolist()

    providers = st.multiselect(
        "LLM Providers",
        options=providers_list,
        default=providers_list[:4] if len(providers_list) > 0 else []
    )
    
    # Modern st.pills identical to scraped UI code
    policies = st.pills(
        "Active MAB Policies",
        options=policies_list,
        default=policies_list if len(policies_list) > 0 else [],
        selection_mode="multi"
    )


if not policies:
    policies = policies_list

# Filter data based on selection
mask = df['provider'].isin(providers) & df['policy'].isin(policies)
filtered_df = df[mask]

# Calculate Pareto Front from pareto.py
pareto_df = compute_pareto_front(filtered_df) if not filtered_df.empty else pd.DataFrame()


# 2. MAIN CHARTS (Right Cell)
right_cell = cols[1].container(border=True)
with right_cell:
    tab1, tab2 = st.tabs(["Cost vs Latency (Pareto Front)", "Latency Over Time"])
    
    with tab1:
        st.write("#### Cost vs. Latency Efficiency")
        if not pareto_df.empty:
            base = alt.Chart(pareto_df).encode(
                x=alt.X('avg_latency:Q', title='Avg Latency (ms)', scale=alt.Scale(zero=False)),
                y=alt.Y('avg_cost:Q', title='Avg Cost (cents)', scale=alt.Scale(domain=[-5, 25], zero=False)),
                color=alt.Color('provider:N', legend=alt.Legend(orient="bottom")),
                tooltip=['provider', 'policy', 'avg_latency', 'avg_cost', 'avg_quality', 'is_pareto']
            )
            
            points = base.mark_circle(size=150).encode(
                opacity=alt.condition(alt.datum.is_pareto, alt.value(1.0), alt.value(0.3)),
                stroke=alt.condition(alt.datum.is_pareto, alt.value('white'), alt.value('transparent')),
                strokeWidth=alt.value(2)
            )
            
            # Line connecting pareto points smoothly
            line = base.transform_filter(
                alt.datum.is_pareto
            ).mark_line(color='#ffffff', strokeDash=[2, 2]).encode(
                order='avg_latency:Q'
            )
            
            st.altair_chart((points + line).properties(height=400).interactive(), use_container_width=True)
            st.caption("Points with thick white borders represent Pareto-optimal configurations.")
        else:
            st.info("Select providers and policies to calculate efficiency.")

    with tab2:
        st.write("#### Inference Latency 1 Min Time (Including Gray Failures)")
        if not filtered_df.empty:
            chart = (
                alt.Chart(filtered_df)
                .mark_circle(size=30, opacity=0.5)
                .encode(
                    x=alt.X('created_at:T', title="Time (HH:MM:SS)", axis=alt.Axis(format='%H:%M:%S', labelAngle=-45, grid=True)),
                    y=alt.Y('latency_ms:Q', title='Latency (ms)', scale=alt.Scale(type="symlog")),
                    color=alt.Color('provider:N', legend=alt.Legend(orient="bottom")),
                    tooltip=['request_id', 'provider', 'latency_ms', 'error_flag', 'policy']
                )
                .properties(height=400)
                .interactive()
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            st.info("Select providers and policies to view tracking data.")




total_reqs = len(filtered_df)
p95_latency = filtered_df['latency_ms'].quantile(0.95) if not filtered_df.empty else 0
failure_rate = (filtered_df['error_flag'].sum() / total_reqs * 100) if total_reqs > 0 else 0
total_spend = filtered_df['cost_cents'].sum() / 100 if not filtered_df.empty else 0
success_rate = 100 - failure_rate

# Calculate AI Quality (0-100%)
avg_quality = (filtered_df['quality_score'].mean() * 100) if not filtered_df.empty else 100.0

health_color = get_status_color(success_rate, is_failure=False)
quality_color = get_status_color(avg_quality, is_failure=False)

# 3. SYSTEM HEALTH METRICS (Bottom Left)
bottom_left_cell = cols[0].container(border=True)
with bottom_left_cell:
    st.markdown('<p style="font-size: 1.1rem; font-weight: 700; margin-bottom: 0px;">Real-time Health</p>', unsafe_allow_html=True)
    
    # Row 1: Load & Latency
    row1 = st.columns([1, 1])
    with row1[0]:
        st.markdown(f"""
            <div style="background-color: #1f2833; padding: 10px; border-radius: 8px; border: 1px solid #45a29e; min-height: 90px; display: flex; flex-direction: column; justify-content: flex-start; overflow: hidden; margin-top: 2px;">
                <p style="font-weight: 400; font-size: 0.75rem; color: #c5c6c7; margin: 0; line-height: 1.2;">Requests</p>
                <div style="height: 8px;"></div>
                <p style="font-size: 1.4rem; font-weight: 400; color: #ffffff; margin: 0; line-height: 1;">{total_reqs:,}</p>
            </div>
        """, unsafe_allow_html=True)
    with row1[1]:
        st.markdown(f"""
            <div style="background-color: #1f2833; padding: 10px; border-radius: 8px; border: 1px solid #45a29e; min-height: 90px; display: flex; flex-direction: column; justify-content: flex-start; overflow: hidden; margin-top: 2px;">
                <p style="font-weight: 400; font-size: 0.75rem; color: #c5c6c7; margin: 0; line-height: 1.2;">P95 Latency</p>
                <div style="height: 8px;"></div>
                <p style="font-size: 1.4rem; font-weight: 400; color: #ffffff; margin: 0; line-height: 1;">{p95_latency:.0f}ms</p>
            </div>
        """, unsafe_allow_html=True)

    st.markdown('<div style="height: 8px;"></div>', unsafe_allow_html=True)
    
    # Row 2: Success & Financials
    row2 = st.columns([1, 1])
    with row2[0]:
        st.markdown(f"""
            <div style="background-color: #1f2833; padding: 10px; border-radius: 8px; border: 1px solid #45a29e; min-height: 90px; display: flex; flex-direction: column; justify-content: flex-start; overflow: hidden;">
                <p style="font-weight: 400; font-size: 0.75rem; color: #c5c6c7; margin: 0; line-height: 1.2;">Success Rate</p>
                <div style="height: 8px;"></div>
                <p style="font-size: 1.4rem; font-weight: 400; color: {health_color}; margin: 0; line-height: 1;">{success_rate:.1f}%</p>
            </div>
        """, unsafe_allow_html=True)
    with row2[1]:
        st.markdown(f"""
            <div style="background-color: #1f2833; padding: 10px; border-radius: 8px; border: 1px solid #45a29e; min-height: 90px; display: flex; flex-direction: column; justify-content: flex-start; overflow: hidden;">
                <p style="font-weight: 400; font-size: 0.75rem; color: #c5c6c7; margin: 0; line-height: 1.2;">Total Spend</p>
                <div style="height: 8px;"></div>
                <p style="font-size: 1.4rem; font-weight: 400; color: #ffffff; margin: 0; line-height: 1;">${total_spend:.2f}</p>
            </div>
        """, unsafe_allow_html=True)

    st.markdown('<div style="height: 8px;"></div>', unsafe_allow_html=True)
    
    # Calculate individual qualities for the sidebar breakdown
    def get_prov_qual(prov_name):
        subset = filtered_df[filtered_df['provider'] == prov_name]
        return (subset['quality_score'].mean() * 100) if not subset.empty else None

    q_groq = get_prov_qual('groq')
    q_openrouter = get_prov_qual('openrouter')
    q_fallback = get_prov_qual('fallback')

    # Row 3: Intelligence Breakdown
    st.markdown('<p style="font-weight: 400; font-size: 0.75rem; color: #c5c6c7; margin-bottom: 4px;">Quality by Provider</p>', unsafe_allow_html=True)
    q_cols = st.columns(3)
    
    def render_qual_mini_box(col, name, val):
        if val is not None:
            color = get_status_color(val, is_failure=False)
            col.markdown(f"""
                <div style="background-color: #1a1a1a; padding: 6px; border-radius: 4px; border-left: 3px solid {color}; text-align: center;">
                    <p style="font-size: 0.65rem; color: #888; margin: 0; text-transform: uppercase;">{name}</p>
                    <p style="font-size: 0.9rem; font-weight: 700; color: {color}; margin: 0;">{val:.0f}%</p>
                </div>
            """, unsafe_allow_html=True)
        else:
            col.markdown(f"""
                <div style="background-color: #1a1a1a; padding: 6px; border-radius: 4px; border-left: 3px solid #333; text-align: center;">
                    <p style="font-size: 0.65rem; color: #555; margin: 0; text-transform: uppercase;">{name}</p>
                    <p style="font-size: 0.9rem; font-weight: 700; color: #555; margin: 0;">N/A</p>
                </div>
            """, unsafe_allow_html=True)

    render_qual_mini_box(q_cols[0], "Groq", q_groq)
    render_qual_mini_box(q_cols[1], "O-Router", q_openrouter)
    render_qual_mini_box(q_cols[2], "Fallback", q_fallback)

    st.markdown('<div style="height: 15px;"></div>', unsafe_allow_html=True)

st.write("---")

# 4. PER-PROVIDER AREA CHARTS (Similar to the UI Scrape's bottom panels)
if not filtered_df.empty:
    st.markdown("## Per-Provider Performance Analytics")
    # Enforce Sequence: Groq → Openrouter → Fallback
    ordered_providers = [p for p in ['groq', 'openrouter', 'fallback'] if p in providers]
    
    NUM_COLS = min(4, len(ordered_providers))
    if NUM_COLS > 0:
        grid_cols = st.columns(NUM_COLS)
        
        for i, provider in enumerate(ordered_providers):
            p_data = filtered_df[filtered_df['provider'] == provider].sort_values("created_at")
            
            with grid_cols[i % NUM_COLS]:
                with st.container(border=True):
                    st.write(f"### {provider.capitalize()}")
                    
                    if not p_data.empty:
                        # Reverting to 1s resampling + ffill as requested for better "second-by-second" texture
                        p_data_agg = p_data.set_index("created_at").resample("1S")["latency_ms"].mean().fillna(method='ffill').reset_index()
                        
                        avg_lat = p_data['latency_ms'].mean()
                        avg_cost = p_data['cost_cents'].mean()
                        fails = p_data['error_flag'].sum()
                        
                        mini_chart = (
                            alt.Chart(p_data_agg)
                            .mark_area(
                                color=alt.Gradient(
                                    gradient='linear',
                                    stops=[alt.GradientStop(color='#66c2a5', offset=0),
                                           alt.GradientStop(color='#3288bd', offset=1)],
                                    x1=1, x2=1, y1=1, y2=0
                                ),
                                opacity=0.7
                            )
                            .encode(
                                x=alt.X('created_at:T', axis=alt.Axis(labels=False, title=None, ticks=False)),
                                y=alt.Y('latency_ms:Q', axis=alt.Axis(labels=False, title=None, ticks=False)),
                                tooltip=['created_at', 'latency_ms']
                            )
                            .properties(height=120)
                        )
                        st.altair_chart(mini_chart, use_container_width=True)
                        
                        mc = st.columns([1.0, 1.0, 1.0, 0.7])
                        mc[0].metric("Lat", f"{avg_lat:.0f}ms")
                        mc[1].metric("Cost", f"{avg_cost:.2f}¢")
                        mc[2].metric("Qual", f"{(p_data['quality_score'].mean()*100):.0f}%")
                        mc[3].metric("Err", f"{fails}")
                    else:
                        st.info("No data")

# 5. DATABASE SNAPSHOT (Raw Data)
with st.expander("Database Snapshot (Raw Inference Logs)"):
    st.dataframe(filtered_df.sort_values(by="created_at", ascending=False).head(200), use_container_width=True)
