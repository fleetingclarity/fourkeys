# Copyright 2020 Google LLC
# Copyright 2023 fleetingclarity <72276886+fleetingclarity@users.noreply.github.com>
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

import mysql.connector
from mysql.connector import Error

config = {
    'host': os.environ.get('FK_DB_HOST'),
    'user': os.environ.get('FK_DB_USER'),
    'password': os.environ.get('FK_DB_PW'),
    'database': 'four_keys'
}


def insert_row_into_events_raw(event):
    if not event:
        raise Exception("No data to insert")

    connection = None
    cursor = None

    try:
        connection = mysql.connector.connect(**config)

        if is_unique(connection, 'events_raw', event["signature"]):
            # Insert row
            row_to_insert = [
                (
                    event["id"],
                    event["event_type"],
                    json.dumps(event["metadata"]),
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
        if connection and connection.is_connected():
            connection.close()


def insert_row_into_events_enriched(event):
    if not event:
        raise Exception("No data to insert")

    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(**config)
        dataset_id = "four_keys"

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
        if connection and connection.is_connected():
            connection.close()


def is_unique(connection, table, signature):
    cursor = connection.cursor()
    sql = f"SELECT signature FROM four_keys.{table} WHERE {SIGNATURE_FIELDS[table]} = '{signature}';"
    result = None
    try:
        cursor.execute(sql)
        result = cursor.fetchall()
    except Error as e:
        print(f'stuff about the failure: {e}')
    finally:
        if cursor:
            cursor.close()
    return not result


def create_unique_id(msg):
    hashed = hashlib.sha1(bytes(json.dumps(msg), "utf-8"))
    return hashed.hexdigest()


SIGNATURE_FIELDS = {
    "events_raw": "signature",
    "events_enriched": "events_raw_signature"
}
