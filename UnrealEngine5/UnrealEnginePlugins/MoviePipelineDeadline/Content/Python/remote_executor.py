# Copyright Epic Games, Inc. All Rights Reserved

# Built-In
import getpass
import json
import os
import re
import traceback
from collections import OrderedDict

# External
import unreal

from deadline_service import get_global_deadline_service_instance
from deadline_job import DeadlineJob
from deadline_utils import get_deadline_info_from_preset

from P4 import P4, P4Exception

p4 = P4()
p4.port = os.environ.get("P4PORT", "NoPort")
p4.user = os.environ.get("P4USER", os.getlogin())
p4.password = os.environ.get("P4PASSWD", "NoPassword")


def create_shot_list(sequence, shots_to_render, target_size, ignore_chunk_size=False):
    # find the shot track in the sequence
    shots_track = sequence.find_tracks_by_exact_type(unreal.MovieSceneCinematicShotTrack)
    if not shots_track or len(shots_track) > 1:
        result = unreal.EditorDialog.show_message(
            "Deadline job submission",
            f"Found none or more than 1 shot tracks in given sequence, cannot pack shots from ChunkSize. Continue without packing ?",
            unreal.AppMsgType.YES_NO,
            default_value=unreal.AppReturnType.YES
        )

        if result == unreal.AppReturnType.YES:
            ignore_chunk_size = True
        else:
            raise RuntimeError("Found none or more than 1 shot tracks in given sequence, cannot pack shots from ChunkSize.")
    else:
        shots_track = shots_track[0]

    # create shot list of tuples containing shot name and frame count
    shot_weights = []
    if ignore_chunk_size:
        target_size = 1
        shot_weights = [(s, idx, 1) for idx, s in enumerate(shots_to_render)]
    else:
        for section in shots_track.get_sections():
            start_frame = unreal.MovieSceneSectionExtensions.get_start_frame(section)
            end_frame = unreal.MovieSceneSectionExtensions.get_end_frame(section)
            frame_count = end_frame - start_frame
            shot_seq = section.get_editor_property('sub_sequence')
            shot_name = shot_seq.get_name()
            # need to find shot name as denominated in the job (which is shotseqname.subseqname)
            shot_name = [s for s in shots_to_render if shot_name in s]
            # skip shot not found in shots_to_render (most probably because it is deactivated)
            if not shot_name:
                continue
            shot_name = shot_name[0]
            # remove the shot from the list, to avoid using the same one when multiple shots have the same name
            # (sequence name will give exact same name, while in shots_to_render same names are extended with a number in braces)
            shots_to_render.remove(shot_name)
            # add shot info to list
            shot_weights.append((shot_name, start_frame, frame_count))

    # sort by decreasing order
    shot_weights.sort(key=lambda x: x[1], reverse=True)
    min_weight = shot_weights[-1][1]

    # pack shots together based on target frame count
    shots = {}
    added_shots = []
    frame_list = []
    task_index = 0
    last_frame = sequence.get_playback_end()
    for shot, start_frame, weight in shot_weights:
        if shot in added_shots:
            continue

        # add shot to current bin
        current_size = weight
        current_bin = [shot]
        added_shots.append(shot)

        # bin can have more, search for more to add
        # only if it would be possible to add at least the smallest shot, otherwise don't bother
        while(current_size + min_weight <= target_size):
            best_shot = None
            best_weight = 0
            min_diff = 1000000
            for other_shot, _, other_weight in shot_weights:
                if other_shot in added_shots:
                    continue

                # check absolute difference between target_size and size with this weight added
                diff = abs(target_size - (current_size + other_weight))

                # if difference is smaller than before found, replace best match with this one
                if diff < min_diff:
                    min_diff = diff
                    best_shot = other_shot
                    best_weight = other_weight

            # add found best shot to current bin
            if best_shot:
                current_size += best_weight
                current_bin.append(best_shot)
                added_shots.append(best_shot)
            # not match found, cannot add more
            else:
                break

        # bin is full, add the shots to the task list
        shots[str(task_index)] = ",".join(current_bin)

        # compute frame range for the task
        #   tasks of only 1 shot uses the actual shot frame range
        #   tasks of multiple shots uses a fake frame range that starts from the last frame of the sequence
        if len(current_bin) == 1:
            end_frame = start_frame + current_size - 1
        else:
            start_frame = last_frame
            end_frame = start_frame + current_size - 1
            last_frame = end_frame + 1

        frame_list.append(f"{start_frame}-{end_frame}")

        unreal.log(
            "task " + str(task_index) +
            "\n  frame range " + str(start_frame) + "-" + str(end_frame) +
            "\n  total frame count " + str(current_size) +
            "\n  shot count " + str(len(current_bin)) +
            "\n  shots " + str(current_bin)
        )
        task_index += 1

    # sort in reverse order, to prevent Deadline from merging sequencial frame ranges into a single one
    frame_list = sorted(frame_list, key=lambda x: int(x.split("-")[0]), reverse=True)

    return shots, frame_list, not ignore_chunk_size


@unreal.uclass()
class MoviePipelineDeadlineRemoteExecutor(unreal.MoviePipelineExecutorBase):
    """
    This class defines the editor implementation for Deadline (what happens when you
    press 'Render (Remote)', which is in charge of taking a movie queue from the UI
    and processing it into something Deadline can handle.
    """

    # The queue we are working on, null if no queue has been provided.
    pipeline_queue = unreal.uproperty(unreal.MoviePipelineQueue)
    job_ids = unreal.uproperty(unreal.Array(str))

    # A MoviePipelineExecutor implementation must override this.
    @unreal.ufunction(override=True)
    def execute(self, pipeline_queue):
        """
        This is called when the user presses Render (Remote) in the UI. We will
        split the queue up into multiple jobs. Each job will be submitted to
        deadline separately, with each shot within the job split into one Deadline
        task per shot.
        """

        unreal.log(f"Asked to execute Queue: {pipeline_queue}")
        unreal.log(f"Queue has {len(pipeline_queue.get_jobs())} jobs")

        # Don't try to process empty/null Queues, no need to send them to
        # Deadline.
        if not pipeline_queue or (not pipeline_queue.get_jobs()):
            self.on_executor_finished_impl()
            return

        # The user must save their work and check it in so that Deadline
        # can sync it.
        dirty_packages = []
        dirty_packages.extend(
            unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()
        )
        dirty_packages.extend(
            unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()
        )

        # Sometimes the dialog will return `False`
        # even when there are no packages to save. so we are
        # being explict about the packages we need to save
        if dirty_packages:
            if not unreal.EditorLoadingAndSavingUtils.save_dirty_packages_with_dialog(
                True, True
            ):
                message = (
                    "One or more jobs in the queue have an unsaved map/content. "
                    "{packages} "
                    "Please save and check-in all work before submission.".format(
                        packages="\n".join(dirty_packages)
                    )
                )

                unreal.log_error(message)
                unreal.EditorDialog.show_message(
                    "Unsaved Maps/Content", message, unreal.AppMsgType.OK
                )
                self.on_executor_finished_impl()
                return

        # Make sure all the maps in the queue exist on disk somewhere,
        # unsaved maps can't be loaded on the remote machine, and it's common
        # to have the wrong map name if you submit without loading the map.
        has_valid_map = (
            unreal.MoviePipelineEditorLibrary.is_map_valid_for_remote_render(
                pipeline_queue.get_jobs()
            )
        )
        if not has_valid_map:
            message = (
                "One or more jobs in the queue have an unsaved map as "
                "their target map. "
                "These unsaved maps cannot be loaded by an external process, "
                "and the render has been aborted."
            )
            unreal.log_error(message)
            unreal.EditorDialog.show_message(
                "Unsaved Maps", message, unreal.AppMsgType.OK
            )
            self.on_executor_finished_impl()
            return

        self.pipeline_queue = pipeline_queue

        deadline_settings = unreal.get_default_object(
            unreal.MoviePipelineDeadlineSettings
        )

        # Arguments to pass to the executable. This can be modified by settings
        # in the event a setting needs to be applied early.
        # In the format of -foo -bar
        # commandLineArgs = ""
        command_args = []

        # Append all of our inherited command line arguments from the editor.
        in_process_executor_settings = unreal.get_default_object(
            unreal.MoviePipelineInProcessExecutorSettings
        )
        inherited_cmds = in_process_executor_settings.inherited_command_line_arguments

        # Sanitize the commandline by removing any execcmds that may
        # have passed through the commandline.
        # We remove the execcmds because, in some cases, users may execute a
        # script that is local to their editor build for some automated
        # workflow but this is not ideal on the farm. We will expect all
        # custom startup commands for rendering to go through the `Start
        # Command` in the MRQ settings.
        inherited_cmds = re.sub(
            ".*(?P<cmds>-execcmds=[\s\S]+[\'\"])",
            "",
            inherited_cmds
        )

        command_args.extend(inherited_cmds.split(" "))
        command_args.extend(
            in_process_executor_settings.additional_command_line_arguments.split(
                " "
            )
        )

        # Get the project level preset
        project_preset = deadline_settings.default_job_preset

        # Get the job and plugin info string.
        # Note:
        #   Sometimes a project level default may not be set,
        #   so if this returns an empty dictionary, that is okay
        #   as we primarily care about the job level preset.
        #   Catch any exceptions here and continue
        try:
            project_job_info, project_plugin_info = get_deadline_info_from_preset(job_preset=project_preset)

        except Exception:
            pass

        deadline_service = get_global_deadline_service_instance()

        for job in self.pipeline_queue.get_jobs():

            # Don't send disabled jobs on Deadline
            if not job.is_enabled():
                unreal.log(f"Ignoring disabled Job `{job.job_name}`")
                continue

            # Don't process jobs without job preset assigned, it crashes the editor
            if not job.job_preset:
                unreal.log(f"Ignoring Job without DeadlineJobPreset assigned `{job.job_name}`")
                continue

            unreal.log(f"Submitting Job `{job.job_name}` to Deadline...")

            try:
                # Create a Deadline job object with the default project level
                # job info and plugin info
                deadline_job = DeadlineJob(project_job_info, project_plugin_info)

                deadline_job_id = self.submit_job(
                    job, deadline_job, command_args, deadline_service
                )

            except Exception as err:
                unreal.log_error(
                    f"Failed to submit job `{job.job_name}` to Deadline, aborting render. \n\tError: {str(err)}"
                )
                unreal.log_error(traceback.format_exc())
                self.on_executor_errored_impl(None, True, str(err))
                unreal.EditorDialog.show_message(
                    "Submission Result",
                    f"Failed to submit job `{job.job_name}` to Deadline with error:\n{str(err)}. "
                    f"See log for more details.",
                    unreal.AppMsgType.OK,
                )
                self.on_executor_finished_impl()
                return

            if not deadline_job_id:
                message = (
                    f"A problem occurred submitting `{job.job_name}`. "
                    f"Either the job doesn't have any data to submit, "
                    f"or an error occurred getting the Deadline JobID. "
                    f"This job status would not be reflected in the UI. "
                    f"Check the logs for more details."
                )
                unreal.log_warning(message)
                unreal.EditorDialog.show_message(
                    "Submission Result", message, unreal.AppMsgType.OK
                )
                return

            else:
                unreal.log(f"Deadline JobId: {deadline_job_id}")
                self.job_ids.append(deadline_job_id)

                # Store the Deadline JobId in our job (the one that exists in
                # the queue, not the duplicate) so we can match up Movie
                # Pipeline jobs with status updates from Deadline.
                job.user_data = deadline_job_id

        # Now that we've sent a job to Deadline, we're going to request a status
        # update on them so that they transition from "Ready" to "Queued" or
        # their actual status in Deadline. self.request_job_status_update(
        # deadline_service)
        message = ""
        if not len(self.job_ids):
            message = "No jobs were sent to Deadline, check if enabled."
        else:
            message = (
                f"Successfully submitted {len(self.job_ids)} jobs to Deadline. JobIds: {', '.join(self.job_ids)}. "
                f"\nPlease use Deadline Monitor to track render job statuses"
            )

        unreal.log(message)

        unreal.EditorDialog.show_message(
            "Submission Result", message, unreal.AppMsgType.OK
        )

        # Set the executor to finished
        self.on_executor_finished_impl()

    @unreal.ufunction(override=True)
    def is_rendering(self):
        # Because we forward unfinished jobs onto another service when the
        # button is pressed, they can always submit what is in the queue and
        # there's no need to block the queue.
        # A MoviePipelineExecutor implementation must override this. If you
        # override a ufunction from a base class you don't specify the return
        # type or parameter types.
        return False

    def submit_job(self, job, deadline_job, command_args, deadline_service):
        """
        Submit a new Job to Deadline
        :param job: Queued job to submit
        :param deadline_job: Deadline job object
        :param list[str] command_args: Commandline arguments to configure for the Deadline Job
        :param deadline_service: An instance of the deadline service object
        :returns: Deadline Job ID
        :rtype: str
        """

        # Get the Job Info and plugin Info
        # If we have a preset set on the job, get the deadline submission details
        try:
            job_info, plugin_info = get_deadline_info_from_preset(job_preset_struct=job.get_deadline_job_preset_struct_with_overrides())

        # Fail the submission if any errors occur
        except Exception as err:
            raise RuntimeError(
                f"An error occurred getting the deadline job and plugin "
                f"details. \n\tError: {err} "
            )

        # check for required fields in pluginInfo
        if "Executable" not in plugin_info:
            raise RuntimeError("An error occurred formatting the Plugin Info string. \n\tMissing \"Executable\" key")
        elif not plugin_info["Executable"]:
            raise RuntimeError(f"An error occurred formatting the Plugin Info string. \n\tExecutable value cannot be empty")
        if "ProjectFile" not in plugin_info:
            raise RuntimeError("An error occurred formatting the Plugin Info string. \n\tMissing \"ProjectFile\" key")
        elif not plugin_info["ProjectFile"]:
            raise RuntimeError(f"An error occurred formatting the Plugin Info string. \n\tProjectFile value cannot be empty")

        auxilliary_files = []

        # get PreJobScript and check that it is an existing full path
        pre_job_script = job_info.get('PreJobScript')
        if pre_job_script and not os.path.exists(pre_job_script):
            raise RuntimeError(f"PreJobScript path provided is not a valid path: {pre_job_script}")

        # default PreJobScript is PreJob.py file that is included in this plugin
        if not pre_job_script:
            job_info['PreJobScript'] = 'PreJob.py'
            auxilliary_files.append(os.path.join(os.path.dirname(__file__), "PreJob.py"))

        # check for Perforce required field in jobInfo
        environment_key_values = {}
        environment_key_indices = {}
        current_env_index = 0
        for key, value in job_info.items():
            if key.startswith('EnvironmentKeyValue'):
                sub_key, sub_val = value.split('=')
                environment_key_values[sub_key] = sub_val
                environment_key_indices[sub_key] = int(key.replace('EnvironmentKeyValue', ''))
                current_env_index += 1

        p4_cl = environment_key_values.get('P4_CL', -1)
        # p4_stream = environment_key_values.get('P4_stream', 'main')
        p4_workspace_prefix = environment_key_values.get('P4_workspace_prefix')

        # Connect to Perforce Server
        with p4.connect():
            # check changelist validity
            try:
                p4_cl = int(p4_cl)
                changelist = p4.run("changelist", "-o", p4_cl)

                # changelist may exists locally
                if changelist[0]['Status'] != 'submitted':
                    raise P4Exception("Changelist is not submitted.")

            except Exception as err:
                # get latest submitted CL and ask user if we should use latest submitted changelist instead
                latest_cl = p4.run_changes("-s", "submitted", "-m", "1")[0]
                result = unreal.EditorDialog.show_message(
                    "Deadline job submission",
                    f"Provided P4 CL ({p4_cl}) is invalid, do you want to use latest submitted CL number {latest_cl['change']} - {latest_cl['desc']} ?",
                    unreal.AppMsgType.YES_NO,
                    default_value=unreal.AppReturnType.YES
                )

                # replace environment variable with latest changelist number
                if result == unreal.AppReturnType.YES:
                    cl_index = environment_key_indices['P4_CL']
                    job_info[f"EnvironmentKeyValue{cl_index}"] = f"P4_CL={latest_cl['change']}"
                # else raise
                else:
                    unreal.log_error(err)
                    raise RuntimeError(
                        f"Provided P4 CL ({p4_cl}) is invalid, cannot create job."
                    )

            # TODO: check stream validity

        # if workspace prefix is not provided, use project name
        if not p4_workspace_prefix:
            project_path = unreal.Paths.get_project_file_path()
            p4_workspace_prefix, _ = os.path.splitext(os.path.basename(project_path))

            # add P4_workspace_prefix in job info
            job_info[f"EnvironmentKeyValue{current_env_index}"] = f"P4_workspace_prefix={p4_workspace_prefix}"
            current_env_index += 1

        # Update the job info with overrides from the UI
        if job.batch_name:
            job_info["BatchName"] = job.batch_name

        if hasattr(job, "comment") and not job_info.get("Comment"):
            job_info["Comment"] = job.comment

        if not job_info.get("Name") or job_info["Name"] == "Untitled":
            job_info["Name"] = job.job_name

        # Make sure a username is set
        # Priority to job.author, then job_info, finally session user
        username = job.author or job_info.get("UserName") or getpass.getuser()
        job.author = username
        job_info["UserName"] = username

        if unreal.Paths.is_project_file_path_set():
            # Trim down to just "Game.uproject" instead of absolute path.
            game_name_or_project_file = (
                unreal.Paths.convert_relative_path_to_full(
                    unreal.Paths.get_project_file_path()
                )
            )

        else:
            raise RuntimeError(
                "Failed to get a project name. Please set a project!"
            )

        # Create a new queue with only this job in it and save it to disk,
        # then load it, so we can send it with the REST API
        new_queue = unreal.MoviePipelineQueue()
        new_job = new_queue.duplicate_job(job)

        duplicated_queue, manifest_path = unreal.MoviePipelineEditorLibrary.save_queue_to_manifest_file(
            new_queue
        )

        # check that the config contains GameOverrides settings
        game_setting = new_job.get_configuration().find_setting_by_class(
            unreal.MoviePipelineGameOverrideSetting
        )
        if not game_setting:
            raise RuntimeError("Config should contain GameOverride settings.")

        # Convert the queue to text (load the serialized json from disk) so we
        # can send it via deadline, and deadline will write the queue to the
        # local machines on job startup.
        serialized_pipeline = unreal.MoviePipelineEditorLibrary.convert_manifest_file_to_string(
            manifest_path
        )

        # Loop through our settings in the job and let them modify the command
        # line arguments/params.
        new_job.get_configuration().initialize_transient_settings()
        # Look for our Game Override setting to pull the game mode to start
        # with. We start with this game mode even on a blank map to override
        # the project default from kicking in.
        game_override_class = None

        out_url_params = []
        out_command_line_args = []
        out_device_profile_cvars = []
        out_exec_cmds = []
        for setting in new_job.get_configuration().get_all_settings():

            out_url_params, out_command_line_args, out_device_profile_cvars, out_exec_cmds = setting.build_new_process_command_line_args(
                out_url_params,
                out_command_line_args,
                out_device_profile_cvars,
                out_exec_cmds,
            )

            # Set the game override
            if setting.get_class() == unreal.MoviePipelineGameOverrideSetting.static_class():
                game_override_class = setting.game_mode_override

        # custom prescript to execute stuff right when Unreal loads
        out_exec_cmds.append("py custom_unreal_prescript.py")

        # This triggers the editor to start looking for render jobs when it
        # finishes loading.
        out_exec_cmds.append("py mrq_rpc.py")

        # Convert the arrays of command line args, device profile cvars,
        # and exec cmds into actual commands for our command line.
        command_args.extend(out_command_line_args)

        if out_device_profile_cvars:
            # -dpcvars="arg0,arg1,..."
            command_args.append(
                '-dpcvars="{dpcvars}"'.format(
                    dpcvars=",".join(out_device_profile_cvars)
                )
            )

        if out_exec_cmds:
            # -execcmds="cmd0,cmd1,..."
            command_args.append(
                '-execcmds="{cmds}"'.format(cmds=",".join(out_exec_cmds))
            )

        # Add support for telling the remote process to wait for the
        # asset registry to complete synchronously
        command_args.append("-waitonassetregistry")

        # Build a shot-mask from this sequence, to split into the appropriate
        # number of tasks. Remove any already-disabled shots before we
        # generate a list, otherwise we make unneeded tasks which get sent to
        # machines
        shots_to_render = []
        shots_inner_name = []
        for shot_index, shot in enumerate(new_job.shot_info):
            if not shot.enabled:
                unreal.log(
                    f"Skipped submitting shot {shot_index} in {job.job_name} "
                    f"to server due to being already disabled!"
                )
            else:
                # check inner names, as those are not made unique by Unreal, while same outer names are appended with a number in braces..
                if shot.inner_name in shots_inner_name:
                    result = unreal.EditorDialog.show_message(
                        "Deadline job submission",
                        f"Found twice the same shot name ({shot.inner_name, shot.outer_name}), it may cause issues when writing files if using shot_name or frame_number_shot in folder and frame name."
                        "\nWould you like to submit anyway ?    Else, consider renaming the shots in the sequence (not the shot assets)",
                        unreal.AppMsgType.YES_NO,
                        default_value=unreal.AppReturnType.NO
                    )

                    # return if user chose to not send the job like this
                    if result != unreal.AppReturnType.YES:
                        unreal.log_error(f"Found twice the same shot name ({shot.inner_name, shot.outer_name})")
                        return

                shots_to_render.append(shot.outer_name)
                shots_inner_name.append(shot.inner_name)

        # If there are no shots enabled,
        # "these are not the droids we are looking for", move along ;)
        # We will catch this later and deal with it
        if not shots_to_render:
            unreal.log_warning("No shots enabled in shot mask, not submitting.")
            return

        # Divide the job to render by the chunk size
        # i.e {"O": "my_new_shot"} or {"0", "shot_1,shot_2,shot_4"}
        '''
        chunk_size = int(job_info.get("ChunkSize", 1))
        shots = {}
        frame_list = []
        for index in range(0, len(shots_to_render), chunk_size):

            shots[str(index)] = ",".join(shots_to_render[index : index + chunk_size])

            frame_list.append(str(index))

        job_info["Frames"] = ",".join(frame_list)
        '''

        # Divide the job to render by the chunk size
        # ChunkSize is counted in frames, and the goal is to create tasks of 1 or more shots
        # totalling approximatively ChunkSize frames
        # NOTE: could automagically determine ChunkSize based on the different shots length ?
        target_size = int(job_info.get("ChunkSize", 100))

        # force ChunkSize to a large number, to prevent Deadline from splitting the tasks more than what we give
        job_info["ChunkSize"] = "1000000"

        # get sequence and create frame list for deadline
        sequence = unreal.SystemLibrary.conv_soft_obj_path_to_soft_obj_ref(new_job.sequence)
        shots, frame_list, has_frame_range = create_shot_list(sequence, shots_to_render, target_size)

        job_info["Frames"] = ",".join(frame_list)
        unreal.log(f'frame list: {job_info["Frames"]}')

        # enable frame timeout, so that task timeout changes based on actual frame count
        # TODO: add on Job Preset object
        if has_frame_range:
            job_info["EnableFrameTimeouts"] = "1"
        # not using frame ranges, so increase task timeout
        else:
            job_info["TaskTimeoutSeconds"] = "3600"
            job_info["EnableFrameTimeouts"] = "0"

        # Get the current index of the ExtraInfoKeyValue pair, we will
        # increment the index, so we do not stomp other settings
        extra_info_key_indexs = set()
        for key in job_info.keys():
            if key.startswith("ExtraInfoKeyValue"):
                _, index = key.split("ExtraInfoKeyValue")
                extra_info_key_indexs.add(int(index))

        # Get the highest number in the index list and increment the number
        # by one
        current_index = max(extra_info_key_indexs) + 1 if extra_info_key_indexs else 0

        # Put the serialized Queue into the Job data but hidden from
        # Deadline UI
        job_info[f"ExtraInfoKeyValue{current_index}"] = f"serialized_pipeline={serialized_pipeline}"

        # Increment the index
        current_index += 1

        # Put the shot info in the job extra info keys
        job_info[f"ExtraInfoKeyValue{current_index}"] = f"shot_info={json.dumps(shots)}"
        current_index += 1

        # Set the job output directory override on the deadline job
        if hasattr(new_job, "output_directory_override"):
            if new_job.output_directory_override.path:
                job_info[f"ExtraInfoKeyValue{current_index}"] = f"output_directory_override={new_job.output_directory_override.path}"

                current_index += 1
            else:
                raise RuntimeError("Output directory override cannot be empty.")

        # Set the job filename format override on the deadline job
        if hasattr(new_job, "filename_format_override"):
            if new_job.filename_format_override:
                job_info[f"ExtraInfoKeyValue{current_index}"] = f"filename_format_override={new_job.filename_format_override}"

                current_index += 1

        # Tell Deadline job about output directory and filename
        output_setting = new_job.get_configuration().find_setting_by_class(
            unreal.MoviePipelineOutputSetting
        )

        # TODO: Resolve path formatting based on render settings to make it understandable by Deadline
        job_info["OutputDirectory0"] = new_job.output_directory_override.path or output_setting.output_directory.path

        # TODO: Resolve filename format based on render settings to make it understandable by Deadline
        job_info["OutputFilename0"] = new_job.filename_format_override or output_setting.file_name_format

        # add map path to job environment, to be used to preload it
        map_path = unreal.SystemLibrary.conv_soft_obj_path_to_soft_obj_ref(new_job.map).get_path_name()
        job_info[f"EnvironmentKeyValue{current_env_index}"] = f"UEMAP_PATH={map_path}"
        current_env_index += 1

        # Force override of output files, to avoid multiple files when task fails and restart
        job_info[f"EnvironmentKeyValue{current_env_index}"] = f"override_output=1"
        current_env_index += 1

        command_args.extend(["-nohmd", "-windowed"])

        # Add resolution to commandline arguments
        # to force considering render settings' output resolution instead of limiting to screen resolution
        output_resolution = output_setting.output_resolution
        command_args.append(f"-ResX={output_resolution.x}")
        command_args.append(f"-ResY={output_resolution.y}")
        command_args.append(f"-ForceRes")

        # Build the command line arguments the remote machine will use.
        # The Deadline plugin will provide the executable since it is local to
        # the machine. It will also write out queue manifest to the correct
        # location relative to the Saved folder

        # Get the current commandline args from the plugin info
        plugin_info_cmd_args = [plugin_info.get("CommandLineArguments", "")]

        if not plugin_info.get("ProjectFile"):
            project_file = plugin_info.get("ProjectFile", game_name_or_project_file)
            plugin_info["ProjectFile"] = project_file

        # This is the map included in the plugin to boot up to.
        project_cmd_args = [
            f"MoviePipelineEntryMap?game={game_override_class.get_path_name()}"
        ]

        # Combine all the compiled arguments
        full_cmd_args = project_cmd_args + command_args + plugin_info_cmd_args

        # Remove any duplicates in the commandline args and convert to a string
        full_cmd_args = " ".join(list(OrderedDict.fromkeys(full_cmd_args))).strip()

        unreal.log(f"Deadline job command line args: {full_cmd_args}")

        # Update the plugin info with the commandline arguments
        plugin_info.update(
            {
                "CommandLineArguments": full_cmd_args,
                "CommandLineMode": plugin_info.get("CommandLineMode", "false"),
            }
        )

        # add auxilliary files to the job, if any
        if auxilliary_files:
            job_info['AuxFiles'] = auxilliary_files

        deadline_job.job_info = job_info
        deadline_job.plugin_info = plugin_info

        # Submit the deadline job
        return deadline_service.submit_job(deadline_job)

    # TODO: For performance reasons, we will skip updating the UI and request
    #  that users use a different mechanism for checking on job statuses.
    #  This will be updated once we have a performant solution.
