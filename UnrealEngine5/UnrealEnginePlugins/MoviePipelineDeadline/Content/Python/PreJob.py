import DeadlineUnrealUtils


def __main__(*args):
    deadline_plugin = args[0]

    manifest_filepath = DeadlineUnrealUtils.write_manifest_file(deadline_plugin, for_cmdline=False)

    override_manifest = ""
    with open(manifest_filepath, "r") as manifest:
        override_manifest = manifest.read()

    deadline_plugin.SetProcessEnvironmentVariable("override_serialized_pipeline", override_manifest)
