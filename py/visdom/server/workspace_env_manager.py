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
import time
from collections import Counter

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

    def __init__(
        self, storage, state, subs=None, sources=None, layouts="", save_threshold=0
    ):
        self.storage = storage
        self.state = state
        self.slug = None
        self.subs = subs if subs is not None else {}
        self.sources = sources if sources is not None else {}
        self.layouts = layouts
        self.save_threshold = save_threshold
        self.dirty_envs = Counter()
        self.last_write_at = None

    def mark_dirty(self, eid):
        """Record that ``eid`` has changed in memory and is not yet on disk.

        Tracked per workspace rather than per server: an ``eid`` like ``main``
        exists in every workspace, so a single shared counter would confuse one
        workspace's unsaved work with another's and save the wrong file.
        """
        self.dirty_envs[eid] += 1
        self.last_write_at = time.time()
        if 0 < self.save_threshold <= self.dirty_envs[eid]:
            self.flush([eid])

    def last_active_at(self):
        """When this workspace was last written to, as a unix timestamp.

        Prefers the in-memory mark, which is exact but resets when the instance
        restarts, and falls back to the mtime of the workspace's directory so a
        restart reports a stale time rather than none at all. ``None`` only when
        the workspace has no storage and has not been written to this run.
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

    def flush(self, eids):
        """Persist the named environments, skipping any already saved.

        Only environments the backend reports as written lose their mark, so one
        it declines is retried on the next pass rather than silently dropped. An
        environment deleted since it was marked has nothing left to save and is
        cleared too.
        """
        if self.storage is None:
            # Nothing to write to, so drop the marks rather than accumulate them
            # forever against a space that is memory-only by construction.
            for eid in eids:
                self.dirty_envs.pop(eid, None)
            return []

        pending = [eid for eid in eids if self.dirty_envs.get(eid)]
        if not pending:
            return []
        written = self.storage.save_envs(self.state, pending)
        saved = set(written)
        for eid in pending:
            if eid in saved or eid not in self.state:
                del self.dirty_envs[eid]
        return written

    def flush_dirty(self):
        """Persist every environment in this workspace changed since the last save."""
        return self.flush(list(self.dirty_envs))

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
    def __init__(self, base_env_path, default_space, eager=False, save_threshold=0):
        self.base_env_path = base_env_path
        self.eager = eager
        self.save_threshold = save_threshold
        self._spaces = {None: default_space}
        self._lock = threading.Lock()

    def space(self, workspace_id, slug=None):
        """Return the WorkspaceSpace for a workspace id, creating it on first use.
        ``None`` returns the default (global) space."""
        if workspace_id is None:
            return self._spaces[None]
        with self._lock:
            space = self._spaces.get(workspace_id)
            if space is None:
                space = self._create_space(workspace_id)
                self._spaces[workspace_id] = space
            if slug and space.slug != slug:
                space.slug = slug
            return space

    def spaces(self):
        """Every space built so far, the default one included.

        Returned as a list rather than a live view so the autosave timer can walk
        it without holding the lock while it writes to disk.
        """
        with self._lock:
            return list(self._spaces.values())

    def workspace_spaces(self):
        """Every space that belongs to a workspace, as (id, space) pairs.

        Excludes the default space, which no proxy ever routes to.
        """
        with self._lock:
            return [
                (wid, space) for wid, space in self._spaces.items() if wid is not None
            ]

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
            save_threshold=self.save_threshold,
        )
