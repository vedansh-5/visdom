#!/usr/bin/env python3

# Copyright 2017-present, The Visdom Authors
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Main application class that pulls handlers together and maintains
all of the required state about the currently running server.
"""

import logging
import os
import platform
import time

import tornado.ioloop
import tornado.web  # noqa E402: gotta install ioloop first

from visdom.utils.shared_utils import warn_once, ensure_dir_exists, get_visdom_path
from visdom.utils.server_utils import LazyEnvData
from visdom.data_model.json_store import JSONStore
from visdom.server.workspace_manager import WorkspaceManager
from visdom.server.workspace_env_manager import WorkspaceEnvManager
from visdom.server.ownership import (
    OwnershipMonitor,
    WS_TRY_AGAIN_LATER,
    local_address,
)
from visdom.server.handlers.socket_handlers import (
    SocketHandler,
    SocketWrap,
    VisSocketHandler,
    VisSocketWrap,
)
from visdom.server.handlers.experiments_handler import (
    ExperimentHparamsHandler,
    ExperimentHparamsUpdateHandler,
    make_live_queue,
)
from visdom.server.handlers.web_handlers import (
    ActivityHandler,
    CloseHandler,
    CompareHandler,
    DataHandler,
    DeleteEnvHandler,
    EnvHandler,
    EnvStateHandler,
    ErrorHandler,
    ExistsHandler,
    ExperimentCompareHandler,
    ExperimentLogHandler,
    ExperimentSearchHandler,
    ExperimentSuggestHandler,
    ForkEnvHandler,
    HealthHandler,
    IndexHandler,
    PostHandler,
    SaveHandler,
    TagsHandler,
    UpdateHandler,
    UploadEnvHandler,
    UserSettingsHandler,
)
from visdom.server.server_state import ServerState
from visdom.server.defaults import (
    DEFAULT_BASE_URL,
    DEFAULT_ENV_PATH,
    DEFAULT_HOSTNAME,
    DEFAULT_MAX_IMAGE_HISTORY,
    DEFAULT_MAX_OLD_CONTENT,
    DEFAULT_MAX_PLOT_HISTORY,
    DEFAULT_MAX_TEXT_LINES,
    DEFAULT_PORT,
    DEFAULT_SAVE_INTERVAL,
    DEFAULT_SAVE_THRESHOLD,
)

tornado_settings = {
    "autoescape": None,
    "debug": "/dbg/" in __file__,
    "static_path": get_visdom_path("static"),
    "template_path": get_visdom_path("static"),
    "compiled_template_cache": False,
}


class Application(tornado.web.Application):
    def __init__(
        self,
        port=DEFAULT_PORT,
        base_url="",
        env_path=DEFAULT_ENV_PATH,
        readonly=False,
        user_credential=None,
        use_frontend_client_polling=False,
        eager_data_loading=False,
        save_interval=DEFAULT_SAVE_INTERVAL,
        save_threshold=DEFAULT_SAVE_THRESHOLD,
    ):
        self.eager_data_loading = eager_data_loading
        self.max_image_history = DEFAULT_MAX_IMAGE_HISTORY
        self.max_old_content = DEFAULT_MAX_OLD_CONTENT
        self.max_plot_history = DEFAULT_MAX_PLOT_HISTORY
        self.max_text_lines = DEFAULT_MAX_TEXT_LINES
        self.env_path = env_path
        self.storage = JSONStore(env_path)
        self.workspace_manager = WorkspaceManager()
        self.state = self.load_state()
        self.user_settings = self.load_user_settings()
        self.ownership_monitor = None
        self.subs = {}
        self.sources = {}
        self.port = port
        self.base_url = base_url
        self.readonly = readonly
        self.user_credential = user_credential
        self.login_enabled = False
        self.last_access = time.time()
        self.wrap_socket = use_frontend_client_polling

        if user_credential:
            self.login_enabled = True
            with open(DEFAULT_ENV_PATH + "COOKIE_SECRET", "r") as fn:
                tornado_settings["cookie_secret"] = fn.read()

        self.server_state = ServerState(
            state=self.state,
            subs=self.subs,
            sources=self.sources,
            storage=self.storage,
            env_path=self.env_path,
            port=self.port,
            login_enabled=self.login_enabled,
            readonly=self.readonly,
            user_credential=self.user_credential,
            base_url=self.base_url,
            wrap_socket=self.wrap_socket,
            user_settings=self.user_settings,
            max_text_lines=self.max_text_lines,
            max_old_content=self.max_old_content,
            max_image_history=self.max_image_history,
            max_plot_history=self.max_plot_history,
            save_interval=save_interval,
            save_threshold=save_threshold,
        )
        self.server_state.live_updates = make_live_queue(self.server_state)

        # Built after the server's own state, because every workspace inherits
        # its configuration and workspace None simply is it.
        self.workspace_env_manager = WorkspaceEnvManager(
            base_env_path=env_path,
            default_state=self.server_state,
            eager=self.eager_data_loading,
        )
        # Handlers reach both through whichever state is bound, so a workspace's
        # state can rebind just as the server's can.
        self.server_state.workspace_manager = self.workspace_manager
        self.server_state.workspace_env_manager = self.workspace_env_manager

        tornado_settings["static_url_prefix"] = self.base_url + "/static/"
        # A traceback and the raw request are debugging aids, not something to
        # hand to whoever provoked the error. `debug` was forced on for every
        # server, which put both on the 500 page -- and, being tornado's debug
        # flag, also turned on autoreload. Follow the operator's logging level
        # instead, and keep the two concerns separate.
        tornado_settings["show_error_details"] = logging.getLogger().isEnabledFor(
            logging.DEBUG
        )
        experiments_url = "%s/experiments" % self.base_url
        server_state_args = {"server_state": self.server_state}
        handlers = [
            (r"%s/events" % self.base_url, PostHandler, server_state_args),
            (r"%s/update" % self.base_url, UpdateHandler, server_state_args),
            (r"%s/close" % self.base_url, CloseHandler, server_state_args),
            (r"%s/socket" % self.base_url, SocketHandler, server_state_args),
            (r"%s/socket_wrap" % self.base_url, SocketWrap, server_state_args),
            (r"%s/vis_socket" % self.base_url, VisSocketHandler, server_state_args),
            (
                r"%s/vis_socket_wrap" % self.base_url,
                VisSocketWrap,
                server_state_args,
            ),
            (r"%s/env/(.*)" % self.base_url, EnvHandler, server_state_args),
            (r"%s/compare/(.*)" % self.base_url, CompareHandler, server_state_args),
            (r"%s/save" % self.base_url, SaveHandler, server_state_args),
            (r"%s/upload_env" % self.base_url, UploadEnvHandler, server_state_args),
            (r"%s/error/(.*)" % self.base_url, ErrorHandler, server_state_args),
            (r"%s/win_exists" % self.base_url, ExistsHandler, server_state_args),
            (r"%s/win_data" % self.base_url, DataHandler, server_state_args),
            (r"%s/delete_env" % self.base_url, DeleteEnvHandler, server_state_args),
            (r"%s/env_state" % self.base_url, EnvStateHandler, server_state_args),
            (r"%s/fork_env" % self.base_url, ForkEnvHandler, server_state_args),
            (r"%s/log" % experiments_url, ExperimentLogHandler, server_state_args),
            (
                r"%s/search" % experiments_url,
                ExperimentSearchHandler,
                server_state_args,
            ),
            (
                r"%s/compare" % experiments_url,
                ExperimentCompareHandler,
                server_state_args,
            ),
            (
                r"%s/suggest" % experiments_url,
                ExperimentSuggestHandler,
                server_state_args,
            ),
            (
                r"%s/hparams" % experiments_url,
                ExperimentHparamsHandler,
                server_state_args,
            ),
            (
                r"%s/hparams/update" % experiments_url,
                ExperimentHparamsUpdateHandler,
                server_state_args,
            ),
            (r"%s/tags" % experiments_url, TagsHandler, server_state_args),
            (
                r"%s/user/(.*)" % self.base_url,
                UserSettingsHandler,
                server_state_args,
            ),
            (r"%s/health" % self.base_url, HealthHandler),
            (r"%s/_activity" % self.base_url, ActivityHandler, {"app": self}),
            (r"%s(.*)" % self.base_url, IndexHandler, server_state_args),
        ]
        super(Application, self).__init__(handlers, **tornado_settings)

    def get_last_access(self):
        if len(self.subs) > 0 or len(self.sources) > 0:
            # update the last access time to now, as someone
            # is currently connected to the server
            self.last_access = time.time()
        return self.last_access

    @property
    def layouts(self):
        """Compatibility view of layouts owned by ``ServerState``."""
        return self.server_state.get_layouts()

    @layouts.setter
    def layouts(self, layouts):
        self.server_state.set_layouts(layouts)

    @property
    def save_interval(self):
        """Compatibility view of the ServerState autosave interval."""
        return self.server_state.save_interval

    @property
    def save_threshold(self):
        """Compatibility view of the ServerState update threshold."""
        return self.server_state.save_threshold

    @property
    def dirty_envs(self):
        """Compatibility view of environments pending persistence."""
        return self.server_state.dirty_envs

    @property
    def autosave(self):
        """Compatibility view of the ServerState autosave timer."""
        return self.server_state.autosave

    @property
    def storage_executor(self):
        """Compatibility view of the ServerState storage worker."""
        return self.server_state.storage_executor

    @property
    def saving_envs(self):
        """Compatibility view of environments with a write in flight."""
        return self.server_state.saving_envs

    @property
    def live_updates(self):
        """Compatibility view of the ServerState hparams refresh queue."""
        return self.server_state.live_updates

    def mark_dirty(self, eid):
        """Compatibility wrapper for the ServerState dirty tracker."""
        return self.server_state.mark_dirty(eid)

    def flush_envs(self, eids):
        """Compatibility wrapper for selective ServerState persistence."""
        return self.server_state.flush_envs(eids)

    def flush_dirty(self):
        """Persist changed environments in every workspace, not just this one.

        ``ServerState.flush_dirty`` only knows its own state, which on a
        multi-tenant server is the state no tenant is using. A workspace nobody
        happens to be looking at when the timer fires still has to be written.

        One future comes back for all of them, so a caller waits the same way
        it would for a single state. Every space shares the one storage worker
        and it runs its queue in order, so a task submitted after the writes
        finds them all finished and simply collects what each reported.
        """
        futures = [
            future
            for future in (
                state.flush_dirty() for state in self.workspace_env_manager.spaces()
            )
            if future is not None
        ]
        if not futures:
            return None
        if len(futures) == 1:
            return futures[0]
        return self.server_state.storage_executor.submit(
            lambda: [eid for future in futures for eid in future.result()]
        )

    def start_autosave(self):
        """Save changed environments on a timer, across every workspace.

        Started here rather than on ``ServerState`` so the callback is the
        application's ``flush_dirty``, which walks all of them.
        """
        if self.server_state.autosave is None and self.server_state.save_interval > 0:
            self.server_state.autosave = tornado.ioloop.PeriodicCallback(
                self.flush_dirty, self.server_state.save_interval * 1000
            )
            self.server_state.autosave.start()
        return self.server_state.autosave

    def start_ownership_monitor(self):
        """Begin checking whether this instance still owns the workspaces it holds
        sockets for. A no-op unless VISDOM_PROXY_URL names the proxy to ask."""
        proxy_url = os.environ.get("VISDOM_PROXY_URL")
        if not proxy_url or self.ownership_monitor is not None:
            return self.ownership_monitor
        try:
            interval = int(os.environ.get("VISDOM_OWNERSHIP_INTERVAL", "30"))
        except ValueError:
            interval = 30
        self.ownership_monitor = OwnershipMonitor(
            self,
            proxy_url,
            "%s/_shard" % self.base_url,
            interval,
            local_address(self.port),
        )
        self.ownership_monitor.start()
        logging.info(
            "ownership monitor on, asking %s every %ds as %s",
            proxy_url,
            interval,
            self.ownership_monitor.self_address,
        )
        return self.ownership_monitor

    def drain_sockets(self, reason="server shutting down, reconnect"):
        """Close every live viewer socket with a retryable code."""
        spaces = [self.workspace_env_manager.space(None)]
        spaces += [
            space for _wid, space in self.workspace_env_manager.workspace_spaces()
        ]
        closed = 0
        for space in spaces:
            for sub in list(space.subs.values()):
                try:
                    sub.close(WS_TRY_AGAIN_LATER, reason)
                    closed += 1
                except Exception as exc:
                    logging.debug("could not close a socket while draining: %s", exc)
        if closed:
            logging.info("drained %d socket(s) before shutdown", closed)
        return closed

    def stop_autosave(self):
        """Stop the timer, so no further tick can queue a write."""
        if self.server_state.autosave is not None:
            self.server_state.autosave.stop()
            self.server_state.autosave = None

    def shutdown_storage(self):
        """Drain and save every workspace, not just this server's own state.

        ``ServerState.shutdown_storage`` saves the one state it belongs to,
        which on a multi-tenant server holds nothing a tenant wrote. The worker
        is shared, so draining it once covers every workspace's queued writes;
        the final save is then per space, or a workspace keeps only whatever
        happened to reach disk before the drain.
        """
        self.stop_autosave()
        self.server_state.storage_executor.shutdown(wait=True)
        for state in self.workspace_env_manager.spaces():
            if state.env_path is not None:
                state.storage.save_all(state.state)
            state.dirty_envs.clear()
            state.saving_envs.clear()

    def save_layouts(self):
        """Compatibility wrapper for callers that still use ``Application``."""
        self.server_state.save_layouts()

    def load_layouts(self):
        """Read layouts through the configured ``DataStore`` backend."""
        if self.env_path is None:
            warn_once(
                "Saving and loading to disk has no effect when running with "
                "env_path=None.",
                RuntimeWarning,
            )
            return ""
        return self.storage.load_layouts()

    def load_state(self):
        state = {}
        env_path = self.env_path
        if env_path is None:
            warn_once(
                "Saving and loading to disk has no effect when running with "
                "env_path=None.",
                RuntimeWarning,
            )
            return {"main": {"jsons": {}, "reload": {}}}
        ensure_dir_exists(env_path)
        for eid in self.storage.list_envs():
            if self.eager_data_loading:
                env_data = self.storage.load_env(eid)
                if not isinstance(env_data, dict):
                    env_data = {}

                if "jsons" not in env_data or "reload" not in env_data:
                    logging.warning(
                        "Environment '%s' is malformed or missing expected fields.",
                        eid,
                    )

                # Copy the whole env rather than picking out jsons/reload, so
                # keys the server does not read itself (such as the experiment
                # metadata blob) survive the load and are still there when the
                # env is saved back. LazyEnvData keeps them for the lazy path.
                state[eid] = dict(env_data)
                state[eid].setdefault("jsons", {})
                state[eid].setdefault("reload", {})
            else:
                state[eid] = LazyEnvData(self.storage, eid)

        if "main" not in state:
            state["main"] = {"jsons": {}, "reload": {}}
            self.storage.save_env("main", state["main"])

        return state

    def load_user_settings(self):
        settings = {}

        """Determines & uses the platform-specific root directory for user configurations."""
        if platform.system() == "Windows":
            base_dir = os.getenv("APPDATA")

            if not base_dir:
                fallback = os.path.expanduser("~")

                if not fallback or fallback == "~":
                    raise RuntimeError(
                        "Could not determine base directory for user configurations."
                    )
                logging.warning("APPDATA not set, falling back to base directory")
                base_dir = fallback

        elif platform.system() == "Darwin":  # osx
            base_dir = os.path.expanduser("~/Library/Preferences")
        else:
            base_dir = os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        config_dir = os.path.join(base_dir, "visdom")

        # initialize user style
        user_css = ""
        home_style_path = os.path.join(config_dir, "style.css")
        if os.path.exists(home_style_path):
            with open(home_style_path, "r") as f:
                user_css += "\n" + f.read()
        if self.env_path is not None:
            project_style_path = os.path.join(self.env_path, "style.css")
            if os.path.exists(project_style_path):
                with open(project_style_path, "r") as f:
                    user_css += "\n" + f.read()

        settings["config_dir"] = config_dir
        settings["user_css"] = user_css

        return settings
