"""Executive dashboards over the published OTC semantic views."""
from contextlib import closing
from datetime import timedelta
import os
from pathlib import Path

import altair as alt
import duckdb
import streamlit as st

PALETTE = ["#155e75", "#33856c", "#cc7334", "#7855a1", "#6a7b8b", "#aa4651"]
BUCKETS = ["Current", "1–30", "31–60", "61–90", "91+", "Unknown"]
DASHBOARD_STYLE = """
<style>
[data-testid="stMainBlockContainer"] {max-width: 1360px; padding-top: 2.2rem;}
</style>
"""


def frame(db, sql, params=()):
    return db.execute(sql, params).df()


def row(db, sql, params=()):
    return db.execute(sql, params).fetchone()


def refresh_options(db, view, column, where, params):
    return [item[0] for item in db.execute(
        f"SELECT DISTINCT {column} FROM {view} WHERE {column} IS NOT NULL AND ({where}) ORDER BY 1",
        params,
    ).fetchall()]


def select(db, view, column, label, *, where="true", params=(), allow_all=True, key):
    options = refresh_options(db, view, column, where, params)
    values = ([None] if allow_all else []) + options
    return st.selectbox(label, values, format_func=lambda x: "All" if x is None else str(x), key=key)


def currency_entity(db, view, key):
    left, right = st.columns(2)
    with left:
        currency = select(db, view, "currency", "Currency", allow_all=False, key=f"{key}_currency")
    with right:
        entity = select(db, view, "legal_entity_id", "Legal entity", where="currency = ?",
                        params=[currency], key=f"{key}_entity")
    return currency, entity


def date_window(db, view, field, key, default_days=90):
    first, last = row(db, f"SELECT min({field}), max({field}) FROM {view}")
    if first is None:
        st.info("This release contains no observations for this view.")
        return None
    start = max(first, last - timedelta(days=default_days - 1))
    chosen = st.date_input("Date range", (start, last), min_value=first, max_value=last, key=key)
    return chosen if len(chosen) == 2 else None


def period_for(days, events=False):
    span = (days[1] - days[0]).days
    if events and span == 0:
        return "5 minutes"
    if events and span <= 14:
        return "hour"
    if span <= 120:
        return "day"
    if span <= 730:
        return "week"
    return "month" if span <= 365 * 15 else "year"


def amount(value, currency):
    return "—" if value is None else f"{currency} {value:,.2f}"


def integer(value):
    return "—" if value is None else f"{value:,}"


def kpis(*items):
    with st.container(horizontal=True):
        for label, value in items:
            with st.container(border=True, width=224, height="stretch", gap="xsmall"):
                st.caption(label)
                st.subheader(value, anchor=False)


def heading(title, description):
    st.title(title)
    st.caption(description)


def plot(chart):
    st.altair_chart(chart.configure_view(stroke=None).configure_axis(
        gridColor="#8e9fab", gridOpacity=0.20, domain=False, labelLimit=155, titlePadding=9,
    ).configure_legend(orient="bottom"), width="stretch")


def trend(data, unit, *, utc=False):
    if data.empty:
        st.info("No observations match the selected filters.")
        return
    plot(alt.Chart(data).mark_line(point=True, strokeWidth=2.3).encode(
        x=alt.X("period:T", title="Period start" + (" (UTC)" if utc else ""), scale=alt.Scale(type="utc")),
        y=alt.Y("value:Q", title=unit, axis=alt.Axis(format=",~f", labelLimit=0, labelOverlap="greedy")),
        color=alt.Color("metric:N", title=None, scale=alt.Scale(range=PALETTE)),
        tooltip=[alt.Tooltip("period:T", title="Period start", format="%d %b %Y %H:%M" if utc else "%d %b %Y"),
                 alt.Tooltip("metric:N", title="Measure"), alt.Tooltip("value:Q", title=unit, format=",.2f")],
    ).properties(height=275))


def horizontal(data, category="category", *, value="value", unit=None, series=False, order="-x"):
    if data.empty:
        st.info("No observations match the selected filters.")
        return
    base = alt.Chart(data).mark_bar(cornerRadiusEnd=3).encode(
        x=alt.X(f"{value}:Q", title=unit, axis=alt.Axis(format=",~f", labelLimit=0, labelOverlap="greedy"), stack=None),
        y=alt.Y(f"{category}:N", title=None, sort=order),
        tooltip=[alt.Tooltip(f"{category}:N", title="Group"), alt.Tooltip(f"{value}:Q", title=unit, format=",.2f")],
    )
    if series:
        base = base.encode(
            yOffset="metric:N",
            color=alt.Color("metric:N", title=None, scale=alt.Scale(range=PALETTE)),
            tooltip=[alt.Tooltip(f"{category}:N", title="Group"), alt.Tooltip("metric:N", title="Measure"),
                     alt.Tooltip(f"{value}:Q", title=unit, format=",.2f")],
        )
    else:
        base = base.encode(color=alt.value(PALETTE[0]))
    plot(base.properties(height=max(145, min(380, data[category].nunique() * (37 if series else 30)))))


def sales(db):
    heading("Sales performance", "Order bookings and net billing by business date and legal entity.")
    days = date_window(db, "sem_sales_daily", "day", "sales_days")
    if days is None:
        return
    currency, entity = currency_entity(db, "sem_sales_daily", "sales")
    count, linked, rows, booked, billed = row(db, "SELECT * FROM sales_kpis(?, ?, ?, ?)", [*days, entity, currency])
    if not rows:
        st.info("No sales observations match the selection.")
        return
    kpis(("Booked amount", amount(booked, currency)), ("Net billed", amount(billed, currency)),
         ("Booked orders", integer(count)), ("With linked shipment", integer(linked)))
    where = "day BETWEEN ? AND ? AND currency = ? AND (? IS NULL OR legal_entity_id = ?)"
    params = [*days, currency, entity, entity]
    grain = period_for(days)
    st.subheader(f"Bookings and billing by {grain}")
    data = frame(db, f"""
        SELECT time_bucket(INTERVAL '1 {grain}', day) AS period,
               sum(booked_amount) AS Bookings, sum(net_billed_amount) AS Billing
        FROM sem_sales_daily WHERE {where} GROUP BY 1 ORDER BY 1
    """, params).melt("period", var_name="metric", value_name="value")
    trend(data, currency)
    st.subheader("Contribution by legal entity")
    data = frame(db, f"""
        SELECT legal_entity_id AS category, sum(booked_amount) AS Bookings,
               sum(net_billed_amount) AS Billing
        FROM sem_sales_daily WHERE {where} GROUP BY 1
    """, params).melt("category", var_name="metric", value_name="value")
    horizontal(data, unit=currency, series=True)
    st.caption("Bookings follow order dates. Billing follows invoice and credit-note posting dates. Gross margin is currently unavailable.")


def fulfillment(db):
    days = date_window(db, "sem_order_lines", "booking_date", "orders_days")
    if days is None:
        return
    chosen = select(db, "sem_order_lines", "legal_entity_id", "Legal entity", key="orders_entity")
    params = [*days, chosen, chosen]
    order_scope = """
        WITH scoped_orders AS (
            SELECT sales_order_id, legal_entity_id, min(booking_date) AS booking_date,
                   bool_or(linked_shipment_lines > 0) AS has_shipment
            FROM sem_order_lines
            WHERE booking_date BETWEEN ? AND ? AND (? IS NULL OR legal_entity_id = ?)
            GROUP BY sales_order_id, legal_entity_id
        )
    """
    total, linked = row(db, order_scope + "SELECT count(*), count(*) FILTER (WHERE has_shipment) FROM scoped_orders", params)
    if not total:
        st.info("No bookings match the selection.")
        return
    kpis(("Booked orders", integer(total)), ("With linked shipment", integer(linked)),
         ("Without linked shipment", integer(total - linked)), ("Shipment-link coverage", f"{linked / total:.0%}"))
    grain = period_for(days)
    st.subheader(f"Booking volume by {grain}")
    volume = frame(db, order_scope + f"""
        SELECT time_bucket(INTERVAL '1 {grain}', booking_date) AS period,
               count(*) AS Bookings, count(*) FILTER (WHERE has_shipment) AS "With linked shipment"
        FROM scoped_orders GROUP BY 1 ORDER BY 1
    """, params).melt("period", var_name="metric", value_name="value")
    trend(volume, "Orders")
    st.subheader("Shipment links by legal entity")
    entity = frame(db, order_scope + """
        SELECT legal_entity_id AS category, count(*) FILTER (WHERE has_shipment) AS "With shipment",
               count(*) FILTER (WHERE NOT has_shipment) AS "Without shipment"
        FROM scoped_orders GROUP BY 1
    """, params).melt("category", var_name="metric", value_name="value")
    horizontal(entity, unit="Orders", series=True)
    st.caption("A linked shipment can represent partial fulfillment. This view groups by booking date.")


def storefront(db):
    days = date_window(db, "sem_orders_5m", "CAST(window_start_utc AS DATE)", "event_days", default_days=30)
    if days is None:
        return
    channel = select(db, "sem_orders_5m", "channel", "Channel", key="event_channel")
    where = "CAST(window_start_utc AS DATE) BETWEEN ? AND ? AND (? IS NULL OR channel = ?)"
    params = [*days, channel, channel]
    accepted, cancelled = row(db, f"""
        SELECT sum(accepted_distinct_orders)::BIGINT, sum(cancellation_events)::BIGINT
        FROM sem_orders_5m WHERE {where}
    """, params)
    if accepted is None:
        st.info("No storefront events match the selection.")
        return
    kpis(("Accepted appearances", integer(accepted)), ("Cancellation events", integer(cancelled)))
    grain = period_for(days, events=True)
    interval = grain if grain == "5 minutes" else f"1 {grain}"
    st.subheader("Storefront activity over time")
    data = frame(db, f"""
        SELECT time_bucket(INTERVAL '{interval}', window_start_utc) AS period,
               sum(accepted_distinct_orders) AS Acceptances, sum(cancellation_events) AS Cancellations
        FROM sem_orders_5m WHERE {where} GROUP BY 1 ORDER BY 1
    """, params).melt("period", var_name="metric", value_name="value")
    trend(data, "Activity", utc=True)
    st.caption("Acceptances are distinct within each five-minute window and channel. Longer periods sum those appearances.")


def orders(db):
    heading("Orders and fulfillment", "Booking volume, shipment links, and storefront activity.")
    section = st.radio("View", ["Fulfillment", "Storefront activity"], horizontal=True, label_visibility="collapsed")
    (fulfillment if section == "Fulfillment" else storefront)(db)


def customer(db):
    heading("Customer 360", "Observed billing concentration across conformed customers.")
    currency = select(db, "sem_customer_360", "currency", "Currency", allow_all=False, key="customer_currency")
    total, identified, unresolved, inactive, covered = row(db, """
        SELECT sum(observed_net_billed_amount),
               count(DISTINCT customer) FILTER (WHERE customer <> 'Unresolved'),
               sum(observed_net_billed_amount) FILTER (WHERE customer = 'Unresolved'),
               count(DISTINCT customer) FILTER (WHERE inactive_120_days IS TRUE),
               count(DISTINCT customer) FILTER (WHERE inactive_120_days IS NOT NULL)
        FROM sem_customer_360 WHERE currency = ?
    """, [currency])
    if currency is None:
        st.info("This release has no customer observations.")
        return
    kpis(("Observed net billing", amount(total, currency)), ("Identified customers", integer(identified)),
         ("Unresolved customer billing", amount(unresolved, currency)),
         ("Inactive for 120 days", integer(inactive) if covered else "Unavailable"))
    st.subheader("Top 12 identified customers by observed net billing")
    horizontal(frame(db, """
        SELECT customer AS category, sum(observed_net_billed_amount) AS value
        FROM sem_customer_360 WHERE currency = ? AND customer <> 'Unresolved'
        GROUP BY 1 HAVING value IS NOT NULL ORDER BY value DESC, category LIMIT 12
    """, [currency]), unit=currency)
    st.caption("Observed billing covers the selected release. The inactivity count is limited to customers with sufficient history.")


def ar(db):
    heading("Receivables aging", "Open invoice balances at the published cutoff.")
    currency, entity = currency_entity(db, "sem_ar_aging", "ar")
    where = "currency = ? AND (? IS NULL OR legal_entity_id = ?)"
    params = [currency, entity, entity]
    count, total, overdue, unknown = row(db, f"""
        SELECT count(*), sum(open_amount), sum(open_amount) FILTER (WHERE days_past_due > 0),
               sum(open_amount) FILTER (WHERE days_past_due IS NULL)
        FROM sem_ar_aging WHERE {where}
    """, params)
    if not count:
        st.info("No open balance observations match the selection.")
        return
    kpis(("Open AR", amount(total, currency)), ("Past due", amount(overdue or 0, currency)),
         ("Open invoices", integer(count)), ("Unknown due date", amount(unknown or 0, currency)))
    left, right = st.columns(2)
    with left:
        st.subheader("Open balance by aging bucket")
        horizontal(frame(db, f"""
            SELECT coalesce(aging_bucket, 'Unknown') AS category, legal_entity_id AS metric, sum(open_amount) AS value
            FROM sem_ar_aging WHERE {where} GROUP BY 1, 2
        """, params), unit=currency, series=True, order=BUCKETS)
    with right:
        st.subheader("Balance coverage")
        horizontal(frame(db, f"""
            SELECT replace(coverage_status, '_', ' ') AS category, sum(open_amount) AS value
            FROM sem_ar_aging WHERE {where} GROUP BY 1
        """, params), unit=currency)


def collections(db):
    heading("Collections", "Invoiced amounts and cash posted against invoice due-month cohorts.")
    currency, entity = currency_entity(db, "sem_collections_monthly", "collections")
    where = "currency = ? AND (? IS NULL OR legal_entity_id = ?)"
    params = [currency, entity, entity]
    invoices, gross, cash, eligible_cohorts, cohorts = row(db, f"""
        SELECT sum(invoices)::BIGINT, sum(gross_due_amount), sum(cash_by_payment_posting_date),
               count(*) FILTER (WHERE eligible_due_amount IS NOT NULL), count(*)
        FROM sem_collections_monthly WHERE {where}
    """, params)
    if not cohorts:
        st.info("No collection cohorts match the selection.")
        return
    kpis(("Invoices in cohorts", integer(invoices)), ("Gross invoice amount", amount(gross, currency)),
         ("Cash posted", amount(cash, currency)), ("Cohorts with eligible due", f"{eligible_cohorts:,} / {cohorts:,}"))
    st.subheader("Gross invoices and posted cash by due month")
    data = frame(db, f"""
        SELECT coalesce(due_month, 'Unknown') AS category, sum(gross_due_amount) AS "Gross invoices",
               sum(cash_by_payment_posting_date) AS "Cash posted"
        FROM sem_collections_monthly WHERE {where}
        GROUP BY 1 ORDER BY category DESC LIMIT 12
    """, params).melt("category", var_name="metric", value_name="value")
    horizontal(data, unit=currency, series=True, order="ascending")
    st.caption("The chart shows up to 12 due-month groups. Approved collection rate and average days to pay require settlement timing.")


def budget(db):
    heading("P&L vs Budget", "Plan and actual compared at a common scenario, version, and reporting scope.")
    view = "sem_pnl_vs_budget"
    currency, entity = currency_entity(db, view, "budget")
    base = "currency = ? AND (? IS NULL OR legal_entity_id = ?)"
    params = [currency, entity, entity]
    for column, label in (("scenario", "Scenario"), ("budget_version", "Budget version"), ("scope_type", "Scope type")):
        selected = select(db, view, column, label, where=base, params=params, allow_all=False, key=f"budget_{column}")
        if selected is None:
            st.info("No budget scopes match the selection.")
            return
        base += f" AND {column} = ?"
        params.append(selected)
    total, comparable, planned, actual, variance = row(db, f"""
        SELECT count(*), count(variance), sum(budget_amount) FILTER (WHERE variance IS NOT NULL),
               sum(actual_amount) FILTER (WHERE variance IS NOT NULL), sum(variance)
        FROM {view} WHERE {base}
    """, params)
    kpis(("Comparable budget", amount(planned, currency)), ("Comparable actual", amount(actual, currency)),
         ("Variance to plan", amount(variance, currency)), ("Comparable plan rows", f"{comparable:,} / {total:,}"))
    if comparable:
        st.subheader("Comparable budget and actual by period")
        data = frame(db, f"""
            SELECT period AS category, sum(budget_amount) AS Budget, sum(actual_amount) AS Actual
            FROM {view} WHERE {base} AND variance IS NOT NULL GROUP BY 1 ORDER BY category DESC LIMIT 12
        """, params).melt("category", var_name="metric", value_name="value")
        horizontal(data, unit=currency, series=True, order="ascending")
    if comparable < total:
        st.subheader("Plan coverage by legal entity")
        coverage = frame(db, f"""
            SELECT legal_entity_id AS category, count(variance) AS Comparable,
                   count(*) FILTER (WHERE variance IS NULL) AS "Actual unavailable"
            FROM {view} WHERE {base} GROUP BY 1
        """, params).melt("category", var_name="metric", value_name="value")
        horizontal(coverage, unit="Plan rows", series=True)
        st.caption("Plan rows without comparable actuals are outside the variance calculation.")


def reconciliation(db):
    heading("Revenue reconciliation", "Billing documents, their mapped ledger postings, and exceptions.")
    view = "sem_revenue_reconciliation"
    scopes = """(
        SELECT currency, legal_entity_id FROM sem_revenue_reconciliation
        UNION
        SELECT currency, legal_entity_id FROM sem_revenue_unlinked_postings
    ) AS scopes"""
    currency, entity = currency_entity(db, scopes, "revenue")
    if currency is None:
        st.info("This release contains no revenue reconciliation observations.")
        return
    where = "currency = ? AND (? IS NULL OR legal_entity_id = ?)"
    params = [currency, entity, entity]
    total, match, exceptions, diff = row(db, f"""
        SELECT count(*), count(*) FILTER (WHERE status = 'matched'),
               count(*) FILTER (WHERE status IS DISTINCT FROM 'matched'), sum(abs(difference))
        FROM {view} WHERE {where}
    """, params)
    unlinked, = row(db, f"SELECT count(*) FROM sem_revenue_unlinked_postings WHERE {where}", params)
    kpis(("Matched documents", f"{match:,} / {total:,}"), ("Documents to review", integer(exceptions)),
         ("Absolute known difference", amount(diff, currency)), ("Unlinked GL lines", integer(unlinked)))
    st.subheader("Document outcomes by legal entity")
    horizontal(frame(db, f"""
        SELECT CASE status WHEN 'matched' THEN 'Matched' WHEN 'difference' THEN 'Difference'
                   ELSE 'Unmatched or uncovered' END AS category,
               legal_entity_id AS metric, count(*) AS value
        FROM {view} WHERE {where} GROUP BY 1, 2
    """, params), unit="Documents", series=True)
    if (exceptions or unlinked) and st.toggle("Show exception records", key="revenue_detail"):
        if exceptions:
            st.caption("Up to 25 billing exceptions, largest known differences first.")
            st.dataframe(frame(db, f"""
                SELECT billing_document_id, legal_entity_id, signed_billed_amount, posted_revenue, difference, status
                FROM {view} WHERE {where} AND status IS DISTINCT FROM 'matched'
                ORDER BY abs(difference) DESC NULLS LAST, billing_document_id LIMIT 25
            """, params), hide_index=True, width="stretch")
        if unlinked:
            st.caption("Up to 25 unlinked GL lines, newest first.")
            st.dataframe(frame(db, f"""
                SELECT posting_date, legal_entity_id, voucher_id, gl_entry_id, debit_amount, credit_amount
                FROM sem_revenue_unlinked_postings WHERE {where} ORDER BY posting_date DESC, gl_entry_id LIMIT 25
            """, params), hide_index=True, width="stretch")


PAGES = {
    "Sales": sales, "Orders": orders, "Customer 360": customer, "AR Aging": ar,
    "Collections": collections, "P&L vs Budget": budget, "Revenue Reconciliation": reconciliation,
}


def main():
    st.set_page_config(page_title="OTC Executive Analytics", layout="wide")
    st.markdown(DASHBOARD_STYLE, unsafe_allow_html=True)
    database = Path(os.environ.get("OTC_SEMANTIC_DB", Path(__file__).with_name("otc.duckdb")))
    st.sidebar.title("OTC Executive Analytics")
    page = st.sidebar.radio("Dashboard", PAGES.keys())
    if not database.is_file():
        st.error("Build the DuckDB serving file first. See semantic/README.md.")
        st.stop()
    with closing(duckdb.connect(str(database), read_only=True)) as db:
        db.execute("SET TimeZone = 'UTC'")
        gold, silver, cutoff = row(db, "SELECT gold_release_id, silver_run_id, cutoff FROM semantic_release")
        st.sidebar.caption(f"Reporting cutoff · {cutoff:%d %b %Y}")
        with st.sidebar.expander("Release lineage"):
            st.text(f"Gold\n{gold}\n\nSilver\n{silver}")
        PAGES[page](db)


main()
