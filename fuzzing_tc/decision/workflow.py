# -*- coding: utf-8 -*-

# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.

import atexit
import glob
import logging
import os
import shutil
import tempfile

import yaml
from tcadmin.appconfig import AppConfig

from ..common.pool import MachineTypes
from ..common.workflow import Workflow as CommonWorkflow
from . import HOOK_PREFIX
from . import WORKER_POOL_PREFIX
from .pool import PoolConfiguration
from .providers import AWS
from .providers import GCP

logger = logging.getLogger()


class Workflow(CommonWorkflow):
    """Fuzzing decision task workflow"""

    def __init__(self):
        super().__init__()

        self.fuzzing_config_dir = None
        self.community_config_dir = None

        # Automatic cleanup at end of execution
        atexit.register(self.cleanup)

    def configure(self, *args, **kwds):
        config = super().configure(*args, **kwds)
        if config is None:
            raise Exception("Specify local_path XOR secret")
        return config

    @staticmethod
    async def tc_admin_boot(resources):
        """Setup the workflow to be usable by tc-admin"""
        appconfig = AppConfig.current()

        # Configure workflow using tc-admin options
        workflow = Workflow()
        config = workflow.configure(
            local_path=appconfig.options.get("fuzzing_configuration"),
            secret=appconfig.options.get("fuzzing_taskcluster_secret"),
            fuzzing_git_repository=appconfig.options.get("fuzzing_git_repository"),
            fuzzing_git_revision=appconfig.options.get("fuzzing_git_revision"),
        )

        # Retrieve remote repositories
        workflow.clone(config)

        # Then generate all our Taskcluster resources
        workflow.generate(resources)

    def clone(self, config):
        """Clone remote repositories according to current setup"""
        super().clone(config)

        # Clone fuzzing & community configuration repos
        self.fuzzing_config_dir = self.git_clone(**config["fuzzing_config"])
        self.community_config_dir = self.git_clone(**config["community_config"])

    def generate(self, resources):

        # Setup resources manager to track only fuzzing instances
        for pattern in self.build_resources_patterns():
            resources.manage(pattern)

        # Load the cloud configuration from community config
        clouds = {
            "aws": AWS(self.community_config_dir),
            "gcp": GCP(self.community_config_dir),
        }

        # Load the machine types
        machines = MachineTypes.from_file(
            os.path.join(self.fuzzing_config_dir, "machines.yml")
        )

        # Browse the files in the repo
        fuzzing_glob = os.path.join(self.fuzzing_config_dir, "pool*.yml")
        for config_file in glob.glob(fuzzing_glob):

            pool_config = PoolConfiguration.from_file(config_file)

            resources.update(pool_config.build_resources(clouds, machines))

    def build_resources_patterns(self):
        """Build regex patterns to manage our resources"""

        # Load existing workerpools from community config
        path = os.path.join(self.community_config_dir, "config/projects/fuzzing.yml")
        assert os.path.exists(path), f"Missing fuzzing community config in {path}"
        community = yaml.safe_load(open(path))
        assert "fuzzing" in community, "Missing fuzzing main key in community config"

        def _suffix(data, key):
            existing = data.get(key, {}).keys()
            if not existing:
                # Manage every resource possible
                return ".*"

            # Exclude existing resources from managed resources
            logger.info(
                "Found existing {} in community config: {}".format(
                    key, ", ".join(existing)
                )
            )
            return "(?!({})$)".format("|".join(existing))

        hook_suffix = _suffix(community["fuzzing"], "hooks")
        pool_suffix = _suffix(community["fuzzing"], "workerPools")

        return [
            rf"Hook={HOOK_PREFIX}/{hook_suffix}",
            rf"WorkerPool={WORKER_POOL_PREFIX}/{pool_suffix}",
            # We only manage all the hooks roles
            rf"Role=hook-id:{HOOK_PREFIX}/.*",
        ]

    def build_tasks(self, pool_name, task_id):
        path = os.path.join(self.fuzzing_config_dir, f"{pool_name}.yml")
        assert os.path.exists(path), f"Missing pool {pool_name}"

        # Build tasks needed for a specific pool
        pool_config = PoolConfiguration.from_file(path)
        tasks = pool_config.build_tasks(task_id)

        # Create all the tasks on taskcluster
        queue = self.taskcluster.get_service("queue")
        for task_id, task in tasks:
            logger.info(f"Creating task {task['metadata']['name']} as {task_id}")
            queue.createTask(task_id, task)

    def cleanup(self):
        """Cleanup temporary folders at end of execution"""
        for folder in (self.community_config_dir, self.fuzzing_config_dir):
            if folder is None or not os.path.exists(folder):
                continue
            if folder.startswith(tempfile.gettempdir()):
                logger.info(f"Removing tempdir clone {folder}")
                shutil.rmtree(folder)
