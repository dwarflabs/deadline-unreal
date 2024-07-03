# deadline-unreal
Deadline and Unreal plugins to send Unreal jobs to Deadline.


Improved version of the default plugins provided with Deadline 10.3.1.3.
Branch `main` is the unmodified code, and branch `dev` is our version.

We do not own any of it nor intend to actively maintain it.

Feel free to fork and improve it as you need :)


# Setup

## Unreal

Unreal side, there are 2 plugins (**MoviePipelineDeadline** and **UnrealDeadlineService**, located in `UnrealEnginePlugins`) that you need to add in your projet and compile.

In the project settings, you need to modify the **Default Remote Executor** and **Default Executor job** (in `Plugins -> Movie render pipeline`) to use the Deadline version of them: **MoviePipelineDeadlineRemoteExecutor** and **MoviePipelineDeadlineExecutorJob**.
You can also define a default **Deadline job preset** in the project settings, in `Plugins -> Movie Pipeline Deadline`.
This asset defines default values for most of the Deadline job options, and allows to choose what options can or cannot be overriden by the user from the MRQ (though we had issues making Unreal correctly do that and ended up modifying the default shown properties in the c++ directly..).
There is an example of such asset in the repository: **DJP_DeadlineJobPresetExample_EditorMode.uasset**.

There are 2 types of Deadline Unreal jobs defined by the boolean option `CommandLineMode`:
- When **True**, the task will launch Unreal in game mode and will render whatever the manifest file in the arguments tells it to do. There is no back-and-forth communication possible between Deadline and Unreal.
This mode doesn't launch the **editor** part of Unreal, this means blueprint nodes that are **editor** will not be working.
- When **False**, the task will launch Unreal in editor mode (exactly like with the UI) and start an RPC server(**Deadline**)/client(**Unreal**) which allows communication. Unreal will ask Deadline what it should render. This allows to keep Unreal open between tasks of the same job. In editor mode, all blueprint editor nodes will correctly work. You can add the `-renderoffscreen` argument (in **CommandLineArguments**) to allow running this job on a farm as a service with no UI.

There is a dependancy on Perforce's python API. When sending a job to Deadline, the user needs to specify the Perforce's changelist id (CL) that the worker should sync to in order to do the render with the correct version of the project. This CL is then checked against Perforce to ensure its validity, and propose the user to use the last valid one if the provided CL is not valid.

We also implemented a feature of **shot packing**, that will try to group small shots (few frames) together in a same task. The goal is to capitalize on Unreal opening time by rendering multiple shots with the same Unreal instance. It also helps optimizing render time by reducing render time differences between tasks.

The most important file if you need to make modifications is `remote_executor.py`, which is what is called when you press "Render remote" in Unreal and will create and send the job to Deadline.
`PreJob.py` and `custom_unreal_prescript.py` are also useful to modify.


## Deadline

Deadline side, there is also the dependency to Perforce's python API. We added it to pythonsync3.zip archive so that it is deployed on all farmers, but you could just make sure it's deployed somewhere reachable by the farmers and add it to the **PYTHONPATH**.

Relative to Perforce, the `JobPreLoad.py` will try to sync the farmer's Perforce repository based on the job's CL.
To retrieve the local Perforce workspace, we use a pattern to match existing workspaces. You probably will need to adapt this part to your own pattern.


# How to use

Once Unreal setup is done (and restarted), in the **MRQ** window there will now be a **Deadline** section for each job that are added to it.
There you can specify some info for the job, and there is an entry for a **Deadline job preset** asset that is mandatory.
To override any values of the preset, you need to check the checkbox in front of the option you're overriding.
**MoviePipelineGameOverrideSetting** needs to be enabled in the configuration of the job.

Click on **Render remote** and that's it :)


# List of changes

- Completed the implementation of **UnrealEngineCmdManagedProcess** (command line mode for rendering), mainly writing the manifest file to the local project and adding it to the command line.
- Forced command line argument `-renderoffscreen`, as typical renderfarm worker do not have the UI setup and live render preview is unnecessary.
- Corrected plugin info entry **CommandLineMode**, to allow choosing the opening/render mode for Unreal jobs (command line or editor).
- Added a **JobPreLoad** that will sync the local Perforce repository based on the CL provided for the job.
- Added a **PreJob**, to write a manifest file in the output directory for debug/info purpose. The manifest is with overrides applied.
- Added a custom pre-script to run at Unreal's opening. Allows to do some process before the render tasks starts.
- Added the sequence's map path to the job info so the pre-script can open it early, preventing issues with unfinished loading of meshes or textures (that would not even load at later frames or with lots of warmup frames).
- Fixed applying overrides to the Unreal job configuration (like output directory and filename). Modifying the configuration does not dirty it, and Unreal ignores overrides if it is not dirty.
- Added override capabilities for texture streaming method and output files override ("Override Existing Output") through environment variables on the job.
- Prevented processing of disabled Unreal jobs.
- Fixed a crash when no job preset was assigned.
- Added custom environment variables for Perforce info.
- Fixed job's username.
- Added a raise when **MoviePipelineGameOverrideSetting** is not enabled. It is usually wanted when rendering cinematic quality images.
- Added a check for identical shots within the same sequence.
- Added a raise when output directory override is not provided, as the default is usually local to the project.
- Included output directory and filename in the job info, enabling Deadline to provide the associated right-click context options.
- Forced setting "Override Existing Output" to **True**. This ensures that if a task starts rendering images and then crashes, it will override the existing images upon restarting instead of creating new images with number offsets. (`shot_name.f1001.png(2)`)
- Forced command line resolution arguments to match the actual output image resolution. Also added `-ForceRes` to force Unreal to consider this resolution instead of the rendering machine's resolution.
- Added a method to pack multiple shots into one task based on **ChunkSize** to balance frames-per-task in the job.
- Added frame ranges to tasks. In case of shot packing, frame ranges will not reflect the actual frame start and end, but will reflect the real frame count.
- Enabled frame timeout (when frame ranges are provided), to allow a more precise timeout management as shots can have widely different frame counts to render.
- Fixed **DeadlineJobPreset** overrides to not apply correctly when adding a job in the **MRQ**.
- Fixed **DefaultJobPreset** to not be saved in the config.
- Fixed auxilliary files key syntax in the Deadline command for sending jobs.


# Further improvements ideas

- Add support for Perforce streams. Current implementation is not ideal because changes needed for a render have to be pushed on the main branch, making them the new default for every users while sender may just want to "test" stuff without officially pushing that to the others.
 
- Find a way to indicate progress to Deadline. Custom executor ? Found this too: `unreal.MoviePipelineLibrary.get_completion_percentage()`

- Add an option to write images local to the farmer, then copy to final destination on task end.

- Implement an override to overwrite output frame range.

- Run a post job script that would compute frames render time and write a file that could be used for statistics.

- Make an option to opt-in/out of shot packing.

- Make an option for timeout type (frame or global).

- Improve error catching. Right now when Unreal crash, the job continues until the timeout stops it.
