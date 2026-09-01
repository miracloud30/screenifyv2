#!/usr/bin/env python3
"""
Seed tenants and jobs into DynamoDB from seed/*.json.
Run by deploy.ps1 after the stack is up. Plain JSON in, correct DynamoDB
types out (ints -> Decimal). Idempotent: put_item overwrites by key.

Usage: python scripts/seed.py <region>
"""
import json
import sys
from decimal import Decimal

import boto3

region = sys.argv[1] if len(sys.argv) > 1 else "eu-north-1"
ddb = boto3.resource("dynamodb", region_name=region)


def to_decimals(obj):
    if isinstance(obj, dict):
        return {k: to_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_decimals(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return Decimal(str(obj))
    return obj


def seed(table_name, path):
    table = ddb.Table(table_name)
    with open(path) as f:
        items = json.load(f)
    for item in items:
        table.put_item(Item=to_decimals(item))
    print(f"  seeded {len(items)} into {table_name}")


if __name__ == "__main__":
    print("Seeding tenants and jobs...")
    seed("screenify-tenants", "seed/tenants.json")
    seed("screenify-jobs", "seed/jobs.json")
    print("Seed complete.")
