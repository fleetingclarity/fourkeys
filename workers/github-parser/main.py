# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json

from pika.adapters.blocking_connection import BlockingChannel
from pika.amqp_object import Properties
from pika.spec import Basic

import shared
import rabbit

BROKER_ADDRESS = os.environ.get('FK_BROKER_ADDRESS')


def index():
    connector = rabbit.RabbitMQConnector(broker_address=BROKER_ADDRESS,
                                         exchange_name='fk_events',
                                         queue_name='fk_work_github',
                                         routing_key='github')
    connector.setup()
    connector.start_consuming(callback=consume)


def consume(ch: BlockingChannel, method: Basic.Deliver, properties: Properties, body: bytes):
    event = None
    msg = json.loads(body)

    if "attributes" not in msg:
        raise Exception("Missing additional attributes")

    try:
        attr = msg["attributes"]

        # Header Event info
        if "headers" in attr:
            headers = attr["headers"]

            # Process Github Events
            if "X-Github-Event" in headers:
                event = process_github_event(headers, msg)

        shared.insert_row_into_events_raw(event)

    except Exception as e:
        entry = {
                "severity": "WARNING",
                "msg": "Data not saved to database",
                "errors": str(e),
                "json_payload": msg
            }
        print(json.dumps(entry))

    ch.basic_ack(delivery_tag=method.delivery_tag, multiple=False)
    print(f'finished processing message with id={msg["message_id"]}')


def process_github_event(headers, msg):
    event_type = headers["X-Github-Event"]
    signature = headers["X-Hub-Signature"]
    source = "github"

    if "Mock" in headers:
        source += "mock"

    types = {"push", "pull_request", "pull_request_review",
             "pull_request_review_comment", "issues",
             "issue_comment", "check_run", "check_suite", "status",
             "deployment_status", "release"}

    if event_type not in types:
        raise Exception("Unsupported GitHub event: '%s'" % event_type)

    metadata = msg

    if event_type == "push":
        time_created = metadata["head_commit"]["timestamp"]
        e_id = metadata["head_commit"]["id"]

    if event_type == "pull_request":
        time_created = metadata["pull_request"]["updated_at"]
        e_id = metadata["repository"]["name"] + "/" + str(metadata["number"])

    if event_type == "pull_request_review":
        time_created = metadata["review"]["submitted_at"]
        e_id = metadata["review"]["id"]

    if event_type == "pull_request_review_comment":
        time_created = metadata["comment"]["updated_at"]
        e_id = metadata["comment"]["id"]

    if event_type == "issues":
        time_created = metadata["issue"]["updated_at"]
        e_id = metadata["repository"]["name"] + "/" + str(metadata["issue"]["number"])

    if event_type == "issue_comment":
        time_created = metadata["comment"]["updated_at"]
        e_id = metadata["comment"]["id"]

    if event_type == "check_run":
        time_created = (metadata["check_run"]["completed_at"] or
                        metadata["check_run"]["started_at"])
        e_id = metadata["check_run"]["id"]

    if event_type == "check_suite":
        time_created = (metadata["check_suite"]["updated_at"] or
                        metadata["check_suite"]["created_at"])
        e_id = metadata["check_suite"]["id"]

    if event_type == "deployment_status":
        time_created = metadata["deployment_status"]["updated_at"]
        e_id = metadata["deployment_status"]["id"]

    if event_type == "status":
        time_created = metadata["updated_at"]
        e_id = metadata["id"]

    if event_type == "release":
        time_created = (metadata["release"]["published_at"] or
                        metadata["release"]["created_at"])
        e_id = metadata["release"]["id"]

    github_event = {
        "event_type": event_type,
        "id": e_id,
        "metadata": json.dumps(metadata),
        "time_created": time_created,
        "signature": signature,
        "msg_id": msg["message_id"],
        "source": source,
    }

    return github_event


if __name__ == "__main__":
    index()
