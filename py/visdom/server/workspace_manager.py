#!/usr/bin/env python3

# Copyright 2017-present, The Visdom Authors
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Resolves a caller plus workspace slug to a workspace id and the caller's role by
calling the visdom-cloud gateway, with a short-lived in-process cache so repeated
requests from the same caller do not hit the gateway each time. Two callers: the
write path (programmatic API key) and the browser read path (session token).
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
        """Write path: resolve an API key + slug to (workspace_id, role)."""
        return self._resolve_cached(
            ("key", api_key, workspace_slug),
            "resolve",
            {"X-API-KEY": api_key},
            workspace_slug,
        )

    def resolve_session(self, session_token, workspace_slug):
        """Browser read path: resolve a session token + slug to (workspace_id, role)."""
        return self._resolve_cached(
            ("session", session_token, workspace_slug),
            "resolve-session",
            {"Authorization": f"Bearer {session_token}"},
            workspace_slug,
        )

    def _resolve_cached(self, cache_key, endpoint, headers, workspace_slug):
        now = time.time()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached[2] > now:
                return cached[0], cached[1]

        workspace_id, role = self._fetch(endpoint, headers, workspace_slug)

        with self._lock:
            self._cache[cache_key] = (workspace_id, role, now + self.cache_ttl)
        return workspace_id, role

    def _fetch(self, endpoint, headers, workspace_slug):
        url = f"{self.gateway_url}/api/v1/visdom/{endpoint}"
        try:
            resp = requests.post(
                url,
                headers=headers,
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
