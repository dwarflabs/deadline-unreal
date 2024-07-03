import unreal
import os


unreal.log_warning("running custom_unreal_prescript")


map_path = os.environ.get("UEMAP_PATH")

# preload the map defined by environment variable UEMAP_PATH
# this is done to prevent rendering issues due to unfinished loading
if map_path:
    unreal.log_warning(f"preloading map {map_path}")
    unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level(map_path)


unreal.log_warning("custom_unreal_prescript finished")
