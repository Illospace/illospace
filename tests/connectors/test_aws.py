"""AWS tests use fake clients. They make no AWS request."""

# ruff: noqa: F811

import json
import logging
import threading
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ParamValidationError
from pydantic import ValidationError

from illo.connectors import aws, store
from illo.connectors.config import AWS_SERVICES, Auth, Connector
from illo.tools.registry import Refusal

KEYS = {"access_key_id": "ACMEFAKEACCESSKEY0001", "secret_access_key": "acme-private-test-value"}


def config(**kwargs):
    return Connector(
        key="aws",
        kind="aws",
        scope="team",
        auth=Auth(type="access_key", secret="aws-illo"),
        **kwargs,
    )


@pytest.mark.parametrize(
    "service,operation",
    [
        ("logs", "StartQuery"),
        ("logs", "StopQuery"),
        ("logs", "FilterLogEvents"),
        ("logs", "DescribeLogGroups"),
        ("logs", "GetQueryResults"),
        ("cloudwatch", "GetMetricData"),
        ("autoscaling", "DescribeAutoScalingGroups"),
        ("elbv2", "DescribeTargetHealth"),
        ("codebuild", "BatchGetBuilds"),
        ("lambda", "ListFunctions"),
        ("rds", "DescribeDBInstances"),
    ],
)
def test_read_filter_allows_named_reads(service, operation):
    aws.allowed(config(), service, operation)


@pytest.mark.parametrize(
    "service",
    [
        "secretsmanager",
        "ssm",
        "kms",
        "sts",
        "iam",
        "s3",
        "dynamodb",
        "sqs",
        "ec2-instance-connect",
    ],
)
def test_secret_and_bulk_services_are_invalid(service):
    with pytest.raises(ValidationError, match="supported AWS read services"):
        config(services=[service])
    with pytest.raises(Refusal, match="does not allow this service"):
        aws.allowed(config(), service, "GetSecretValue")


@pytest.mark.parametrize(
    "service,operation",
    [
        ("logs", "DeleteLogGroup"),
        ("logs", "PutLogEvents"),
        ("lambda", "Invoke"),
        ("cloudwatch", "StartQuery"),
        ("ecs", "ExecuteCommand"),
        ("lambda", "GetLayerVersion"),
        ("lambda", "GetLayerVersionByArn"),
    ],
)
def test_writes_and_code_downloads_are_refused(service, operation):
    with pytest.raises(Refusal, match="safe read"):
        aws.allowed(config(), service, operation)


def test_defaults_and_service_subset():
    assert config().services == list(AWS_SERVICES)
    assert config().customer_data is True
    with pytest.raises(Refusal):
        aws.allowed(config(services=["logs"]), "cloudwatch", "GetMetricData")
    for args in ({"kind": "aws", "scope": "personal"}, {"kind": "aws", "scope": "team"}):
        with pytest.raises(ValidationError):
            Connector(key="aws", **args)
    assert Connector(
        key="acme", kind="mcp", scope="team", url="https://example.com/mcp"
    ).customer_data
    assert not Connector(
        key="acme", kind="mcp", scope="team", url="https://example.com/mcp", customer_data=False
    ).customer_data
    assert Connector(key="google", kind="google", scope="personal").customer_data is None
    with pytest.raises(ValidationError):
        Connector(key="google", kind="google", scope="personal", customer_data=True)


@pytest.mark.parametrize(
    "service,value,expected",
    [
        (
            "lambda",
            {
                "Configuration": {
                    "FunctionName": "acme-api",
                    "Environment": {"Variables": {"KEY": "secret"}},
                },
                "Code": {"Location": "secret"},
            },
            {"Configuration": {"FunctionName": "acme-api"}},
        ),
        (
            "lambda",
            {
                "Functions": [
                    {"Environment": {"Variables": {"KEY": "secret"}}, "FunctionName": "acme-api"}
                ]
            },
            {"Functions": [{"FunctionName": "acme-api"}]},
        ),
        (
            "codebuild",
            {
                "projects": [
                    {
                        "environment": {
                            "environmentVariables": [
                                {"name": "KEY", "value": "secret", "type": "PLAINTEXT"}
                            ],
                            "registryCredential": {"credential": "secret"},
                        }
                    }
                ]
            },
            {
                "projects": [
                    {
                        "environment": {
                            "environmentVariables": [{"name": "KEY", "type": "PLAINTEXT"}]
                        }
                    }
                ]
            },
        ),
        (
            "ecs",
            {
                "taskDefinition": {
                    "containerDefinitions": [
                        {
                            "environment": [{"name": "KEY", "value": "secret"}],
                            "secrets": [{"name": "KEY", "valueFrom": "secret"}],
                            "repositoryCredentials": {"credentialsParameter": "secret"},
                            "environmentFiles": [{"value": "secret"}],
                            "logConfiguration": {
                                "secretOptions": [{"name": "KEY", "valueFrom": "secret"}]
                            },
                        }
                    ]
                }
            },
            {
                "taskDefinition": {
                    "containerDefinitions": [
                        {
                            "environment": [{"name": "KEY"}],
                            "secrets": [{"name": "KEY"}],
                            "logConfiguration": {"secretOptions": [{"name": "KEY"}]},
                        }
                    ]
                }
            },
        ),
        (
            "rds",
            {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "acme-db",
                        "MasterUserPassword": "secret",
                        "nested": {"Password": "secret"},
                    }
                ]
            },
            {"DBInstances": [{"DBInstanceIdentifier": "acme-db", "nested": {}}]},
        ),
    ],
)
def test_secret_fields_are_removed(service, value, expected):
    assert aws.strip_fields(value, service) == expected
    assert '"secret"' not in json.dumps(aws.strip_fields(value, service))


class FakeClient:
    def __init__(self, result=None, error=None):
        self.meta = SimpleNamespace(method_to_api_mapping={"read": "GetQueryResults"})
        self.result = result or {
            "results": [[{"field": "@message", "value": "acme log"}]],
            "nextToken": "page-2",
        }
        self.error, self.calls, self.closed = error, [], False

    def read(self, **parameters):
        self.calls.append((parameters, threading.get_ident()))
        logging.getLogger("botocore.endpoint").debug("Request: %s", KEYS)
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "code,message",
    [
        (
            "AccessDeniedException",
            "AWS refused this read. The key has no permission for logs:GetQueryResults.",
        ),
        (
            "AccessDenied",
            "AWS refused this read. The key has no permission for logs:GetQueryResults.",
        ),
        (
            "ThrottlingException",
            "AWS could not complete this read. Error code: ThrottlingException.",
        ),
        (
            "private address: acme@example.com",
            "AWS could not complete this read. Error code: UnknownError.",
        ),
    ],
)
def test_errors_never_return_aws_message(monkeypatch, code, message):
    fake = FakeClient(
        error=ClientError(
            {"Error": {"Code": code, "Message": KEYS["secret_access_key"]}}, "GetQueryResults"
        )
    )
    monkeypatch.setattr(aws, "client", lambda *a: fake)
    with pytest.raises(Refusal) as error:
        aws.invoke(KEYS, "logs", "GetQueryResults", {}, "us-east-1")
    assert str(error.value) == message and fake.closed


@pytest.mark.parametrize(
    "error,message",
    [
        (
            EndpointConnectionError(endpoint_url="https://acme-private.example"),
            "AWS could not complete this read.",
        ),
        (
            ParamValidationError(report="private parameter"),
            "The AWS parameters do not match this operation.",
        ),
    ],
)
def test_sdk_errors_are_fixed(monkeypatch, error, message):
    fake = FakeClient(error=error)
    monkeypatch.setattr(aws, "client", lambda *a: fake)
    with pytest.raises(Refusal) as exc:
        aws.invoke(KEYS, "logs", "GetQueryResults", {}, "us-east-1")
    assert str(exc.value) == message and fake.closed


def test_unknown_api_name_is_a_clear_refusal(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(aws, "client", lambda *a: fake)
    with pytest.raises(Refusal, match="does not have this read operation"):
        aws.invoke(KEYS, "logs", "GetUnknownRead", {}, "us-east-1")
    assert not fake.calls and fake.closed


def test_bounded_json_keeps_token_and_size():
    result = aws.bounded({"events": ["é" * 300000], "nextToken": "page-2"})
    assert result["truncated"] and result["nextToken"] == "page-2"
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 262144
    assert aws.bounded({"nextToken": "page-2"}) == {"nextToken": "page-2"}


def test_vault_keys_override_all_ambient_credentials(monkeypatch, tmp_path):
    credentials = tmp_path / "fake-credentials"
    credentials.write_text(
        "[acme-host]\naws_access_key_id = ambient-file-key\naws_secret_access_key = ambient-file-secret\n"
    )
    for name, value in {
        "AWS_ACCESS_KEY_ID": "ambient-env-key",
        "AWS_SECRET_ACCESS_KEY": "ambient-env-secret",
        "AWS_SESSION_TOKEN": "ambient-env-token",
        "AWS_PROFILE": "acme-host",
        "AWS_SHARED_CREDENTIALS_FILE": str(credentials),
        "AWS_CONFIG_FILE": str(credentials),
        "AWS_ENDPOINT_URL": "https://acme-override.example",
        "AWS_EC2_METADATA_DISABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    # These SDK hooks must never run. No request leaves this test.
    import botocore.credentials
    import botocore.httpsession

    def forbidden(*args, **kwargs):
        pytest.fail("AWS must not read ambient credentials or make a request")

    monkeypatch.setattr(botocore.credentials.CredentialResolver, "load_credentials", forbidden)
    monkeypatch.setattr(botocore.httpsession.URLLib3Session, "send", forbidden)
    sdk = aws.client(KEYS, "logs", "us-east-1")
    try:
        creds = sdk._request_signer._credentials.get_frozen_credentials()
        assert (creds.access_key, creds.secret_key, creds.token) == (*KEYS.values(), "")
        assert sdk.meta.endpoint_url == "https://logs.us-east-1.amazonaws.com"
        assert sdk.meta.config.connect_timeout == 5 and sdk.meta.config.read_timeout == 20
        assert sdk.meta.config.retries["total_max_attempts"] == 3
        for missing in ({}, {"access_key_id": "", "secret_access_key": "x"}, None):
            with pytest.raises(Refusal, match="vault key"):
                aws.client(missing, "logs", "us-east-1")
    finally:
        sdk.close()


async def test_call_runs_in_thread_with_vault_keys(connect_ctx, monkeypatch, caplog):
    ctx = connect_ctx
    fake, seen = FakeClient(), []

    def create(keys, service, region):
        seen.append((keys, service, region))
        return fake

    monkeypatch.setattr(aws, "client", create)
    caplog.set_level(logging.DEBUG)
    async with ctx.pool.connection() as conn:
        await store.vault(ctx).set(conn, "aws-illo", json.dumps(KEYS))
        result = await aws.read(ctx, conn, config(), "logs", "GetQueryResults", {"queryId": "q1"})
    assert result["nextToken"] == "page-2"
    assert fake.calls[0][0] == {"queryId": "q1"} and fake.calls[0][1] != threading.get_ident()
    assert seen == [(KEYS, "logs", "us-east-1")]
    assert all(v not in caplog.text for v in KEYS.values())
    assert all(v not in str(r.__dict__) for r in caplog.records for v in KEYS.values())


# Use the same private workspace fixture as the existing connector forms.
from tests.connectors.test_chat import connect_ctx  # noqa: E402,F401
