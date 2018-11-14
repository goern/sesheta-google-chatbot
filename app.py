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

import os
import logging
import json

import responder
import uvicorn

from prometheus_client import Gauge, Counter, generate_latest

from thoth.common import init_logging

from chatbot.__version__ import __version__
from chatbot.responses import create_card_response


# Info Metric
bot_info = Gauge(
    "sesheta_bot_info",  # what promethus ses
    "Sesheta Google Hangouts Chat Bot information",  # what the human reads
    ["version"],  # what labels I use
)
bot_info.labels(__version__).inc()

http_requests_total = Counter(
    "sesheta_http_requests_total", "Total number of HTTP POST requests received from Google Hangouts Chat.", ["method"]
)

init_logging()

_LOGGER = logging.getLogger("thoth.sesheta")
_DEBUG = os.getenv("GOOGLE_CHATBOT_DEBUG", False)

api = responder.API(title="Sesheta Google Chatbot", version=__version__)
api.add_route("/", static=True)

api.debug = _DEBUG


@api.route(before_request=True)
def prepare_response(req, resp):
    """Just add my signature."""
    resp.headers["X-Sesheta-Version"] = f"v{__version__}"


@api.route("/metrics")
async def metrics(req, resp):
    """Return the Prometheus Metrics."""
    _LOGGER.debug("exporting metrics registry...")

    resp.text = generate_latest().decode("utf-8")


@api.route("/bot")  # FIXME this URL should contain a super secret string...
async def bot(req, resp):
    """Handle all requests sent to this endpoint from Hangouts Chat."""
    if req.method != "post":
        http_requests_total.labels("get").inc()

        resp.text = "Only POST allowed"
        return

    event = None
    http_requests_total.labels("post").inc()

    try:
        event = await req.media()
    except json.decoder.JSONDecodeError as excptn:
        _LOGGER.error(excptn)

    _LOGGER.debug("received an event from hangouts: %r.", event)

    if event["type"] == "ADDED_TO_SPACE" and event["space"]["type"] == "ROOM":
        text = f'Thanks for adding me to "{event["space"]["displayName"]}"!'
    elif event["type"] == "MESSAGE":
        text = "You said: `%s`" % event["message"]["text"]
    elif event["type"] == "CARD_CLICKED":
        action_name = event["action"]["actionMethodName"]
        parameters = event["action"]["parameters"]
        _LOGGER.info(f"{action_name} with {parameters}")
    else:
        return

    resp.media = create_card_response(text)


if __name__ == "__main__":
    logging.getLogger("thoth").setLevel(logging.DEBUG if _DEBUG else logging.INFO)

    _LOGGER.debug("Debug mode is on")

    _LOGGER.info(f"Hi, I am Sesheta, I will track your karma, and I'm running v{__version__}")

    api.run(address="0.0.0.0", port=8080, debug=_DEBUG)
