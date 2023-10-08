#  Copyright (c) 2023. fleetingclarity <fleetingclarity@proton.me>

import pytest
import json
from unittest import mock
import main
import pika
from pika.exceptions import AMQPConnectionError


def test_missing_msg_attributes():
    with pytest.raises(Exception) as e:
        main.consume(None, None, None, json.dumps({"foo": "bar"}).encode())

    assert "Missing additional attributes" in str(e.value)


def test_github_event_processed():
    headers = {
        "X-Github-Event": "issues",
        "X-Hub-Signature": "sha1=b4e0e6c8a926415afa2a752406e0a862d0044b66",
        "User-Agent": "GitHub-Hookshot/mock",
        "Mock": "True"
    }

    event_payload = {
        "issue": {
            "created_at": "2023-10-03 18:37:11.224716",
            "updated_at": "2023-10-08 08:46:01.603352",
            "closed_at": "2023-10-08 08:46:01.603355",
            "number": 614,
            "labels": [{"name": "Incident"}],
            "body": "root cause: 0834a2e88bf0049dfb75cce942cb843147b3cd2a"
        },
        "repository": {"name": "foobar"},
        "attributes": {"headers": headers},
        "publishTime": "2023-10-08 13:46:01.606895",
        "message_id": 7116780781096697856
    }

    # Expected result after processing
    github_event_expected = {
        "event_type": "issues",
        "id": "foobar/614",
        "metadata": json.dumps(event_payload),
        "time_created": "2023-10-08 08:46:01.603352",
        "signature": "sha1=b4e0e6c8a926415afa2a752406e0a862d0044b66",
        "msg_id": 7116780781096697856,
        "source": "githubmock"  # based on the provided headers
    }

    # Mocking the function that writes to the database
    with mock.patch('shared.insert_row_into_events_raw') as mock_insert_function:
        main.consume(None, None, None, json.dumps(event_payload).encode('utf-8'))

    mock_insert_function.assert_called_with(github_event_expected)


def test_github_event_avoid_id_conflicts_pull_requests():
    headers = {"X-Github-Event": "pull_request", "X-Hub-Signature": "foo", "Mock": "True"}
    event_payload = {
        "pull_request": {
            "updated_at": "2023-06-15T13:12:14Z",
            "number": 477
        },
        "repository": {
            "name": "reponame"
        },
        "attributes": {"headers": headers}
    }

    github_event_calculated = main.process_github_event(headers=headers, msg=event_payload)
    github_event_expected = {
        "id": "reponame/477"
    }

    assert github_event_calculated["id"] == github_event_expected["id"]


def test_github_event_avoid_id_conflicts_issues():
    headers = {"X-Github-Event": "issues", "X-Hub-Signature": "foo", "Mock": "True"}
    event_payload = {
        "issue": {
            "updated_at": "2023-06-15T13:12:14Z",
            "number": 477
        },
        "repository": {
            "name": "reponame"
        },
        "attributes": {"headers": headers}
    }

    github_event_calculated = main.process_github_event(headers=headers, msg=event_payload)
    github_event_expected = {
        "id": "reponame/477"
    }

    assert github_event_calculated["id"] == github_event_expected["id"]


def test_unsupported_github_event_raises_exception():
    headers = {"X-Github-Event": "unsupported_event", "X-Hub-Signature": "foo"}
    msg = {}

    with pytest.raises(Exception) as e:
        main.process_github_event(headers=headers, msg=msg)

    assert "Unsupported GitHub event" in str(e.value)


def test_create_connection_with_successful_connection():
    with mock.patch("pika.BlockingConnection") as MockedConnection:
        main.create_connection()

    assert MockedConnection.call_count == 1


def test_create_connection_with_retries():
    with mock.patch("pika.BlockingConnection", side_effect=pika.exceptions.AMQPConnectionError):
        with pytest.raises(Exception) as e:
            main.create_connection()

    assert f"Failed to connect to RabbitMQ after multiple retries ({main.MAX_RETRIES})" in str(e.value)


def test_index_with_keyboard_interrupt():
    with mock.patch("main.create_connection") as MockedCreateConnection, \
            mock.patch.object(main, 'channel', autospec=True) as mock_channel:

        mock_channel.start_consuming.side_effect = KeyboardInterrupt
        main.index()

    mock_channel.stop_consuming.assert_called_once()


def test_index_with_connection_closed_by_broker(capsys):
    with mock.patch("main.create_connection") as MockedCreateConnection, \
            mock.patch.object(main, 'channel', autospec=True) as mock_channel:

        mock_channel.start_consuming.side_effect = pika.exceptions.ConnectionClosedByBroker
        main.index()

    mock_channel.stop_consuming.assert_called_once()
    assert "Connection was closed by the broker." in capsys.readouterr().out

