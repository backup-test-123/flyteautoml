import os
import ujson
import math
import random
import shutil
import itertools

from flytekit.sdk.workflow import workflow_class, Output, Input
from flytekit.sdk.types import Types
from flytekit.sdk.tasks import python_task, inputs, outputs
from flytekit.common import utils as flytekit_utils

from models.classifier.resnet50.train_resnet50 import train_resnet50_model, download_data
from utils.flyte_utils.fetch_executions import fetch_workflow_latest_execution, fetch_workflow_execution
from utils.flyte_utils.collect_blobs import collect_blobs

from models.classifier.resnet50.constants import DEFAULT_IMG_SIZE
from models.classifier.resnet50.constants import DEFAULT_BATCH_SIZE
from models.classifier.resnet50.constants import DEFAULT_PATIENCE
from models.classifier.resnet50.constants import DEFAULT_EPOCHS
from models.classifier.resnet50.constants import DEFAULT_WEIGHTS

DEFAULT_PROJECT_NAME = "flytekubecondemo2019"
DATAPREP_WORKFLOW_NAME = "workflows.data_preparation_workflow.DataPreparationWorkflow"
DEFAULT_DOMAIN = "development"

DEFAULT_VALIDATION_DATA_RATIO = 0.2
PURPOSES = ['training', 'validation']

DEFAULT_TRAINING_VALIDATION_CONFIG_FILE = "models/classifier/resnet50/configs/model_training_config_demo.json"
DEFAULT_DATAPREP_WF_EXECUTION_ID = "ff25dd48a39934dc5b96"  # staging
# DEFAULT_DATAPREP_WF_EXECUTION_ID = "fab5d832671ec4c31819"  # prod

def split_training_validation_streams(labeled_streams, validation_data_ratio):
    n_validation_streams = {
        c: int(math.ceil(len(labeled_streams[c]) * validation_data_ratio)) for c in labeled_streams.keys()
    }
    for _, s in labeled_streams.items():
        random.shuffle(s)

    validation_streams = {c: labeled_streams[c][:n_validation_streams[c]] for c in labeled_streams.keys()}
    training_streams = {c: labeled_streams[c][n_validation_streams[c]:] for c in labeled_streams.keys()}

    return {"training": training_streams, "validation": validation_streams}


@inputs(
    training_validation_config_json=Types.Generic,
    streams_metadata_path=Types.String,  # The path to a json file listing the metadata (e.g., class) of each stream
    validation_data_ratio=Types.Float,
)
@outputs(
    training_clean_mpblob=Types.MultiPartBlob,
    training_dirty_mpblob=Types.MultiPartBlob,
    validation_clean_mpblob=Types.MultiPartBlob,
    validation_dirty_mpblob=Types.MultiPartBlob,
)
@python_task(cache=True, cache_version="4")
def rearrange_data(
        wf_params,
        training_validation_config_json,
        streams_metadata_path,
        validation_data_ratio,
        training_clean_mpblob,
        training_dirty_mpblob,
        validation_clean_mpblob,
        validation_dirty_mpblob,
):
    # Get the latest execution of the data_prep_workflow
    dataprep_wf_execution = fetch_workflow_execution(
        project=DEFAULT_PROJECT_NAME, domain=DEFAULT_DOMAIN, exec_id=DEFAULT_DATAPREP_WF_EXECUTION_ID)

    print("Data Prep Workflow:")
    print(dataprep_wf_execution)

    available_streams_mpblobs = dataprep_wf_execution.outputs["selected_frames_mpblobs"]
    available_streams_names = dataprep_wf_execution.outputs["streams_names_out"]

    streams_metadata_blob = Types.Blob.fetch(remote_path=streams_metadata_path)
    metadata_fp = open(streams_metadata_blob.local_path)
    streams_metadata = ujson.load(metadata_fp)

    all_streams = streams_metadata.get("streams", {})
    wf_params.logging.info("all streams from metadata: ")
    wf_params.logging.info(all_streams)
    selections = training_validation_config_json.get("train_validation_datasets", {})
    wf_params.logging.info("selections: ")
    wf_params.logging.info(selections)
    training_validation_streams = [{"stream": name, "class": metadata["class"]} for name, metadata in all_streams.items()
                                   if name in selections.keys()]

    # Splitting the set of streams into validation and training
    streams = {
        "clean": [s["stream"] for s in training_validation_streams if s["class"] == "clean"],
        "dirty": [s["stream"] for s in training_validation_streams if s["class"] == "dirty"],
    }
    split_streams = split_training_validation_streams(streams, validation_data_ratio)

    print("training_streams:")
    print(split_streams['training'])
    print("validation_streams:")
    print(split_streams['validation'])

    # Download multipartblobs to the target folders and then upload it
    # final_mpblobs = {k: {} for k in PURPOSES}
    final_mpblobs = {
        'training': {
            'dirty': training_dirty_mpblob,
            'clean': training_clean_mpblob,
        },
        'validation': {
            'dirty': validation_dirty_mpblob,
            'clean': validation_clean_mpblob,
        },
    }
    for purpose, label in itertools.product(PURPOSES, streams.keys()):
        with flytekit_utils.AutoDeletingTempDir() as output_dir:
            for stream in split_streams[purpose][label]:
                idx = available_streams_names.index(stream)
                mpblob = available_streams_mpblobs[idx]
                mpblob.download()
                files = os.listdir(mpblob.local_path)
                for f in files:
                    shutil.move(os.path.join(mpblob.local_path, f), output_dir.name)
                files = os.listdir(output_dir.name)
                print("There are {} files in output dir {} ({}:{})".format(len(files), output_dir.name, purpose, label))
            final_mpblobs[purpose][label].set(output_dir.name)

            # final_mpblobs[purpose][label] = Types.MultiPartBlob.from_python_std(output_dir.name)  # TODO: Matt
            #final_mpblobs[purpose][label] = output_dir.name

    # training_clean_mpblob.set(final_mpblobs['training']['clean'].remote_location)
    # training_dirty_mpblob.set(final_mpblobs['training']['dirty'].remote_location)
    # validation_dirty_mpblob.set(final_mpblobs['validation']['dirty'].remote_location)
    # validation_clean_mpblob.set(final_mpblobs['validation']['clean'].remote_location)


@inputs(
    training_clean_mpblob=Types.MultiPartBlob,
    training_dirty_mpblob=Types.MultiPartBlob,
    validation_clean_mpblob=Types.MultiPartBlob,
    validation_dirty_mpblob=Types.MultiPartBlob,
)
@outputs(
    model_blobs=[Types.Blob],
    model_files_names=[Types.String],
)
@python_task(cache=True, cache_version="2", gpu_request="1", gpu_limit="1", memory_request="64Gi")
def train_on_datasets(
        wf_params,
        training_clean_mpblob,
        validation_clean_mpblob,
        training_dirty_mpblob,
        validation_dirty_mpblob,
        model_blobs,
        model_files_names,
):

    with flytekit_utils.AutoDeletingTempDir("output_models") as output_models_dir:
        with flytekit_utils.AutoDeletingTempDir("training") as training_dir:
            with flytekit_utils.AutoDeletingTempDir("validation") as validation_dir:
                download_data(training_dir.name, {"clean": training_clean_mpblob, "dirty": training_dirty_mpblob})
                download_data(validation_dir.name, {"clean": validation_clean_mpblob, "dirty": validation_dirty_mpblob})

                train_resnet50_model(
                    train_directory=training_dir.name,
                    validation_directory=validation_dir.name,
                    output_model_folder=output_models_dir.name,
                    logger=wf_params.logging,
                    patience=DEFAULT_PATIENCE,
                    size=DEFAULT_IMG_SIZE,
                    batch_size=DEFAULT_BATCH_SIZE,
                    epochs=DEFAULT_EPOCHS,
                    weights=DEFAULT_WEIGHTS,
                )
                # save results to Workflow output
                blobs, files_names_list = collect_blobs(output_models_dir.name)
                model_blobs.set(blobs)
                model_files_names.set(files_names_list)

    """
    # write results to storage path also
    for file in files_names_list:
        location = model_output_path + file
        out_blob = Types.Blob.create_at_known_location(location)

        with out_blob as out_writer:
            with open(output_folder + "/" + file, mode="rb") as in_reader:
                out_writer.write(in_reader.read())

    # keep the model_config with the trained model
    location = model_output_path + MODEL_CONFIG_FILE_NAME
    out_blob = Types.Blob.create_at_known_location(location)
    with out_blob as out_writer:
        out_writer.write((model_config_string).encode("utf-8"))

    # write metadata to track what execution this was done by
    location = model_output_path + MODEL_GENERATED_BY_FILE_NAME
    out_blob = Types.Blob.create_at_known_location(location)
    with out_blob as out_writer:
        out_writer.write((f"workflow_id: {wf_params.execution_id}").encode("utf-8"))
    """


@workflow_class
class ClassifierTrainWorkflow:
    streams_metadata_path = Input(Types.String, required=True)
    training_validation_config_json = Input(Types.Generic, default=ujson.loads(open(DEFAULT_TRAINING_VALIDATION_CONFIG_FILE).read()))
    validation_data_ratio = Input(Types.Float, default=DEFAULT_VALIDATION_DATA_RATIO)

    rearrange_data_task = rearrange_data(
        training_validation_config_json=training_validation_config_json,
        streams_metadata_path=streams_metadata_path,
        validation_data_ratio=validation_data_ratio,
    )

    train_on_datasets_task = train_on_datasets(
        training_clean_mpblob=rearrange_data_task.outputs.training_clean_mpblob,
        training_dirty_mpblob=rearrange_data_task.outputs.training_dirty_mpblob,
        validation_clean_mpblob=rearrange_data_task.outputs.validation_clean_mpblob,
        validation_dirty_mpblob=rearrange_data_task.outputs.validation_dirty_mpblob,
    )

    trained_models = Output(train_on_datasets_task.outputs.model_blobs, sdk_type=[Types.Blob])
    model_file_names = Output(train_on_datasets_task.outputs.model_files_names, sdk_type=[Types.String])

    # ------------------------------------------------------------
    """
    interactive_validate_model_config_task = interactive_validate_model_config(
        model_config_path=model_config_path
    )

    download_and_prepare_datasets_task = download_and_prepare_datasets(
        model_config_string=interactive_validate_model_config_task.outputs.model_config_string
    )

    train_on_datasets_task = train_on_datasets(
        train_data_mpblobs=download_and_prepare_datasets_task.outputs.train_data_mpblobs,
        validation_data_mpblobs=download_and_prepare_datasets_task.outputs.validate_zips_out,
        model_config_string=interactive_validate_model_config_task.outputs.model_config_string,
        model_output_path=model_output_path,
    )
    """

train_lp = ClassifierTrainWorkflow.create_launch_plan()

