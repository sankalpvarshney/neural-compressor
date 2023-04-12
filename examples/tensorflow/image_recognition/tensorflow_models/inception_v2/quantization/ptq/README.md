Step-by-Step
============

This document list steps of reproducing Intel inception_v2 model tuning and benchmark results via Neural Compressor.
This example can run on Intel CPUs and GPUs.

> **Note**: 
> Most of those models are both supported in Intel optimized TF 1.15.x and Intel optimized TF 2.x. Validated TensorFlow [Version](/docs/source/installation_guide.md#validated-software-environment).
# Prerequisite

## 1. Environment

### Installation
Recommend python 3.6 or higher version.

```shell
# Install Intel® Neural Compressor
pip install neural-compressor
```

### Install Intel Tensorflow
```shell
pip install intel-tensorflow
```

### Installation Dependency packages
```shell
cd examples/tensorflow/object_detection/tensorflow_models/quantization/ptq
pip install -r requirements.txt
```

### Install Intel Extension for Tensorflow
#### Quantizing the model on Intel GPU
Intel Extension for Tensorflow is mandatory to be installed for quantizing the model on Intel GPUs.

```shell
pip install --upgrade intel-extension-for-tensorflow[gpu]
```
For any more details, please follow the procedure in [install-gpu-drivers](https://github.com/intel-innersource/frameworks.ai.infrastructure.intel-extension-for-tensorflow.intel-extension-for-tensorflow/blob/master/docs/install/install_for_gpu.md#install-gpu-drivers)

#### Quantizing the model on Intel CPU(Experimental)
Intel Extension for Tensorflow for Intel CPUs is experimental currently. It's not mandatory for quantizing the model on Intel CPUs.

```shell
pip install --upgrade intel-extension-for-tensorflow[cpu]
```

## 2. Prepare pre-trained model
The inception_v2 checkpoint file comes from [models](https://github.com/tensorflow/models/tree/master/research/slim#pre-trained-models).
We can get the pb file by convert the checkpoint file.

  1. Download the checkpoint file from [here](https://github.com/tensorflow/models/tree/master/research/slim#pre-trained-models)
  ```shell
  wget http://download.tensorflow.org/models/inception_v2_2016_08_28.tar.gz
  tar -xvf inception_v2_2016_08_28.tar.gz
  ```

  2. Exporting the Inference Graph
  ```shell
  git clone https://github.com/tensorflow/models
  cd models/research/slim
  python export_inference_graph.py \
          --alsologtostderr \
          --model_name=inception_v2 \
          --output_file=/tmp/inception_v2_inf_graph.pb
  ```
  Make sure to use intel-tensorflow v1.15, and pip install tf_slim.
  #### Install Intel Tensorflow 1.15 up2
  Check your python version and use pip install 1.15.0 up2 from links below:
  https://storage.googleapis.com/intel-optimized-tensorflow/intel_tensorflow-1.15.0up2-cp36-cp36m-manylinux2010_x86_64.whl                
  https://storage.googleapis.com/intel-optimized-tensorflow/intel_tensorflow-1.15.0up2-cp37-cp37m-manylinux2010_x86_64.whl
  https://storage.googleapis.com/intel-optimized-tensorflow/intel_tensorflow-1.15.0up2-cp35-cp35m-manylinux2010_x86_64.whl
  > Please note: The ImageNet dataset has 1001, the **VGG** and **ResNet V1** final layers have only 1000 outputs rather than 1001. So we need add the `--labels_offset=1` flag in the inference graph exporting command.

  3. Use [Netron](https://lutzroeder.github.io/netron/) to get the input/output layer name of inference graph pb, for vgg_16 the output layer name is `InceptionV2/Predictions/Reshape_1`

  4. Freezing the exported Graph, please use the tool `freeze_graph.py` in [tensorflow v1.15.2](https://github.com/tensorflow/tensorflow/blob/v1.15.2/tensorflow/python/tools/freeze_graph.py) repo 
  ```shell
  python freeze_graph.py \
          --input_graph=/tmp/inception_v2_inf_graph.pb \
          --input_checkpoint=./inception_v2.ckpt \
          --input_binary=true \
          --output_graph=./frozen_inception_v2.pb \
          --output_node_names=InceptionV2/Predictions/Reshape_1
  ```

## 3. Prepare Dataset

  TensorFlow [models](https://github.com/tensorflow/models) repo provides [scripts and instructions](https://github.com/tensorflow/models/tree/master/research/slim#an-automated-script-for-processing-imagenet-data) to download, process and convert the ImageNet dataset to the TF records format.
  We also prepared related scripts in ` examples/tensorflow/image_recognition/tensorflow_models/imagenet_prepare` directory. To download the raw images, the user must create an account with image-net.org. If you have downloaded the raw data and preprocessed the validation data by moving the images into the appropriate sub-directory based on the label (synset) of the image. we can use below command ro convert it to tf records format.

  ```shell
  cd examples/tensorflow/image_recognition/tensorflow_models/
  # convert validation subset
  bash prepare_dataset.sh --output_dir=./inception_v2/quantization/ptq/data --raw_dir=/PATH/TO/img_raw/val/ --subset=validation
  # convert train subset
  bash prepare_dataset.sh --output_dir=./inception_v2/quantization/ptq/data --raw_dir=/PATH/TO/img_raw/train/ --subset=train
  ```

# Run

## 1 Quantization

  ```shell
  cd examples/tensorflow/image_recognition/tensorflow_models/inception_v2/quantization/ptq
  bash run_tuning.sh --input_model=/PATH/TO/frozen_inception_v2.pb \
      --output_model=./nc_inception_v2.pb --dataset_location=/path/to/ImageNet/
  ```

## 2. Benchmark
  ```shell
  cd examples/tensorflow/image_recognition/tensorflow_models/inception_v2/quantization/ptq
  bash run_benchmark.sh --input_model=./nc_inception_v2.pb --mode=accuracy --dataset_location=/path/to/ImageNet/ --batch_size=32
  bash run_benchmark.sh --input_model=./nc_inception_v2.pb --mode=performance --dataset_location=/path/to/ImageNet/ --batch_size=1
  ```
