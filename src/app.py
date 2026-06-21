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
sns = boto3.client("sns")
cfn = boto3.client("cloudformation")

PARAM_NAME = os.environ["NUKE_DATETIME_PARAM"]
ENDPOINT_SECRET = os.environ["ENDPOINT_SECRET"]
STACK_NAME = os.environ.get("STACK_NAME", "dms")
ALERT_TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN")


S3_RESOURCE_TYPES = ["S3Bucket", "S3Object", "S3MultipartUpload"]

CFN_TO_NUKE = {
    "AWS::Lambda::Function": "LambdaFunction",
    "AWS::IAM::Role": "IAMRole",
    "AWS::SSM::Parameter": "SSMParameter",
    "AWS::ApiGatewayV2::Api": "APIGatewayV2API",
    "AWS::ApiGatewayV2::Stage": "APIGatewayV2Stage",
    "AWS::Events::Rule": "CloudWatchEventsRule",
    "AWS::SNS::Topic": "SNSTopic",
    "AWS::SNS::Subscription": "SNSSubscription",
    "AWS::CloudWatch::Alarm": "CloudWatchAlarm",
}

MANAGED_POLICIES = [
    "AdministratorAccess",
    "AWSLambdaBasicExecutionRole",
]


def lambda_handler(event, context):
    action = event.get("action")
    if action == "nuke_check":
        return _handle_nuke_check(context)
    if action == "daily_reminder":
        return _handle_daily_reminder()
    if action == "dryrun":
        return _handle_dryrun()

    # Legacy: EventBridge without Input
    if event.get("source") == "aws.events":
        return _handle_nuke_check(context)

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
    _send_notification(
        "DMS Timer Reset",
        f"The dead man's switch timer has been reset.\n\n"
        f"New nuke date: {nuke_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Days remaining: 30",
    )
    return _json_response(200, {
        "nuke_at": nuke_at.isoformat(),
        "message": "Timer reset by 30 days.",
    })


def _handle_daily_reminder():
    nuke_at = _get_nuke_datetime()
    remaining = (nuke_at - datetime.now(timezone.utc)).total_seconds()
    days = remaining / 86400

    if days <= 5:
        _send_notification(
            f"DMS WARNING: {max(0, int(days))} days remaining",
            f"Your dead man's switch will activate on {nuke_at.strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
            f"Days remaining: {days:.1f}\n\n"
            f"Reset the timer now to prevent account deletion.",
        )

    return {"statusCode": 200}


def _handle_nuke_check(context):
    nuke_at = _get_nuke_datetime()
    if datetime.now(timezone.utc) >= nuke_at:
        _run_nuke(context)
    return {"statusCode": 200}


def _handle_dryrun():
    account_id = sts.get_caller_identity()["Account"]
    filters = _get_stack_protection_filters()

    print("=== Phase 1: S3 dry run ===")
    s3_config = _build_nuke_config(
        account_id, filters=filters, resource_targets=S3_RESOURCE_TYPES,
    )
    _exec_aws_nuke(s3_config, dry_run=True)

    print("=== Phase 2: Everything else dry run ===")
    full_config = _build_nuke_config(account_id, filters=filters)
    _exec_aws_nuke(full_config, dry_run=True)

    return {"statusCode": 200, "body": "Dry run complete — check CloudWatch logs."}


def _run_nuke(context):
    account_id = sts.get_caller_identity()["Account"]
    filters = _get_stack_protection_filters()

    print("=== Phase 1: Nuking S3 ===")
    s3_config = _build_nuke_config(
        account_id, filters=filters, resource_targets=S3_RESOURCE_TYPES,
    )
    _exec_aws_nuke(s3_config, timeout=420)

    remaining_ms = context.get_remaining_time_in_millis()
    timeout = max(60, (remaining_ms // 1000) - 30)
    print(f"=== Phase 2: Nuking everything else ({timeout}s remaining) ===")
    full_config = _build_nuke_config(account_id, filters=filters)
    _exec_aws_nuke(full_config, timeout=timeout)


def _exec_aws_nuke(config, dry_run=False, timeout=None):
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, dir="/tmp"
    ) as f:
        yaml.dump(config, f, default_flow_style=False)
        config_path = f.name

    cmd = [
        "/usr/local/bin/aws-nuke", "run",
        "--config", config_path,
        "--no-prompt",
        "--no-alias-check",
    ]
    if not dry_run:
        cmd.append("--no-dry-run")

    if dry_run:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for line in proc.stdout:
            print(line, end="")
        proc.wait()
        print(f"=== aws-nuke finished (exit code {proc.returncode}) ===")
    else:
        try:
            subprocess.run(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"aws-nuke timed out after {timeout}s — will continue on next invocation")
        except subprocess.CalledProcessError as e:
            print(f"aws-nuke exited with code {e.returncode}")


def _get_stack_protection_filters():
    resources = cfn.describe_stack_resources(
        StackName=STACK_NAME,
    )["StackResources"]

    filters = {}
    role_names = []

    for r in resources:
        nuke_type = CFN_TO_NUKE.get(r["ResourceType"])
        physical_id = r["PhysicalResourceId"]

        if not nuke_type:
            continue

        filters.setdefault(nuke_type, []).append(physical_id)

        if r["ResourceType"] == "AWS::IAM::Role":
            role_names.append(physical_id)
        elif r["ResourceType"] == "AWS::Lambda::Function":
            filters.setdefault("CloudWatchLogsLogGroup", []).append(
                f"/aws/lambda/{physical_id}"
            )

    for role_name in role_names:
        attachments = filters.setdefault("IAMRolePolicyAttachment", [])
        for policy in MANAGED_POLICIES:
            attachments.append(f"{role_name} -> {policy}")

    print(f"Stack protection filters: {json.dumps(filters, indent=2)}")
    return filters


def _build_nuke_config(account_id, filters=None, resource_targets=None):
    config = {
        "blocklist": ["000000000000"],
        "regions": ["all"],
        "accounts": {
            account_id: {
                "settings": {
                    "EC2Instance": {"DisableDeletionProtection": True},
                    "RDSInstance": {"DisableDeletionProtection": True},
                    "RDSCluster": {"DisableDeletionProtection": True},
                    "ElasticSearchDomain": {"DisableDeletionProtection": True},
                    "OpenSearchDomain": {"DisableDeletionProtection": True},
                },
            }
        },
    }

    if filters:
        config["accounts"][account_id]["filters"] = filters

    if resource_targets:
        config["resource-types"] = {"includes": resource_targets}

    return config


def _get_nuke_datetime():
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


def _set_nuke_datetime(dt):
    ssm.put_parameter(
        Name=PARAM_NAME,
        Value=dt.isoformat(),
        Type="String",
        Overwrite=True,
    )


def _send_notification(subject, message):
    if not ALERT_TOPIC_ARN:
        print(f"No ALERT_TOPIC_ARN set, skipping notification: {subject}")
        return
    sns.publish(
        TopicArn=ALERT_TOPIC_ARN,
        Subject=subject,
        Message=message,
    )


def _json_response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        },
        "body": json.dumps(body),
    }
