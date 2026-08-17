#!/usr/bin/env python3

# Copyright 2017-present, The Visdom Authors
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Contain the basic web request handlers that all other handlers derive from
"""

import logging
import traceback
import http.client
import time

import tornado.web
import tornado.websocket

from visdom.server.workspace_manager import WorkspaceAuthError

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


class WorkspaceScopedMixin:
    """Shared workspace resolution + state-binding for HTTP and socket handlers.

    ``resolve_workspace`` maps the request's X-Visdom-Workspace header plus its
    credential (the X-API-KEY header on the write path, or the ``session_token``
    cookie on the browser read path) to (workspace_id, role) via the gateway.
    ``bind_workspace`` re-points state/storage/subs/sources at that workspace's
    isolated space so subsequent handler logic operates on that workspace's slice.
    Returns None (no binding) for ordinary keyless/local traffic.

    ``space`` exposes the bound space itself, for the parts of a workspace's slice
    that are not mutable containers and so cannot be written through a re-pointed
    attribute — saved layouts being the one such case.
    """

    workspace_manager = None
    workspace_env_manager = None
    workspace_slug = None
    workspace_role = None
    _space = None

    @property
    def space(self):
        """The bound workspace's space, or the default space when unbound."""
        if self._space is not None:
            return self._space
        if self.workspace_env_manager is not None:
            return self.workspace_env_manager.space(None)
        return None

    def resolve_workspace(self):
        if self.workspace_manager is None:
            return None
        workspace_slug = self.request.headers.get("X-Visdom-Workspace")
        if not workspace_slug:
            return None
        api_key = self.request.headers.get("X-API-KEY")
        session_token = self.get_cookie("session_token")
        if api_key:
            return self.workspace_manager.resolve(api_key, workspace_slug)
        if session_token:
            return self.workspace_manager.resolve_session(session_token, workspace_slug)
        return None

    def bind_workspace(self, workspace_id):
        if self.workspace_env_manager is None:
            return
        space = self.workspace_env_manager.space(workspace_id)
        self._space = space
        self.state = space.state
        self.storage = space.storage
        self.subs = space.subs
        self.sources = space.sources
        self.mark_dirty = space.mark_dirty


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


class BaseHandler(WorkspaceScopedMixin, tornado.web.RequestHandler):
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
            self.workspace_env_manager = getattr(app, "workspace_env_manager", None)

    def render(self, template_name, **kwargs):
        kwargs.setdefault("cloud_context", None)
        return super().render(template_name, **kwargs)

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
        self.workspace_env_manager = None
        super(BaseHandler, self).__init__(*request, **kwargs)

    def prepare(self):
        """Bind a workspace-authenticated request to its workspace's state slice.

        Engages only when the request carries the X-Visdom-Workspace header (set
        by the python client on the write path, or injected by nginx from the
        ``/vis/w/<slug>/`` path on the browser read path), so ordinary visdom
        traffic (health checks, keyless clients) is unaffected. Authenticates via
        either the X-API-KEY header (write path) or the ``session_token`` cookie
        (browser read path), denies writes for the viewer role, and re-points this
        handler's state/storage/subs/sources at that workspace's isolated space so
        all subsequent handler logic operates on that workspace's environments.
        """
        try:
            resolved = self.resolve_workspace()
        except WorkspaceAuthError as exc:
            self.set_status(exc.status_code)
            self.finish({"error": exc.message})
            return
        if resolved is None:
            return

        workspace_id, role = resolved
        self.workspace_slug = self.request.headers.get("X-Visdom-Workspace")
        self.workspace_role = role
        endpoint = self.request.path.rsplit("/", 1)[-1]
        if role == "viewer" and endpoint in WRITE_ENDPOINTS:
            self.set_status(403)
            self.finish({"error": "Your role is read-only in this workspace."})
            return

        self.bind_workspace(workspace_id)

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
