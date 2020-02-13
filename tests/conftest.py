# -*- coding: utf-8 -*-
import json
import os

import pytest
import responses

from decision.pool import MachineTypes
from decision.providers import AWS

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def mock_taskcluster():
    """Mock Taskcluster HTTP services"""

    # Setup mock url for taskcluster services
    os.environ["TASKCLUSTER_ROOT_URL"] = "http://taskcluster.test"

    # Add a basic configuration for the workflow in a secret
    secret = {
        "community_config": {"url": "git@github.com:projectA/repo.git"},
        "fuzzing_config": {"url": "git@github.com:projectB/repo.git"},
        "private_key": "ssh super secret",
    }
    responses.add(
        responses.GET,
        "http://taskcluster.test/api/secrets/v1/secret/mock-fuzzing-tc",
        body=json.dumps({"secret": secret}),
        content_type="application/json",
    )


@pytest.fixture
def mock_aws():
    """Mock Amazon Cloud provider setup"""
    return AWS(os.path.join(FIXTURES_DIR, "community"))


@pytest.fixture
def mock_machines():
    """Mock a static list of machines"""
    path = os.path.join(FIXTURES_DIR, "machines.yml")
    assert os.path.exists(path)
    return MachineTypes.from_file(path)