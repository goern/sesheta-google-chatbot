#!/usr/bin/env python3
# google-chatbot
# Copyright(C) 2018 Christoph Görn
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

import responder
import uvicorn
import google

from datetime import datetime

from httplib2 import Http

from google.cloud import pubsub_v1
from googleapiclient.discovery import build
from oauth2client.service_account import ServiceAccountCredentials
from prometheus_client import Gauge, Counter, generate_latest

from thoth.common import init_logging

from chatbot.__version__ import __version__


init_logging()

_LOGGER = logging.getLogger("thoth.sesheta")
_DEBUG = os.getenv("GOOGLE_CHATBOT_DEBUG", False)

TOPIC_NAME = "projects/sesheta-chatbot/topics/chat-events"
SUBSCRIPTION_NAME = f"projects/sesheta-chatbot/subscriptions/sesheta-{uuid.uuid4().hex}"

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


def append_to_sheet(event):
    """Appends a new row to the Sesheta Google Sheet."""
    SCOPES = "https://www.googleapis.com/auth/spreadsheets"

    # The ID and range of a sample spreadsheet.
    SESHETA_SPREADSHEET_ID = "15_g9x0Xx3LQctukoJCnwrwMRk0fqe-vzxfpfrYP1z94"
    RANGE_NAME = "current input"

    credentials = ServiceAccountCredentials.from_json_keyfile_name("sesheta-chatbot-968e13a86991.json", SCOPES)
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

    values = [[datetime.utcnow().isoformat(), _from, _message, _type, _space]]
    body = {"values": values}
    result = (
        sheet.values()
        .append(spreadsheetId=SESHETA_SPREADSHEET_ID, range=RANGE_NAME, valueInputOption="RAW", body=body)
        .execute()
    )
    print("{0} cells appended.".format(result.get("updates").get("updatedCells")))


def callback(message):
    """Process the message we received from the Pub/Sub subscription."""
    event = json.loads(message.data.decode("utf8"))
    _LOGGER.debug(event)

    try:
        if event["type"] == "REMOVED_FROM_SPACE":
            _LOGGER.info("Bot removed from  %s" % event["space"]["name"])
        elif event["type"] == "ADDED_TO_SPACE" and event["space"]["type"] == "ROOM":
            resp = {"text": ("Thanks for adding me to {}!".format(event["space"]["name"]))}
        elif event["type"] == "ADDED_TO_SPACE" and event["space"]["type"] == "DM":
            resp = {"text": ("Thanks for having me in this one on one chat, {}!".format(event["user"]["displayName"]))}
        elif event["message"]["space"]["type"].upper() == "DM":
            sesheta_events_total.labels("dm").inc()
        elif event["message"]["space"]["type"].upper() == "ROOM":
            sesheta_events_total.labels("room").inc()
        elif event["type"] == "CARD_CLICKED":
            action_name = event["action"]["actionMethodName"]
            parameters = event["action"]["parameters"]
            _LOGGER.info("%r: %r", action_name, parameters)
    except KeyError as excptn:
        _LOGGER.error(excptn)
        return

    append_to_sheet(event)

    answer = f"Hey {event['message']['sender']['displayName']}, thanks for the info, I have recorded that fact!"

    scopes = "https://www.googleapis.com/auth/chat.bot"
    credentials = ServiceAccountCredentials.from_json_keyfile_name("sesheta-chatbot-968e13a86991.json", scopes)
    chat = build("chat", "v1", http=credentials.authorize(Http()))
    resp = chat.spaces().messages().create(parent=event["space"]["name"], body={"text": answer}).execute()
    _LOGGER.debug(resp)

    message.ack()


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
