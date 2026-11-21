from datetime import timedelta

import pendulum
from airflow.sdk import dag, get_current_context, task

from bronze_ingest import (
    copy_manifests,
    dealer_collections,
    drain_kafka,
    erp_collections,
    pos_collections,
    save_snapshot,
)


@task
def erpnext():
    save_snapshot("erpnext", get_current_context()["run_id"], erp_collections())


@task
def parent_pos():
    save_snapshot("parent-pos", get_current_context()["run_id"], pos_collections())


@task
def dealer_sql():
    save_snapshot("dealer-sql", get_current_context()["run_id"], dealer_collections())


@task
def inbound_files():
    copy_manifests()


@task
def kafka_orders():
    drain_kafka()


DAG_OPTIONS = {
    "start_date": pendulum.datetime(2026, 1, 1, tz="UTC"),
    "schedule": None,
    "catchup": False,
    "max_active_runs": 1,
    "default_args": {"retries": 1, "retry_delay": timedelta(seconds=30)},
    "tags": ["otc", "bronze"],
}


@dag(dag_id="otc_bronze_rest", **DAG_OPTIONS)
def rest_dag():
    erpnext()
    parent_pos()


@dag(dag_id="otc_bronze_sql", **DAG_OPTIONS)
def sql_dag():
    dealer_sql()


@dag(dag_id="otc_bronze_files", **DAG_OPTIONS)
def files_dag():
    inbound_files()


@dag(dag_id="otc_bronze_kafka", **DAG_OPTIONS)
def kafka_dag():
    kafka_orders()


rest_dag()
sql_dag()
files_dag()
kafka_dag()
