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

from datetime import datetime
import os
import json
import time

from pika.adapters.blocking_connection import BlockingChannel

import shared
import rabbit
from pika.amqp_object import Properties
from pika.spec import Basic

import pika

BROKER_ADDRESS = os.environ.get('FK_BROKER_ADDRESS')


def index():
    connector = rabbit.RabbitMQConnector(broker_address=BROKER_ADDRESS,
                                         exchange_name='fk_events',
                                         queue_name='fk_work_gitlab',
                                         routing_key='gitlab')
    connector.setup()
    connector.start_consuming(callback=consume)


def consume(ch: BlockingChannel, method: Basic.Deliver, properties: Properties, body: bytes):
    event = None
    # Check request for JSON
    msg = json.loads(body)

    if "attributes" not in msg:
        raise Exception("Missing additional attributes")

    try:
        attr = msg["attributes"]

        # Header Event info
        if "headers" in attr:
            headers = attr["headers"]

            # Process Gitlab Events
            if "X-Gitlab-Event" in headers:
                event = process_gitlab_event(headers, msg)

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


def process_gitlab_event(headers, msg):
    # Unique hash for the event
    signature = shared.create_unique_id(msg)
    source = "gitlab"

    if "Mock" in headers:
        source += "mock"

    types = {"push", "merge_request",
             "note", "tag_push", "issue",
             "pipeline", "job", "deployment",
             "build"}

    metadata = msg

    event_type = metadata["object_kind"]

    if event_type not in types:
        raise Exception("Unsupported Gitlab event: '%s'" % event_type)

    if event_type in ("push", "tag_push"):
        e_id = metadata["checkout_sha"]
        for commit in metadata["commits"]:
            if commit["id"] == e_id:
                time_created = commit["timestamp"]

    if event_type in ("merge_request", "note", "issue", "pipeline"):
        event_object = metadata["object_attributes"]
        e_id = event_object["id"]
        time_created = (
            event_object.get("updated_at") or
            event_object.get("finished_at") or
            event_object.get("created_at"))

    if event_type in ("job"):
        e_id = metadata["build_id"]
        time_created = (
            event_object.get("finished_at") or
            event_object.get("started_at"))

    if event_type in ("deployment"):
        e_id = metadata["deployment_id"]
        time_created = metadata["status_changed_at"]

    if event_type in ("build"):
        e_id = metadata["build_id"]
        time_created = (
            metadata.get("build_finished_at") or
            metadata.get("build_started_at") or
            metadata.get("build_created_at"))

    # Some timestamps come in a format like "2021-04-28 21:50:00 +0200"
    # BigQuery does not accept this as a valid format
    # Removing the extra timezone information below
    try:
        dt = datetime.strptime(time_created, '%Y-%m-%d %H:%M:%S %z')
        time_created = dt.strftime('%Y-%m-%d %H:%M:%S')

    # If the timestamp is not parsed correctly,
    # we will default to the string from the event payload
    except Exception:
        pass

    gitlab_event = {
        "event_type": event_type,
        "id": e_id,
        "metadata": metadata,
        # If time_created not supplied by event, default to event_handler publishTime
        "time_created": time_created or msg["publishTime"],
        "signature": signature,
        "msg_id": msg["message_id"],
        "source": source,
    }

    return gitlab_event


if __name__ == "__main__":
    index()
