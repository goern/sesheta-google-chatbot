#!/usr/bin/env python3
# google-chatbot
# Copyright(C) 2018,2019 Christoph Görn
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""This is a Google Hangouts Chat Bot. No Magic, Just Karma"""


import signal
import os
import logging
import json
import uuid
import threading

import requests
import responder
import uvicorn
import google
import kafka


from datetime import datetime

from httplib2 import Http

from google.cloud import pubsub_v1
from googleapiclient.discovery import build
from oauth2client.service_account import ServiceAccountCredentials
from prometheus_client import Gauge, Counter, generate_latest
from kafka import KafkaProducer

from thoth.common import init_logging

from chatbot.__version__ import __version__


init_logging()

_LOGGER = logging.getLogger("thoth.sesheta")
_DEBUG = os.getenv("GOOGLE_CHATBOT_DEBUG", False)
_KAFAK_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

LUIS_APP_ID = os.getenv("LUIS_APP_ID")
LUIS_APP_KEY = os.getenv("LUIS_APP_KEY")
TOPIC_NAME = "projects/sesheta-chatbot/topics/chat-events"
SUBSCRIPTION_NAME = f"projects/sesheta-chatbot/subscriptions/sesheta-{uuid.uuid4().hex}"
GOOGLE_CHATBOT_TOPIC_NAME = "cyborg_regidores_hangout"

subscriber = pubsub_v1.SubscriberClient()

api = responder.API(title="Sesheta Google Chatbot", version=__version__)
api.add_route("/", static=True)

api.debug = _DEBUG


# Info Metric
bot_info = Gauge(
    "sesheta_bot_info",  # what promethus ses
    "Sesheta Google Hangouts Chat Bot information",  # what the human reads
    ["version"],  # what labels I use
)
bot_info.labels(__version__).inc()

sesheta_events_total = Counter(
    "sesheta_events_total", "Total number of events received from Google Hangouts Chat.", ["space_type"]
)


def append_to_sheet(event: dict):
    """Appends a new row to the Sesheta Google Sheet."""
    _LOGGER.debug("adding chat event to our google sheet...")

    SCOPES = "https://www.googleapis.com/auth/spreadsheets"

    # The ID and range of a sample spreadsheet.
    SESHETA_SPREADSHEET_ID = "15_g9x0Xx3LQctukoJCnwrwMRk0fqe-vzxfpfrYP1z94"  # FIXME this should come from ENV
    RANGE_NAME = "current_input"

    credentials = ServiceAccountCredentials.from_json_keyfile_name("etc/service_account.json", SCOPES)
    http_auth = credentials.authorize(Http())

    sheets = build("sheets", "v4", http=http_auth)
    sheet = sheets.spreadsheets()

    result = sheet.values().get(spreadsheetId=SESHETA_SPREADSHEET_ID, range=RANGE_NAME).execute()
    values = result.get("values", [])

    _from = event["user"]["displayName"]
    try:
        _message = event["message"]["text"]
    except KeyError as excptn:
        _LOGGER.error(excptn)
        _LOGGER.debug(event)
        _message = None
    _type = event["type"]
    _space = event["space"]["name"]
    _space_name = ""

    try:
        _space_name = event["space"]["displayName"]
    except KeyError:
        pass

    values = [[datetime.utcnow().isoformat(), _from, _message, _space_name, _type, _space]]
    body = {"values": values}
    result = (
        sheet.values()
        .append(spreadsheetId=SESHETA_SPREADSHEET_ID, range=RANGE_NAME, valueInputOption="RAW", body=body)
        .execute()
    )
    print("{0} cells appended.".format(result.get("updates").get("updatedCells")))


def send_to_kafka(event: dict):
    """Send the received event to Kafka."""
    _LOGGER.debug("sending chat event to kafka topic...")

    try:
        producer = KafkaProducer(
            bootstrap_servers=_KAFAK_BOOTSTRAP_SERVERS,
            acks=1,  # Wait for leader to write the record to its local log only.
            compression_type="gzip",
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            security_protocol="SSL",
            ssl_check_hostname=False,
            ssl_cafile="conf/ca.pem",
        )
    except kafka.errors.NoBrokersAvailable as excptn:
        _LOGGER.debug("while trying to reconnect KafkaProducer: we failed...")
        _LOGGER.error(excptn)
        return

    try:
        producer.send(GOOGLE_CHATBOT_TOPIC_NAME, event)
    except AttributeError as excptn:
        _LOGGER.debug(excptn)
    except (kafka.errors.NotLeaderForPartitionError, kafka.errors.KafkaTimeoutError) as excptn:
        _LOGGER.error(excptn)
        producer.close()
        producer = None

    return


def generate_answer(event: dict):
    """Generate an answer based on the event."""
    _LOGGER.debug(f"generating an answer based on the event... {event}")

    if event["type"] == "MESSAGE":
        message = event["message"]["text"]
        sender = event["message"]["sender"]["name"].split("/")[1]
        space = event["message"]["space"]["name"].split("/")[1]

        headers = {"Ocp-Apim-Subscription-Key": LUIS_APP_KEY}
        params = {"q": message, "timezoneOffset": "0", "verbose": "true", "spellCheck": "false", "staging": "false"}

        answer_text = "Puh, right now I just can give you some generic response, sorry."

        try:
            r = requests.get(
                f"https://westus.api.cognitive.microsoft.com/luis/v2.0/apps/{LUIS_APP_ID}",
                headers=headers,
                params=params,
            )

            topScoringIntent = r.json()["topScoringIntent"]["intent"]
            topScoringIntentScore = r.json()["topScoringIntent"]["score"]
            entities = r.json()["entities"]

            if topScoringIntent == "help":
                answer_text = "Hi, I am Sesheta, a bot run by the AI CoE, please feel free to join our chat at https://chat.google.com/room/AAAARndRdLM"
            if topScoringIntent == "takeNoteForNewsletter":
                if topScoringIntentScore < 0.8:
                    answer_text = "I am unsure what you want to say, could you please rephrase?"
                    return

                answer_text = "this seems like an interesting information, thanks"

                if len(entities) > 0:
                    for entity in entities:
                        if entity["type"] == "builtin.url":
                            answer_text = (
                                f"I have taken down a note, and a human will have a look at {entity['entity']}"
                            )
            if topScoringIntent == "weather":
                answer_text = "Sorry, I don't know about the weather"

        except Exception as e:
            _LOGGER.error("[Errno {0}] {1}".format(e.errno, e.strerror))

        return answer_text


def callback(message):
    """Process the message we received from the Pub/Sub subscription."""
    answer = None
    thread_id = None
    event = json.loads(message.data.decode("utf8"))
    _LOGGER.debug(event)

    try:
        if event["type"] == "REMOVED_FROM_SPACE":
            _LOGGER.info("Bot removed from  %s" % event["space"]["name"])
        elif event["type"] == "ADDED_TO_SPACE" and event["space"]["type"] == "ROOM":
            answer = f"Thanks for adding me to '{event['space']['displayName']}'!"
        elif event["type"] == "ADDED_TO_SPACE" and event["space"]["type"] == "DM":
            answer = f"Thanks for having me in this one on one chat, {event['user']['displayName']}!"
        elif event["message"]["space"]["type"].upper() == "DM":
            sesheta_events_total.labels("dm").inc()

            thread_id = event["message"]["thread"]
        elif event["message"]["space"]["type"].upper() == "ROOM":
            sesheta_events_total.labels("room").inc()

            thread_id = event["message"]["thread"]
        elif event["type"] == "CARD_CLICKED":
            action_name = event["action"]["actionMethodName"]
            parameters = event["action"]["parameters"]
            _LOGGER.info("%r: %r", action_name, parameters)
    except KeyError as excptn:
        _LOGGER.error(excptn)
        return

    # ok, no further answer required
    if event["type"] == "REMOVED_FROM_SPACE":
        return

    if answer is None:
        answer = generate_answer(event)

    if answer is not None:
        response = {"text": answer}

        if thread_id is not None:
            response["thread"] = thread_id

        try:
            scopes = "https://www.googleapis.com/auth/chat.bot"
            credentials = ServiceAccountCredentials.from_json_keyfile_name("etc/service_account.json", scopes)
            chat = build("chat", "v1", http=credentials.authorize(Http()))
            resp = chat.spaces().messages().create(parent=event["space"]["name"], body=response).execute()
            _LOGGER.debug(resp)
        except googleapiclient.errors.HttpError as excptn:
            _LOGGER.error(excptn)
            message.ack()
            return

        except KeyError as excptn:
            _LOGGER.error(excptn)
            return

    try:
        append_to_sheet(event)

        send_to_kafka(event)

        message.ack()
    except Exception as generalException:
        _LOGGER.error(generalException)


class GooglePubSubSubscriber(threading.Thread):
    """A Google PubSub subscriber thread."""

    def run(self):
        try:
            subscriber.create_subscription(name=SUBSCRIPTION_NAME, topic=TOPIC_NAME)
        except google.api_core.exceptions.AlreadyExists as excptn:
            _LOGGER.error(excptn)
            subscriber.delete_subscription(SUBSCRIPTION_NAME)

        future = subscriber.subscribe(SUBSCRIPTION_NAME, callback)

        self.alive = True
        try:
            while self.alive:
                future.result()

        finally:
            future.close()

    def stop(self):
        self.alive = False
        self.join()


@api.route(before_request=True)
def prepare_response(req, resp):
    """Just add my signature."""
    resp.headers["X-Sesheta-Version"] = f"v{__version__}"


@api.route("/metrics")
async def metrics(req, resp):
    """Return the Prometheus Metrics."""
    _LOGGER.debug("exporting metrics registry...")

    resp.text = generate_latest().decode("utf-8")


if __name__ == "__main__":
    logging.getLogger("thoth").setLevel(logging.DEBUG if _DEBUG else logging.INFO)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

    _LOGGER.debug("Debug mode is on")
    _LOGGER.info(f"Hi, I am Sesheta, I will track your karma, and I'm running v{__version__}")

    # TODO handle SIGTERM, SIGKILL, SIGINT and delete the subscription on exit
    pubsub_receiver = GooglePubSubSubscriber()
    pubsub_receiver.start()

    api.run(address="0.0.0.0", port=8080, debug=_DEBUG)

    pubsub_receiver.stop()
