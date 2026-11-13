"""Read one AWS page with explicit vault keys and fixed service limits."""

import asyncio
import json
import logging
import re
from contextlib import contextmanager

import boto3
import botocore.session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from illo.connectors import store
from illo.connectors.http import _PrivateRequestLog, private_http_logs
from illo.tools.registry import Refusal

PREFIXES = ("Describe", "Get", "List", "Filter", "BatchGet", "Lookup")
# These reads return a URL that downloads code.
DENIED = {
    "lambda": {"GetLayerVersion", "GetLayerVersionByArn"},
}
REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")
ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9.]{0,79}$")


@contextmanager
def private_aws_logs():
    # SDK debug records can contain signed headers and request bodies.
    for name in list(logging.Logger.manager.loggerDict):
        if name.startswith(("botocore", "boto3", "urllib3")):
            logger = logging.getLogger(name)
            if not any(isinstance(f, _PrivateRequestLog) for f in logger.filters):
                logger.addFilter(_PrivateRequestLog())
    with private_http_logs():
        yield


def client(keys, service, region):
    if (
        not isinstance(keys, dict)
        or not all(
            isinstance(keys.get(k), str) and keys[k].strip()
            for k in ("access_key_id", "secret_access_key")
        )
        or not REGION.fullmatch(region)
    ):
        raise Refusal("The AWS vault key or region is invalid.")
    # Never load host files, a profile, an endpoint override, or metadata credentials.
    core = botocore.session.Session()
    core.set_config_variable("config_file", "/dev/null")
    core.set_config_variable("credentials_file", "/dev/null")
    core.get_component("config_store").set_config_variable("profile", None)
    core.get_component("config_store").set_config_variable("data_path", None)
    core.set_config_variable("ec2_metadata_v1_disabled", True)
    core.set_credentials(keys["access_key_id"], keys["secret_access_key"], token="")
    session = boto3.Session(
        botocore_session=core,
        aws_access_key_id=keys["access_key_id"],
        aws_secret_access_key=keys["secret_access_key"],
        aws_session_token="",
        region_name=region,
    )
    return session.client(
        service,
        region_name=region,
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 2, "mode": "standard"},
            ignore_configured_endpoint_urls=True,
            proxies={},
        ),
    )


def allowed(config, service, operation):
    if service not in config.services:
        raise Refusal("The AWS connector does not allow this service.")
    if not (
        operation.startswith(PREFIXES)
        or service == "logs"
        and operation in {"StartQuery", "StopQuery"}
    ) or operation in DENIED.get(service, set()):
        raise Refusal("The AWS connector allows only safe read operations.")


def strip_fields(value, service):
    if isinstance(value, list):
        return [strip_fields(v, service) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        lower = key.lower()
        if lower == "responsemetadata":
            continue
        if service == "lambda" and lower in {"environment", "code", "location", "repositoryurl"}:
            continue
        if service == "rds" and ("password" in lower or lower == "secretstring"):
            continue
        if service == "codebuild" and lower in {
            "environmentvariables",
            "exportedenvironmentvariables",
            "registrycredential",
            "auth",
        }:
            if lower in {"environmentvariables", "exportedenvironmentvariables"}:
                result[key] = [{"name": v.get("name"), "type": v.get("type")} for v in item]
            continue
        if service == "ecs" and lower in {
            "environment",
            "secrets",
            "secretoptions",
            "repositorycredentials",
            "environmentfiles",
        }:
            if lower in {"environment", "secrets", "secretoptions"}:
                result[key] = [{"name": v.get("name")} for v in item]
            continue
        result[key] = strip_fields(item, service)
    return result


def bounded(value, limit=262144):
    """Keep valid JSON and the next-page token when the page is too large."""
    encoded = json.dumps(value, default=str, ensure_ascii=False)
    if len(encoded.encode()) <= limit:
        return json.loads(encoded)
    tokens = {
        k: v
        for k, v in value.items()
        if k.lower()
        in {"nexttoken", "nextmarker", "marker", "nextforwardtoken", "nextbackwardtoken"}
    }
    result = {**tokens, "truncated": True, "data": ""}
    if len(json.dumps(result, ensure_ascii=False).encode()) > limit:
        raise Refusal("The AWS page token is too large.")
    low, high = 0, len(encoded)
    while low < high:
        middle = (low + high + 1) // 2
        result["data"] = encoded[:middle]
        if len(json.dumps(result, ensure_ascii=False).encode()) <= limit:
            low = middle
        else:
            high = middle - 1
    result["data"] = encoded[:low]
    return result


def invoke(keys, service, operation, parameters, region):
    try:
        with private_aws_logs():
            aws = client(keys, service, region)
            try:
                # Map exact public API names only. Never invoke an arbitrary client method.
                method = next(
                    (
                        name
                        for name, api in aws.meta.method_to_api_mapping.items()
                        if api == operation
                    ),
                    None,
                )
                if not method:
                    raise Refusal("AWS does not have this read operation.")
                with private_aws_logs():
                    return strip_fields(getattr(aws, method)(**parameters), service)
            finally:
                aws.close()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "UnknownError")
        code = code if isinstance(code, str) and ERROR_CODE.fullmatch(code) else "UnknownError"
        if code in {
            "AccessDenied",
            "AccessDeniedException",
            "UnauthorizedOperation",
            "UnauthorizedException",
        }:
            raise Refusal(
                f"AWS refused this read. The key has no permission for {service}:{operation}."
            ) from None
        # The message names the fault in the request, such as a malformed query. It holds no key.
        detail = " ".join(str(exc.response.get("Error", {}).get("Message", "")).split())[:300]
        if any(isinstance(v, str) and v and v in detail for v in keys.values()):
            detail = ""
        raise Refusal(f"AWS could not complete this read. Error code: {code}. {detail}".rstrip()) from None
    except ParamValidationError as exc:
        # The report names only the parameters that the run sent.
        detail = " ".join(str(exc.kwargs.get("report", "")).split())[:400]
        raise Refusal(f"The AWS parameters do not match this operation. {detail}".rstrip()) from None
    except BotoCoreError:
        raise Refusal("AWS could not complete this read.") from None


async def read(ctx, conn, config, service, operation, parameters, region=None):
    allowed(config, service, operation)
    raw = await store.vault(ctx).get(conn, config.auth.secret)
    try:
        keys = json.loads(raw or "null")
    except (ValueError, TypeError):
        raise Refusal("The AWS vault key is invalid.") from None
    return bounded(
        await asyncio.to_thread(
            invoke, keys, service, operation, parameters, region or config.region
        ),
        min(config.max_bytes, 262144),
    )


async def check_key(keys, region):
    # STS is available only to this private-form check, never to the model tool.
    return await asyncio.to_thread(invoke, keys, "sts", "GetCallerIdentity", {}, region)
