# -*- coding: utf-8 -*-

# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import logging
import os

from .workflow import Workflow


def main():
    parser = argparse.ArgumentParser("Fuzzing decision task")
    parser.add_argument(
        "pool_name", type=str, help="The target fuzzing pool to create tasks for"
    )
    parser.add_argument(
        "--taskcluster-secret",
        type=str,
        help="Taskcluster Secret path for configuration",
        default=os.environ.get("TASKCLUSTER_SECRET"),
    )
    parser.add_argument(
        "--configuration",
        type=str,
        help="Local configuration file replacing Taskcluster secrets for fuzzing",
    )
    parser.add_argument(
        "--task-id",
        type=str,
        help="Taskcluster decision task creating new fuzzing tasks",
        default=os.environ.get("TASK_ID"),
    )
    parser.add_argument(
        "--git-repository",
        help="A git repository containing the Fuzzing configuration",
        default=os.environ.get("FUZZING_GIT_REPOSITORY"),
    )
    parser.add_argument(
        "--git-revision",
        help="A git revision for the fuzzing git repository",
        default=os.environ.get("FUZZING_GIT_REVISION"),
    )
    args = parser.parse_args()

    # We need both task & task group information
    if not args.task_id:
        raise Exception("Missing decision task id")

    # Setup logger
    logging.basicConfig(level=logging.INFO)

    # Configure workflow using the secret or local configuration
    workflow = Workflow()
    config = workflow.configure(
        local_path=args.configuration,
        secret=args.taskcluster_secret,
        fuzzing_git_repository=args.git_repository,
        fuzzing_git_revision=args.git_revision,
    )

    # Retrieve remote repositories
    workflow.clone(config)

    # Build all task definitions for that pool
    workflow.build_tasks(args.pool_name, args.task_id, config)
