import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta

import boto3
import yaml
from botocore.exceptions import ClientError

ssm = boto3.client("ssm")
sts = boto3.client("sts")

PARAM_NAME = os.environ["NUKE_DATETIME_PARAM"]
ENDPOINT_SECRET = os.environ["ENDPOINT_SECRET"]

ALL_REGIONS = [
    "global",
    "us-east-1", "us-east-2",
    "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-west-3",
    "eu-central-1", "eu-central-2",
    "eu-north-1", "eu-south-1",
    "ap-southeast-1", "ap-southeast-2", "ap-southeast-3",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-south-1", "ap-south-2",
    "ap-east-1",
    "sa-east-1",
    "ca-central-1", "ca-west-1",
    "me-south-1", "me-central-1",
    "af-south-1",
    "il-central-1",
]


def lambda_handler(event, context):
    if event.get("source") == "aws.events":
        return _handle_schedule()

    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "")

    if method == "OPTIONS":
        return {
            "statusCode": 204,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        }

    if path == f"/{ENDPOINT_SECRET}/status" and method == "GET":
        return _handle_status()
    if path == f"/{ENDPOINT_SECRET}/reset" and method == "POST":
        return _handle_reset()
    if path == f"/{ENDPOINT_SECRET}/dryrun" and method == "GET":
        return _handle_dryrun()

    return {"statusCode": 404, "body": "Not Found"}


def _handle_status():
    nuke_at = _get_nuke_datetime()
    return _json_response(200, {
        "nuke_at": nuke_at.isoformat(),
        "seconds_remaining": max(0, int((nuke_at - datetime.now(timezone.utc)).total_seconds())),
    })


def _handle_reset():
    nuke_at = datetime.now(timezone.utc) + timedelta(days=30)
    _set_nuke_datetime(nuke_at)
    return _json_response(200, {
        "nuke_at": nuke_at.isoformat(),
        "message": "Timer reset by 30 days.",
    })


def _handle_dryrun():
    account_id = sts.get_caller_identity()["Account"]
    config = _build_nuke_config(account_id)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, dir="/tmp"
    ) as f:
        yaml.dump(config, f, default_flow_style=False)
        config_path = f.name

    # API Gateway HTTP API enforces a 29-second response timeout.
    # We cut the subprocess at 25s so we can still return partial results.
    truncated = False
    try:
        result = subprocess.run(
            [
                "/usr/local/bin/aws-nuke",
                "run",
                "--config", config_path,
                "--force",
                "--bypass-alias-check",
                # no --no-dry-run: this is intentionally a dry run
            ],
            timeout=25,
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or "") + (e.stderr or "")
        truncated = True

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "truncated": truncated,
            "note": "Results may be partial — API Gateway enforces a 29-second timeout." if truncated else None,
            "output": output,
        }),
    }


def _handle_schedule():
    nuke_at = _get_nuke_datetime()
    if datetime.now(timezone.utc) >= nuke_at:
        _run_nuke()
    return {"statusCode": 200}


def _run_nuke():
    account_id = sts.get_caller_identity()["Account"]
    config = _build_nuke_config(account_id)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, dir="/tmp"
    ) as f:
        yaml.dump(config, f, default_flow_style=False)
        config_path = f.name

    # 14 minutes — Lambda timeout is 15. If the account is large the daily
    # EventBridge rule will re-trigger until everything is gone.
    subprocess.run(
        [
            "/usr/local/bin/aws-nuke",
            "run",
            "--config", config_path,
            "--no-dry-run",
            "--force",
            "--bypass-alias-check",
        ],
        timeout=840,
        check=True,
    )


def _build_nuke_config(account_id: str) -> dict:
    return {
        # blocklist must contain at least one account ID that is NOT the target
        "blocklist": ["000000000000"],
        "regions": ALL_REGIONS,
        "accounts": {
            account_id: {
                "settings": {
                    "EC2Instance": {"DisableDeletionProtection": True},
                    "RDSInstance": {"DisableDeletionProtection": True},
                    "RDSCluster": {"DisableDeletionProtection": True},
                    "ElasticSearchDomain": {"DisableDeletionProtection": True},
                    "OpenSearchDomain": {"DisableDeletionProtection": True},
                }
            }
        },
    }


def _get_nuke_datetime() -> datetime:
    try:
        resp = ssm.get_parameter(Name=PARAM_NAME)
        value = resp["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ParameterNotFound":
            raise
        value = "UNSET"

    if value == "UNSET":
        nuke_at = datetime.now(timezone.utc) + timedelta(days=30)
        _set_nuke_datetime(nuke_at)
        return nuke_at

    return datetime.fromisoformat(value)


def _set_nuke_datetime(dt: datetime):
    ssm.put_parameter(
        Name=PARAM_NAME,
        Value=dt.isoformat(),
        Type="String",
        Overwrite=True,
    )


def _json_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        },
        "body": json.dumps(body),
    }
