# Copyright 2020 Google LLC
# Copyright 2023 fleetingclarity <fleetingclarity@proton.me>
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
import hashlib
import json
import os
import time

import psycopg2
from psycopg2 import Error, OperationalError, extras, pool

config = {
    'host': os.environ.get('FK_DB_HOST'),
    'user': os.environ.get('FK_DB_USER'),
    'password': os.environ.get('FK_DB_PW'),
    'port': os.environ.get('FK_DB_PORT', 5432),
    'database': 'fourkeys'
}

MAX_RETRIES = 5
BASE_WAIT_TIME = 2  # in seconds
MIN_CONNS = 1
MAX_CONNS = 5

connection_pool = psycopg2.pool.SimpleConnectionPool(
    MIN_CONNS,
    MAX_CONNS,
    database=config['database'],
    user=config['user'],
    password=config['password'],
    host=config['host'],
    port=config['port']
)


def get_connection():
    retries = 0
    while retries < MAX_RETRIES:
        try:
            return connection_pool.getconn()
        except (OperationalError, pool.PoolError) as e:
            retries += 1
            print(f'Failed to connect, attempt {retries} of {MAX_RETRIES}. Error: {e}')
            time.sleep(BASE_WAIT_TIME * (2 ** retries))  # exponential backoff
            continue
    raise Exception("Unable to establish database connection from the pool after multiple retries.")


def return_connection(conn):
    connection_pool.putconn(conn)


def shutdown():
    if connection_pool:
        connection_pool.closeall()


def insert_row_into_events_raw(event):
    if not event:
        raise Exception("No data to insert")

    connection = None
    cursor = None

    try:
        connection = get_connection()

        if is_unique(connection, 'events_raw', event["signature"]):
            # first check that we're inserting a string and not a python dict
            if type(event['metadata']) is not str:
                event['metadata'] = json.dumps(event['metadata'])

            # Insert row
            row_to_insert = [
                (
                    event["id"],
                    event["event_type"],
                    event["metadata"],
                    event["time_created"],
                    event["signature"],
                    event["msg_id"],
                    event["source"],
                )
            ]
            cursor = connection.cursor()
            insert_query = """
            INSERT INTO events_raw (id, event_type, metadata, time_created, signature, msg_id, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            try:
                cursor.executemany(insert_query, row_to_insert)
                connection.commit()
            except Error as e:
                entry = {
                    "severity": "WARNING",
                    "msg": "Row not inserted.",
                    "errors": str(e),
                    "row": row_to_insert,
                }
                print(json.dumps(entry))
    except Error as e:
        print(f'error inserting a row: {e}')
        connection.rollback()
    finally:
        if cursor:
            cursor.close()
        if connection:
            return_connection(connection)


def insert_row_into_events_enriched(event):
    if not event:
        raise Exception("No data to insert")

    connection = None
    cursor = None
    try:
        connection = get_connection()

        if is_unique(connection, 'events_enriched', event["events_raw_signature"]):
            # Insert row
            row_to_insert = [
                (
                    event["events_raw_signature"],
                    json.dumps(event["enriched_metadata"])
                )
            ]
            insert_query = """
            INSERT INTO events_enriched (events_raw_signature, enriched_metadata)
            VALUES (%s, %s)
            """
            try:
                cursor = connection.cursor()
                cursor.executemany(insert_query, row_to_insert)
                connection.commit()
            except Error as e:
                entry = {
                    "severity": "WARNING",
                    "msg": "Row not inserted.",
                    "errors": e,
                    "row": row_to_insert,
                }
                print(json.dumps(entry))

    except Error as e:
        print(f'error inserting enriched: {e}')
        connection.rollback()
    finally:
        if cursor:
            cursor.close()
        if connection:
            return_connection(connection)


def is_unique(connection, table, signature):
    cursor = connection.cursor()
    sql = f"SELECT {SIGNATURE_FIELDS[table]} FROM {table} WHERE {SIGNATURE_FIELDS[table]} = '{signature}';"
    result = None
    try:
        cursor.execute(sql)
        result = cursor.fetchall()
    except Error as e:
        print(f'stuff about the failure: {e}')
        connection.rollback()
    finally:
        if cursor:
            cursor.close()
    return not result


def create_unique_id(msg):
    hashed = hashlib.sha1(bytes(json.dumps(msg), "utf-8"))
    return hashed.hexdigest()


def get_previous_deployment(deployment_id):
    connection = get_connection()
    cursor = connection.cursor(cursor_factory=extras.RealDictCursor)
    sql = f"""WITH deployments_with_previous AS (
        SELECT
            deploy_id,
            LAG(deploy_id) OVER (ORDER BY time_created) AS previous_deploy_id,
            LAG(time_created) OVER (ORDER BY time_created) AS previous_time_created,
            LAG(main_commit) OVER (ORDER BY time_created) AS previous_main_commit
        FROM deploys
    )
    SELECT
        dp.previous_deploy_id,
        dp.previous_time_created,
        dp.previous_main_commit
    FROM deploys d
    JOIN deployments_with_previous dp ON d.deploy_id = dp.deploy_id
    WHERE d.deploy_id = '{deployment_id}';
    """
    try:
        cursor.execute(sql)
        result = cursor.fetchall()
    except Error as e:
        print(f'unable to query for deployment prior to {deployment_id}: {e}')
        connection.rollback()
        return None
    finally:
        if cursor:
            cursor.close()
        if connection:
            return_connection(connection)
    return result[0]


def get_changes_between(earlier, later):
    connection = get_connection()
    cursor = connection.cursor(cursor_factory=extras.RealDictCursor)
    sql = f"""WITH changes_in_range AS (
        SELECT
            source,
            event_type,
            change_id,
            time_created
        FROM changes
        WHERE time_created between '{earlier}' AND '{later}'
        ORDER BY time_created DESC
    )
    SELECT change_id, time_created
    FROM changes_in_range
    ORDER BY time_created DESC;
    """
    try:
        cursor.execute(sql)
        results = cursor.fetchall()
    except Error as e:
        print(f'unable to query for changes between {earlier} and {later}: {e}')
        connection.rollback()
        return None
    finally:
        if cursor:
            cursor.close()
        if connection:
            return_connection(connection)
    return results


SIGNATURE_FIELDS = {
    "events_raw": "signature",
    "events_enriched": "events_raw_signature"
}
