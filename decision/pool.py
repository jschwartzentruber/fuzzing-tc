# coding: utf-8
import os
import re
from datetime import datetime
from datetime import timedelta

import yaml
from taskcluster.utils import fromNow
from taskcluster.utils import slugId
from taskcluster.utils import stringDate
from tcadmin.resources import Hook
from tcadmin.resources import Role
from tcadmin.resources import WorkerPool

from decision import DECISION_TASK_SECRET
from decision import HOOK_PREFIX
from decision import OWNER_EMAIL
from decision import PROVIDER_IDS
from decision import PROVISIONER_ID
from decision import SCHEDULER_ID
from decision import WORKER_POOL_PREFIX

DESCRIPTION = """*DO NOT EDIT* - This resource is configured automatically.

Fuzzing workers generated by decision task"""

FIELDS = frozenset(
    (
        "cloud",
        "command",
        "container",
        "cores_per_task",
        "cpu",
        "cycle_time",
        "disk_size",
        "imageset",
        "macros",
        "metal",
        "minimum_memory_per_core",
        "name",
        "parents",
        "platform",
        "scopes",
        "tasks",
    )
)
CPU_ALIASES = {
    "x86_64": "x64",
    "amd64": "x64",
    "x86-64": "x64",
    "x64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
}
PROVIDERS = frozenset(("aws", "gcp"))
ARCHITECTURES = frozenset(("x64", "arm64"))


class MachineTypes:
    """Database of all machine types available, by provider and architecture.
    """

    def __init__(self, machines_data):
        for provider, provider_archs in machines_data.items():
            assert provider in PROVIDERS, f"unknown provider: {provider}"
            for arch, machines in provider_archs.items():
                assert arch in ARCHITECTURES, f"unknown architecture: {provider}.{arch}"
                for machine, spec in machines.items():
                    missing = list({"cpu", "ram"} - set(spec))
                    extra = list(set(spec) - {"cpu", "ram", "metal"})
                    assert (
                        not missing
                    ), f"machine {provider}.{arch}.{machine} missing required keys: {missing!r}"
                    assert (
                        not extra
                    ), f"machine {provider}.{arch}.{machine} has unknown keys: {extra!r}"
        self._data = machines_data

    @classmethod
    def from_file(cls, machines_yml):
        with open(machines_yml) as machines_fp:
            return cls(yaml.safe_load(machines_fp.read()))

    def cpus(self, provider, architecture, machine):
        return self._data[provider][architecture][machine]["cpu"]

    def filter(self, provider, architecture, min_cpu, min_ram_per_cpu, metal=False):
        """Generate machine types which fit the given requirements.

        Args:
            provider (str): the cloud provider (aws or google)
            architecture (str): the cpu architecture (x64 or arm64)
            min_cpu (int): the least number of acceptable cpu cores
            min_ram_per_cpu (float): the least amount of memory acceptable per cpu core
            metal (bool): whether a bare-metal instance is required

        Returns:
            generator of str: machine type names for the given provider/architecture
        """
        for name, spec in self._data[provider][architecture].items():
            if (
                spec["cpu"] >= min_cpu
                and (spec["ram"] / spec["cpu"]) >= min_ram_per_cpu
            ):
                if not metal or (metal and spec.get("metal", False)):
                    yield name


class PoolConfiguration:
    """Fuzzing Pool Configuration

    Attributes:
        cloud (str): cloud provider, like aws or gcp
        command (list): list of strings, command to execute in the image/container
        container (str): name of the container
        cores_per_task (int): number of cores to be allocated per task
        cpu (int): cpu architecture (eg. x64/arm64)
        cycle_time (int): maximum run time of this pool in seconds
        disk_size (int): disk size in GB
        imageset (str): imageset name in community-tc-config/config/imagesets.yml
        macros (dict): dictionary of environment variables passed to the target
        metal (bool): whether or not the target requires to be run on bare metal
        minimum_memory_per_core (float): minimum RAM to be made available per core in GB
        name (str): descriptive name of the configuration
        parents (list): list of parents to inherit from
        platform (str): operating system of the target (linux, windows)
        scopes (list): list of taskcluster scopes required by the target
        tasks (int): number of tasks to run (each with `cores_per_task`)
    """

    def __init__(self, filename, data, _flattened=None):
        missing = list(set(data) - FIELDS)
        extra = list(FIELDS - set(data))
        assert not missing, f"configuration is missing fields: {missing!r}"
        assert not extra, f"configuration has extra fields: {extra!r}"

        # "normal" fields
        self.filename = filename
        self.container = data["container"]
        self.cores_per_task = data["cores_per_task"]
        self.imageset = data["imageset"]
        self.metal = data["metal"]
        self.name = data["name"]
        assert self.name is not None, "name is required for every configuration"
        self.platform = data["platform"]
        self.tasks = data["tasks"]

        # dict fields
        self.macros = data["macros"].copy()

        # list fields
        self.command = data["command"][:]
        self.parents = data["parents"][:]
        self.scopes = data["scopes"][:]

        # size fields
        self.minimum_memory_per_core = self.disk_size = None
        if data["minimum_memory_per_core"] is not None:
            self.minimum_memory_per_core = self.parse_size(
                data["minimum_memory_per_core"], self.parse_size("1g")
            )
        if data["disk_size"] is not None:
            self.disk_size = int(
                self.parse_size(data["disk_size"], self.parse_size("1g"))
            )

        # time fields
        self.cycle_time = None
        if data["cycle_time"] is not None:
            self.cycle_time = int(self.parse_time(data["cycle_time"]))

        # other special fields
        self.cpu = self.cloud = None
        if data["cpu"] is not None:
            cpu = self.alias_cpu(data["cpu"])
            assert cpu in ARCHITECTURES
            self.cpu = cpu

        assert "cloud" in data, "Missing cloud configuration"
        assert data["cloud"] in PROVIDERS, "Invalid cloud - use {}".format(
            ",".join(PROVIDERS)
        )
        self.cloud = data["cloud"]

        if _flattened is None:
            _flattened = set()
        self._flatten(_flattened)

        # Build pool id
        self.id = f"{self.platform}-{self.filename}"

    def is_complete(self):
        for field in FIELDS:
            assert getattr(self, field) is not None

    def _flatten(self, flattened):
        for parent in self.parents:
            assert (
                parent not in flattened
            ), f"attempt to resolve cyclic configuration, {parent} already encountered"
            flattened.add(parent)
            parent_obj = self.from_file(parent, flattened)

            # "normal" overwriting fields
            for field in (
                "cloud",
                "container",
                "cores_per_task",
                "cpu",
                "cycle_time",
                "disk_size",
                "imageset",
                "metal",
                "minimum_memory_per_core",
                "name",
                "platform",
                "tasks",
            ):
                if getattr(self, field) is None:
                    setattr(self, field, getattr(parent_obj, field))

            # merged dict fields
            for field in ("macros",):
                copy = getattr(parent_obj, field).copy()
                copy.update(getattr(self, field))
                setattr(self, field, copy)

            # merged list fields
            for field in ("scopes",):
                setattr(
                    self,
                    field,
                    list(set(getattr(self, field)) + set(getattr(parent_obj, field))),
                )

            # overwriting list fields
            for field in ("command",):
                if not getattr(self, field):
                    setattr(self, field, getattr(parent_obj, field)[:])

    def get_machine_list(self, machine_types):
        """
        Args:
            machine_types (MachineTypes): database of all machine types

        Returns:
            generator of machine (name, capacity): instance type name and task capacity
        """
        for machine in machine_types.filter(
            self.cloud,
            self.cpu,
            self.cores_per_task,
            self.minimum_memory_per_core,
            self.metal,
        ):
            cpus = machine_types.cpus(self.cloud, self.cpu, machine)
            yield (machine, cpus // self.cores_per_task)

    def build_resources(self, providers, machine_types):
        """Build the full tc-admin resources to compare and build the pool"""

        # Select a cloud provider according to configuration
        assert self.cloud in providers, f"Cloud Provider {self.cloud} not available"
        provider = providers[self.cloud]

        # Build the pool configuration for selected machines
        machines = self.get_machine_list(machine_types)
        config = {
            "minCapacity": 0,
            "maxCapacity": self.tasks,
            "launchConfigs": provider.build_launch_configs(
                self.imageset, machines, self.disk_size
            ),
        }

        # Mandatory scopes to execute the hook
        # or create new tasks
        decision_task_scopes = (
            f"queue:scheduler-id:{SCHEDULER_ID}",
            f"queue:create-task:highest:{PROVISIONER_ID}/{self.id}",
            f"secrets:get:{DECISION_TASK_SECRET}",
        )

        # Build the decision task payload that will trigger the new fuzzing tasks
        decision_task = {
            "created": {"$fromNow": "0 seconds"},
            "deadline": {"$fromNow": "1 hour"},
            "expires": {"$fromNow": "1 month"},
            "extra": {},
            "metadata": {
                "description": DESCRIPTION,
                "name": f"Fuzzing decision {self.id}",
                "owner": OWNER_EMAIL,
                "source": "https://github.com/MozillaSecurity/fuzzing-tc",
            },
            "payload": {
                "artifacts": {},
                "cache": {},
                "capabilities": {},
                "env": {"TASKCLUSTER_SECRET": DECISION_TASK_SECRET},
                "features": {"taskclusterProxy": True},
                "image": {
                    "type": "indexed-image",
                    "path": "public/fuzzing-tc-decision.tar",
                    "namespace": "project.fuzzing.config.master",
                },
                "command": ["fuzzing-decision", self.filename],
                "maxRunTime": 3600,
            },
            "priority": "high",
            "provisionerId": PROVISIONER_ID,
            "workerType": self.id,
            "retries": 1,
            "routes": [],
            "schedulerId": SCHEDULER_ID,
            "scopes": decision_task_scopes,
            "tags": {},
        }

        pool = WorkerPool(
            workerPoolId=f"{WORKER_POOL_PREFIX}/{self.id}",
            providerId=PROVIDER_IDS[self.cloud],
            description=DESCRIPTION,
            owner=OWNER_EMAIL,
            emailOnError=True,
            config=config,
        )

        hook = Hook(
            hookGroupId=HOOK_PREFIX,
            hookId=self.id,
            name=self.name,
            description="Generated Fuzzing hook",
            owner=OWNER_EMAIL,
            emailOnError=True,
            schedule=(),  # TODO
            task=decision_task,
            bindings=(),
            triggerSchema={},
        )

        role = Role(
            roleId=f"hook-id:{HOOK_PREFIX}/{self.id}",
            description=DESCRIPTION,
            scopes=tuple(self.scopes) + decision_task_scopes,
        )

        return [pool, hook, role]

    def build_tasks(self, parent_task_id):
        """Create fuzzing tasks and attach them to a decision task"""
        now = datetime.utcnow()
        for i in range(1, self.tasks + 1):
            task_id = slugId()
            task = {
                "taskGroupId": parent_task_id,
                "dependencies": [parent_task_id],
                "created": stringDate(now),
                "deadline": stringDate(now + timedelta(seconds=self.cycle_time)),
                "expires": stringDate(fromNow("1 month", now)),
                "extra": {},
                "metadata": {
                    "description": DESCRIPTION,
                    "name": f"Fuzzing task {self.id} - {i}/{self.tasks}",
                    "owner": OWNER_EMAIL,
                    "source": "https://github.com/MozillaSecurity/fuzzing-tc",
                },
                "payload": {
                    "artifacts": {},
                    "cache": {},
                    "capabilities": {},
                    "env": {},
                    "features": {"taskclusterProxy": True},
                    "image": self.container,
                    "maxRunTime": self.cycle_time,
                },
                "priority": "high",
                "provisionerId": PROVISIONER_ID,
                "workerType": self.id,
                "retries": 1,
                "routes": [],
                "schedulerId": SCHEDULER_ID,
                "scopes": self.scopes,
                "tags": {},
            }

            yield task_id, task

    @classmethod
    def from_file(cls, pool_yml, _flattened=None):
        assert os.path.exists(pool_yml), "Invalid file"
        filename, _ = os.path.splitext(os.path.basename(pool_yml))
        with open(pool_yml) as pool_fd:
            return cls(filename, yaml.safe_load(pool_fd), _flattened)

    @staticmethod
    def alias_cpu(cpu_name):
        """
        Args:
            cpu_name: a cpu string like x86_64 or x64

        Returns:
            str: x64 or arm64
        """
        return CPU_ALIASES[cpu_name.lower()]

    @staticmethod
    def parse_size(size, divisor=1):
        """Parse a human readable size like "4g" into (4 * 1024 * 1024 * 1024)

        Args:
            size (str): size as a string, with si prefixes allowed
            divisor (int): unit to divide by (eg. 1024 for result in kb)

        Returns:
            float: size with si prefix expanded and divisor applied
        """
        match = re.match(
            r"\s*(\d+\.\d*|\.\d+|\d+)\s*([kmgt]?)b?\s*", size, re.IGNORECASE
        )
        assert (
            match is not None
        ), "size should be a number followed by optional si prefix"
        result = float(match.group(1))
        multiplier = {
            "": 1,
            "k": 1024,
            "m": 1024 * 1024,
            "g": 1024 * 1024 * 1024,
            "t": 1024 * 1024 * 1024 * 1024,
        }[match.group(2).lower()]
        return result * multiplier / divisor

    @staticmethod
    def parse_time(time, divisor=1):
        """Parse a human readable time like 1h30m or 30m10s

        Args:
            time (str): time as a string
            divisor (int): seconds to divide by (1s default, 60 for result in minutes, etc.)

        Returns:
            float: time in seconds (or units determined by divisor)
        """
        result = 0
        got_anything = False
        while time:
            match = re.match(r"\s*(\d+)\s*([wdhms]?)\s*(.*)", time, re.IGNORECASE)
            assert (
                got_anything or match is not None
            ), "time should be a number followed by optional unit"
            if match is None:
                break
            if match.group(2):
                multiplier = {
                    "w": 7 * 24 * 60 * 60,
                    "d": 24 * 60 * 60,
                    "h": 60 * 60,
                    "m": 60,
                    "s": 1,
                }[match.group(2).lower()]
            else:
                assert not match.group(3), "trailing data"
                assert not got_anything, "multipart time must specify all units"
                multiplier = 1
            got_anything = True
            result += int(match.group(1)) * multiplier
            time = match.group(3)
        return result / divisor


def test_main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="machines.yml")
    parser.add_argument(
        "--cpu", help="cpu architecture", choices=ARCHITECTURES, default="x64"
    )
    parser.add_argument(
        "--provider", help="cloud provider", choices=PROVIDERS, default="aws"
    )
    parser.add_argument(
        "--cores", help="minimum number of cpu cores", type=int, required=True
    )
    parser.add_argument(
        "--ram", help="minimum amount of ram per core, eg. 4gb", required=True
    )
    parser.add_argument("--metal", help="bare metal machines", action="store_true")
    args = parser.parse_args()

    ram = PoolConfiguration.parse_size(args.ram, PoolConfiguration.parse_size("1g"))
    type_list = MachineTypes.from_file(args.input)
    for machine in type_list.filter(
        args.provider, args.cpu, args.cores, ram, args.metal
    ):
        print(machine)


if __name__ == "__main__":
    test_main()
