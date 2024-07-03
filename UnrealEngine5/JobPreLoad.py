#!/usr/bin/env python3

import os
import socket 
import sys
from pathlib import Path

from Deadline.Scripting import RepositoryUtils
from P4 import P4


WORKSPACE_PATTERN = "{prefix}_{user}_{machine}"


def __main__(deadline_plugin):
    # Add the location of the plugin package to the system path so the plugin
    # can import supplemental modules if it needs to
    plugin_path = Path(__file__)
    if plugin_path.parent not in sys.path:
        sys.path.append(plugin_path.parent.as_posix())

    # initialize P4
    p4 = P4()
    p4.port = os.environ.get("P4PORT", "NoPort")
    p4.user = os.environ.get("P4USER", os.getlogin())
    # NOTE: Ideal would be that this P4PASSWD is set as the encrypted md5 of the actual password
    # Perforce's documentation talks about doing that here: https://www.perforce.com/manuals/cmdref/Content/CmdRef/P4PASSWD.html
    # but we could not set it correctly.. (on Windows) if you ever try and success in doing this, let us know :)
    p4.password = os.environ.get("P4PASSWD", "NoPassword")

    # sync P4 local workspace for render
    with p4.connect() as p4:
        p4.run_login()

        # get project prefix
        workspace_prefix = deadline_plugin.GetJob().GetJobEnvironmentKeyValue("P4_workspace_prefix")

        if not workspace_prefix:
            deadline_plugin.FailRender(
                "Workspace prefix not provided, cannot synchronize P4 workspace for render."
            )

        # find perforce workspace by matching a pattern, for example prefix_user_machine
        workspace_pattern = WORKSPACE_PATTERN.format(prefix=workspace_prefix, user=p4.user, machine=socket.gethostname())

        found_workspaces = p4.run_clients("-u", p4.user, "-e", workspace_pattern)

        if len(found_workspaces) != 1:
            deadline_plugin.FailRender(
                f"Found 0 or more than 1 workspaces for pattern {workspace_pattern}, cannot determine workspace."
                f"\nWorkspace pattern is {workspace_pattern}. Workspace prefix should be project name"
            )

        workspace = found_workspaces[0]
        p4.client = workspace['client']

        # add ProjectRoot to process env so that job can retrieve it
        project_root = workspace['Root']
        deadline_plugin.SetProcessEnvironmentVariable("ProjectRoot", project_root)

        # get target CL
        p4_cl = deadline_plugin.GetJob().GetJobEnvironmentKeyValue("P4_CL")

        # set workspace to target CL
        current_cl = p4.run("changes", "-m", "1", f"{project_root}\\...#have")
        deadline_plugin.LogInfo(f"current CL is {current_cl}")
        deadline_plugin.LogInfo(f"wanted CL is  {p4_cl}")
        if not current_cl or current_cl[0]['change'] != p4_cl:
            try:
                synced_cl = p4.run("sync", f"{project_root}\\...@{p4_cl}")
                deadline_plugin.LogInfo(f"synced to CL {synced_cl}")
            except Exception as e:
                fail = True
                if e.warnings:
                    # It is possible that P4 will consider that the repo is on the correct CL
                    # while `p4 changes -m 1 root\\...#have` returns another CL number
                    # For now, ignore the warning and continue the job
                    warning = e.warnings[0]
                    if f"@{p4_cl} - file(s) up-to-date." in warning:
                        fail = False
                        deadline_plugin.LogWarning(f"Current CL is not the same as wanted CL but P4 says that repo is correctly synced: {e}")
                if fail:
                    deadline_plugin.FailRender(f"Errors: {e.errors} \nWarnings: {e.warnings}")

    # update job output directory
    job = deadline_plugin.GetJob()

    output_directory_override = job.GetJobExtraInfoKeyValue("output_directory_override")
    if os.path.isdir(output_directory_override):
        RepositoryUtils.SetJobOutputDirectories(job, [output_directory_override])

    RepositoryUtils.SaveJob(job)
