FROM apache/airflow:3.1.8-python3.12
USER airflow
RUN pip install --no-cache-dir "apache-airflow==3.1.8" "psycopg[binary]==3.2.13" "confluent-kafka==2.10.0" boto3
COPY --chown=airflow:root dags /opt/otc/dags
