import json
import os
# import re


def apply_override_output_directory(deadline_plugin, serialized_pipeline, key):
    # get output directory override
    output_directory_override = deadline_plugin.GetJob().GetJobExtraInfoKeyValue("output_directory_override")
    if output_directory_override:
        default_output_key = key + '.DefaultConfig.DefaultOutputSetting'
        serialized_pipeline['Exports']\
            .setdefault(default_output_key, {})\
                .setdefault('Properties', {})\
                    ['OutputDirectory'] = \
                        {
                            "__Type": "StructProperty",
                            "__StructName": "DirectoryPath",
                            "__Value": {
                                "Path": {
                                    "__Type": "StrProperty",
                                    "__Value": output_directory_override
                                }
                            }
                        }


def apply_override_output_override(deadline_plugin, serialized_pipeline, key):
    # get output override
    override_output = deadline_plugin.GetJob().GetJobEnvironmentKeyValue("override_output")
    if override_output != None:
        override_output = int(override_output)
        default_output_key = key + '.DefaultConfig.DefaultOutputSetting'
        serialized_pipeline['Exports']\
            .setdefault(default_output_key, {})\
                .setdefault('Properties', {})\
                    ['bOverrideExistingOutput'] = \
                        {
                            "__Type": "BoolProperty",
                            "__Value": override_output
                        }


def apply_override_texture_streaming(deadline_plugin, serialized_pipeline, key):
    # get texture streaming override
    texture_streaming_override = deadline_plugin.GetJob().GetJobEnvironmentKeyValue("texture_streaming_override")

    if texture_streaming_override:
        # assert override is correct
        # possible values are:
        # None -> This will not change the texture streaming method / cvars the users has set.
        # Disabled -> Disable the Texture Streaming system. Requires the highest amount of VRAM, but helps if Fully Load Used Textures still has blurry textures.
        # FullyLoad -> Fully load used textures instead of progressively streaming them in over multiple frames. Requires less VRAM but can occasionally still results in blurry textures.
        tex_stream_values = ["None", "Disabled", "FullyLoad"]

        if texture_streaming_override not in tex_stream_values:
            deadline_plugin.FailRender(
                "Texture streaming override has an incorrect value. "\
                f"{texture_streaming_override} not in {tex_stream_values}"
            )

        # find the MoviePipelineGameOverrideSetting_X
        override_tex_stream_key = key + '.DefaultConfig.MoviePipelineGameOverrideSetting_'
        for key_name in serialized_pipeline['Exports'].keys():
            if key_name.startswith(override_tex_stream_key):
                override_tex_stream_key = key_name
                break

        # assert we found the key
        if override_tex_stream_key not in serialized_pipeline['Exports'].keys():
            deadline_plugin.FailRender(
                "Could not find MoviePipelineGameOverrideSetting to override texture streaming."
            )

        # apply the override
        serialized_pipeline['Exports']\
            .setdefault(override_tex_stream_key, {})\
                .setdefault('Properties', {})\
                    ['TextureStreaming'] = \
                        {
                            "__Type": "EnumProperty",
                            "__EnumName": "EMoviePipelineTextureStreamingMethod",
                            "__Value": f"EMoviePipelineTextureStreamingMethod::{texture_streaming_override}"
                        }


def write_manifest_file(deadline_plugin, for_cmdline=False, project_root=None):
    # for some reasons, importing re at the start of the file does not work
    # need to import it here
    import re

    # create manifest file from serialized pipeline
    serialized_pipeline_str = deadline_plugin.GetJob().GetJobExtraInfoKeyValue("serialized_pipeline")

    if not serialized_pipeline_str:
        deadline_plugin.FailRender(
            "No serialized_pipeline found in extra info key values"
        )

    # read string as dict
    serialized_pipeline = json.loads(serialized_pipeline_str)

    # in commandline mode, Unreal will render all enabled shots in the manifest file
    # so we need to properly disable/enable shots here
    if for_cmdline:
        # get shot repartition per task
        shot_info = deadline_plugin.GetJob().GetJobExtraInfoKeyValue("shot_info")

        if shot_info:
            task_id = deadline_plugin.GetCurrentTaskId()
            shot_info = json.loads(shot_info)
            shots = shot_info.get(task_id, "").split(",")

            for key, value in serialized_pipeline.get('Exports', {}).items():
                if re.fullmatch("MoviePipelineQueue_\d+:MoviePipelineDeadlineExecutorJob_\d+.MoviePipelineExecutorShot_\d+", key):
                    shot_name = value.get('Properties', {}).get('OuterName', {}).get('__Value', "")

                    if not shot_name:
                        continue

                    # enable or disable shot based on what this task should render
                    enabled = False
                    if shot_name in shots:
                        enabled = True

                    serialized_pipeline['Exports'][key]['Properties']['bEnabled'] = {
                        "__Type": "BoolProperty",
                        "__Value": enabled
                    }

    # edit serialized pipeline to apply overrides
    # and move the render preset to another location otherwise the overrides are not taken into account
    #   move PresetOrigin from MoviePipelineQueue_X:MoviePipelineDeadlineExecutorJob_X
    #   to   ConfigOrigin in   MoviePipelineQueue_X:MoviePipelineDeadlineExecutorJob_X.DefaultConfig
    job_name = 'NoJobName'
    for key, value in serialized_pipeline.get('Exports', {}).items():
        if re.fullmatch("MoviePipelineQueue_\d+:MoviePipelineDeadlineExecutorJob_\d+", key):
            job_name = value.get('Properties', {}).get('JobName', {}).get('__Value', '')

            # search for field PresetOrigin
            preset = value.get('Properties', {}).get('PresetOrigin')

            # if found, need to move it to DefaultConfig
            if preset:
                del value['Properties']['PresetOrigin']

                config_key = key + '.DefaultConfig'
                serialized_pipeline['Exports']\
                    .setdefault(config_key, {})\
                        .setdefault('Properties', {})\
                            ['ConfigOrigin'] = preset

            # apply overrides, if any
            apply_override_output_directory(deadline_plugin, serialized_pipeline, key)
            apply_override_output_override(deadline_plugin, serialized_pipeline, key)
            apply_override_texture_streaming(deadline_plugin, serialized_pipeline, key)

            break

    # re-stringify the dict so we can write it in the file
    serialized_pipeline_str = json.dumps(serialized_pipeline, indent=4)

    manifest_filepath = None
    if for_cmdline:
        # for commandline mode, Unreal is quite picky for the manifest file...
        # better keep it in default directory with default name
        manifest_filepath = f"{project_root}/Saved/MovieRenderPipeline/QueueManifest.utxt"
    else:
        # copy manifest file to output directory, for debug purpose only, Unreal will not read this
        output_dir = deadline_plugin.GetJob().GetJobExtraInfoKeyValue("output_directory_override")
        manifest_filepath = os.path.join(output_dir, f"{job_name}_QueueManifest.utxt")

    manifest_filepath = manifest_filepath.replace("\\", "/")

    # ensure dir hierarchy is created
    os.makedirs(os.path.dirname(manifest_filepath), exist_ok=True)

    # write manifest file
    with open(manifest_filepath, "w") as manifest:
        manifest.write(serialized_pipeline_str)

    return serialized_pipeline_str    
