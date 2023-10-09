#  Copyright (c) 2023. fleetingclarity <fleetingclarity@proton.me>

from unittest.mock import patch, Mock

import pika
import pika.exceptions

from rabbit import RabbitMQConnector


def raise_exception(*args, **kwargs):
    raise pika.exceptions.AMQPConnectionError


def test_connection_retries():
    with patch("rabbit.pika") as mock_pika:
        # Simulate connection failures for the first 4 attempts
        mock_connection = Mock()
        side_effects = [raise_exception for _ in range(4)] + [mock_connection]
        mock_pika.BlockingConnection.side_effect = side_effects

        connector = RabbitMQConnector("localhost", "exchange", "queue", "routing_key")

        # This should succeed on the 5th attempt
        connection = connector.create_connection()
