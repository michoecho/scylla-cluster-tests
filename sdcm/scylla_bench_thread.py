# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2020 ScyllaDB

import os
import re
import uuid
import time
import logging
import contextlib
from enum import Enum

from sdcm.loader import ScyllaBenchStressExporter
from sdcm.prometheus import nemesis_metrics_obj
from sdcm.sct_events import Severity
from sdcm.sct_events.loaders import ScyllaBenchEvent, SCYLLA_BENCH_ERROR_EVENTS_PATTERNS
from sdcm.utils.common import FileFollowerThread, convert_metric_to_ms
from sdcm.stress_thread import format_stress_cmd_error, DockerBasedStressThread
from sdcm.utils.docker_remote import RemoteDocker
from sdcm.wait import wait_for


LOGGER = logging.getLogger(__name__)


class ScyllaBenchModes(str, Enum):
    WRITE = "write"
    READ = "read"
    COUNTER_UPDATE = "counter_update"
    COUNTER_READ = "counter_read"
    SCAN = "scan"


class ScyllaBenchWorkloads(str, Enum):
    UNIFORM = "uniform"
    TIMESERIES = "timeseries"
    SEQUENTIAL = "sequential"


class ScyllaBenchStressEventsPublisher(FileFollowerThread):
    def __init__(self, node, sb_log_filename, event_id=None):
        super().__init__()
        self.sb_log_filename = sb_log_filename
        self.node = str(node)
        self.event_id = event_id

    def run(self):
        while not self.stopped():
            exists = os.path.isfile(self.sb_log_filename)
            if not exists:
                time.sleep(0.5)
                continue

            for line_number, line in enumerate(self.follow_file(self.sb_log_filename)):
                if self.stopped():
                    break

                for pattern, event in SCYLLA_BENCH_ERROR_EVENTS_PATTERNS:
                    if self.event_id:
                        # Connect the event to the stress load
                        event.event_id = self.event_id

                    if pattern.search(line):
                        event.add_info(node=self.node, line=line, line_number=line_number).publish()


class ScyllaBenchThread(DockerBasedStressThread):  # pylint: disable=too-many-instance-attributes

    DOCKER_IMAGE_PARAM_NAME = "stress_image.scylla-bench"
    _SB_STATS_MAPPING = {
        # Mapping for scylla-bench statistic and configuration keys to db stats keys
        'Mode': 'Mode',
        'Workload': 'Workload',
        'Timeout': 'Timeout',
        'Consistency level': 'Consistency level',
        'Partition count': 'Partition count',
        'Clustering rows': 'Clustering rows',
        'Page size': 'Page size',
        'Concurrency': 'Concurrency',
        'Connections': 'Connections',
        'Maximum rate': 'Maximum rate',
        'Client compression': 'Client compression',
        'Clustering row size': 'Clustering row size',
        'Rows per request': 'Rows per request',
        'Total rows': 'Total rows',
        'max': 'latency max',
        '99.9th': 'latency 99.9th percentile',
        '99th': 'latency 99th percentile',
        '95th': 'latency 95th percentile',
        '90th': '90th',
        'median': 'latency median',
        'Operations/s': 'op rate',
        'Rows/s': 'row rate',
        'Total ops': 'Total partitions',
        'Time (avg)': 'Total operation time',
    }

    # pylint: disable=too-many-arguments
    def __init__(self, stress_cmd, loader_set, timeout, node_list=None, round_robin=False,
                 stop_test_on_failure=False, stress_num=1, credentials=None, params=None):
        super().__init__(loader_set=loader_set, stress_cmd=stress_cmd, timeout=timeout, stress_num=stress_num,
                         node_list=node_list, round_robin=round_robin, params=params,
                         stop_test_on_failure=stop_test_on_failure)
        if credentials and 'username=' not in self.stress_cmd:
            self.stress_cmd += " -username {} -password {}".format(*credentials)

        if not any(opt in self.stress_cmd for opt in ('-error-at-row-limit', '-error-limit')):
            result = re.search(r"-retry-number[= ]+(\d+) ", self.stress_cmd)
            if not (result and int(result.group(1)) > 1):
                # make it fail after having 1000 errors at row
                self.stress_cmd += ' -error-at-row-limit 1000'

        # Find stress mode:
        #    "scylla-bench -workload=sequential -mode=write -replication-factor=3 -partition-count=100"
        #    "scylla-bench -workload=uniform -mode=read -replication-factor=3 -partition-count=100"
        self.sb_mode: ScyllaBenchModes = ScyllaBenchModes(re.search(r"-mode=(.+?) ", stress_cmd).group(1))
        self.sb_workload: ScyllaBenchWorkloads = ScyllaBenchWorkloads(
            re.search(r"-workload=(.+?) ", stress_cmd).group(1))

    def verify_results(self):
        sb_summary = []
        errors = []

        results = self.get_results()

        for _, result in results:
            if not result:
                # Silently skip if stress command threw an error, since it was already reported in _run_stress
                continue
            output = result.stdout + result.stderr

            lines = output.splitlines()
            node_cs_res = self._parse_bench_summary(lines)  # pylint: disable=protected-access

            if node_cs_res:
                sb_summary.append(node_cs_res)

        return sb_summary, errors

    def create_stress_cmd(self, stress_cmd):
        if self.connection_bundle_file:
            stress_cmd = f'{stress_cmd.strip()} -cloud-config-path={self.target_connection_bundle_file}'
        else:
            # Select first seed node to send the scylla-bench cmds
            ips = ",".join([n.cql_ip_address for n in self.node_list])
            stress_cmd = f'{stress_cmd.strip()} -nodes {ips}'

        return stress_cmd

    def _run_stress(self, loader, loader_idx, cpu_idx):  # pylint: disable=too-many-locals
        cmd_runner = None
        if "k8s" in self.params.get("cluster_backend"):
            cmd_runner = loader.remoter
            cmd_runner_name = loader.remoter.pod_name
            cleanup_context = contextlib.nullcontext()
        else:
            cpu_options = ""
            if self.stress_num > 1:
                cpu_options = f'--cpuset-cpus="{cpu_idx}"'
            cmd_runner = cleanup_context = RemoteDocker(
                loader, self.params.get('stress_image.scylla-bench'),
                extra_docker_opts=f'{cpu_options} --label shell_marker={self.shell_marker} --network=host',
            )
            cmd_runner_name = loader.ip_address

        if self.connection_bundle_file:
            cmd_runner.send_files(str(self.connection_bundle_file), self.target_connection_bundle_file)

        if self.sb_mode == ScyllaBenchModes.WRITE and self.sb_workload == ScyllaBenchWorkloads.TIMESERIES:
            loader.parent_cluster.sb_write_timeseries_ts = write_timestamp = time.time_ns()
            LOGGER.debug("Set start-time: %s", write_timestamp)
            stress_cmd = re.sub(r"SET_WRITE_TIMESTAMP", f"{write_timestamp}", self.stress_cmd)
            LOGGER.debug("Replaced stress command: %s", stress_cmd)

        elif self.sb_mode == ScyllaBenchModes.READ and self.sb_workload == ScyllaBenchWorkloads.TIMESERIES:
            write_timestamp = wait_for(lambda: loader.parent_cluster.sb_write_timeseries_ts,
                                       step=5,
                                       timeout=30,
                                       text='Waiting for "scylla-bench -workload=timeseries -mode=write" been started, to pick up timestamp'
                                       )
            LOGGER.debug("Found write timestamp %s", write_timestamp)
            stress_cmd = re.sub(r"GET_WRITE_TIMESTAMP", f"{write_timestamp}", self.stress_cmd)
            LOGGER.debug("replaced stress command %s", stress_cmd)
        else:
            stress_cmd = self.stress_cmd
            LOGGER.debug("Scylla bench command: %s", self.stress_cmd)

        if not os.path.exists(loader.logdir):
            os.makedirs(loader.logdir, exist_ok=True)

        log_file_name = os.path.join(loader.logdir, f'scylla-bench-l{loader_idx}-{uuid.uuid4()}.log')
        stress_cmd = self.create_stress_cmd(stress_cmd)
        with ScyllaBenchStressExporter(instance_name=cmd_runner_name,
                                       metrics=nemesis_metrics_obj(),
                                       stress_operation=self.sb_mode,
                                       stress_log_filename=log_file_name,
                                       loader_idx=loader_idx), \
                cleanup_context, \
                ScyllaBenchStressEventsPublisher(node=loader, sb_log_filename=log_file_name) as publisher, \
                ScyllaBenchEvent(node=loader, stress_cmd=stress_cmd,
                                 log_file_name=log_file_name) as scylla_bench_event:
            publisher.event_id = scylla_bench_event.event_id
            result = None
            try:
                result = cmd_runner.run(
                    cmd=stress_cmd,
                    timeout=self.timeout,
                    log_file=log_file_name,
                    retry=0,
                )
            except Exception as exc:  # pylint: disable=broad-except
                errors_str = format_stress_cmd_error(exc)
                if "truncate: seastar::rpc::timeout_error" in errors_str:
                    scylla_bench_event.severity = Severity.ERROR
                elif self.stop_test_on_failure:
                    scylla_bench_event.severity = Severity.CRITICAL
                else:
                    scylla_bench_event.severity = Severity.ERROR

                scylla_bench_event.add_error([errors_str])

        return loader, result

    @classmethod
    def _parse_bench_summary(cls, lines):
        """
        Parsing bench results, only parse the summary results.
        Collect results of all nodes and return a dictionaries' list,
        the new structure data will be easy to parse, compare, display or save.
        """
        results = {'keyspace_idx': None, 'stdev gc time(ms)': None, 'Total errors': None,
                   'total gc count': None, 'loader_idx': None, 'total gc time (s)': None,
                   'total gc mb': 0, 'cpu_idx': None, 'avg gc time(ms)': None, 'latency mean': None}

        for line in lines:
            line.strip()
            # Parse load params
            # pylint: disable=too-many-boolean-expressions
            if line.startswith('Results'):
                continue
            if 'c-o fixed latency' in line:
                # Ignore C-O Fixed latencies
                #
                # c-o fixed latency :
                #   max:        5.668863ms
                #   99.9th:	    5.537791ms
                #   99th:       3.440639ms
                #   95th:       3.342335ms
                break

            split = line.split(':', maxsplit=1)
            if len(split) < 2:
                continue
            key = split[0].strip()
            value = ' '.join(split[1].split())
            if target_key := cls._SB_STATS_MAPPING.get(key):
                if value.isdecimal():
                    value = int(value)
                else:
                    value = convert_metric_to_ms(value)
                results[target_key] = value
            else:
                LOGGER.debug('unknown result key found: `%s` with value `%s`', key, value)
        row_rate = results.get('row rate')
        if row_rate is not None:
            results['partition rate'] = row_rate
        return results
