# Copyright Epic Games, Inc. All Rights Reserved

"""
This script handles processing jobs and shots in the current loaded queue
"""
import unreal
import os

from .utils import (
    movie_pipeline_queue,
    execute_render,
    setup_remote_render_jobs,
    update_render_output
)


def setup_render_parser(subparser):
    """
    This method adds a custom execution function and args to a render subparser

    :param subparser: Subparser for processing custom sequences
    """

    # Function to process arguments
    subparser.set_defaults(func=_process_args)


def render_jobs(
    is_remote=False,
    is_cmdline=False,
    executor_instance=None,
    remote_batch_name=None,
    remote_job_preset=None,
    output_dir_override=None,
    output_filename_override=None
):
    """
    This renders the current state of the queue

    :param bool is_remote: Is this a remote render
    :param bool is_cmdline: Is this a commandline render
    :param executor_instance: Movie Pipeline Executor instance
    :param str remote_batch_name: Batch name for remote renders
    :param str remote_job_preset: Remote render job preset
    :param str output_dir_override: Movie Pipeline output directory override
    :param str output_filename_override: Movie Pipeline filename format override
    :return: MRQ executor
    """

    if not movie_pipeline_queue.get_jobs():
        # Make sure we have jobs in the queue to work with
        raise RuntimeError("There are no jobs in the queue!!")

    # Update the job
    for job in movie_pipeline_queue.get_jobs():
        config = job.get_configuration()

        # Override texture streaming method
        texture_streaming_override = os.environ.get("texture_streaming_override")
        if texture_streaming_override:
            game_setting = config.find_setting_by_class(
                unreal.MoviePipelineGameOverrideSetting
            )

            if texture_streaming_override == "Disabled":
                game_setting.texture_streaming = unreal.MoviePipelineTextureStreamingMethod.DISABLED
            elif texture_streaming_override == "FullyLoad":
                game_setting.texture_streaming = unreal.MoviePipelineTextureStreamingMethod.FULLY_LOAD
            elif texture_streaming_override == "None":
                game_setting.texture_streaming = unreal.MoviePipelineTextureStreamingMethod.NONE

        # get override output
        override_output = os.environ.get("override_output")
        if override_output != None:
            override_output = int(override_output)

        # update output settings
        update_render_output(
            job,
            output_dir=output_dir_override,
            output_filename=output_filename_override,
            override_output=override_output,
        )

        # Get the job output settings
        output_setting = config.find_setting_by_class(
            unreal.MoviePipelineOutputSetting
        )

        # Allow flushing flies to disk per shot.
        # Required for the OnIndividualShotFinishedCallback to get called.
        output_setting.flush_disk_writes_per_shot = True

        # Workaround to make the config dirty so that the overrides are actually accounted for.
        # Modifying directly the various settings class does not make the config dirty
        # and when rendering starts, the config is reset to the default saved one.
        job.set_configuration(config)

    if is_remote:
        setup_remote_render_jobs(
            remote_batch_name,
            remote_job_preset,
            movie_pipeline_queue.get_jobs(),
        )

    try:
        # Execute the render.
        # This will execute the render based on whether its remote or local
        executor = execute_render(
            is_remote,
            executor_instance=executor_instance,
            is_cmdline=is_cmdline,
        )

    except Exception as err:
        unreal.log_error(
            f"An error occurred executing the render.\n\tError: {err}"
        )
        raise

    return executor


def _process_args(args):
    """
    Function to process the arguments for the sequence subcommand
    :param args: Parsed Arguments from parser
    """

    return render_jobs(
        is_remote=args.remote,
        is_cmdline=args.cmdline,
        remote_batch_name=args.batch_name,
        remote_job_preset=args.deadline_job_preset,
        output_dir_override=args.output_override,
        output_filename_override=args.filename_override
    )
