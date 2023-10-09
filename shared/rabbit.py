#  Copyright (c) 2023. fleetingclarity <fleetingclarity@proton.me>


import pika
import pika.exceptions
import time


class RabbitMQConnector:
    def __init__(self, broker_address, exchange_name, queue_name, routing_key, max_retries=5, retry_delay=5):
        self.broker_address = broker_address
        self.exchange_name = exchange_name
        self.queue_name = queue_name
        self.routing_key = routing_key

        self.connection = None
        self.channel = None

        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def create_connection(self):
        parameters = pika.ConnectionParameters(
            host=self.broker_address,
            heartbeat=600,
            blocked_connection_timeout=300
        )
        for _ in range(self.max_retries):
            try:
                return pika.BlockingConnection(parameters)
            except pika.exceptions.AMQPConnectionError:
                print(f"failed to connect to RabbitMQ, retrying in {self.retry_delay} seconds...")
                time.sleep(self.retry_delay)
        raise Exception(f"Failed to connect to RabbitMQ after multiple retries ({self.max_retries})")

    def setup(self):
        print("creating connection to broker...")
        self.connection = self.create_connection()
        print("connected...")

        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange=self.exchange_name, exchange_type='direct')
        result = self.channel.queue_declare(queue=self.queue_name, exclusive=False)
        assert result.method.queue == self.queue_name, f'Unexpected queue name "{result.method.queue}" returned!'

        self.channel.queue_bind(exchange=self.exchange_name, queue=self.queue_name, routing_key=self.routing_key)

    def start_consuming(self, callback):
        print(f' [*] Waiting for work in the {self.queue_name} queue. Press CTRL+C to exit')
        self.channel.basic_consume(
            queue=self.queue_name, on_message_callback=callback, auto_ack=False
        )
        try:
            self.channel.start_consuming()
        except pika.exceptions.ConnectionClosedByBroker:
            print("Connection was closed by the broker.")
            self.channel.stop_consuming()
            self.connection.close()
        except pika.exceptions.AMQPChannelError:
            print("Caught a channel error.")
            self.channel.stop_consuming()
            self.connection.close()
        except KeyboardInterrupt:
            print("CTRL+C detected, stopping...")
            self.channel.stop_consuming()
            self.connection.close()
        except Exception as e:
            print(f"an unexpected error type={type(e)} occurred: {e}")
            import traceback
            traceback.print_exc()
            self.channel.stop_consuming()
            self.connection.close()
