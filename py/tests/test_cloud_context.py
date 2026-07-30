#!/usr/bin/env python3

# Copyright 2017-present, The Visdom Authors
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for the cloud context payload handed to the index template."""

import json

from visdom.server.handlers.base_handlers import WorkspaceScopedMixin
from visdom.server.handlers.web_handlers import build_cloud_context


def test_no_slug_returns_none():
    assert build_cloud_context(None, None) is None
    assert build_cloud_context("", "admin") is None


def test_slug_returns_json_payload():
    payload = build_cloud_context("team-alpha", "admin")
    assert json.loads(payload) == {"slug": "team-alpha", "role": "admin"}


def test_payload_is_a_string_not_a_dict():
    payload = build_cloud_context("team-alpha", "admin")
    assert isinstance(payload, str)


def test_slug_with_quotes_is_escaped():
    payload = build_cloud_context('a"b</script>', "viewer")
    assert "</script>" not in payload
    assert json.loads(payload)["slug"] == 'a"b</script>'


def test_mixin_defaults_are_none():
    assert WorkspaceScopedMixin.workspace_slug is None
    assert WorkspaceScopedMixin.workspace_role is None
