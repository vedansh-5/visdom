#!/usr/bin/env python3

# Copyright 2017-present, The Visdom Authors
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Owns one isolated slice of visdom state per workspace, so a multi-tenant
deployment keeps each workspace's environments, disk storage, and realtime
subscribers separate. Handlers do not talk to this manager directly; instead,
once a request's workspace is resolved, they re-point ``self.state`` /
``self.storage`` / ``self.subs`` / ``self.sources`` at the matching space, so the
existing handler code operates on that workspace's slice unchanged.

Workspace ``None`` maps to the default (global) space, so plain single-tenant /
local visdom behaves exactly as before.
"""

import os
import threading

from visdom.data_model.json_store import JSONStore
from visdom.utils.server_utils import LazyEnvData
from visdom.utils.shared_utils import ensure_dir_exists


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


class WorkspaceSpace:
    """A single workspace's isolated env state, disk storage, socket peers, and
    saved view layouts."""

    def __init__(self, storage, state, subs=None, sources=None, layouts=""):
        self.storage = storage
        self.state = state
        self.subs = subs if subs is not None else {}
        self.sources = sources if sources is not None else {}
        self.layouts = layouts

    def save_layouts(self, layouts):
        """Replace this workspace's saved layouts and persist them.

        Layouts are a string rather than a mutable container, so handlers cannot
        write through a re-pointed attribute the way they do for ``state`` and
        ``subs``; they go through the space instead.
        """
        self.layouts = layouts
        if self.storage is not None:
            self.storage.save_layouts(layouts)


class WorkspaceEnvManager:
    def __init__(self, base_env_path, default_space, eager=False):
        self.base_env_path = base_env_path
        self.eager = eager
        self._spaces = {None: default_space}
        self._lock = threading.Lock()

    def space(self, workspace_id):
        """Return the WorkspaceSpace for a workspace id, creating it on first use.
        ``None`` returns the default (global) space."""
        if workspace_id is None:
            return self._spaces[None]
        with self._lock:
            space = self._spaces.get(workspace_id)
            if space is None:
                space = self._create_space(workspace_id)
                self._spaces[workspace_id] = space
            return space

    def _create_space(self, workspace_id):
        if self.base_env_path is None:
            return WorkspaceSpace(None, build_state(None))
        ws_path = os.path.join(self.base_env_path, "workspaces", str(workspace_id))
        ensure_dir_exists(ws_path)
        storage = JSONStore(ws_path)
        return WorkspaceSpace(
            storage,
            build_state(storage, eager=self.eager),
            layouts=storage.load_layouts(),
        )
