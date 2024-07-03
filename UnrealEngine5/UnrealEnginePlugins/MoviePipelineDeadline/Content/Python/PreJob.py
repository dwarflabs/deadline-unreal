import DeadlineUnrealUtils


def __main__(*args):
    deadline_plugin = args[0]

    override_manifest = DeadlineUnrealUtils.write_manifest_file(deadline_plugin, for_cmdline=False)

    deadline_plugin.SetProcessEnvironmentVariable("override_serialized_pipeline", override_manifest)
