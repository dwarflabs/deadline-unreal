import DeadlineUnrealUtils
from Deadline.Scripting import RepositoryUtils


def __main__(*args):
    deadline_plugin = args[0]

    # write the final manifest in the output directory
    manifest_filepath = DeadlineUnrealUtils.write_manifest_file(deadline_plugin, for_cmdline=False)

    override_manifest = ""
    with open(manifest_filepath, "r") as manifest:
        override_manifest = manifest.read()

    # add it to the job's environment variables, so that render tasks can get and use it
    job = deadline_plugin.GetJob()
    job.SetJobEnvironmentKeyValue("override_serialized_pipeline", override_manifest)

    RepositoryUtils.SaveJob(job)
