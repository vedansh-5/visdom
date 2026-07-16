#!/usr/bin/env python3

# Copyright 2017-present, The Visdom Authors
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Resolves a programmatic API key plus workspace slug to a workspace id and the
caller's role by calling the visdom-cloud gateway, with a short-lived in-process
cache so repeated writes from the same client do not hit the gateway each time.
"""

import os
import threading
import time

import requests


class WorkspaceAuthError(Exception):
    """Raised when the gateway declines or cannot service a resolve request."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class WorkspaceManager:
    def __init__(self, gateway_url=None, cache_ttl=45.0, timeout=5.0):
        self.gateway_url = (
            gateway_url or os.environ.get("VISDOM_GATEWAY_URL", "http://localhost:8085")
        ).rstrip("/")
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self._cache = {}
        self._lock = threading.Lock()

    def resolve(self, api_key, workspace_slug):
        """Return (workspace_id, role), using the cache when a fresh entry exists."""
        cache_key = (api_key, workspace_slug)
        now = time.time()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached[2] > now:
                return cached[0], cached[1]

        workspace_id, role = self._fetch(api_key, workspace_slug)

        with self._lock:
            self._cache[cache_key] = (workspace_id, role, now + self.cache_ttl)
        return workspace_id, role

    def _fetch(self, api_key, workspace_slug):
        url = f"{self.gateway_url}/api/v1/visdom/resolve"
        try:
            resp = requests.post(
                url,
                headers={"X-API-KEY": api_key},
                json={"workspace_slug": workspace_slug},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise WorkspaceAuthError(503, f"workspace resolve unavailable: {exc}")

        if resp.status_code == 200:
            data = resp.json()
            return data["workspace_id"], data["role"]

        detail = "workspace resolution denied"
        try:
            detail = resp.json().get("detail", detail)
        except ValueError:
            pass
        raise WorkspaceAuthError(resp.status_code, detail)
