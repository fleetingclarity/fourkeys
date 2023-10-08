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

import json
import os
import sys
import random
from datetime import datetime

from flask import abort, Flask, request
from snowflake import SnowflakeGenerator
import pika

import sources

# it's ok for multiple nodes to use the same machine_id but a bit of randomness should help avoid collisions
machine_id = random.randint(0, 1023)
generator = SnowflakeGenerator(machine_id)

BROKER_ADDRESS = os.environ.get("FK_BROKER_ADDRESS")

app = Flask(__name__)
print(f'Starting event_handler node with machine_id {machine_id}')


@app.route("/", methods=["GET", "POST"])
def index():
    """
    Receives event data from a webhook, checks if the source is authorized,
    checks if the signature is verified, and then sends the data to the broker.
    """

    # Check if the source is authorized
    source = sources.get_source(request.headers)

    if source not in sources.AUTHORIZED_SOURCES:
        abort(403, f"Source not authorized: {source}")

    auth_source = sources.AUTHORIZED_SOURCES[source]
    signature_sources = {**request.headers, **request.args}
    signature = signature_sources.get(auth_source.signature, None)

    if not signature:
        abort(403, "Signature not found in request headers")

    body = request.data

    # Verify the signature
    verify_signature = auth_source.verification
    if not verify_signature(signature, body):
        abort(403, "Signature does not match expected signature")

    # Remove the Auth header so we do not publish it
    headers = dict(request.headers)
    if "Authorization" in headers:
        del headers["Authorization"]

    publish_to_broker(source, body, headers)

    # Flush the stdout to avoid log buffering.
    sys.stdout.flush()
    return "", 204


def publish_to_broker(source, msg, headers):
    """
    Publishes the message to the message broker
    """
    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters(BROKER_ADDRESS))
        channel = connection.channel()
        exchange = 'fk_events'
        channel.exchange_declare(exchange=exchange, exchange_type='direct')
        message = json.loads(msg)
        message['attributes'] = {}
        message['attributes']['headers'] = headers
        message['publishTime'] = str(datetime.utcnow())

        assign_id(message)
        print(f'publishing message with id={message["message_id"]} to {exchange}.{source}')

        channel.basic_publish(
            exchange=exchange, routing_key=source, body=json.dumps(message).encode('utf-8')
        )

        print(f"Published message: {json.dumps(message)}")

    except Exception as e:
        # Log any exceptions to stackdriver
        entry = dict(severity="WARNING", message=e)
        print(entry)


def assign_id(msg):
    msg['message_id'] = next(generator)


if __name__ == "__main__":
    PORT = int(os.getenv("PORT")) if os.getenv("PORT") else 8080

    # This is used when running locally. Gunicorn is used to run the
    # application on Cloud Run. See entrypoint in Dockerfile.
    app.run(host="0.0.0.0", port=PORT, debug=True)
