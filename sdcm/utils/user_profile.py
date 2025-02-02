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
# Copyright (c) 2023 ScyllaDB

""" This module keeps utilities that help to extract schema definition and stress commands from user profile """
from __future__ import absolute_import, annotations

import os
import re

import yaml


def get_stress_command_for_profile(params, stress_cmds_part, search_for_user_profile, stress_cmd=None):
    """
    Extract stress command from test profiles if this command runs user profile and ignore not user profile command
    Example:
        "cassandra-stress user profile=/tmp/c-s_lwt_big_data_multidc.yaml n=10000000 ops'(insert_query=1)'
        cl=QUORUM -mode native cql3 -rate threads=1000"
    """
    if not stress_cmd:
        stress_cmd = params.get(stress_cmds_part) or []
        stress_cmd = [cmd for cmd in stress_cmd if search_for_user_profile in cmd]

    if not stress_cmd:
        return []

    if not isinstance(stress_cmd, list):
        stress_cmd = [stress_cmd]

    return stress_cmd


def get_view_cmd_from_profile(profile_content, name_substr, all_entries=False):
    """
    Extract materialized view creation command from user profile, 'extra_definitions' part
    """
    all_mvs = profile_content['extra_definitions']
    mv_cmd = [cmd for cmd in all_mvs if name_substr in cmd]

    mv_cmd = [mv_cmd[0]] if not all_entries and mv_cmd else mv_cmd
    return mv_cmd


def get_view_name_from_stress_cmd(mv_create_cmd, name_substr):
    """
    Get materialized view name from creation command (how to get creation command see "get_view_cmd_from_profile" func)
    Example:
        extra_definitions:
          - create MATERIALIZED VIEW cf_update_2_columns_mv_pk as select * from blog_posts where domain is not null;

    """
    find_mv_name = re.search(r'materialized view (.*%s.*) as' % name_substr, mv_create_cmd, re.I)
    return find_mv_name.group(1) if find_mv_name else None


def get_view_name_from_user_profile(params, stress_cmds_part: str, search_for_user_profile: str, view_name_substr: str):
    """
    Get materialized view name from user profile defined in the test yaml.
    It may be used when we want query from materialized view that was created during running user profile
    """
    stress_cmd = get_stress_command_for_profile(params=params, stress_cmds_part=stress_cmds_part,
                                                search_for_user_profile=search_for_user_profile)
    if not stress_cmd:
        return ""

    _, profile = get_profile_content(stress_cmd[0])
    mv_cmd = get_view_cmd_from_profile(profile, view_name_substr)
    return get_view_name_from_stress_cmd(mv_cmd[0], view_name_substr)


def get_keyspace_from_user_profile(params, stress_cmds_part: str, search_for_user_profile: str):
    """
     Get keyspace name from user profile defined in the test yaml.
     Example:
        # Keyspace Name
        keyspace: mv_synchronous_ks
    """
    stress_cmd = get_stress_command_for_profile(params=params, stress_cmds_part=stress_cmds_part,
                                                search_for_user_profile=search_for_user_profile)
    if not stress_cmd:
        return ""

    _, profile = get_profile_content(stress_cmd[0])
    return profile['keyspace']


def get_table_from_user_profile(params, stress_cmds_part: str, search_for_user_profile: str):
    """
     Get table name from user profile defined in the test yaml.
     Example:
        # Table name
        table: blog_posts
    """
    stress_cmd = get_stress_command_for_profile(params=params, stress_cmds_part=stress_cmds_part,
                                                search_for_user_profile=search_for_user_profile)
    if not stress_cmd:
        return ""

    _, profile = get_profile_content(stress_cmd[0])
    return profile['table']


def get_profile_content(stress_cmd):
    """
    Looking profile yaml in data_dir or the path as is to get the user profile
    and loading it's yaml

    :return: (profile_filename, dict with yaml)
    """

    cs_profile = re.search(r'profile=(.*\.yaml)', stress_cmd).group(1)
    sct_cs_profile = os.path.join(os.path.dirname(__file__), '../../', 'data_dir', os.path.basename(cs_profile))
    if os.path.exists(sct_cs_profile):
        cs_profile = sct_cs_profile
    elif not os.path.exists(cs_profile):
        raise FileNotFoundError('User profile file {} not found'.format(cs_profile))

    with open(cs_profile, encoding="utf-8") as yaml_stream:
        profile = yaml.safe_load(yaml_stream)
    return cs_profile, profile
