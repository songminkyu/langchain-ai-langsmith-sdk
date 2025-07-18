"""Test the LangSmith client."""

import asyncio
import dataclasses
import gc
import inspect
import io
import itertools
import json
import logging
import math
import pathlib
import sys
import time
import uuid
import warnings
import weakref
from datetime import datetime, timezone
from enum import Enum
from io import BytesIO
from typing import Callable, Dict, List, Literal, NamedTuple, Optional, Type, Union
from unittest import mock
from unittest.mock import MagicMock, patch

import dataclasses_json
import pytest
import requests
from multipart import MultipartParser, MultipartPart, parse_options_header
from pydantic import BaseModel
from requests import HTTPError

import langsmith.env as ls_env
import langsmith.utils as ls_utils
from langsmith import AsyncClient, EvaluationResult, aevaluate, evaluate, run_trees
from langsmith import schemas as ls_schemas
from langsmith._internal import _orjson
from langsmith._internal._serde import _serialize_json
from langsmith.client import (
    Client,
    _construct_url,
    _convert_stored_attachments_to_attachments_dict,
    _dataset_examples_path,
    _dumps_json,
    _is_langchain_hosted,
    _parse_token_or_url,
)
from langsmith.utils import LangSmithUserError

_CREATED_AT = datetime(2015, 1, 1, 0, 0, 0)


def test_is_localhost() -> None:
    assert ls_utils._is_localhost("http://localhost:1984")
    assert ls_utils._is_localhost("http://localhost:1984")
    assert ls_utils._is_localhost("http://0.0.0.0:1984")
    assert not ls_utils._is_localhost("http://example.com:1984")


def test__is_langchain_hosted() -> None:
    assert _is_langchain_hosted("https://api.smith.langchain.com")
    assert _is_langchain_hosted("https://beta.api.smith.langchain.com")
    assert _is_langchain_hosted("https://dev.api.smith.langchain.com")


def _clear_env_cache():
    ls_utils.get_env_var.cache_clear()


def test_validate_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Scenario 1: Both LANGCHAIN_ENDPOINT and LANGSMITH_ENDPOINT
    # are set, but api_url is not
    _clear_env_cache()
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain-endpoint.com")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langsmith-endpoint.com")

    client = Client(auto_batch_tracing=False)
    assert client.api_url == "https://api.smith.langsmith-endpoint.com"

    # Scenario 2: Both LANGCHAIN_ENDPOINT and LANGSMITH_ENDPOINT
    #  are set, and api_url is set
    _clear_env_cache()
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain-endpoint.com")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langsmith-endpoint.com")

    client = Client(
        api_url="https://api.smith.langchain.com",
        api_key="123",
        auto_batch_tracing=False,
    )
    assert client.api_url == "https://api.smith.langchain.com"

    # Scenario 3: LANGCHAIN_ENDPOINT is set, but LANGSMITH_ENDPOINT is not
    _clear_env_cache()
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain-endpoint.com")
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)

    client = Client(auto_batch_tracing=False)
    assert client.api_url == "https://api.smith.langchain-endpoint.com"

    # Scenario 4: LANGCHAIN_ENDPOINT is not set, but LANGSMITH_ENDPOINT is set
    _clear_env_cache()
    monkeypatch.delenv("LANGCHAIN_ENDPOINT", raising=False)
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langsmith-endpoint.com")

    client = Client(auto_batch_tracing=False)
    assert client.api_url == "https://api.smith.langsmith-endpoint.com"


def test_validate_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Scenario 1: Both LANGCHAIN_API_KEY and LANGSMITH_API_KEY are set,
    # but api_key is not
    _clear_env_cache()
    monkeypatch.setenv("LANGCHAIN_API_KEY", "env_langchain_api_key")
    monkeypatch.setenv("LANGSMITH_API_KEY", "env_langsmith_api_key")

    client = Client()
    assert client.api_key == "env_langsmith_api_key"

    # Scenario 2: Both LANGCHAIN_API_KEY and LANGSMITH_API_KEY are set,
    # and api_key is set
    _clear_env_cache()
    monkeypatch.setenv("LANGCHAIN_API_KEY", "env_langchain_api_key")
    monkeypatch.setenv("LANGSMITH_API_KEY", "env_langsmith_api_key")

    client = Client(api_url="https://api.smith.langchain.com", api_key="123")
    assert client.api_key == "123"

    # Scenario 3: LANGCHAIN_API_KEY is set, but LANGSMITH_API_KEY is not
    monkeypatch.setenv("LANGCHAIN_API_KEY", "env_langchain_api_key")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)

    client = Client()
    assert client.api_key == "env_langchain_api_key"

    # Scenario 4: LANGCHAIN_API_KEY is not set, but LANGSMITH_API_KEY is set
    _clear_env_cache()
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_API_KEY", "env_langsmith_api_key")

    client = Client()
    assert client.api_key == "env_langsmith_api_key"


def test_validate_multiple_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env_cache()
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", "https://api.smith.langchain-endpoint.com")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://api.smith.langsmith-endpoint.com")
    monkeypatch.setenv("LANGSMITH_RUNS_ENDPOINTS", "{}")

    with pytest.raises(ls_utils.LangSmithUserError):
        Client()

    monkeypatch.undo()
    with pytest.raises(ls_utils.LangSmithUserError):
        Client(
            api_url="https://api.smith.langchain.com",
            api_key="123",
            api_urls={"https://api.smith.langchain.com": "123"},
        )

    data = {
        "https://api.smith.langsmith-endpoint_1.com": "123",
        "https://api.smith.langsmith-endpoint_2.com": "456",
        "https://api.smith.langsmith-endpoint_3.com": "789",
    }
    monkeypatch.delenv("LANGCHAIN_ENDPOINT", raising=False)
    monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
    monkeypatch.setenv("LANGSMITH_RUNS_ENDPOINTS", json.dumps(data))
    client = Client(auto_batch_tracing=False)
    assert client._write_api_urls == data
    assert client.api_url == "https://api.smith.langsmith-endpoint_1.com"
    assert client.api_key == "123"


@mock.patch("langsmith.client.requests.Session")
def test_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env_cache()
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    with patch.dict("os.environ", {}, clear=True):
        client = Client(api_url="http://localhost:1984", api_key="123")
        assert "x-api-key" in client._headers
        assert client._headers["x-api-key"] == "123"

        client_no_key = Client(api_url="http://localhost:1984")
        assert "x-api-key" not in client_no_key._headers


@mock.patch("langsmith.client.requests.Session")
def test_cached_header_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env_cache()
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    with patch.dict("os.environ", {}, clear=True):
        client = Client(
            api_url="http://localhost:1984",
            api_key="123",
            timeout_ms=(2000, 4000),
            auto_batch_tracing=False,
        )
        assert client._timeout == (2.0, 4.0)
        assert client._headers["x-api-key"] == "123"
        # Changing API key should update headers
        client.api_key = "abc"
        assert client._headers["x-api-key"] == "abc"

        mock_response = MagicMock()
        client.session.request.return_value = mock_response
        with patch("langsmith.client.ls_utils.raise_for_status_with_text"):
            client.request_with_retries("GET", "/test")
        args, kwargs = client.session.request.call_args
        assert kwargs["timeout"] == client._timeout
        assert kwargs["headers"]["x-api-key"] == "abc"


@mock.patch("langsmith.client.requests.Session")
def test_upload_csv(mock_session_cls: mock.Mock) -> None:
    _clear_env_cache()
    dataset_id = str(uuid.uuid4())
    example_1 = ls_schemas.Example(
        id=str(uuid.uuid4()),
        created_at=_CREATED_AT,
        inputs={"input": "1"},
        outputs={"output": "2"},
        dataset_id=dataset_id,
    )
    example_2 = ls_schemas.Example(
        id=str(uuid.uuid4()),
        created_at=_CREATED_AT,
        inputs={"input": "3"},
        outputs={"output": "4"},
        dataset_id=dataset_id,
    )
    mock_response = mock.Mock()
    mock_response.json.return_value = {
        "id": dataset_id,
        "name": "test.csv",
        "description": "Test dataset",
        "owner_id": "the owner",
        "created_at": _CREATED_AT,
        "examples": [example_1, example_2],
    }
    mock_session = mock.Mock()

    def mock_request(*args, **kwargs):  # type: ignore
        if args[0] == "POST" and args[1].endswith("datasets"):
            return mock_response
        return MagicMock()

    mock_session.request.return_value = mock_response
    mock_session_cls.return_value = mock_session

    client = Client(
        api_url="http://localhost:1984",
        api_key="123",
    )
    client._tenant_id = uuid.uuid4()
    csv_file = ("test.csv", BytesIO(b"input,output\n1,2\n3,4\n"))

    dataset = client.upload_csv(
        csv_file,
        description="Test dataset",
        input_keys=["input"],
        output_keys=["output"],
    )

    assert dataset.id == uuid.UUID(dataset_id)
    assert dataset.name == "test.csv"
    assert dataset.description == "Test dataset"


def test_async_methods() -> None:
    """For every method defined on the Client, if there is a

    corresponding async method, then the async method args should be a
    superset of the sync method args.
    """
    sync_methods = [
        method
        for method in dir(Client)
        if not method.startswith("_")
        and callable(getattr(Client, method))
        and not asyncio.iscoroutinefunction(getattr(Client, method))
    ]
    async_methods = [
        method
        for method in dir(Client)
        if not method.startswith("_")
        and method not in {"arun_on_dataset"}
        and callable(getattr(Client, method))
        and asyncio.iscoroutinefunction(getattr(Client, method))
    ]

    for async_method in async_methods:
        sync_method = async_method[1:]  # Remove the "a" from the beginning
        assert sync_method in sync_methods
        sync_args = set(inspect.signature(Client.__dict__[sync_method]).parameters)
        async_args = set(inspect.signature(Client.__dict__[async_method]).parameters)
        extra_args = sync_args - async_args
        assert (
            not extra_args
        ), f"Extra args for {async_method} (compared to {sync_method}): {extra_args}"


def test_create_run_unicode() -> None:
    inputs = {
        "foo": "これは私の友達です",
        "bar": "این یک کتاب است",
        "baz": "😊🌺🎉💻🚀🌈🍕🏄‍♂️🎁🐶🌟🏖️👍🚲🎈",
        "qux": "나는\u3000밥을\u3000먹었습니다.",
        "는\u3000밥": "나는\u3000밥을\u3000먹었습니다.",
    }
    session = mock.Mock()
    session.request = mock.Mock()
    client = Client(api_url="http://localhost:1984", api_key="123", session=session)
    id_ = uuid.uuid4()
    client.create_run("my_run", inputs=inputs, run_type="llm", id=id_)
    client.update_run(id_, status="completed")


@pytest.mark.parametrize("use_multipart_endpoint", (True, False))
def test_create_run_mutate(
    use_multipart_endpoint: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = {"messages": ["hi"], "mygen": (i for i in range(10))}
    session = mock.Mock()
    session.request = mock.Mock()
    client = Client(
        api_url="http://localhost:1984",
        api_key="123",
        session=session,
        info=ls_schemas.LangSmithInfo(
            batch_ingest_config=ls_schemas.BatchIngestConfig(
                use_multipart_endpoint=use_multipart_endpoint,
                size_limit_bytes=None,  # Note this field is not used here
                size_limit=100,
                scale_up_nthreads_limit=16,
                scale_up_qsize_trigger=1000,
                scale_down_nempty_trigger=4,
            )
        ),
    )
    id_ = uuid.uuid4()
    run_dict = dict(
        id=id_,
        name="my_run",
        inputs=inputs,
        run_type="llm",
        trace_id=id_,
        dotted_order=run_trees._create_current_dotted_order(
            datetime.now(timezone.utc), id_
        ),
    )
    client.create_run(**run_dict)  # type: ignore
    inputs["messages"].append("there")  # type: ignore
    outputs = {"messages": ["hi", "there"]}
    client.update_run(
        id_,
        outputs=outputs,
        end_time=datetime.now(timezone.utc),
        trace_id=id_,
        dotted_order=run_dict["dotted_order"],
    )
    if use_multipart_endpoint:
        for _ in range(10):
            time.sleep(0.1)  # Give the background thread time to stop
            payloads = [
                (call[2]["headers"], call[2]["data"])
                for call in session.request.mock_calls
                if call.args and call.args[1].endswith("runs/multipart")
            ]
            if payloads:
                break
        else:
            assert False, "No payloads found"

        parts: List[MultipartPart] = []
        for payload in payloads:
            headers, data = payload
            assert headers["Content-Type"].startswith("multipart/form-data")
            # this is a current implementation detail, if we change implementation
            # we update this assertion
            assert isinstance(data, bytes)
            boundary = parse_options_header(headers["Content-Type"])[1]["boundary"]
            parser = MultipartParser(io.BytesIO(data), boundary)
            parts.extend(parser.parts())

        assert [p.name for p in parts] == [
            f"post.{id_}",
            f"post.{id_}.inputs",
            f"post.{id_}.outputs",
        ]
        assert [p.headers.get("content-type") for p in parts] == [
            "application/json",
            "application/json",
            "application/json",
        ]
        outputs_parsed = json.loads(parts[2].value)
        assert outputs_parsed == outputs
        inputs_parsed = json.loads(parts[1].value)
        assert inputs_parsed["messages"] == ["hi"]
        assert inputs_parsed["mygen"].startswith(  # type: ignore
            "<generator object test_create_run_mutate.<locals>."
        )
        run_parsed = json.loads(parts[0].value)
        assert "inputs" not in run_parsed
        assert "outputs" not in run_parsed
        assert run_parsed["trace_id"] == str(id_)
        assert run_parsed["dotted_order"] == run_dict["dotted_order"]
    else:
        for _ in range(10):
            time.sleep(0.1)  # Give the background thread time to stop
            payloads = [
                json.loads(call[2]["data"])
                for call in session.request.mock_calls
                if call.args and call.args[1].endswith("runs/batch")
            ]
            if payloads:
                break
        else:
            assert False, "No payloads found"
        posts = [pr for payload in payloads for pr in payload.get("post", [])]
        patches = [pr for payload in payloads for pr in payload.get("patch", [])]
        inputs = next(
            (
                pr["inputs"]
                for pr in itertools.chain(posts, patches)
                if pr.get("inputs")
            ),
            {},
        )
        outputs = next(
            (
                pr["outputs"]
                for pr in itertools.chain(posts, patches)
                if pr.get("outputs")
            ),
            {},
        )
        # Check that the mutated value wasn't posted
        assert "messages" in inputs
        assert inputs["messages"] == ["hi"]
        assert "mygen" in inputs
        assert inputs["mygen"].startswith(  # type: ignore
            "<generator object test_create_run_mutate.<locals>."
        )
        assert outputs == {"messages": ["hi", "there"]}


@mock.patch("langsmith.client.requests.Session")
def test_upsert_examples_multipart(mock_session_cls: mock.Mock) -> None:
    """Test that upsert_examples_multipart sends correct multipart data."""
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_session.request.return_value = mock_response
    mock_session_cls.return_value = mock_session

    client = Client(
        api_url="http://localhost:1984",
        api_key="123",
        info={"instance_flags": {"examples_multipart_enabled": True}},
    )

    # Create test data
    example_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    created_at = datetime(2015, 1, 1, 0, 0, 0)

    example = ls_schemas.ExampleUpsertWithAttachments(
        id=example_id,
        dataset_id=dataset_id,
        created_at=created_at,
        inputs={"input": "test input"},
        outputs={"output": "test output"},
        metadata={"meta": "data"},
        split="train",
        attachments={
            "file1": ("text/plain", b"test data"),
            "file2": ls_schemas.Attachment(
                mime_type="application/json", data=b'{"key": "value"}'
            ),
        },
    )
    client.upsert_examples_multipart(upserts=[example])

    # Verify the request
    assert mock_session.request.call_count == 1
    call_args = mock_session.request.call_args

    assert call_args[0][0] == "POST"
    assert call_args[0][1].endswith("/v1/platform/examples/multipart")

    # Parse the multipart data
    request_data = call_args[1]["data"]
    content_type = call_args[1]["headers"]["Content-Type"]
    boundary = parse_options_header(content_type)[1]["boundary"]

    parser = MultipartParser(
        io.BytesIO(
            request_data
            if isinstance(request_data, bytes)
            else request_data.to_string()
        ),
        boundary,
    )
    parts = list(parser.parts())

    # Verify all expected parts are present
    expected_parts = {
        str(example_id): {
            "dataset_id": str(dataset_id),
            "created_at": created_at.isoformat(),
            "metadata": {"meta": "data"},
            "split": "train",
        },
        f"{example_id}.inputs": {"input": "test input"},
        f"{example_id}.outputs": {"output": "test output"},
        f"{example_id}.attachment.file1": "test data",
        f"{example_id}.attachment.file2": '{"key": "value"}',
    }

    assert len(parts) == len(expected_parts)

    for part in parts:
        name = part.name
        assert name in expected_parts, f"Unexpected part: {name}"

        if name.endswith(".attachment.file1"):
            assert part.value == expected_parts[name]
            assert part.headers["Content-Type"] == "text/plain; length=9"
        elif name.endswith(".attachment.file2"):
            assert part.value == expected_parts[name]
            assert part.headers["Content-Type"] == "application/json; length=16"
        else:
            value = json.loads(part.value)
            assert value == expected_parts[name]
            assert part.headers["Content-Type"] == "application/json"


@mock.patch("langsmith.client.requests.Session")
def test_upsert_examples_multipart_missing_file(
    mock_session_cls: mock.Mock, tmp_path, caplog
) -> None:
    """Attachment file paths that do not exist are skipped."""
    caplog.set_level(logging.WARNING)
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"result": "ok"}
    mock_session.request.return_value = mock_response
    mock_session_cls.return_value = mock_session

    client = Client(
        api_url="http://localhost:1984",
        api_key="123",
        info={"instance_flags": {"examples_multipart_enabled": True}},
    )

    example_id = uuid.uuid4()
    dataset_id = uuid.uuid4()

    missing_path = tmp_path / "no-file.txt"
    example = ls_schemas.ExampleUpsertWithAttachments(
        id=example_id,
        dataset_id=dataset_id,
        created_at=_CREATED_AT,
        inputs={"input": "test"},
        outputs={"output": "out"},
        attachments={
            "missing": ("text/plain", missing_path),
            "good": ("text/plain", b"data"),
        },
    )

    with caplog.at_level(logging.WARNING):
        client.upsert_examples_multipart(
            upserts=[example], dangerously_allow_filesystem=True
        )

    call_args = mock_session.request.call_args
    request_data = call_args[1]["data"]
    content_type = call_args[1]["headers"]["Content-Type"]
    boundary = parse_options_header(content_type)[1]["boundary"]

    parser = MultipartParser(
        io.BytesIO(
            request_data
            if isinstance(request_data, bytes)
            else request_data.to_string()
        ),
        boundary,
    )
    parts = list(parser.parts())
    part_names = [p.name for p in parts]

    assert f"{example_id}.attachment.good" in part_names
    assert f"{example_id}.attachment.missing" not in part_names
    assert "Attachment file not found" in caplog.text


@mock.patch("langsmith.client.requests.Session")
def test_update_example_multipart_none_preserves_existing(
    mock_session_cls: mock.Mock,
) -> None:
    """Test that updating with None inputs/outputs via multipart preserves existing values."""
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_session.request.return_value = mock_response
    mock_session_cls.return_value = mock_session

    # Mock the read_example call to return existing example
    existing_example = ls_schemas.Example(
        id=str(uuid.uuid4()),
        dataset_id=str(uuid.uuid4()),
        inputs={"existing": "input"},
        outputs={"existing": "output"},
        created_at=datetime.now(),
    )

    client = Client(
        api_url="http://localhost:1984",
        api_key="123",
        info={"instance_flags": {"dataset_examples_multipart_enabled": True}},
    )

    # Mock read_example to return the existing example
    with mock.patch.object(Client, "read_example", return_value=existing_example):
        # Update with omitted inputs and outputs
        client.update_example(
            example_id=existing_example.id,
            metadata={"updated": "metadata"},
        )

    # Verify the multipart request was made
    assert mock_session.request.call_count == 1
    call_args = mock_session.request.call_args
    assert call_args[0][0] == "PATCH"
    # Check it's calling the correct multipart endpoint
    assert "/v1/platform/datasets/" in call_args[0][1]
    assert call_args[0][1].endswith("/examples")

    # Parse the multipart data
    request_data = call_args[1]["data"]
    content_type = call_args[1]["headers"]["Content-Type"]
    boundary = parse_options_header(content_type)[1]["boundary"]

    parser = MultipartParser(
        io.BytesIO(
            request_data
            if isinstance(request_data, bytes)
            else request_data.to_string()
        ),
        boundary,
    )
    parts = list(parser.parts())

    # Verify that inputs and outputs parts are NOT included (preserving existing values)
    part_names = [p.name for p in parts]
    assert str(existing_example.id) in part_names
    assert f"{existing_example.id}.inputs" not in part_names
    assert f"{existing_example.id}.outputs" not in part_names

    # Verify the main example part contains updated metadata
    example_part = next(p for p in parts if p.name == str(existing_example.id))
    example_data = json.loads(example_part.value)
    assert example_data["metadata"] == {"updated": "metadata"}


class CallTracker:
    def __init__(self) -> None:
        self.counter = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.counter += 1


@pytest.mark.flaky(reruns=5)
@pytest.mark.parametrize("supports_batch_endpoint", [True, False])
@pytest.mark.parametrize("auto_batch_tracing", [True, False])
def test_client_gc(auto_batch_tracing: bool, supports_batch_endpoint: bool) -> None:
    session = mock.MagicMock(spec=requests.Session)
    api_url = "http://localhost:1984"

    def mock_get(*args, **kwargs):
        if args[0] == f"{api_url}/info":
            response = mock.Mock()
            if supports_batch_endpoint:
                response.json.return_value = {}
            else:
                response.raise_for_status.side_effect = HTTPError()
                response.status_code = 404
            return response
        else:
            return MagicMock()

    session.get.side_effect = mock_get
    client = Client(
        api_url=api_url,
        api_key="123",
        auto_batch_tracing=auto_batch_tracing,
        session=session,
    )
    tracker = CallTracker()
    weakref.finalize(client, tracker)
    assert tracker.counter == 0

    for _ in range(10):
        id = uuid.uuid4()
        client.create_run(
            "my_run",
            inputs={},
            run_type="llm",
            id=id,
            trace_id=id,
            dotted_order=id,
        )

    if auto_batch_tracing:
        assert client.tracing_queue
        client.tracing_queue.join()

        request_calls = [
            call
            for call in session.request.mock_calls
            if call.args and call.args[0] == "POST"
        ]
        assert len(request_calls) >= 1

        for call in request_calls:
            assert call.args[0] == "POST"
            assert call.args[1] == "http://localhost:1984/runs/batch"
        get_calls = [
            call
            for call in session.request.mock_calls
            if call.args and call.args[0] == "GET"
        ]
        # assert len(get_calls) == 1
        for call in get_calls:
            assert call.args[1] == f"{api_url}/info"
    else:
        request_calls = [
            call
            for call in session.request.mock_calls
            if call.args and call.args[0] == "POST"
        ]

        assert len(request_calls) == 10
        for call in request_calls:
            assert call.args[0] == "POST"
            assert call.args[1] == "http://localhost:1984/runs"
        if auto_batch_tracing:
            get_calls = [
                call
                for call in session.get.mock_calls
                if call.args and call.args[0] == "GET"
            ]
            for call in get_calls:
                assert call.args[1] == f"{api_url}/info"
    del client
    time.sleep(3)  # Give the background thread time to stop
    gc.collect()  # Force garbage collection
    assert tracker.counter == 1, "Client was not garbage collected"


@pytest.mark.parametrize("auto_batch_tracing", [True, False])
def test_client_gc_no_batched_runs(auto_batch_tracing: bool) -> None:
    session = mock.MagicMock(spec=requests.Session)
    client = Client(
        api_url="http://localhost:1984",
        api_key="123",
        auto_batch_tracing=auto_batch_tracing,
        session=session,
    )
    tracker = CallTracker()
    weakref.finalize(client, tracker)
    assert tracker.counter == 0

    # because no trace_id/dotted_order provided, auto batch is disabled
    for _ in range(10):
        client.create_run("my_run", inputs={}, run_type="llm", id=uuid.uuid4())
    request_calls = [
        call
        for call in session.request.mock_calls
        if call.args and call.args[0] == "POST"
    ]
    assert len(request_calls) == 10
    for call in request_calls:
        assert call.args[1] == "http://localhost:1984/runs"

    del client
    time.sleep(2)  # Give the background thread time to stop
    gc.collect()  # Force garbage collection
    assert tracker.counter == 1, "Client was not garbage collected"


@pytest.mark.parametrize("auto_batch_tracing", [True, False])
def test_create_run_with_filters(auto_batch_tracing: bool) -> None:
    session = mock.MagicMock(spec=requests.Session)

    def filter_inputs(inputs: dict) -> dict:
        return {"hi there": "woah"}

    def filter_outputs(outputs: dict):
        return {k: v + "goodbye" for k, v in outputs.items()}

    client = Client(
        api_url="http://localhost:1984",
        api_key="123",
        auto_batch_tracing=auto_batch_tracing,
        session=session,
        hide_inputs=filter_inputs,
        hide_outputs=filter_outputs,
    )
    tracker = CallTracker()
    weakref.finalize(client, tracker)
    assert tracker.counter == 0
    expected = ['"hi there":"woah"']
    for _ in range(3):
        id_ = uuid.uuid4()
        client.create_run("my_run", inputs={"foo": "bar"}, run_type="llm", id=id_)
        output_val = uuid.uuid4().hex[:5]
        client.update_run(
            id_, end_time=datetime.now(), outputs={"theoutput": output_val}
        )
        expected.append(output_val + "goodbye")

    request_calls = [
        call
        for call in session.request.mock_calls
        if call.args and call.args[0] in {"POST", "PATCH"}
    ]
    all_posted = "\n".join(
        [call.kwargs["data"].decode("utf-8") for call in request_calls]
    )
    assert all([exp in all_posted for exp in expected])


@pytest.mark.parametrize(
    "hide_metadata_config, expected_metadata_key_present",
    [
        (True, False),  # hide_metadata=True should remove metadata
        (False, True),  # hide_metadata=False should keep metadata
        (None, True),  # hide_metadata=None should keep metadata
        (lambda metadata: {**metadata, "modified": True}, True),  # callable modifies
    ],
)
def test_hide_metadata(
    hide_metadata_config: Optional[Union[Callable[[dict], dict], bool]],
    expected_metadata_key_present: bool,
) -> None:
    """Test the hide_metadata functionality in Client."""
    session = mock.MagicMock(spec=requests.Session)
    initial_metadata = {"initial_key": "initial_value"}

    client = Client(
        api_url="http://localhost:1984",
        api_key="123",
        auto_batch_tracing=False,  # Easier to inspect single calls
        session=session,
        hide_metadata=hide_metadata_config,
    )

    run_id = uuid.uuid4()
    client.create_run(
        "my_run_metadata_test",
        inputs={"in": "put"},
        run_type="llm",
        id=run_id,
        extra={"metadata": initial_metadata},
    )

    post_call = None
    for call in session.request.mock_calls:
        if len(call.args) > 1 and call.args[0] == "POST" and "runs" in call.args[1]:
            post_call = call
            break

    assert post_call is not None, "POST request to /runs not found"

    payload_data = post_call.kwargs.get("data", b"{}")
    if isinstance(payload_data, bytes):
        payload_str = payload_data.decode("utf-8")
    else:
        payload_str = str(payload_data)

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        if isinstance(payload_data, dict):
            payload = payload_data
        else:
            raise

    payload_extra = payload.get("extra", {})

    if expected_metadata_key_present:
        assert (
            "metadata" in payload_extra
        ), f"Metadata key should be present in extra {payload_extra}"
        if callable(hide_metadata_config):
            # Check if the callable modified the metadata as expected
            assert payload_extra["metadata"].get("modified") is True
        else:
            assert all(
                k in payload_extra["metadata"] and v == payload_extra["metadata"][k]
                for k, v in initial_metadata.items()
            )
    else:
        assert all(
            k not in payload_extra["metadata"] for k in initial_metadata
        ), f"Metadata key should NOT be present in extra {payload_extra}"


@pytest.mark.flaky(retries=3)
def test_client_gc_after_autoscale() -> None:
    session = mock.MagicMock(spec=requests.Session)
    client = Client(
        api_url="http://localhost:1984",
        api_key="123",
        session=session,
        auto_batch_tracing=True,
    )
    tracker = CallTracker()
    weakref.finalize(client, tracker)
    assert tracker.counter == 0

    tracing_queue = client.tracing_queue
    assert tracing_queue is not None

    for _ in range(50_000):
        id = uuid.uuid4()
        client.create_run(
            "my_run",
            inputs={},
            run_type="llm",
            id=id,
            trace_id=id,
            dotted_order=id,
        )

    del client
    tracing_queue.join()
    time.sleep(2)  # Give the background threads time to stop
    gc.collect()  # Force garbage collection
    assert tracker.counter == 1, "Client was not garbage collected"

    request_calls = [
        call
        for call in session.request.mock_calls
        if call.args and call.args[0] == "POST"
    ]
    assert len(request_calls) >= 500 and len(request_calls) <= 550
    for call in request_calls:
        assert call.args[0] == "POST"
        assert call.args[1] == "http://localhost:1984/runs/batch"


@pytest.mark.parametrize("supports_batch_endpoint", [True, False])
@pytest.mark.parametrize("auto_batch_tracing", [True, False])
def test_create_run_includes_langchain_env_var_metadata(
    supports_batch_endpoint: bool,
    auto_batch_tracing: bool,
) -> None:
    session = mock.Mock()
    session.request = mock.Mock()
    api_url = "http://localhost:1984"

    def mock_get(*args, **kwargs):
        if args[0] == f"{api_url}/info":
            response = mock.Mock()
            if supports_batch_endpoint:
                response.json.return_value = {}
            else:
                response.raise_for_status.side_effect = HTTPError()
                response.status_code = 404
            return response
        else:
            return MagicMock()

    session.get.side_effect = mock_get
    client = Client(
        api_url=api_url,
        api_key="123",
        auto_batch_tracing=auto_batch_tracing,
        session=session,
    )
    inputs = {
        "foo": "これは私の友達です",
        "bar": "این یک کتاب است",
        "baz": "😊🌺🎉💻🚀🌈🍕🏄‍♂️🎁🐶🌟🏖️👍🚲🎈",
        "qux": "나는\u3000밥을\u3000먹었습니다.",
        "는\u3000밥": "나는\u3000밥을\u3000먹었습니다.",
    }

    # Set the environment variables just for this test
    with patch.dict("os.environ", {"LANGCHAIN_REVISION": "abcd2234"}):
        # Clear the cache to ensure the environment variables are re-read
        ls_env.get_langchain_env_var_metadata.cache_clear()
        id_ = uuid.uuid4()
        start_time = datetime.now()
        client.create_run(
            "my_run",
            inputs=inputs,
            run_type="llm",
            id=id_,
            trace_id=id_,
            dotted_order=f"{start_time.strftime('%Y%m%dT%H%M%S%fZ')}{id_}",
            start_time=start_time,
        )
        if tracing_queue := client.tracing_queue:
            tracing_queue.join()
        # Check the posted value in the request
        posted_value = json.loads(session.request.call_args[1]["data"])
        if auto_batch_tracing:
            assert (
                posted_value["post"][0]["extra"]["metadata"]["LANGCHAIN_REVISION"]
                == "abcd2234"
            )
        else:
            assert posted_value["extra"]["metadata"]["LANGCHAIN_REVISION"] == "abcd2234"
            assert "LANGCHAIN_API_KEY" not in posted_value["extra"]["metadata"]


@pytest.mark.parametrize("source_type", ["api", "model"])
def test_create_feedback_string_source_type(source_type: str) -> None:
    session = mock.Mock()
    client = Client(api_url="http://localhost:1984", api_key="123", session=session)
    request_object = mock.Mock()
    request_object.json.return_value = {
        "id": uuid.uuid4(),
        "key": "Foo",
        "created_at": _CREATED_AT,
        "modified_at": _CREATED_AT,
        "run_id": uuid.uuid4(),
    }
    session.post.return_value = request_object
    id_ = uuid.uuid4()
    client.create_feedback(
        id_,
        key="Foo",
        feedback_source_type=source_type,
    )


def test_pydantic_serialize() -> None:
    """Test that pydantic objects can be serialized."""
    test_uuid = uuid.uuid4()
    test_time = datetime.now()

    class ChildPydantic(BaseModel):
        uid: uuid.UUID
        child_path_keys: Dict[pathlib.Path, pathlib.Path]

    class MyPydantic(BaseModel):
        foo: str
        uid: uuid.UUID
        tim: datetime
        ex: Optional[str] = None
        child: Optional[ChildPydantic] = None
        path_keys: Dict[pathlib.Path, pathlib.Path]

    obj = MyPydantic(
        foo="bar",
        uid=test_uuid,
        tim=test_time,
        child=ChildPydantic(
            uid=test_uuid, child_path_keys={pathlib.Path("foo"): pathlib.Path("bar")}
        ),
        path_keys={pathlib.Path("foo"): pathlib.Path("bar")},
    )
    res = json.loads(json.dumps(obj, default=_serialize_json))
    expected = {
        "foo": "bar",
        "uid": str(test_uuid),
        "tim": test_time.isoformat(),
        "child": {
            "uid": str(test_uuid),
            "child_path_keys": {"foo": "bar"},
        },
        "path_keys": {"foo": "bar"},
    }
    assert res == expected

    obj2 = {"output": obj}
    res2 = json.loads(json.dumps(obj2, default=_serialize_json))
    assert res2 == {"output": expected}


def test_serialize_json(caplog) -> None:
    caplog.set_level(logging.ERROR)

    class MyClass:
        def __init__(self, x: int) -> None:
            self.x = x
            self.y = "y"
            self.a_list = [1, 2, 3]
            self.a_tuple = (1, 2, 3)
            self.a_set = {1, 2, 3}
            self.a_dict = {"foo": "bar"}
            self.my_bytes = b"foo"

        def __repr__(self) -> str:
            return "I fell back"

        def __hash__(self) -> int:
            return 1

    class ClassWithTee:
        def __init__(self) -> None:
            tee_a, tee_b = itertools.tee(range(10))
            self.tee_a = tee_a
            self.tee_b = tee_b

        def __repr__(self):
            return "tee_a, tee_b"

    class MyPydantic(BaseModel):
        foo: str
        bar: int
        path_keys: Dict[pathlib.Path, "MyPydantic"]

    @dataclasses.dataclass
    class MyDataclass:
        foo: str
        bar: int

        def something(self) -> None:
            pass

    class MyEnum(str, Enum):
        FOO = "foo"
        BAR = "bar"

    class ClassWithFakeDict:
        def dict(self) -> Dict:
            raise ValueError("This should not be called")

        def to_dict(self) -> Dict:
            return {"foo": "bar"}

    @dataclasses_json.dataclass_json
    @dataclasses.dataclass
    class Person:
        name: str

    uid = uuid.uuid4()
    current_time = datetime.now()

    class MyNamedTuple(NamedTuple):
        foo: str
        bar: int

    to_serialize = {
        "uid": uid,
        "time": current_time,
        "my_class": MyClass(1),
        "class_with_tee": ClassWithTee(),
        "my_dataclass": MyDataclass("foo", 1),
        "my_enum": MyEnum.FOO,
        "my_pydantic": MyPydantic(
            foo="foo",
            bar=1,
            path_keys={pathlib.Path("foo"): MyPydantic(foo="foo", bar=1, path_keys={})},
        ),
        "my_pydantic_class": MyPydantic,
        "person": Person(name="foo_person"),
        "a_bool": True,
        "a_none": None,
        "a_str": "foo",
        "an_int": 1,
        "a_float": 1.1,
        "named_tuple": MyNamedTuple(foo="foo", bar=1),
        "fake_json": ClassWithFakeDict(),
        "some_set": set("a"),
        "set_with_class": set([MyClass(1)]),
        "my_mock": MagicMock(text="Hello, world"),
    }
    res = _orjson.loads(_dumps_json(to_serialize))
    assert (
        "model_dump" not in caplog.text
    ), f"Unexpected error logs were emitted: {caplog.text}"

    expected = {
        "uid": str(uid),
        "time": current_time.isoformat(),
        "my_class": "I fell back",
        "class_with_tee": "tee_a, tee_b",
        "my_dataclass": {"foo": "foo", "bar": 1},
        "my_enum": "foo",
        "my_pydantic": {
            "foo": "foo",
            "bar": 1,
            "path_keys": {"foo": {"foo": "foo", "bar": 1, "path_keys": {}}},
        },
        "my_pydantic_class": lambda x: "MyPydantic" in x,
        "person": {"name": "foo_person"},
        "a_bool": True,
        "a_none": None,
        "a_str": "foo",
        "an_int": 1,
        "a_float": 1.1,
        "named_tuple": {"bar": 1, "foo": "foo"},
        "fake_json": {"foo": "bar"},
        "some_set": ["a"],
        "set_with_class": ["I fell back"],
        "my_mock": lambda x: "Mock" in x,
    }
    assert set(expected) == set(res)
    for k, v in expected.items():
        try:
            if callable(v):
                assert v(res[k]), f"Failed for {k}"
            else:
                assert res[k] == v, f"Failed for {k}"
        except AssertionError:
            raise

    @dataclasses.dataclass
    class CyclicClass:
        other: Optional["CyclicClass"]

        def __repr__(self) -> str:
            return "my_cycles..."

    my_cyclic = CyclicClass(other=CyclicClass(other=None))
    my_cyclic.other.other = my_cyclic  # type: ignore

    res = _orjson.loads(_dumps_json({"cyclic": my_cyclic}))
    assert res == {"cyclic": "my_cycles..."}
    expected = {"foo": "foo", "bar": 1}


def test__dumps_json():
    chars = "".join(chr(cp) for cp in range(0, sys.maxunicode + 1))
    trans_table = str.maketrans("", "", "")
    all_chars = chars.translate(trans_table)
    serialized_json = _dumps_json({"chars": all_chars})
    assert isinstance(serialized_json, bytes)
    serialized_str = serialized_json.decode("utf-8")
    assert '"chars"' in serialized_str
    assert "\\uD800" not in serialized_str
    assert "\\uDC00" not in serialized_str


@patch("langsmith.client.requests.Session", autospec=True)
def test_host_url(_: MagicMock) -> None:
    client = Client(api_url="https://api.foobar.com/api", api_key="API_KEY")
    assert client._host_url == "https://api.foobar.com"

    client = Client(
        api_url="https://api.langsmith.com",
        api_key="API_KEY",
        web_url="https://web.langsmith.com",
    )
    assert client._host_url == "https://web.langsmith.com"

    client = Client(api_url="http://localhost:8000", api_key="API_KEY")
    assert client._host_url == "http://localhost"

    client = Client(api_url="https://eu.api.smith.langchain.com", api_key="API_KEY")
    assert client._host_url == "https://eu.smith.langchain.com"

    client = Client(api_url="https://dev.api.smith.langchain.com", api_key="API_KEY")
    assert client._host_url == "https://dev.smith.langchain.com"

    client = Client(api_url="https://api.smith.langchain.com", api_key="API_KEY")
    assert client._host_url == "https://smith.langchain.com"


@patch("langsmith.client.time.sleep")
def test_retry_on_connection_error(mock_sleep: MagicMock):
    mock_session = MagicMock()
    client = Client(api_key="test", session=mock_session, auto_batch_tracing=False)
    mock_session.request.side_effect = requests.ConnectionError()

    with pytest.raises(ls_utils.LangSmithConnectionError):
        client.request_with_retries("GET", "https://test.url", stop_after_attempt=2)
    assert mock_session.request.call_count == 2


@patch("langsmith.client.time.sleep")
def test_http_status_500_handling(mock_sleep):
    mock_session = MagicMock()
    client = Client(api_key="test", session=mock_session, auto_batch_tracing=False)
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = HTTPError()
    mock_session.request.return_value = mock_response

    with pytest.raises(ls_utils.LangSmithAPIError):
        client.request_with_retries("GET", "https://test.url", stop_after_attempt=2)
    assert mock_session.request.call_count == 2


@patch("langsmith.client.time.sleep")
def test_pass_on_409_handling(mock_sleep):
    mock_session = MagicMock()
    client = Client(api_key="test", session=mock_session, auto_batch_tracing=False)
    mock_response = MagicMock()
    mock_response.status_code = 409
    mock_response.raise_for_status.side_effect = HTTPError()
    mock_session.request.return_value = mock_response
    response = client.request_with_retries(
        "GET",
        "https://test.url",
        stop_after_attempt=5,
        to_ignore=[ls_utils.LangSmithConflictError],
    )
    assert mock_session.request.call_count == 1
    assert response == mock_response


@patch("langsmith.client.ls_utils.raise_for_status_with_text")
def test_http_status_429_handling(mock_raise_for_status):
    mock_session = MagicMock()
    client = Client(api_key="test", session=mock_session)
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_session.request.return_value = mock_response
    mock_raise_for_status.side_effect = HTTPError()
    with pytest.raises(ls_utils.LangSmithRateLimitError):
        client.request_with_retries("GET", "https://test.url")


@patch("langsmith.client.ls_utils.raise_for_status_with_text")
def test_http_status_401_handling(mock_raise_for_status):
    mock_session = MagicMock()
    client = Client(api_key="test", session=mock_session)
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_session.request.return_value = mock_response
    mock_raise_for_status.side_effect = HTTPError()
    with pytest.raises(ls_utils.LangSmithAuthError):
        client.request_with_retries("GET", "https://test.url")


@patch("langsmith.client.ls_utils.raise_for_status_with_text")
def test_http_status_404_handling(mock_raise_for_status):
    mock_session = MagicMock()
    client = Client(api_key="test", session=mock_session)
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_session.request.return_value = mock_response
    mock_raise_for_status.side_effect = HTTPError()
    with pytest.raises(ls_utils.LangSmithNotFoundError):
        client.request_with_retries("GET", "https://test.url")


@patch("langsmith.client.ls_utils.raise_for_status_with_text")
def test_batch_ingest_run_retry_on_429(mock_raise_for_status):
    mock_session = MagicMock()
    client = Client(api_key="test", session=mock_session)
    mock_response = MagicMock()
    mock_response.headers = {"retry-after": "0.5"}
    mock_response.status_code = 429
    mock_session.request.return_value = mock_response
    mock_raise_for_status.side_effect = HTTPError()

    client.batch_ingest_runs(
        create=[
            {
                "name": "test",
                "id": str(uuid.uuid4()),
                "trace_id": str(uuid.uuid4()),
                "dotted_order": str(uuid.uuid4()),
            }
        ],
    )
    # Check that there were 3 post calls (may be other get calls though)
    assert mock_session.request.call_count >= 3
    # count the number of POST requests
    assert (
        sum([1 for call in mock_session.request.call_args_list if call[0][0] == "POST"])
        == 3
    )


MB = 1024 * 1024


@pytest.mark.parametrize("payload_size", [MB, 5 * MB, 9 * MB, 21 * MB])
@pytest.mark.parametrize("use_multipart_endpoint", (True, False))
def test_batch_ingest_run_splits_large_batches(
    payload_size: int, use_multipart_endpoint: bool
):
    mock_session = MagicMock()
    client = Client(api_key="test", session=mock_session)
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_session.request.return_value = mock_response
    # Create 6 run ops total, each with an inputs dictionary that's payload_size bytess
    run_ids = [str(uuid.uuid4()) for _ in range(3)]
    patch_ids = [str(uuid.uuid4()) for _ in range(3)]
    posts = [
        {
            "name": "test",
            "id": run_id,
            "trace_id": run_id,
            "dotted_order": run_id,
            "inputs": {"x": "a" * payload_size},
            "start_time": "2021-01-01T00:00:00Z",
        }
        for run_id in run_ids
    ]
    patches = [
        {
            "id": run_id,
            "trace_id": run_id,
            "dotted_order": run_id,
            "end_time": "2021-01-01T00:00:00Z",
            "outputs": {"y": "b" * payload_size},
        }
        for run_id in patch_ids
    ]

    if use_multipart_endpoint:
        client.multipart_ingest(create=posts, update=patches)
        # multipart endpoint should only send one request
        expected_num_requests = 1
        # count the number of POST requests
        assert sum(
            [1 for call in mock_session.request.call_args_list if call[0][0] == "POST"]
        ) in (expected_num_requests, expected_num_requests + 1)

        request_bodies = [
            op
            for call in mock_session.request.call_args_list
            for op in (
                MultipartParser(
                    (
                        io.BytesIO(call[1]["data"])
                        if isinstance(call[1]["data"], bytes)
                        else call[1]["data"]
                    ),
                    parse_options_header(call[1]["headers"]["Content-Type"])[1][
                        "boundary"
                    ],
                )
                if call[0][0] == "POST"
                else []
            )
        ]
        all_run_ids = run_ids + patch_ids

        # Check that all the run_ids are present in the request bodies
        for run_id in all_run_ids:
            assert any(
                [body.name.split(".")[1] == run_id for body in request_bodies]
            ), run_id
    else:
        client.batch_ingest_runs(create=posts, update=patches)
        # we can support up to 20MB per batch, so we need to find the number of batches
        # we should be sending
        max_in_batch = max(1, (20 * MB) // (payload_size + 20))

        expected_num_requests = min(6, math.ceil((len(run_ids) * 2) / max_in_batch))
        # count the number of POST requests
        assert (
            sum(
                [
                    1
                    for call in mock_session.request.call_args_list
                    if call[0][0] == "POST"
                ]
            )
            == expected_num_requests
        )
        request_bodies = [
            op
            for call in mock_session.request.call_args_list
            for reqs in (
                _orjson.loads(call[1]["data"]).values() if call[0][0] == "POST" else []
            )
            for op in reqs
        ]
        all_run_ids = run_ids + patch_ids

        # Check that all the run_ids are present in the request bodies
        for run_id in all_run_ids:
            assert any([body["id"] == str(run_id) for body in request_bodies])

        # Check that no duplicate run_ids are present in the request bodies
        assert len(request_bodies) == len(set([body["id"] for body in request_bodies]))


def test_sampling_and_batching():
    """Test that sampling and batching work correctly."""
    # Setup mock client
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_session.request.return_value = mock_response

    counter = 0

    def mock_should_sample():
        nonlocal counter
        counter += 1
        return counter % 2 != 0

    with patch(
        "langsmith.client.Client._should_sample", side_effect=mock_should_sample
    ):
        client = Client(
            api_key="test-api-key",
            auto_batch_tracing=True,
            tracing_sampling_rate=0.5,
            session=mock_session,
        )

        project_name = "__test_batch"

        # Create parent runs
        run_params = []
        for i in range(4):
            run_id = uuid.uuid4()
            params = {
                "id": run_id,
                "project_name": project_name,
                "name": f"test_run {i}",
                "run_type": "llm",
                "inputs": {"text": f"hello world {i}"},
                "dotted_order": "foo",
                "trace_id": run_id,
            }
            client.create_run(**params)
            run_params.append(params)

        client.flush()

        # Create child runs and update parent runs
        child_run_params = []
        for i, run_param in enumerate(run_params):
            child_run_id = uuid.uuid4()
            child_params = {
                "id": child_run_id,
                "project_name": project_name,
                "name": f"test_child_run {i}",
                "run_type": "llm",
                "inputs": {"text": f"child world {i}"},
                "dotted_order": "foo",
                "trace_id": run_param["id"],
            }
            client.create_run(**child_params)
            child_run_params.append(child_params)

        client.flush()

        # Verify all requests
        post_calls = [
            call
            for call in mock_session.request.mock_calls
            if call.args and call.args[0] == "POST"
        ]
        assert len(post_calls) >= 2

        # Verify that only odd-numbered runs were sampled (due to our counter logic)
        assert post_calls[0].args[1].endswith("/runs/batch")
        assert post_calls[1].args[1].endswith("/runs/batch")

        data = post_calls[0].kwargs["data"]
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        batch_data = json.loads(data)

        # Verify posts only contain odd-numbered runs
        assert len(batch_data.get("post", [])) == 2
        for post in batch_data.get("post", []):
            assert "text" in post["inputs"]
            if "hello world" in post["inputs"]["text"]:
                i = int(post["inputs"]["text"].split()[-1])
                assert i % 2 == 0
            elif "child world" in post["inputs"]["text"]:
                i = int(post["inputs"]["text"].split()[-1])
                assert i % 2 == 0

        data = post_calls[1].kwargs["data"]
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        batch_data = json.loads(data)
        assert len(batch_data.get("post", [])) == 2
        for p in batch_data.get("post", []):
            run_id = p["id"]
            original_run = next((r for r in run_params if str(r["id"]) == run_id), None)
            if original_run:
                i = int(original_run["name"].split()[-1])
                assert i % 2 == 0


@mock.patch("langsmith.client.requests.Session")
def test_select_eval_results(mock_session_cls: mock.Mock):
    expected = EvaluationResult(
        key="foo",
        value="bar",
        score=7899082,
        metadata={"a": "b"},
        comment="hi",
        feedback_config={"c": "d"},
    )
    client = Client(api_key="test")
    for count, input_ in [
        (1, expected),
        (1, expected.dict()),
        (1, {"results": [expected]}),
        (1, {"results": [expected.dict()]}),
        (2, {"results": [expected.dict(), expected.dict()]}),
        (2, {"results": [expected, expected]}),
    ]:
        op = client._select_eval_results(input_)
        assert len(op) == count
        assert op == [expected] * count

    expected2 = EvaluationResult(
        key="foo",
        metadata={"a": "b"},
        comment="this is a comment",
        feedback_config={"c": "d"},
    )

    as_reasoning = {
        "reasoning": expected2.comment,
        **expected2.dict(exclude={"comment"}),
    }
    for input_ in [as_reasoning, {"results": [as_reasoning]}, {"results": [expected2]}]:
        assert client._select_eval_results(input_) == [
            expected2,
        ]


@pytest.mark.parametrize("client_cls", [Client, AsyncClient])
@mock.patch("langsmith.client.requests.Session")
def test_validate_api_key_if_hosted(
    monkeypatch: pytest.MonkeyPatch, client_cls: Union[Type[Client], Type[AsyncClient]]
) -> None:
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    with pytest.warns(ls_utils.LangSmithMissingAPIKeyWarning):
        client_cls(api_url="https://api.smith.langchain.com")
    with warnings.catch_warnings():
        # Check no warning is raised here.
        warnings.simplefilter("error")
        client_cls(api_url="http://localhost:1984")


def test_parse_token_or_url():
    # Test with URL
    url = "https://smith.langchain.com/public/419dcab2-1d66-4b94-8901-0357ead390df/d"
    api_url = "https://api.smith.langchain.com"
    assert _parse_token_or_url(url, api_url) == (
        api_url,
        "419dcab2-1d66-4b94-8901-0357ead390df",
    )

    url = "https://smith.langchain.com/public/419dcab2-1d66-4b94-8901-0357ead390df/d"
    beta_api_url = "https://beta.api.smith.langchain.com"
    # Should still point to the correct public one
    assert _parse_token_or_url(url, beta_api_url) == (
        api_url,
        "419dcab2-1d66-4b94-8901-0357ead390df",
    )

    token = "419dcab2-1d66-4b94-8901-0357ead390df"
    assert _parse_token_or_url(token, api_url) == (
        api_url,
        token,
    )

    # Test with UUID object
    token_uuid = uuid.UUID("419dcab2-1d66-4b94-8901-0357ead390df")
    assert _parse_token_or_url(token_uuid, api_url) == (
        api_url,
        str(token_uuid),
    )

    # Test with custom num_parts
    url_custom = (
        "https://smith.langchain.com/public/419dcab2-1d66-4b94-8901-0357ead390df/p/q"
    )
    assert _parse_token_or_url(url_custom, api_url, num_parts=3) == (
        api_url,
        "419dcab2-1d66-4b94-8901-0357ead390df",
    )

    # Test with invalid URL
    invalid_url = "https://invalid.com/419dcab2-1d66-4b94-8901-0357ead390df"
    with pytest.raises(LangSmithUserError):
        _parse_token_or_url(invalid_url, api_url)


_PROMPT_COMMITS = [
    (
        True,
        "tools",
        {
            "owner": "-",
            "repo": "tweet-generator-example-with-tools",
            "commit_hash": "b862ce708ffeb932331a9345ea2a2fe6a76d62cf83e9aab834c24bb12bd516c9",
            "manifest": {
                "lc": 1,
                "type": "constructor",
                "id": ["langchain", "schema", "runnable", "RunnableSequence"],
                "kwargs": {
                    "first": {
                        "lc": 1,
                        "type": "constructor",
                        "id": ["langchain", "prompts", "chat", "ChatPromptTemplate"],
                        "kwargs": {
                            "input_variables": ["topic"],
                            "metadata": {
                                "lc_hub_owner": "-",
                                "lc_hub_repo": "tweet-generator-example",
                                "lc_hub_commit_hash": "c39837bd8d010da739d6d4adc7f2dca2f2461521661a393d37606f5c696109a5",
                            },
                            "messages": [
                                {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "chat",
                                        "SystemMessagePromptTemplate",
                                    ],
                                    "kwargs": {
                                        "prompt": {
                                            "lc": 1,
                                            "type": "constructor",
                                            "id": [
                                                "langchain",
                                                "prompts",
                                                "prompt",
                                                "PromptTemplate",
                                            ],
                                            "kwargs": {
                                                "input_variables": [],
                                                "template_format": "f-string",
                                                "template": "Generate a tweet based on the provided topic.",
                                            },
                                        }
                                    },
                                },
                                {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "chat",
                                        "HumanMessagePromptTemplate",
                                    ],
                                    "kwargs": {
                                        "prompt": {
                                            "lc": 1,
                                            "type": "constructor",
                                            "id": [
                                                "langchain",
                                                "prompts",
                                                "prompt",
                                                "PromptTemplate",
                                            ],
                                            "kwargs": {
                                                "input_variables": ["topic"],
                                                "template_format": "f-string",
                                                "template": "{topic}",
                                            },
                                        }
                                    },
                                },
                            ],
                        },
                        "name": "StructuredPrompt",
                    },
                    "last": {
                        "lc": 1,
                        "type": "constructor",
                        "id": ["langchain", "schema", "runnable", "RunnableBinding"],
                        "kwargs": {
                            "bound": {
                                "lc": 1,
                                "type": "constructor",
                                "id": [
                                    "langchain",
                                    "chat_models",
                                    "anthropic",
                                    "ChatAnthropic",
                                ],
                                "kwargs": {
                                    "temperature": 1,
                                    "max_tokens": 1024,
                                    "top_p": 1,
                                    "top_k": -1,
                                    "anthropic_api_key": {
                                        "id": ["ANTHROPIC_API_KEY"],
                                        "lc": 1,
                                        "type": "secret",
                                    },
                                    "model": "claude-3-5-sonnet-20240620",
                                },
                            },
                            "kwargs": {
                                "tools": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "GenerateTweet",
                                            "description": "Submit your tweet.",
                                            "parameters": {
                                                "properties": {
                                                    "tweet": {
                                                        "type": "string",
                                                        "description": "The generated tweet.",
                                                    }
                                                },
                                                "required": ["tweet"],
                                                "type": "object",
                                            },
                                        },
                                    },
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "SomethingElse",
                                            "description": "",
                                            "parameters": {
                                                "properties": {
                                                    "aval": {
                                                        "type": "array",
                                                        "items": {"type": "string"},
                                                    }
                                                },
                                                "required": [],
                                                "type": "object",
                                            },
                                        },
                                    },
                                ]
                            },
                        },
                    },
                },
            },
            "examples": [],
        },
    ),
    (
        True,
        "structured",
        {
            "owner": "-",
            "repo": "tweet-generator-example",
            "commit_hash": "e8da7f9e80471ace9b96c4f8fd55a215020126521f1da8f66130604c101fc522",
            "manifest": {
                "lc": 1,
                "type": "constructor",
                "id": ["langchain", "schema", "runnable", "RunnableSequence"],
                "kwargs": {
                    "first": {
                        "lc": 1,
                        "type": "constructor",
                        "id": [
                            "langchain_core",
                            "prompts",
                            "structured",
                            "StructuredPrompt",
                        ],
                        "kwargs": {
                            "input_variables": ["topic"],
                            "metadata": {
                                "lc_hub_owner": "langchain-ai",
                                "lc_hub_repo": "tweet-generator-example",
                                "lc_hub_commit_hash": "7c32ca78a2831b6b3a3904eb5704b48a0730e93f29afb0853cfaefc42dc09f9c",
                            },
                            "messages": [
                                {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "chat",
                                        "SystemMessagePromptTemplate",
                                    ],
                                    "kwargs": {
                                        "prompt": {
                                            "lc": 1,
                                            "type": "constructor",
                                            "id": [
                                                "langchain",
                                                "prompts",
                                                "prompt",
                                                "PromptTemplate",
                                            ],
                                            "kwargs": {
                                                "input_variables": [],
                                                "template_format": "f-string",
                                                "template": "Generate a tweet about the given topic.",
                                            },
                                        }
                                    },
                                },
                                {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "chat",
                                        "HumanMessagePromptTemplate",
                                    ],
                                    "kwargs": {
                                        "prompt": {
                                            "lc": 1,
                                            "type": "constructor",
                                            "id": [
                                                "langchain",
                                                "prompts",
                                                "prompt",
                                                "PromptTemplate",
                                            ],
                                            "kwargs": {
                                                "input_variables": ["topic"],
                                                "template_format": "f-string",
                                                "template": "{topic}",
                                            },
                                        }
                                    },
                                },
                            ],
                            "schema_": {
                                "title": "GenerateTweet",
                                "description": "Submit your tweet.",
                                "type": "object",
                                "properties": {
                                    "tweet": {
                                        "type": "string",
                                        "description": "The generated tweet.",
                                    }
                                },
                                "required": ["tweet"],
                            },
                        },
                        "name": "StructuredPrompt",
                    },
                    "last": {
                        "lc": 1,
                        "type": "constructor",
                        "id": ["langchain", "schema", "runnable", "RunnableBinding"],
                        "kwargs": {
                            "bound": {
                                "lc": 1,
                                "type": "constructor",
                                "id": [
                                    "langchain",
                                    "chat_models",
                                    "anthropic",
                                    "ChatAnthropic",
                                ],
                                "kwargs": {
                                    "temperature": 1,
                                    "max_tokens": 1024,
                                    "top_p": 1,
                                    "top_k": -1,
                                    "anthropic_api_key": {
                                        "id": ["ANTHROPIC_API_KEY"],
                                        "lc": 1,
                                        "type": "secret",
                                    },
                                    "model": "claude-3-5-sonnet-20240620",
                                },
                            },
                            "kwargs": {},
                        },
                    },
                },
            },
            "examples": [],
        },
    ),
    (
        True,
        "none",
        {
            "owner": "-",
            "repo": "tweet-generator-example-with-nothing",
            "commit_hash": "06c657373bdfcadec0d4d0933416b2c11f1b283ef3d1ca5dfb35dd6ed28b9f78",
            "manifest": {
                "lc": 1,
                "type": "constructor",
                "id": ["langchain", "schema", "runnable", "RunnableSequence"],
                "kwargs": {
                    "first": {
                        "lc": 1,
                        "type": "constructor",
                        "id": ["langchain", "prompts", "chat", "ChatPromptTemplate"],
                        "kwargs": {
                            "messages": [
                                {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "chat",
                                        "SystemMessagePromptTemplate",
                                    ],
                                    "kwargs": {
                                        "prompt": {
                                            "lc": 1,
                                            "type": "constructor",
                                            "id": [
                                                "langchain",
                                                "prompts",
                                                "prompt",
                                                "PromptTemplate",
                                            ],
                                            "kwargs": {
                                                "input_variables": [],
                                                "template_format": "f-string",
                                                "template": "Generate a tweet about the given topic.",
                                            },
                                        }
                                    },
                                },
                                {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "chat",
                                        "HumanMessagePromptTemplate",
                                    ],
                                    "kwargs": {
                                        "prompt": {
                                            "lc": 1,
                                            "type": "constructor",
                                            "id": [
                                                "langchain",
                                                "prompts",
                                                "prompt",
                                                "PromptTemplate",
                                            ],
                                            "kwargs": {
                                                "input_variables": ["topic"],
                                                "template_format": "f-string",
                                                "template": "{topic}",
                                            },
                                        }
                                    },
                                },
                            ],
                            "input_variables": ["topic"],
                        },
                    },
                    "last": {
                        "lc": 1,
                        "type": "constructor",
                        "id": ["langchain", "schema", "runnable", "RunnableBinding"],
                        "kwargs": {
                            "bound": {
                                "lc": 1,
                                "type": "constructor",
                                "id": [
                                    "langchain",
                                    "chat_models",
                                    "openai",
                                    "ChatOpenAI",
                                ],
                                "kwargs": {
                                    "openai_api_key": {
                                        "id": ["OPENAI_API_KEY"],
                                        "lc": 1,
                                        "type": "secret",
                                    },
                                    "model": "gpt-4o-mini",
                                },
                            },
                            "kwargs": {},
                        },
                    },
                },
            },
            "examples": [],
        },
    ),
    (
        False,
        "tools",
        {
            "owner": "-",
            "repo": "tweet-generator-example-with-tools",
            "commit_hash": "b862ce708ffeb932331a9345ea2a2fe6a76d62cf83e9aab834c24bb12bd516c9",
            "manifest": {
                "lc": 1,
                "type": "constructor",
                "id": ["langchain", "prompts", "chat", "ChatPromptTemplate"],
                "kwargs": {
                    "input_variables": ["topic"],
                    "metadata": {
                        "lc_hub_owner": "-",
                        "lc_hub_repo": "tweet-generator-example",
                        "lc_hub_commit_hash": "c39837bd8d010da739d6d4adc7f2dca2f2461521661a393d37606f5c696109a5",
                    },
                    "messages": [
                        {
                            "lc": 1,
                            "type": "constructor",
                            "id": [
                                "langchain",
                                "prompts",
                                "chat",
                                "SystemMessagePromptTemplate",
                            ],
                            "kwargs": {
                                "prompt": {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "prompt",
                                        "PromptTemplate",
                                    ],
                                    "kwargs": {
                                        "input_variables": [],
                                        "template_format": "f-string",
                                        "template": "Generate a tweet based on the provided topic.",
                                    },
                                }
                            },
                        },
                        {
                            "lc": 1,
                            "type": "constructor",
                            "id": [
                                "langchain",
                                "prompts",
                                "chat",
                                "HumanMessagePromptTemplate",
                            ],
                            "kwargs": {
                                "prompt": {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "prompt",
                                        "PromptTemplate",
                                    ],
                                    "kwargs": {
                                        "input_variables": ["topic"],
                                        "template_format": "f-string",
                                        "template": "{topic}",
                                    },
                                }
                            },
                        },
                    ],
                },
                "name": "StructuredPrompt",
            },
            "examples": [],
        },
    ),
    (
        False,
        "structured",
        {
            "owner": "-",
            "repo": "tweet-generator-example",
            "commit_hash": "e8da7f9e80471ace9b96c4f8fd55a215020126521f1da8f66130604c101fc522",
            "manifest": {
                "lc": 1,
                "type": "constructor",
                "id": ["langchain_core", "prompts", "structured", "StructuredPrompt"],
                "kwargs": {
                    "input_variables": ["topic"],
                    "metadata": {
                        "lc_hub_owner": "langchain-ai",
                        "lc_hub_repo": "tweet-generator-example",
                        "lc_hub_commit_hash": "7c32ca78a2831b6b3a3904eb5704b48a0730e93f29afb0853cfaefc42dc09f9c",
                    },
                    "messages": [
                        {
                            "lc": 1,
                            "type": "constructor",
                            "id": [
                                "langchain",
                                "prompts",
                                "chat",
                                "SystemMessagePromptTemplate",
                            ],
                            "kwargs": {
                                "prompt": {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "prompt",
                                        "PromptTemplate",
                                    ],
                                    "kwargs": {
                                        "input_variables": [],
                                        "template_format": "f-string",
                                        "template": "Generate a tweet about the given topic.",
                                    },
                                }
                            },
                        },
                        {
                            "lc": 1,
                            "type": "constructor",
                            "id": [
                                "langchain",
                                "prompts",
                                "chat",
                                "HumanMessagePromptTemplate",
                            ],
                            "kwargs": {
                                "prompt": {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "prompt",
                                        "PromptTemplate",
                                    ],
                                    "kwargs": {
                                        "input_variables": ["topic"],
                                        "template_format": "f-string",
                                        "template": "{topic}",
                                    },
                                }
                            },
                        },
                    ],
                    "schema_": {
                        "title": "GenerateTweet",
                        "description": "Submit your tweet.",
                        "type": "object",
                        "properties": {
                            "tweet": {
                                "type": "string",
                                "description": "The generated tweet.",
                            }
                        },
                        "required": ["tweet"],
                    },
                },
                "name": "StructuredPrompt",
            },
            "examples": [],
        },
    ),
    (
        False,
        "none",
        {
            "owner": "-",
            "repo": "tweet-generator-example-with-nothing",
            "commit_hash": "06c657373bdfcadec0d4d0933416b2c11f1b283ef3d1ca5dfb35dd6ed28b9f78",
            "manifest": {
                "lc": 1,
                "type": "constructor",
                "id": ["langchain", "prompts", "chat", "ChatPromptTemplate"],
                "kwargs": {
                    "messages": [
                        {
                            "lc": 1,
                            "type": "constructor",
                            "id": [
                                "langchain",
                                "prompts",
                                "chat",
                                "SystemMessagePromptTemplate",
                            ],
                            "kwargs": {
                                "prompt": {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "prompt",
                                        "PromptTemplate",
                                    ],
                                    "kwargs": {
                                        "input_variables": [],
                                        "template_format": "f-string",
                                        "template": "Generate a tweet about the given topic.",
                                    },
                                }
                            },
                        },
                        {
                            "lc": 1,
                            "type": "constructor",
                            "id": [
                                "langchain",
                                "prompts",
                                "chat",
                                "HumanMessagePromptTemplate",
                            ],
                            "kwargs": {
                                "prompt": {
                                    "lc": 1,
                                    "type": "constructor",
                                    "id": [
                                        "langchain",
                                        "prompts",
                                        "prompt",
                                        "PromptTemplate",
                                    ],
                                    "kwargs": {
                                        "input_variables": ["topic"],
                                        "template_format": "f-string",
                                        "template": "{topic}",
                                    },
                                }
                            },
                        },
                    ],
                    "input_variables": ["topic"],
                },
            },
            "examples": [],
        },
    ),
]


@pytest.mark.parametrize("include_model, manifest_type, manifest_data", _PROMPT_COMMITS)
def test_pull_prompt(
    include_model: bool,
    manifest_type: Literal["structured", "tool", "none"],
    manifest_data: dict,
):
    try:
        from langchain_core.language_models.base import BaseLanguageModel
        from langchain_core.output_parsers import JsonOutputKeyToolsParser
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.prompts.structured import StructuredPrompt
        from langchain_core.runnables import RunnableBinding, RunnableSequence
    except ImportError:
        pytest.skip("Skipping test that requires langchain")
    # Create a mock session
    mock_session = mock.Mock()
    # prompt_commit = ls_schemas.PromptCommit(**manifest_data)
    mock_session.request.side_effect = lambda method, url, **kwargs: mock.Mock(
        json=lambda: manifest_data if "/commits/" in url else None
    )

    # Create a client with Info pre-created and version >= 0.6
    info = ls_schemas.LangSmithInfo(version="0.6.0")
    client = Client(
        api_url="http://localhost:1984",
        api_key="fake_api_key",
        session=mock_session,
        info=info,
    )
    with mock.patch.dict(
        "os.environ",
        {
            "ANTHROPIC_API_KEY": "test_anthropic_key",
            "OPENAI_API_KEY": "test_openai_key",
        },
    ):
        result = client.pull_prompt(
            prompt_identifier=manifest_data["repo"], include_model=include_model
        )
    expected_prompt_type = (
        StructuredPrompt if manifest_type == "structured" else ChatPromptTemplate
    )
    if include_model:
        assert isinstance(result, RunnableSequence)
        assert isinstance(result.first, expected_prompt_type)
        if manifest_type != "structured":
            assert not isinstance(result.first, StructuredPrompt)
            assert len(result.steps) == 2
            if manifest_type == "tool":
                assert result.steps[1].kwargs.get("tools")
        else:
            assert len(result.steps) == 3
            assert isinstance(result.steps[1], RunnableBinding)
            assert result.steps[1].kwargs.get("tools")
            assert isinstance(result.steps[1].bound, BaseLanguageModel)
            assert isinstance(result.steps[2], JsonOutputKeyToolsParser)

    else:
        assert isinstance(result, expected_prompt_type)
        if manifest_type != "structured":
            assert not isinstance(result, StructuredPrompt)


def test_evaluate_methods() -> None:
    client_args = set(inspect.signature(Client.evaluate).parameters).difference(
        {"self"}
    )
    eval_args = set(inspect.signature(evaluate).parameters).difference({"client"})
    assert client_args == eval_args

    client_args = set(inspect.signature(Client.aevaluate).parameters).difference(
        {"self"}
    )
    eval_args = set(inspect.signature(aevaluate).parameters).difference({"client"})
    extra_args = client_args - eval_args
    assert not extra_args


@patch("langsmith.client.requests.Session")
def test_create_run_with_zstd_compression(mock_session_cls: mock.Mock) -> None:
    """Test that runs are sent using zstd compression when compression is enabled."""
    # Prepare a mocked session
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_session.request.return_value = mock_response
    mock_session_cls.return_value = mock_session

    with patch.dict("os.environ", {}, clear=True):
        info = ls_schemas.LangSmithInfo(
            version="0.6.0",
            instance_flags={"zstd_compression_enabled": True},
            batch_ingest_config=ls_schemas.BatchIngestConfig(
                use_multipart_endpoint=True,
                size_limit=1,
                size_limit_bytes=128,
                scale_up_nthreads_limit=4,
                scale_up_qsize_trigger=3,
                scale_down_nempty_trigger=1,
            ),
        )
        client = Client(
            api_url="http://localhost:1984",
            api_key="123",
            auto_batch_tracing=True,
            session=mock_session,
            info=info,
        )

        # Create a few runs with larger payloads so there's something to compress
        for i in range(2):
            run_id = uuid.uuid4()
            client.create_run(
                name=f"my_test_run_{i}",
                run_type="llm",
                inputs={"some_key": "some_val" * 1000},
                id=run_id,
                trace_id=run_id,
                dotted_order=str(run_id),
            )

        # Let the background threads flush
        if client.tracing_queue:
            client.tracing_queue.join()
        if client._futures is not None:
            for fut in client._futures:
                fut.result()

    time.sleep(0.1)

    # Inspect the calls
    post_calls = []
    for call_obj in mock_session.request.mock_calls:
        if call_obj.args and call_obj.args[0] == "POST":
            post_calls.append(call_obj)
    assert (
        len(post_calls) >= 1
    ), "Expected at least one POST to the compression endpoint"

    call_data = post_calls[0][2]["data"]

    if hasattr(call_data, "read"):
        call_data = call_data.read()

    zstd_magic = b"\x28\xb5\x2f\xfd"
    assert call_data.startswith(zstd_magic), (
        "Expected the request body to start with zstd magic bytes; "
        "it appears runs were not compressed."
    )


@patch("langsmith.client.requests.Session")
def test_create_feedback_with_zstd_compression(mock_session_cls: mock.Mock) -> None:
    """Test that feedback is sent using zstd compression when compression is enabled."""
    # Prepare a mocked session
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_session.request.return_value = mock_response
    mock_session_cls.return_value = mock_session

    with patch.dict("os.environ", {}, clear=True):
        info = ls_schemas.LangSmithInfo(
            version="0.8.11",
            instance_flags={"zstd_compression_enabled": True},
            batch_ingest_config=ls_schemas.BatchIngestConfig(
                use_multipart_endpoint=True,
                size_limit=1,
                size_limit_bytes=128,
                scale_up_nthreads_limit=4,
                scale_up_qsize_trigger=3,
                scale_down_nempty_trigger=1,
            ),
        )
        client = Client(
            api_url="http://localhost:1984",
            api_key="123",
            auto_batch_tracing=True,
            session=mock_session,
            info=info,
        )

        # Create a few runs with larger payloads so there's something to compress
        run_id = uuid.uuid4()

        feedback_data = {
            "comment": "test comment",
            "score": 0.95,
        }
        client.create_feedback(
            run_id=run_id, key="test_key", trace_id=run_id, **feedback_data
        )

        # Let the background threads flush
        if client.tracing_queue:
            client.tracing_queue.join()
        if client._futures is not None:
            for fut in client._futures:
                fut.result()

        time.sleep(0.1)

    # Inspect the calls
    post_calls = [
        call_obj
        for call_obj in mock_session.request.mock_calls
        if call_obj.args and call_obj.args[0] == "POST"
    ]
    assert len(post_calls) == 1, "Expected exactly one POST request"

    call_data = post_calls[0][2]["data"]
    if hasattr(call_data, "read"):
        call_data = call_data.read()

    # Check for zstd magic bytes
    zstd_magic = b"\x28\xb5\x2f\xfd"
    assert call_data.startswith(zstd_magic), (
        "Expected the request body to start with zstd magic bytes; "
        "it appears feedback was not compressed."
    )

    # Verify Content-Encoding header
    headers = post_calls[0][2]["headers"]
    assert (
        headers.get("Content-Encoding") == "zstd"
    ), "Expected Content-Encoding header to be 'zstd'"


@patch("langsmith.client.requests.Session")
def test_create_run_without_compression_support(mock_session_cls: mock.Mock) -> None:
    """Test that runs use regular multipart when server doesn't support compression."""
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_session.request.return_value = mock_response
    mock_session_cls.return_value = mock_session

    with patch.dict("os.environ", {}, clear=True):
        info = ls_schemas.LangSmithInfo(
            version="0.6.0",
            instance_flags={},  # No compression flag
            batch_ingest_config=ls_schemas.BatchIngestConfig(
                use_multipart_endpoint=True,
                size_limit=1,
                size_limit_bytes=128,
                scale_up_nthreads_limit=4,
                scale_up_qsize_trigger=3,
                scale_down_nempty_trigger=1,
            ),
        )
        client = Client(
            api_url="http://localhost:1984",
            api_key="123",
            auto_batch_tracing=True,
            session=mock_session,
            info=info,
        )

        run_id = uuid.uuid4()
        inputs = {"key": "there"}
        client.create_run(
            name="test_run",
            run_type="llm",
            inputs=inputs,
            id=run_id,
            trace_id=run_id,
            dotted_order=str(run_id),
        )

        outputs = {"key": "hi there"}

        client.update_run(
            run_id,
            outputs=outputs,
            end_time=datetime.now(timezone.utc),
            trace_id=run_id,
            dotted_order=str(run_id),
        )

        if client.tracing_queue:
            client.tracing_queue.join()

    time.sleep(0.1)

    post_calls = [
        call_obj
        for call_obj in mock_session.request.mock_calls
        if call_obj.args and call_obj.args[0] == "POST"
    ]
    assert len(post_calls) >= 1

    payloads = [
        (call[2]["headers"], call[2]["data"])
        for call in mock_session.request.mock_calls
        if call.args and call.args[1].endswith("runs/multipart")
    ]
    if not payloads:
        assert False, "No payloads found"

    parts: List[MultipartPart] = []
    for payload in payloads:
        headers, data = payload
        assert headers["Content-Type"].startswith("multipart/form-data")
        assert isinstance(data, bytes)
        boundary = parse_options_header(headers["Content-Type"])[1]["boundary"]
        parser = MultipartParser(io.BytesIO(data), boundary)
        parts.extend(parser.parts())

    assert [p.name for p in parts] == [
        f"post.{run_id}",
        f"post.{run_id}.inputs",
        f"post.{run_id}.outputs",
    ]
    assert [p.headers.get("content-type") for p in parts] == [
        "application/json",
        "application/json",
        "application/json",
    ]

    outputs_parsed = json.loads(parts[2].value)
    assert outputs_parsed == outputs
    inputs_parsed = json.loads(parts[1].value)
    assert inputs_parsed == inputs
    run_parsed = json.loads(parts[0].value)
    assert run_parsed["trace_id"] == str(run_id)
    assert run_parsed["dotted_order"] == str(run_id)


@patch("langsmith.client.requests.Session")
def test_create_run_with_disabled_compression(mock_session_cls: mock.Mock) -> None:
    """Test that runs use regular multipart when compression is explicitly disabled."""

    # Clear the cache to ensure the environment variable is re-evaluated
    ls_utils.get_env_var.cache_clear()

    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_session.request.return_value = mock_response
    mock_session_cls.return_value = mock_session

    with patch.dict(
        "os.environ", {"LANGSMITH_DISABLE_RUN_COMPRESSION": "true"}, clear=True
    ):
        info = ls_schemas.LangSmithInfo(
            version="0.6.0",
            instance_flags={"zstd_compression_enabled": True},  # Enabled on server
            batch_ingest_config=ls_schemas.BatchIngestConfig(
                use_multipart_endpoint=True,
                size_limit=1,
                size_limit_bytes=128,
                scale_up_nthreads_limit=4,
                scale_up_qsize_trigger=3,
                scale_down_nempty_trigger=1,
            ),
        )
        client = Client(
            api_url="http://localhost:1984",
            api_key="123",
            auto_batch_tracing=True,
            session=mock_session,
            info=info,
        )

        run_id = uuid.uuid4()
        inputs = {"key": "there"}
        client.create_run(
            name="test_run",
            run_type="llm",
            inputs=inputs,
            id=run_id,
            trace_id=run_id,
            dotted_order=str(run_id),
        )

        outputs = {"key": "hi there"}
        client.update_run(
            run_id,
            outputs=outputs,
            end_time=datetime.now(timezone.utc),
            trace_id=run_id,
            dotted_order=str(run_id),
        )

        # Let the background threads flush
        if client.tracing_queue:
            client.tracing_queue.join()

    time.sleep(0.1)

    post_calls = [
        call_obj
        for call_obj in mock_session.request.mock_calls
        if call_obj.args and call_obj.args[0] == "POST"
    ]
    assert len(post_calls) >= 1

    payloads = [
        (call[2]["headers"], call[2]["data"])
        for call in mock_session.request.mock_calls
        if call.args and call.args[1].endswith("runs/multipart")
    ]
    if not payloads:
        assert False, "No payloads found"

    parts: List[MultipartPart] = []
    for payload in payloads:
        headers, data = payload
        assert headers["Content-Type"].startswith("multipart/form-data")
        assert isinstance(data, bytes)
        boundary = parse_options_header(headers["Content-Type"])[1]["boundary"]
        parser = MultipartParser(io.BytesIO(data), boundary)
        parts.extend(parser.parts())

    assert [p.name for p in parts] == [
        f"post.{run_id}",
        f"post.{run_id}.inputs",
        f"post.{run_id}.outputs",
    ]
    assert [p.headers.get("content-type") for p in parts] == [
        "application/json",
        "application/json",
        "application/json",
    ]

    outputs_parsed = json.loads(parts[2].value)
    assert outputs_parsed == outputs
    inputs_parsed = json.loads(parts[1].value)
    assert inputs_parsed == inputs
    run_parsed = json.loads(parts[0].value)
    assert run_parsed["trace_id"] == str(run_id)
    assert run_parsed["dotted_order"] == str(run_id)


def test__dataset_examples_path():
    dataset_id = "123"
    api_url = "https://foobar.com/api"
    expected = "https://foobar.com/api/v1/platform/datasets/123/examples"
    for suffix in ("", "/", "/v1", "/v1/"):
        path = _dataset_examples_path(api_url + suffix, dataset_id)
        actual = (api_url + suffix).rstrip("/") + path
        assert expected == actual


def test__construct_url():
    api_url = "https://foobar.com/api"
    pathname = "v1/platform/datasets/123/examples"
    expected = "https://foobar.com/api/v1/platform/datasets/123/examples"
    for suffix in ("", "/"):
        for prefix in ("", "/", "https://foobar.com/api/"):
            actual = _construct_url(api_url + suffix, prefix + pathname)
            assert actual == expected


@mock.patch("langsmith.client.requests.get")
def test__convert_stored_attachments_to_attachments_dict(mock_get: mock.Mock):
    """Test URL construction in attachment downloading."""
    # Mock the requests.get response
    mock_response = mock.Mock()
    mock_response.content = b"test attachment data"
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    # Test case 1: api_url=None (existing behavior - presigned_url is already complete URL)
    data_with_complete_url = {
        "attachment_urls": {
            "attachment.test_file": {
                "presigned_url": "https://foobar.com/bucket/file.txt?signature=xyz",
                "mime_type": "text/plain",
            }
        }
    }

    result = _convert_stored_attachments_to_attachments_dict(
        data_with_complete_url, attachments_key="attachment_urls", api_url=None
    )

    assert "test_file" in result
    assert (
        result["test_file"]["presigned_url"]
        == "https://foobar.com/bucket/file.txt?signature=xyz"
    )
    assert result["test_file"]["mime_type"] == "text/plain"
    assert result["test_file"]["reader"].read() == b"test attachment data"

    # Verify requests.get was called with the complete URL as-is
    mock_get.assert_called_with(
        "https://foobar.com/bucket/file.txt?signature=xyz", stream=True
    )

    # Reset mock for next test case
    mock_get.reset_mock()
    mock_response.content = b"test attachment data 2"

    # Test case 2: api_url provided (new behavior - constructs full URL from API base + path)
    data_with_relative_url = {
        "attachment_urls": {
            "attachment.test_file2": {
                "presigned_url": "/api/public/download?jwt=abc123",
                "mime_type": "image/png",
            }
        }
    }

    result = _convert_stored_attachments_to_attachments_dict(
        data_with_relative_url,
        attachments_key="attachment_urls",
        api_url="https://api.langsmith.com",
    )

    assert "test_file2" in result
    assert (
        result["test_file2"]["presigned_url"] == "/api/public/download?jwt=abc123"
    )  # Original preserved
    assert result["test_file2"]["mime_type"] == "image/png"
    assert result["test_file2"]["reader"].read() == b"test attachment data 2"

    # Verify requests.get was called with the constructed full URL
    mock_get.assert_called_with(
        "https://api.langsmith.com/api/public/download?jwt=abc123", stream=True
    )

    # Reset mock for edge case test
    mock_get.reset_mock()

    # Test case 3: Edge case - api_url provided but presigned_url is already complete URL
    data_with_complete_url_edge_case = {
        "attachment_urls": {
            "attachment.test_file3": {
                "presigned_url": "https://example.foobar.com/file.jpg?token=456",
                "mime_type": "image/jpeg",
            }
        }
    }

    result = _convert_stored_attachments_to_attachments_dict(
        data_with_complete_url_edge_case,
        attachments_key="attachment_urls",
        api_url="https://api.langsmith.com",
    )

    assert "test_file3" in result
    # Verify requests.get was called with the complete URL unchanged
    mock_get.assert_called_with(
        "https://example.foobar.com/file.jpg?token=456", stream=True
    )

    # Test case 4: No attachments key present
    data_no_attachments = {}
    result = _convert_stored_attachments_to_attachments_dict(
        data_no_attachments,
        attachments_key="attachment_urls",
        api_url="https://api.langsmith.com",
    )
    assert result == {}

    # Test case 5: Empty attachments
    data_empty_attachments = {"attachment_urls": {}}
    result = _convert_stored_attachments_to_attachments_dict(
        data_empty_attachments,
        attachments_key="attachment_urls",
        api_url="https://api.langsmith.com",
    )
    assert result == {}

    # Test case 6: Attachments without "attachment." prefix are ignored
    data_mixed_keys = {
        "attachment_urls": {
            "attachment.valid_file": {
                "presigned_url": "/download/valid",
                "mime_type": "text/plain",
            },
            "invalid_file": {
                "presigned_url": "/download/invalid",
                "mime_type": "text/plain",
            },
        }
    }

    mock_get.reset_mock()
    result = _convert_stored_attachments_to_attachments_dict(
        data_mixed_keys,
        attachments_key="attachment_urls",
        api_url="https://api.langsmith.com",
    )

    assert "valid_file" in result
    assert "invalid_file" not in result
    # Only valid attachment should trigger a request
    mock_get.assert_called_once_with(
        "https://api.langsmith.com/download/valid", stream=True
    )
