#!/usr/bin/env python3

# Copyright 2017-present, The Visdom Authors
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Owns one isolated slice of server state per workspace, so a multi-tenant
deployment keeps each workspace's environments, disk storage, and realtime
subscribers separate.

Each workspace gets its own ``ServerState``, the same facade a single-tenant
server has, differing only in which containers and storage it points at. Once a
request's workspace is resolved, ``bind_workspace`` swaps the handler's
``server_state`` for that workspace's one, so every accessor the handler already
reads through resolves inside that workspace without the handler knowing.

Workspace ``None`` maps to the server's own state, so plain single-tenant or
local visdom behaves exactly as before.
"""

import os
import threading
import time
from collections import Counter

from visdom.data_model.json_store import JSONStore
from visdom.server.server_state import ServerState
from visdom.utils.server_utils import LazyEnvData
from visdom.utils.shared_utils import ensure_dir_exists

# How long a disk scan is reused before the directories are walked again.
DISK_SCAN_TTL = 30.0

# Configuration a workspace inherits from the server it runs inside. Only the
# containers and the storage differ per workspace; how the server behaves does
# not change with which tenant is asking.
_INHERITED_SETTINGS = (
    "port",
    "login_enabled",
    "readonly",
    "user_credential",
    "base_url",
    "wrap_socket",
    "user_settings",
    "max_text_lines",
    "max_old_content",
    "max_image_history",
    "max_plot_history",
    "save_interval",
    "save_threshold",
)


def build_state(storage, eager=False, ensure_main=True):
    """Build an in-memory env-state dict from a storage backend, mirroring
    Application.load_state but reusable for any workspace's storage."""
    state = {}
    if storage is None:
        return {"main": {"jsons": {}, "reload": {}}} if ensure_main else {}
    for eid in storage.list_envs():
        if eager:
            env_data = storage.load_env(eid)
            state[eid] = {
                "jsons": env_data.get("jsons", {}),
                "reload": env_data.get("reload", {}),
            }
        else:
            state[eid] = LazyEnvData(storage, eid)
    if ensure_main and "main" not in state:
        state["main"] = {"jsons": {}, "reload": {}}
        storage.save_env("main", state["main"])
    return state


class WorkspaceState(ServerState):
    """One workspace's state, and what an operator can ask about it.

    A ``ServerState`` in every respect the handlers care about. The additions
    are the slug, which the proxy hashes on and this process would otherwise
    never learn, and enough activity to answer whether anyone is using the
    workspace at all.
    """

    def __init__(self, *, slug=None, **kwargs):
        super().__init__(**kwargs)
        self.slug = slug
        self.last_write_at = None

    def mark_dirty(self, eid):
        """Persist as usual, and remember that this workspace was written to."""
        self.last_write_at = time.time()
        return super().mark_dirty(eid)

    def flush_envs(self, eids):
        """Persist as usual, but never hold marks a save can never clear.

        ``flush_envs`` keeps the mark on anything the backend did not report as
        written, so it is retried rather than silently dropped. With no path to
        write to nothing is ever reported, so the marks would accumulate for the
        life of the process against a workspace that is memory-only by
        construction.
        """
        if self.env_path is None:
            for eid in list(eids):
                self.dirty_envs.pop(eid, None)
            return []
        return super().flush_envs(eids)

    def last_active_at(self):
        """When this workspace was last written to, as a unix timestamp.

        Prefers the in-memory mark, which is exact but resets when the instance
        restarts, and falls back to the mtime of the workspace's directory so a
        restart reports a stale time rather than none at all.
        """
        if self.last_write_at is not None:
            return self.last_write_at
        env_path = getattr(self.storage, "env_path", None)
        if not env_path:
            return None
        try:
            return os.path.getmtime(env_path)
        except OSError:
            return None

    def activity(self):
        """Live socket counts for this workspace, plus when it was last written.

        Readers and writers are counted separately because they mean different
        things to an operator: a workspace with viewers but no sources is being
        watched, one with sources and no viewers is being fed by a training run
        nobody is looking at.
        """
        return {
            "slug": self.slug,
            "viewers": len(self.subs),
            "writers": len(self.sources),
            "last_active_at": self.last_active_at(),
        }


class WorkspaceEnvManager:
    def __init__(self, base_env_path, default_state, eager=False):
        self.base_env_path = base_env_path
        self.default_state = default_state
        self.eager = eager
        self._states = {None: default_state}
        self._lock = threading.Lock()
        self._disk_lock = threading.Lock()
        self._disk_cache = {}
        self._disk_scanned_at = None

    def space(self, workspace_id, slug=None):
        """Return the state for a workspace id, creating it on first use.
        ``None`` returns the server's own (default) state."""
        if workspace_id is None:
            return self._states[None]
        with self._lock:
            state = self._states.get(workspace_id)
            if state is None:
                state = self._create_space(workspace_id, slug)
                self._states[workspace_id] = state
            if slug and state.slug != slug:
                state.slug = slug
            return state

    def spaces(self):
        """Every state built so far, the server's own included.

        Returned as a list rather than a live view so the autosave timer can walk
        it without holding the lock while it writes to disk.
        """
        with self._lock:
            return list(self._states.values())

    def workspace_spaces(self):
        """Every state that belongs to a workspace, as (id, state) pairs.

        Excludes the default one, which no proxy ever routes to.
        """
        with self._lock:
            return [
                (wid, state) for wid, state in self._states.items() if wid is not None
            ]

    def stored_workspaces(self):
        """Every workspace with a directory on disk, with its size and last write.

        ``workspace_spaces`` only knows the workspaces something has touched since
        this process started, so on its own it reports a freshly restarted server
        as having no workspaces at all. Disk is the durable record, and it is what
        makes a dormant workspace distinguishable from one that has merely not
        been visited yet.

        Cached, because this walks every workspace's files and neither a size nor
        a last write needs to be accurate to the second.
        """
        if self.base_env_path is None:
            return {}

        now = time.monotonic()
        with self._disk_lock:
            if (
                self._disk_scanned_at is not None
                and now - self._disk_scanned_at < DISK_SCAN_TTL
            ):
                return self._disk_cache

        root = os.path.join(self.base_env_path, "workspaces")
        found = {}
        try:
            entries = list(os.scandir(root))
        except OSError:
            entries = []

        for entry in entries:
            if not entry.is_dir():
                continue
            total = 0
            newest = 0.0
            try:
                for item in os.scandir(entry.path):
                    try:
                        stat = item.stat()
                    except OSError:
                        continue
                    total += stat.st_size
                    newest = max(newest, stat.st_mtime)
            except OSError:
                continue
            found[entry.name] = {"bytes": total, "last_active_at": newest or None}

        with self._disk_lock:
            self._disk_cache = found
            self._disk_scanned_at = now
        return found

    def _create_space(self, workspace_id, slug=None):
        """Build a workspace's state, inheriting the server's configuration.

        The containers and the storage are new; everything about how the server
        behaves is copied, so a workspace cannot end up with different limits or
        a different save interval from the server it runs in.
        """
        if self.base_env_path is None:
            # A store over no path, not the absence of a store: the application
            # does the same for its own state, and the persistence calls then
            # no-op instead of having to be guarded at every call site.
            storage = JSONStore(None)
            env_path = None
        else:
            env_path = os.path.join(self.base_env_path, "workspaces", str(workspace_id))
            ensure_dir_exists(env_path)
            storage = JSONStore(env_path)

        settings = {
            name: getattr(self.default_state, name) for name in _INHERITED_SETTINGS
        }
        state = WorkspaceState(
            slug=slug,
            state=build_state(storage, eager=self.eager),
            subs={},
            sources={},
            storage=storage,
            env_path=env_path,
            **settings,
        )
        # Built by the application against its own state, and safe to share:
        # the queue carries the environments it was asked about, not a slice of
        # any one workspace's data.
        state.live_updates = self.default_state.live_updates
        # A handler bound to this workspace has to be able to rebind, so the
        # managers travel with the state rather than only with the server's.
        state.workspace_manager = getattr(self.default_state, "workspace_manager", None)
        state.workspace_env_manager = self
        state.dirty_envs = Counter()
        return state
