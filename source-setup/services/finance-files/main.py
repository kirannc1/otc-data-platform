import hashlib
import json
import os

import boto3
from botocore.exceptions import ClientError

FILES = {
    "parent": {
        "bank.csv": "transaction_id,date,currency,amount_minor,payment_reference\nP-B1,2026-11-10,INR,2500000,P-P1\n",
        "budget.csv": "period,category,currency,budget_minor\n2026-11,PUMP,INR,12000000\n",
    },
    "dealer": {
        "bank.csv": "reference,booking_day,credit_inr,receipt\nD-B1,2026-11-12,10000.00,D-P1\n",
        "budget.csv": "month,local_account,plan_inr\n2026-11,400-D,50000.00\n",
    },
}


def main():
    client = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"])
    bucket = os.environ["INBOUND_BUCKET"]
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"404", "NoSuchBucket"}:
            raise
        client.create_bucket(Bucket=bucket)
    for entity, files in FILES.items():
        prefix = f"{entity}/finance/dealer-order-v1"
        entries = []
        for name, text in files.items():
            data = text.encode()
            key = f"{prefix}/{name}"
            client.put_object(Bucket=bucket, Key=key, Body=data)
            entries.append({"key": key, "sha256": hashlib.sha256(data).hexdigest(), "rows": 1})
        client.put_object(
            Bucket=bucket, Key=f"{prefix}/manifest.json",
            Body=json.dumps({"entity": entity, "files": entries}).encode(),
        )
        print(f"Published {prefix}/manifest.json", flush=True)


if __name__ == "__main__":
    main()
