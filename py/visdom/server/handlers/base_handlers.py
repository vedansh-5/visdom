#!/usr/bin/env python3

# Copyright 2017-present, The Visdom Authors
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Contain the basic web request handlers that all other handlers derive from
"""

import json
import logging
import traceback
import http.client
import time

import tornado.escape
import tornado.web
import tornado.websocket

from visdom.server.workspace_manager import WorkspaceAuthError
from visdom.utils.server_utils import escape_eid

# POST endpoints that mutate environment state; viewers are denied these.
WRITE_ENDPOINTS = {
    "events",
    "update",
    "close",
    "delete_env",
    "save",
    "fork_env",
    "upload_env",
}


_COMMON_APP_ATTRIBUTES = (
    "state",
    "subs",
    "sources",
    "port",
    "env_path",
    "storage",
    "login_enabled",
    "mark_dirty",
)

_WEB_APP_ATTRIBUTES = _COMMON_APP_ATTRIBUTES + (
    "max_text_lines",
    "max_old_content",
    "max_image_history",
)

_SOCKET_APP_ATTRIBUTES = _COMMON_APP_ATTRIBUTES + ("readonly",)


def _copy_app_attributes(handler, app, attrs, store_app=False):
    if app is None:
        return

    if store_app:
        handler.app = app

    for attr in attrs:
        setattr(handler, attr, getattr(app, attr))


class BaseWebSocketHandler(tornado.websocket.WebSocketHandler):
    """
    Implements any required overriden functionality from the basic tornado
    websocket handler. Also contains some shared logic for all WebSocketHandler
    classes.
    """

    def initialize(self, app=None):
        """Common initialization shared by WebSocket handlers."""
        _copy_app_attributes(self, app, _SOCKET_APP_ATTRIBUTES, store_app=True)

    def get_current_user(self):
        """
        This method determines the self.current_user
        based the value of cookies that set in POST method
        at IndexHandler by self.set_secure_cookie
        """
        try:
            return self.get_secure_cookie("user_password")
        except Exception:  # Not using secure cookies
            return None


class BaseHandler(tornado.web.RequestHandler):
    """
    Implements any required overriden functionality from the basic tornado
    request handlers, and contains any convenient shared logic helpers.
    """

    def initialize(self, app=None):
        """Common initialization shared by most handlers.

        Copies frequently-used attributes from the application instance.
        Subclasses that need additional attributes should call
        ``super().initialize(app)`` and then set their own.

        The ``app`` parameter defaults to ``None`` so that handlers
        registered without an ``app`` dict (e.g. HealthHandler) still work.
        """
        _copy_app_attributes(self, app, _WEB_APP_ATTRIBUTES)
        if app is not None:
            self.workspace_manager = getattr(app, "workspace_manager", None)

    def is_authorized(self):
        """Update access time and validate authentication for protected methods."""
        self.last_access = time.time()
        if self.login_enabled and not self.current_user:
            self.set_status(401)
            return False
        return True

    def __init__(self, *request, **kwargs):
        self.include_host = False
        self.workspace_manager = None
        super(BaseHandler, self).__init__(*request, **kwargs)

    def prepare(self):
        """Scope a workspace-authenticated request to its workspace namespace.

        Only engages when the request carries both the X-API-KEY and
        X-Visdom-Workspace headers, so ordinary visdom traffic (browser reads,
        health checks, keyless clients) is unaffected. Resolves the key and
        workspace through the gateway, denies writes for the viewer role, and
        rewrites the body's eid to ``ws_<workspace_id>/<eid>`` so all state for
        this request lands in that workspace's environment namespace.
        """
        if self.workspace_manager is None:
            return
        api_key = self.request.headers.get("X-API-KEY")
        workspace_slug = self.request.headers.get("X-Visdom-Workspace")
        if not api_key or not workspace_slug:
            return

        try:
            workspace_id, role = self.workspace_manager.resolve(api_key, workspace_slug)
        except WorkspaceAuthError as exc:
            self.set_status(exc.status_code)
            self.finish({"error": exc.message})
            return

        endpoint = self.request.path.rsplit("/", 1)[-1]
        if role == "viewer" and endpoint in WRITE_ENDPOINTS:
            self.set_status(403)
            self.finish(
                {"error": "This API key's role is read-only in this workspace."}
            )
            return

        prefix = f"ws_{workspace_id}"
        self._scope_body_eid(prefix)
        if getattr(self, "EID_IN_PATH", False) and self.path_args:
            self._scope_path_eid(prefix)

    def _scope_body_eid(self, prefix):
        if not self.request.body:
            return
        try:
            body = tornado.escape.json_decode(
                tornado.escape.to_basestring(self.request.body)
            )
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        eid = body.get("eid")
        if eid is None or str(eid).startswith(prefix):
            return
        body["eid"] = f"{prefix}/{eid}"
        self.request.body = tornado.escape.utf8(json.dumps(body))

    def _scope_path_eid(self, prefix):
        args = list(self.path_args)
        eid = args[0]
        if not eid or str(eid).startswith(prefix):
            return
        args[0] = escape_eid(f"{prefix}/{eid}")
        self.path_args = args

    def get_current_user(self):
        """
        This method determines the self.current_user
        based the value of cookies that set in POST method
        at IndexHandler by self.set_secure_cookie
        """
        try:
            return self.get_secure_cookie("user_password")
        except Exception:  # Not using secure cookies
            return None

    def write_error(self, status_code, **kwargs):
        logging.error("ERROR: %s: %s" % (status_code, kwargs))
        if "exc_info" in kwargs:
            logging.info(
                "Traceback: {}".format(traceback.format_exception(*kwargs["exc_info"]))
            )
            debug = self.settings.get("debug")
            title = http.client.responses.get(status_code, "Unknown Error")
            logging.error("rendering error page")
            exc_info = kwargs["exc_info"]
            # exc_info is a tuple consisting of:
            # 1. The class of the Exception
            # 2. The actual Exception that was thrown
            # 3. The traceback object
            try:
                params = {
                    "error": exc_info[1] if debug else None,
                    "trace_info": (
                        traceback.format_exception(*exc_info) if debug else None
                    ),
                    "request": self.request.__dict__ if debug else None,
                    "status_code": status_code,
                    "title": title,
                }
                self.render("error.html", **params)
                logging.error("rendering complete")
                return
            except Exception as e:
                logging.error(e)
            self.set_status(status_code)
            self.write(
                f"""
                <h1>{status_code} - {title}</h1>
                """
            )
