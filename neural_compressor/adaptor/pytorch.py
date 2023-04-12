#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import gc
import math
import os
from collections import OrderedDict, UserDict, namedtuple
from packaging.version import Version
import yaml
from functools import partial
from neural_compressor.utils.utility import dump_elapsed_time
from .adaptor import adaptor_registry, Adaptor
from ..utils.utility import LazyImport, CpuInfo, GLOBAL_STATE, MODE
from ..utils.utility import Statistics
from ..utils import logger
from .query import QueryBackendCapability
from ..data.dataloaders.base_dataloader import BaseDataLoader
from .torch_utils.smooth_quant import TorchSmoothQuant
torch = LazyImport("torch")
json = LazyImport("json")
hvd = LazyImport("horovod.torch")
torch_utils = LazyImport("neural_compressor.adaptor.torch_utils")
ipex = LazyImport("intel_extension_for_pytorch")

REDUCE_RANGE = False if CpuInfo().vnni else True
logger.debug("Reduce range is {}".format(str(REDUCE_RANGE)))


def get_torch_version():
    try:
        torch_version = torch.__version__.split('+')[0]
    except ValueError as e:  # pragma: no cover
        assert False, 'Got an unknown version of torch: {}'.format(e)
    version = Version(torch_version)
    return version


def get_torch_white_list(approach):
    version = get_torch_version()
    import torch.quantization as tq
    if version.release < Version("1.7.0").release:  # pragma: no cover
        white_list = \
            set(tq.default_mappings.DEFAULT_DYNAMIC_MODULE_MAPPING.keys()) \
            if approach == 'post_training_dynamic_quant' else \
            tq.default_mappings.DEFAULT_QCONFIG_PROPAGATE_WHITE_LIST
    elif version.release < Version("1.8.0").release:  # pragma: no cover
        white_list = \
            set(tq.quantization_mappings.get_dynamic_quant_module_mappings().keys()) \
            if approach == 'post_training_dynamic_quant' else \
            tq.quantization_mappings.get_qconfig_propagation_list()
    else:
        white_list = \
            set(tq.quantization_mappings.get_default_dynamic_quant_module_mappings().keys()) \
            if approach == 'post_training_dynamic_quant' else \
            tq.quantization_mappings.get_default_qconfig_propagation_list()
    return white_list


def pytorch_forward_wrapper(model, input, device='cpu', conf=None, running_mode='inference'):
    version = get_torch_version()
    if isinstance(input, dict) or isinstance(input, UserDict):
        if device == 'cpu':
            output = model(**input)
        elif device == 'ipex':  # pragma: no cover
            # have to split the case to avoid exposing ipex.DEVICE outside
            # which require intel extension installed
            if version.release < Version("1.12.0").release:
                if running_mode == "calibration":
                    with ipex.quantization.calibrate(conf, default_recipe=True):   # pylint: disable=E1101
                        output = model(**input)
                else:
                    output = model(**input)
            else:
                output = model(**input)
        else:  # pragma: no cover
            for inp in input.keys():
                input[inp] = input[inp].to("dpcpp" if device=="gpu" else device) \
                    if isinstance(input[inp], torch.Tensor) else input[inp]
            output = model(**input)
    elif isinstance(input, list) or isinstance(input, tuple):
        if device == 'cpu':
            output = model(*input)
        elif device == 'ipex':  # pragma: no cover
            if version.release < Version("1.12.0").release:
                if running_mode == "calibration":
                    with ipex.quantization.calibrate(conf, default_recipe=True):   # pylint: disable=E1101
                        output = model(*input)
                else:
                    output = model(*input)
            else:
                output = model(*input)
        else:  # pragma: no cover
            tmp_device = "dpcpp" if device == "gpu" else device
            input = [inp.to(tmp_device) \
                    if isinstance(inp, torch.Tensor) else inp
                    for inp in input] # pylint: disable=E1133
            output = model(*input)
    else:
        if device == 'cpu' or not isinstance(input, torch.Tensor):
            output = model(input)
        elif device == 'ipex':  # pragma: no cover
            if version.release < Version("1.12.0").release:
                if running_mode == "calibration":
                    with ipex.quantization.calibrate(conf, default_recipe=True):    # pylint: disable=E1101
                        output = model(input)
                else:
                    output = model(input)
            else:
                output = model(input)
        else:  # pragma: no cover
            input = input.to("dpcpp" if device == "gpu" else device)  # pylint: disable=no-member
            output = model(input)
    return output


def get_example_inputs(model, dataloader):  # pragma: no cover
    version = get_torch_version()
    # Suggest set dataloader like calib_dataloader
    if dataloader is None:
        return None
    try:
        for idx, (input, label) in enumerate(dataloader):
            output = pytorch_forward_wrapper(model,
                                             input)
            if isinstance(input, dict) or isinstance(input, UserDict):
                assert version.release >= Version("1.12.0").release, \
                "INC support IPEX version >= 1.12.0"
                if "label" in input.keys():
                    input.pop("label")
                named_input = namedtuple("input", input.keys())
                input = named_input._make(input.values())
                return input
            if isinstance(input, list) or isinstance(input, tuple):
                return tuple(input)
            if isinstance(input, torch.Tensor):
                return input
            break
    except Exception as e:
        for idx, input in enumerate(dataloader):
            output = pytorch_forward_wrapper(model,
                                     input)
            if isinstance(input, dict) or isinstance(input, UserDict):
                assert version.release >= Version("1.12.0").release, \
                "INC support IPEX version >= 1.12.0"
                if "label" in input.keys():
                    input.pop("label")
                named_input = namedtuple("input", input.keys())
                input = named_input._make(input.values())
                return input
            if isinstance(input, list) or isinstance(input, tuple):
                return tuple(input)
            if isinstance(input, torch.Tensor):
                return input
            break
    if idx == 0:
        assert False, "Please checkout the example_inputs format."

def get_ops_recursively(model, prefix, ops={}):
    """This is a helper function for `graph_info`,
        and it will get all ops from model.
    Args:
        model (object): input model
        prefix (string): prefix of op name
        ops (dict): dict of ops from model {op name: type}.
    Returns:
        None
    """
    version = get_torch_version()
    if version.release < Version("1.7.0").release:  # pragma: no cover
        white_list = \
            (set(torch.quantization.default_mappings.DEFAULT_MODULE_MAPPING.values()) |
            set(torch.quantization.default_mappings.DEFAULT_QAT_MODULE_MAPPING.values()) |
            set(torch.quantization.default_mappings.DEFAULT_DYNAMIC_MODULE_MAPPING.values()) |
            set(torch.quantization.default_mappings.DEFAULT_MODULE_MAPPING.keys()) |
            set(torch.quantization.default_mappings.DEFAULT_QAT_MODULE_MAPPING.keys()) |
            set(torch.quantization.default_mappings.DEFAULT_DYNAMIC_MODULE_MAPPING.keys()) |
            torch.quantization.default_mappings._INCLUDE_QCONFIG_PROPAGATE_LIST)
    elif version.release < Version("1.8.0").release:  # pragma: no cover
        white_list = torch.quantization.get_compare_output_module_list()
    else:
        white_list = torch.quantization.get_default_compare_output_module_list()

    for name, child in model.named_children():
        op_name = prefix + '.' + name if prefix != '' else name
        if type(child) in white_list and not isinstance(child, torch.nn.Sequential) and \
            type(child) != torch.quantization.stubs.DeQuantStub:
            ops[op_name] = unify_op_type_mapping[str(child.__class__.__name__)] \
                if str(child.__class__.__name__) in unify_op_type_mapping else \
                str(child.__class__.__name__)
        get_ops_recursively(child, op_name, ops)


def _cfg_to_qconfig(tune_cfg, observer_type='post_training_static_quant'):
    """Convert tune configure to quantization config for each op.

        Args:
            tune_cfg (dict): dictionary of tune configure for each op
            observer_type (str, optional): specify observer type, Default is 'ptq_static',
                                           options: 'ptq_dynamic', 'qat'.

        Returns:
            op_qcfgs (dict): dictionary of quantization configure for each op

        tune_cfg should be a format like below:
        {
          'fuse': {'int8': [['CONV2D', 'RELU', 'BN'], ['CONV2D', 'RELU']],
                   'fp32': [['CONV2D', 'RELU', 'BN']]},
          'calib_iteration': 10,
          'op': {
             ('op1', 'CONV2D'): {
               'activation':  {'dtype': 'uint8',
                               'algorithm': 'minmax',
                               'scheme':'sym',
                               'granularity': 'per_tensor'},
               'weight': {'dtype': 'int8',
                          'algorithm': 'kl',
                          'scheme':'asym',
                          'granularity': 'per_channel'}
             },
             ('op2', 'RELU): {
               'activation': {'dtype': 'int8',
               'scheme': 'asym',
               'granularity': 'per_tensor',
               'algorithm': 'minmax'}
             },
             ('op3', 'CONV2D'): {
               'activation':  {'dtype': 'fp32'},
               'weight': {'dtype': 'fp32'}
             },
             ...
          }
        }
    """
    op_qcfgs = OrderedDict()
    op_qcfgs['bf16_ops_list'] = []
    for key in tune_cfg['op']:
        value = tune_cfg['op'][key]
        assert isinstance(value, dict)
        assert 'activation' in value
        if ('weight' in value and value['weight']['dtype'] == 'fp32') or \
           ('weight' not in value and value['activation']['dtype'] == 'fp32'):
            op_qcfgs[key[0]] = None
        elif ('weight' in value and value['weight']['dtype'] == 'bf16') or \
           ('weight' not in value and value['activation']['dtype'] == 'bf16'):
            op_qcfgs['bf16_ops_list'].append(key)
            op_qcfgs[key[0]] = None
        else:
            if 'weight' in value:
                weight = value['weight']
                scheme = weight['scheme']
                granularity = weight['granularity']
                algorithm = weight['algorithm']
                dtype = weight['dtype']
                if observer_type == 'quant_aware_training' and \
                    key[1] not in ['Embedding', 'EmbeddingBag', 'LSTM', 'GRU',
                                    'LSTMCell', 'GRUCell', 'RNNCell']:
                    weights_fake_quantize = _fake_quantize(algorithm, scheme, granularity, dtype)
                else:
                    weights_observer = _observer(algorithm, scheme, granularity, dtype)
            else:
                if observer_type == 'quant_aware_training':
                    weights_fake_quantize = torch.quantization.default_weight_fake_quant
                else:
                    weights_observer = torch.quantization.default_per_channel_weight_observer

            activation = value['activation']
            scheme = activation['scheme']
            granularity = activation['granularity']
            algorithm = activation['algorithm']
            dtype = activation['dtype']
            compute_dtype = activation['compute_dtype'] \
                            if 'compute_dtype' in activation \
                                and activation['compute_dtype'] is not None \
                            else 'uint8'

            if observer_type == 'quant_aware_training':
                if key[1] in ['LSTM', 'GRU', 'LSTMCell', 'GRUCell', 'RNNCell']:
                    activation_observer = _observer(algorithm, scheme, granularity,
                        dtype, 'post_training_dynamic_quant', compute_dtype)

                elif key[1] not in ['Embedding', 'EmbeddingBag']:
                    activation_fake_quantize = _fake_quantize(algorithm, scheme, granularity, dtype,
                                                              compute_dtype)

                else:
                    activation_observer = \
                        _observer(algorithm, scheme, granularity, dtype, observer_type, compute_dtype)
            elif value['activation']['quant_mode'] == 'static':
                activation_observer = _observer(algorithm, scheme, granularity,
                    dtype, 'post_training_static_quant', compute_dtype)
            elif value['activation']['quant_mode'] == 'dynamic':
                activation_observer = _observer(algorithm, scheme, granularity,
                    dtype, 'post_training_dynamic_quant', compute_dtype)

            version = get_torch_version()
            if observer_type == 'quant_aware_training':
                if key[1] in ['LSTM', 'GRU', 'LSTMCell', 'GRUCell', 'RNNCell',
                  'Embedding', 'EmbeddingBag']:
                    if version.release >= Version("1.11.0").release:
                        if key[1] in ['Embedding', 'EmbeddingBag']:
                            qconfig = torch.quantization.float_qparams_weight_only_qconfig
                        else:
                            qconfig = torch.quantization.per_channel_dynamic_qconfig
                    else:
                        qconfig = torch.quantization.QConfigDynamic(
                                activation=activation_observer, weight=weights_observer)
                else:
                    qconfig = torch.quantization.QConfig(activation=activation_fake_quantize,
                                                     weight=weights_fake_quantize)
            elif value['activation']['quant_mode'] == 'static':
                qconfig = torch.quantization.QConfig(activation=activation_observer,
                                                     weight=weights_observer)
            else:
                if version.release < Version("1.6.0").release:  # pragma: no cover
                    qconfig = torch.quantization.QConfigDynamic(weight=weights_observer)
                elif version.release >= Version("1.11.0").release:
                    if key[1] in ['Embedding', 'EmbeddingBag']:
                        qconfig = torch.quantization.float_qparams_weight_only_qconfig
                    else:
                        qconfig = torch.quantization.per_channel_dynamic_qconfig
                else:
                    qconfig = torch.quantization.QConfigDynamic(activation=activation_observer,
                                                                weight=weights_observer)

            op_qcfgs[key[0]] = qconfig

    return op_qcfgs


def _cfgs_to_fx_cfgs(op_cfgs, observer_type='post_training_static_quant'):
    """Convert quantization config to a format that meets the requirements of torch.fx.

        Args:
            op_cfgs (dict): dictionary of quantization configure for each op
            observer_type (str, optional): specify observer type, Default is 'ptq_static',
                                           options: 'ptq_dynamic', 'qat'.

        Returns:
            fx_op_cfgs (dict): dictionary of quantization configure that meets
                               the requirements of torch.fx

    example: fx_op_cfgs = {"": default_qconfig,
                           "module_name": [("layer4.1.conv2", per_channel_weight_qconfig)]}
    """
    version = get_torch_version()
    if observer_type == 'post_training_dynamic_quant':
        model_qconfig = torch.quantization.default_dynamic_qconfig
    elif observer_type == 'quant_aware_training':
        model_qconfig = torch.quantization.QConfig(
                            activation=torch.quantization.FakeQuantize.with_args(
                                    dtype=torch.quint8,
                                    qscheme=torch.per_tensor_affine,
                                    reduce_range=REDUCE_RANGE),
                            weight=torch.quantization.default_weight_fake_quant) \
                        if version.release < Version("1.10.0").release else \
                          torch.quantization.QConfig(
                            activation=torch.quantization.FusedMovingAvgObsFakeQuantize.with_args(
                                       dtype=torch.quint8,
                                       qscheme=torch.per_tensor_affine,
                                       reduce_range=REDUCE_RANGE),
                            weight=torch.quantization.default_fused_per_channel_wt_fake_quant)
    else:
        model_qconfig = torch.quantization.QConfig(
            activation=torch.quantization.HistogramObserver.with_args(reduce_range=REDUCE_RANGE),
            weight=torch.quantization.default_per_channel_weight_observer)

    if version.release >= Version("1.13.0").release:  # pragma: no cover
        from torch.ao.quantization import QConfigMapping
        fx_op_cfgs = QConfigMapping()
        if observer_type != 'post_training_dynamic_quant':
            fx_op_cfgs.set_global(model_qconfig)
    else:
        fx_op_cfgs = dict()
        if observer_type != 'post_training_dynamic_quant':
            fx_op_cfgs[""] = model_qconfig
        op_tuple_cfg_list = []

    for key, value in op_cfgs.items():
        if key == "default_qconfig":
            if version.release >= Version("1.13.0").release:  # pragma: no cover
                fx_op_cfgs.set_global(value)
            else:
                fx_op_cfgs[""] = value
            continue
        if version.release >= Version("1.13.0").release:  # pragma: no cover
            fx_op_cfgs.set_module_name(key, value)
        else:
            op_tuple = (key, value)
            op_tuple_cfg_list.append(op_tuple)

    if version.release < Version("1.13.0").release:  # pragma: no cover
        fx_op_cfgs["module_name"] = op_tuple_cfg_list
    elif observer_type != 'post_training_dynamic_quant':
        from torch.ao.quantization import get_default_qconfig_mapping
        for name, q_config  in get_default_qconfig_mapping().to_dict()['object_type']:
            fx_op_cfgs.set_object_type(name, q_config)

    return fx_op_cfgs


def _observer(algorithm,
              scheme,
              granularity,
              dtype,
              observer_type='post_training_static_quant',
              compute_dtype='uint8'):
    """Construct an observer module, In forward, observer will update the statistics of
       the observed Tensor. And they should provide a `calculate_qparams` function
       that computes the quantization parameters given the collected statistics.

    Args:
        algorithm (string): What algorithm for computing the quantization parameters based on.
        scheme (string): Quantization scheme to be used.
        granularity (string): What granularity to computing the quantization parameters,
                              per channel or per tensor.
        dtype (string): Quantized data type
        observer_type (string): Observer type, default is 'post_training_static_quant'.

    Returns:
        oberser (object)
    """
    from .torch_utils.util import match_datatype_pattern, calculate_quant_min_max, _get_signed_and_bits
    if observer_type == 'post_training_dynamic_quant' and \
                get_torch_version().release >= Version("1.6.0").release:
        return torch.quantization.default_dynamic_quant_observer

    compute_dtype_dict = {'int8': torch.qint8, 'uint8': torch.quint8, 'None': None}
    if compute_dtype in compute_dtype_dict:
        compute_dtype = compute_dtype_dict[compute_dtype]
    else:  # pragma: no cover
        assert False, "Unsupport compute_dtype with {}".format(compute_dtype)

    quant_min, quant_max = None, None 
    dtype_dict = {'int8': torch.qint8, 'uint8': torch.quint8, 'fp32': torch.float}
    if dtype in dtype_dict:
        torch_dtype = dtype_dict[dtype]
    else:  # pragma: no cover
        #TODO to handle int4
        if match_datatype_pattern(dtype):
            logger.info((f"Currently, PyTorch does not natively support {dtype},"+ \
                f"it will simulate its numerics instead."))
            unsigned, num_bits = _get_signed_and_bits(dtype)
            torch_dtype = torch.quint8 if unsigned else torch.qint8
            quant_min, quant_max = calculate_quant_min_max(unsigned, num_bits)
            logger.info((f"For {dtype}, replace it with {torch_dtype} and " + \
                f"set quant_min: {quant_min}, quant_max: {quant_max}"))
        else:
            assert False, "Unsupport dtype with {}".format(dtype)

    if algorithm == 'placeholder' or torch_dtype == torch.float:  # pragma: no cover
        return torch.quantization.PlaceholderObserver \
            if get_torch_version().release < Version("1.8.0").release \
                else torch.quantization.PlaceholderObserver.with_args(dtype=torch_dtype,
                                                                      compute_dtype=compute_dtype)
    if algorithm == 'minmax':
        if granularity == 'per_channel':
            observer = torch.quantization.PerChannelMinMaxObserver
            if scheme == 'sym':
                qscheme = torch.per_channel_symmetric
            elif scheme == 'asym_float':
                qscheme = torch.per_channel_affine_float_qparams
            else:
                qscheme = torch.per_channel_affine
        else:
            assert granularity == 'per_tensor'
            observer = torch.quantization.MinMaxObserver
            if scheme == 'sym':
                qscheme = torch.per_tensor_symmetric
            else:
                assert scheme == 'asym'
                qscheme = torch.per_tensor_affine
    else:
        assert algorithm == 'kl'
        observer = torch.quantization.HistogramObserver
        assert granularity == 'per_tensor'
        if scheme == 'sym':
            qscheme = torch.per_tensor_symmetric
        else:
            assert scheme == 'asym'
            qscheme = torch.per_tensor_affine

    return observer.with_args(qscheme=qscheme,
                              dtype=torch_dtype,
                              reduce_range=(REDUCE_RANGE and scheme == 'asym'),
                              quant_min=quant_min,
                              quant_max=quant_max)


def _fake_quantize(algorithm, scheme, granularity, dtype, compute_dtype='uint8'):
    """Construct a fake quantize module, In forward, fake quantize module will update
       the statistics of the observed Tensor and fake quantize the input.
       They should also provide a `calculate_qparams` function
       that computes the quantization parameters given the collected statistics.

    Args:
        algorithm (string): What algorithm for computing the quantization parameters based on.
        scheme (string): Quantization scheme to be used.
        granularity (string): What granularity to computing the quantization parameters,
                              per channel or per tensor.
        dtype (sting): Quantized data type

    Return:
        fake quantization (object)
    """
    version = get_torch_version()
    if scheme == 'asym_float' \
                 and version.release >= Version("1.7.0").release:
        return torch.quantization.default_float_qparams_observer
    if algorithm == 'placeholder' or dtype == 'fp32':  # pragma: no cover
        return _observer(algorithm, scheme, granularity, dtype, compute_dtype=compute_dtype)
    fake_quant = torch.quantization.FakeQuantize \
                 if version.release < Version("1.10.0").release else \
                     torch.quantization.FusedMovingAvgObsFakeQuantize
    if algorithm == 'minmax':
        if granularity == 'per_channel':
            observer = torch.quantization.MovingAveragePerChannelMinMaxObserver
            if scheme == 'sym':
                qscheme = torch.per_channel_symmetric
            else:
                assert scheme == 'asym'
                qscheme = torch.per_channel_affine
        else:
            assert granularity == 'per_tensor'
            observer = torch.quantization.MovingAverageMinMaxObserver
            if scheme == 'sym':
                qscheme = torch.per_tensor_symmetric
            else:
                assert scheme == 'asym'
                qscheme = torch.per_tensor_affine
    else:  # pragma: no cover
        # Histogram observer is too slow for quantization aware training
        assert algorithm == 'kl'
        observer = torch.quantization.HistogramObserver
        assert granularity == 'per_tensor'
        if scheme == 'sym':
            qscheme = torch.per_tensor_symmetric
        else:
            assert scheme == 'asym'
            qscheme = torch.per_tensor_affine

    if dtype == 'int8':
        qmin = -128
        qmax = 127
        dtype = torch.qint8
    else:
        assert dtype == 'uint8'
        qmin = 0
        qmax = 255
        dtype = torch.quint8

    return fake_quant.with_args(observer=observer,
                                quant_min=qmin,
                                quant_max=qmax,
                                dtype=dtype,
                                qscheme=qscheme,
                                reduce_range=(REDUCE_RANGE and scheme == 'asym'))


def _propagate_qconfig(model,
                       op_qcfgs,
                       is_qat_convert=False,
                       approach='post_training_static_quant'):
    """Propagate qconfig through the module hierarchy and assign `qconfig`
       attribute on each leaf module

    Args:
        model (object): input model
        op_qcfgs (dict): dictionary that maps from name or type of submodule to
                         quantization configuration, qconfig applies to all submodules of a
                         given module unless qconfig for the submodules are specified (when
                         the submodule already has qconfig attribute)
        is_qat_convert (bool): flag that specified this function is used to QAT prepare
                               for pytorch 1.7 or above.
        approach (str): quantization approach
    Return:
        None, module is modified inplace with qconfig attached
    """
    fallback_ops = []
    _propagate_qconfig_recursively(model, '', op_qcfgs)

    if approach != 'post_training_dynamic_quant':
        for k, v in op_qcfgs.items():
            if v is None and not is_qat_convert:
                fallback_ops.append(k)

        if fallback_ops and not is_qat_convert:
            _fallback_quantizable_ops_recursively(model, '', fallback_ops, op_qcfgs)


def _propagate_qconfig_recursively(model, prefix, op_qcfgs, qconfig_parent=None):
    """This is a helper function for `propagate_qconfig`

    Args:
        model (object): input model
        prefix (string): prefix of op name
        op_qcfgs (dict): dictionary that maps from name or type of submodule to
                        quantization configuration
        qconfig_parent (object, optional): qconfig of parent module

    Returns:
        None
    """
    for name, child in model.named_children():
        op_name = prefix + name
        child.qconfig = qconfig_parent
        qconfig_son = None
        if op_name in op_qcfgs:
            child.qconfig = op_qcfgs[op_name]
            # for submodules of fused module, like nn.ConvBnRelu2d.
            qconfig_son = child.qconfig
        elif type(child) == torch.quantization.DeQuantStub:
            version = get_torch_version()
            if version.release >= Version("1.8.0").release:
                child.qconfig = torch.quantization.QConfig(
                    activation=torch.quantization.MinMaxObserver.with_args(
                        reduce_range=REDUCE_RANGE),
                    weight=torch.quantization.default_per_channel_weight_observer)
        _propagate_qconfig_recursively(child, op_name + '.', op_qcfgs, qconfig_son)


def _find_quantized_op_num(module, op_qcfgs, prefix='', op_count=0):
    """This is a helper function for `_fallback_quantizable_ops_recursively`

    Args:
        model (object): input model
        op_cfgs (dict): dictionary of quantization configure for each op
        prefix (str): prefix of op name
        op_count (int, optional): count the quantizable op quantity in this module

    Returns:
        the quantizable op quantity in this module
    """
    for name_tmp, child_tmp in module.named_children():
        op_name = prefix + '.' + name_tmp if prefix != '' else name_tmp
        if op_name in op_qcfgs.keys() and \
          type(child_tmp) != torch.quantization.QuantStub:
            op_count += 1
        else:
            op_count = _find_quantized_op_num(child_tmp, op_qcfgs, op_name, op_count)
    return op_count


def _fallback_quantizable_ops_recursively(model, prefix, fallback_ops, op_qcfgs):
    """Handle all fallback ops(fp32 ops)

    Args:
        model (object): input model
        prefix (string): the prefix of op name
        fallback_ops (list): list of fallback ops(fp32 ops)
        op_cfgs (dict): dictionary of quantization configure for each op

    Returns:
        None
    """
    class DequantQuantWrapper(torch.nn.Module):
        """A wrapper class that wraps the input module, adds DeQuantStub and
           surround the call to module with call to dequant.
           this is used by fallback layer when the data type of quantized op
           is  input:int8/output:int8.

           This is used by the fallback utility functions to add the dequant and
           quant modules, before `convert` function `QuantStub` will just be observer,
           it observes the input tensor, after `convert`, `QuantStub`
           will be swapped to `nnq.Quantize` which does actual quantization. Similarly
           for `DeQuantStub`.
        """
        def __init__(self, module, observer=None):
            super(DequantQuantWrapper, self).__init__()
            if not module.qconfig and observer:
                weights_observer = observer('minmax', 'asym', 'per_channel', 'int8')
                activation_observer = observer('minmax', 'sym', 'per_tensor', 'uint8')
                module.qconfig = torch.quantization.QConfig(activation=activation_observer,
                                                            weight=weights_observer)
            self.add_module('quant', torch.quantization.QuantStub(module.qconfig))
            self.add_module('dequant', torch.quantization.DeQuantStub())
            self.add_module('module', module)
            version = get_torch_version()
            if version.release >= Version("1.8.0").release:
                self.dequant.qconfig = module.qconfig
            module.qconfig = None
            self.train(module.training)

        def forward(self, X):
            X = self.dequant(X)
            X = self.module(X)
            return self.quant(X)

        def add(self, x, y):
            # type: (Tensor, Tensor) -> Tensor
            x = self.dequant(x)
            y = self.dequant(y)
            r = self.module.add(x, y)
            return self.quant(r)

        def add_scalar(self, x, y):
            # type: (Tensor, float) -> Tensor
            x = self.dequant(x)
            r = self.module.add_scalar(x, y)
            return self.quant(r)

        def mul(self, x, y):
            # type: (Tensor, Tensor) -> Tensor
            x = self.dequant(x)
            y = self.dequant(y)
            r = self.module.mul(x, y)
            return self.quant(r)

        def mul_scalar(self, x, y):
            # type: (Tensor, float) -> Tensor
            x = self.dequant(x)
            r = self.module.mul_scalar(x, y)
            return self.quant(r)

        def cat(self, x, dim=0):
            # type: (List[Tensor], int) -> Tensor
            X = [self.dequant(x_) for x_ in x]
            r = self.module.cat(X, dim)
            return self.quant(r)

        def add_relu(self, x, y):
            # type: (Tensor, Tensor) -> Tensor
            x = self.dequant(x)
            y = self.dequant(y)
            r = self.module.add_relu(x, y)
            return self.quant(r)

    for name, child in model.named_children():
        op_name = prefix + '.' + name if prefix != '' else name
        if op_name in fallback_ops:
            child.qconfig = None
            quantize_op_num = _find_quantized_op_num(model, op_qcfgs, prefix=prefix)
            if quantize_op_num == 1:
                found = False
                for name_tmp, child_tmp in model.named_children():
                    if isinstance(child_tmp, torch.quantization.QuantStub) or isinstance(
                            child_tmp, torch.quantization.DeQuantStub):
                        model._modules[name_tmp] = torch.nn.Identity()
                        found = True
                if not found:
                    model._modules[name] = DequantQuantWrapper(child, observer=_observer)
            else:
                model._modules[name] = DequantQuantWrapper(child, observer=_observer)
        else:
            _fallback_quantizable_ops_recursively(child, op_name, fallback_ops, op_qcfgs)


@adaptor_registry
class TemplateAdaptor(Adaptor):
    """Tample adaptor of PyTorch framework.

    Args:
        framework_specific_info (dict): dictionary of tuning configure from yaml file.
    """
    def __init__(self, framework_specific_info):
        super(TemplateAdaptor, self).__init__(framework_specific_info)
        import torch.quantization as tq
        self.version = get_torch_version()
        # set torch random seed
        random_seed = framework_specific_info['random_seed']
        torch.manual_seed(random_seed)

        self.bf16_ops = []
        self.use_bf16 = framework_specific_info.get('use_bf16', True)
        self.device = framework_specific_info['device']
        self.q_dataloader = framework_specific_info['q_dataloader']
        self.q_func = framework_specific_info.get('q_func', None)
        self.benchmark = (GLOBAL_STATE.STATE == MODE.BENCHMARK)
        self.workspace_path = framework_specific_info['workspace_path']
        self.is_baseline = False if GLOBAL_STATE.STATE == MODE.BENCHMARK else True
        self.query_handler = None
        self.approach = ''
        self.pre_optimized_model = None
        self.sub_module_list = None
        self.default_qconfig = framework_specific_info.get('default_qconfig', None)
        self.performance_only = framework_specific_info.get("performance_only", False)
        self.example_inputs = framework_specific_info.get("example_inputs", None)

        if 'approach' in framework_specific_info:  # pragma: no cover
            self.approach = framework_specific_info['approach']
            if framework_specific_info['approach'] in ["post_training_static_quant",
                "post_training_auto_quant"]:
                if self.version.release < Version("1.7.0").release:
                    self.q_mapping = tq.default_mappings.DEFAULT_MODULE_MAPPING
                elif self.version.release < Version("1.8.0").release:
                    self.q_mapping = \
                        tq.quantization_mappings.get_static_quant_module_mappings()
                else:
                    self.q_mapping = \
                        tq.quantization_mappings.get_default_static_quant_module_mappings()
            elif framework_specific_info['approach'] == "quant_aware_training":
                if self.version.release < Version("1.7.0").release:
                    self.q_mapping = tq.default_mappings.DEFAULT_QAT_MODULE_MAPPING
                elif self.version.release < Version("1.8.0").release:
                    self.q_mapping = \
                        tq.quantization_mappings.get_qat_module_mappings()
                else:
                    self.q_mapping = \
                        tq.quantization_mappings.get_default_qat_module_mappings()
            elif framework_specific_info['approach'] == "post_training_dynamic_quant":
                if self.version.release < Version("1.7.0").release:
                    self.q_mapping = \
                        tq.default_mappings.DEFAULT_DYNAMIC_MODULE_MAPPING
                elif self.version.release < Version("1.8.0").release:
                    self.q_mapping = \
                        tq.quantization_mappings.get_dynamic_quant_module_mappings()
                else:
                    self.q_mapping = \
                        tq.quantization_mappings.get_default_dynamic_quant_module_mappings()
            else:
                assert False, "Unsupport approach: {}".format(self.approach)

        self.fp32_results = []
        self.fp32_preds_as_label = False

    def calib_func(self, model, dataloader, tmp_iterations, conf=None):
        try:
            for idx, (input, label) in enumerate(dataloader):
                output = pytorch_forward_wrapper(model,
                                                 input,
                                                 device=self.device,
                                                 conf=conf,
                                                 running_mode='calibration')
                if idx >= tmp_iterations - 1:
                    break
        except Exception as e:
            for idx, input in enumerate(dataloader):
                output = pytorch_forward_wrapper(model,
                                                 input,
                                                 device=self.device,
                                                 conf=conf,
                                                 running_mode='calibration')
                if idx >= tmp_iterations - 1:
                    break

    def model_calibration(self,
                          q_model,
                          dataloader,
                          iterations=1,
                          conf=None,
                          calib_sampling_size=1):
        assert iterations > 0
        with torch.no_grad():
            if isinstance(dataloader, BaseDataLoader):
                batch_size = dataloader.batch_size
                try:
                    for i in range(batch_size):
                        if calib_sampling_size % (batch_size - i) == 0:
                            calib_batch_size = batch_size - i
                            if i != 0:
                                logger.warning("Reset `calibration.dataloader.batch_size` field "
                                               "to {}".format(calib_batch_size) +
                                               " to make sure the sampling_size is "
                                               "divisible exactly by batch size")
                            break
                    tmp_iterations = int(math.ceil(calib_sampling_size / calib_batch_size))
                    dataloader.batch(calib_batch_size)
                    self.calib_func(q_model, dataloader, tmp_iterations, conf)
                except Exception:  # pragma: no cover
                    logger.warning("Fail to forward with batch size={}, set to {} now.".format(
                        batch_size, 1))
                    dataloader.batch(1)
                    self.calib_func(q_model, dataloader, calib_sampling_size, conf)
            else:  # pragma: no cover
                if hasattr(dataloader, 'batch_size') and \
                  calib_sampling_size % dataloader.batch_size != 0:
                    logger.warning(
                        "Please note that calibration sampling size {} " \
                        "isn't divisible exactly by batch size {}. " \
                        "So the real sampling size is {}.".
                        format(calib_sampling_size, dataloader.batch_size,
                               dataloader.batch_size * iterations))

                self.calib_func(q_model, dataloader, iterations, conf)

    def eval_func(self, model, dataloader, postprocess, metrics, measurer, iteration, conf=None):
        results = []
        try:
            for idx, (input, label) in enumerate(dataloader):
                if measurer is not None:
                    measurer.start()

                output = pytorch_forward_wrapper(model, input, device=self.device, conf=conf)
                if self.device != "cpu":  # pragma: no cover
                    output = output.to("cpu")
                    label = label.to("cpu")
                if measurer is not None:
                    measurer.end()
                if postprocess is not None:
                    output, label = postprocess((output, label))
                if metrics:
                    for metric in metrics:
                        if not hasattr(metric, "compare_label") or \
                            (hasattr(metric, "compare_label") and metric.compare_label):
                            metric.update(output, label)

                    # If distributed dataloader, gather all outputs to update metric
                    if getattr(dataloader, 'distributed', False) or \
                      isinstance(dataloader.sampler, \
                      torch.utils.data.distributed.DistributedSampler):
                        hvd.init()
                        for metric in metrics:
                            metric.hvd = hvd

                if self.fp32_preds_as_label:
                    self.fp32_results.append(output) if self.is_baseline else \
                        results.append(output)
                if idx + 1 == iteration:
                    break
        except Exception as e:
            logger.warning("The dataloader didn't include label, will try input without label!")
            for idx, input in enumerate(dataloader):
                if (isinstance(input, dict) or isinstance(input, UserDict)):
                    if not self.benchmark:
                        assert "label" in input, \
                            "The dataloader must include label to measure the metric!"
                        label = input["label"].to("cpu")
                elif not self.benchmark:
                    assert False, "The dataloader must include label to measure the metric!"

                if measurer is not None:
                    measurer.start()

                output = pytorch_forward_wrapper(model, input, device=self.device, conf=conf)

                if measurer is not None:
                    measurer.end()

                if self.device != "cpu" and not self.benchmark:  # pragma: no cover
                    if isinstance(output, dict) or isinstance(input, UserDict):
                        for key in output:
                            output[key] = output[key].to("cpu")
                    elif isinstance(output, list) or isinstance(output, tuple):
                        for tensor in output:
                            tensor = tensor.to("cpu")
                    else:
                        output = output.to("cpu")

                if postprocess is not None and not self.benchmark:
                    output, label = postprocess((output, label))

                if metrics and not self.benchmark:
                    for metric in metrics:
                        if not hasattr(metric, "compare_label") or \
                            (hasattr(metric, "compare_label") and metric.compare_label):
                            metric.update(output, label)

                    # If distributed dataloader, gather all outputs to update metric
                    if getattr(dataloader, 'distributed', False) or \
                      isinstance(dataloader.sampler, \
                      torch.utils.data.distributed.DistributedSampler):
                        hvd.init()
                        for metric in metrics:
                            metric.hvd = hvd

                if self.fp32_preds_as_label:
                    self.fp32_results.append(output) if self.is_baseline else \
                        results.append(output)
                if idx + 1 == iteration:
                    break
        return results

    def model_eval(self,
                   model,
                   dataloader,
                   postprocess=None,
                   metrics=None,
                   measurer=None,
                   iteration=-1,
                   conf=None):
        with torch.no_grad():
            if metrics:
                for metric in metrics:
                    metric.reset()
            if isinstance(dataloader, BaseDataLoader) and not self.benchmark:
                try:
                    results = self.eval_func(model, dataloader, postprocess, metrics, measurer,
                                             iteration, conf)
                except Exception:  # pragma: no cover
                    logger.warning("Fail to forward with batch size={}, set to {} now.".format(
                        dataloader.batch_size, 1))
                    dataloader.batch(1)
                    results = self.eval_func(model, dataloader, postprocess, metrics, measurer,
                                             iteration, conf)
            else:  # pragma: no cover
                results = self.eval_func(model, dataloader, postprocess, metrics, measurer,
                                         iteration, conf)

        if self.fp32_preds_as_label:
            if self.is_baseline:
                results = torch_utils.util.collate_torch_preds(self.fp32_results)
                reference = results
            else:
                reference = torch_utils.util.collate_torch_preds(self.fp32_results)
                results = torch_utils.util.collate_torch_preds(results)
            for metric in metrics:
                if hasattr(metric, "compare_label") and not metric.compare_label:
                    metric.update(results, reference)

        acc = 0 if metrics is None else [metric.result() for metric in metrics]
        return acc if not isinstance(acc, list) or len(acc) > 1 else acc[0]

    def _get_quantizable_ops_recursively(self, model, prefix, quantizable_ops):
        """This is a helper function for `query_fw_capability`,
           and it will get all quantizable ops from model.

        Args:
            model (object): input model
            prefix (string): prefix of op name
            quantizable_ops (list): list of quantizable ops from model include op name and type.

        Returns:
            None
        """

        raise NotImplementedError

    def _get_quantizable_ops(self, model):
        """This is a helper function to get all quantizable ops from model.

        Args:
            model (object): input model which is PyTorch model

        Returns:
            q_capability (dictionary): tuning capability for each op from model.
        """
        tmp_model = model
        tmp_model.eval()
        quantizable_ops = []
        self._get_quantizable_ops_recursively(tmp_model, '', quantizable_ops)
        # capability = self.query_handler.get_quantization_capability()['dynamic'] \
        #     if self.approach == "post_training_dynamic_quant" else \
        #     self.query_handler.get_quantization_capability()['quant_aware'] \
        #     if self.approach == "quant_aware_training" else \
        #     self.query_handler.get_quantization_capability()['static']
        
        q_capability = {}
        q_capability['optypewise'] = OrderedDict()
        q_capability['opwise'] = OrderedDict()
        quant_datatypes = self.query_handler.get_quant_datatypes()

        if self.approach == "quant_aware_training":
            capability_pair = [(self.query_handler.get_quantization_capability()['quant_aware'], 'static')]
            fp32_config = {'activation': {'dtype': 'fp32'}, 'weight': {'dtype': 'fp32'}}
            # Ignore LayerNorm, InstanceNorm3d and Embedding quantizable ops,
            # due to huge accuracy regression in PyTorch.
            if isinstance(self, PyTorch_IPEXAdaptor):
                additional_skipped_module_classes = {}
            else:
                additional_skipped_module_classes = {'LayerNorm', 'InstanceNorm3d', 'Dropout'}
            no_fp32_ops = {'QuantStub'}
            for pair in capability_pair:
                capability, mode = pair
                for q_op in quantizable_ops:
                    if q_op not in q_capability['opwise']:
                        q_capability['opwise'][q_op] = []
                    if q_op[1] not in q_capability['optypewise']:
                        q_capability['optypewise'][q_op[1]] = []

                    op_cfg = copy.deepcopy(capability[q_op[1]]) if q_op[1] in capability \
                        else copy.deepcopy(capability['default'])

                    op_cfg['activation']['quant_mode'] = mode if q_op[1] not in \
                        ['LSTM', 'GRU', 'LSTMCell', 'GRUCell', 'RNNCell'] else 'dynamic'

                    # skip the op that only include fp32
                    if q_op[1] not in additional_skipped_module_classes:
                        if op_cfg not in q_capability['opwise'][q_op]:
                            q_capability['opwise'][q_op].append(op_cfg)
                        if op_cfg not in q_capability['optypewise'][q_op[1]]:
                            q_capability['optypewise'][q_op[1]].append(op_cfg)

                    if q_op[1] not in no_fp32_ops:
                        if fp32_config not in q_capability['opwise'][q_op]:
                            q_capability['opwise'][q_op].append(fp32_config)
                        if fp32_config not in q_capability['optypewise'][q_op[1]]:
                            q_capability['optypewise'][q_op[1]].append(fp32_config)
        else:
            for datatype in quant_datatypes:
                if self.approach == "post_training_dynamic_quant":
                    capability_pair = [
                        (self.query_handler.get_quantization_capability(datatype).get('dynamic', {}), 'dynamic')]
                elif self.approach == "post_training_static_quant":
                    capability_pair = [
                        (self.query_handler.get_quantization_capability(datatype).get('static', {}), 'static')]
                else:
                    capability_pair = [
                        (self.query_handler.get_quantization_capability(datatype).get('static', {}), 'static'),
                        (self.query_handler.get_quantization_capability(datatype).get('dynamic', {}), 'dynamic')]

                fp32_config = {'activation': {'dtype': 'fp32'}, 'weight': {'dtype': 'fp32'}}
                # Ignore LayerNorm, InstanceNorm3d and Embedding quantizable ops,
                # due to huge accuracy regression in PyTorch.
                if isinstance(self, PyTorch_IPEXAdaptor):
                    additional_skipped_module_classes = {}
                else:
                    additional_skipped_module_classes = {'LayerNorm', 'InstanceNorm3d', 'Dropout'}
                no_fp32_ops = {'QuantStub'}
                for pair in capability_pair:
                    capability, mode = pair
                    for q_op in quantizable_ops:
                        op_cfg = None
                        if q_op not in q_capability['opwise']:
                            q_capability['opwise'][q_op] = []
                        if q_op[1] not in q_capability['optypewise']:
                            q_capability['optypewise'][q_op[1]] = []

                        if mode == 'static' and q_op[1] in ['LSTM', 'GRU', 'LSTMCell', 'GRUCell', 'RNNCell']:
                            continue

                        op_cfg = copy.deepcopy(capability[q_op[1]]) if q_op[1] in capability \
                            else copy.deepcopy(capability.get('default', fp32_config))

                        op_cfg['activation']['quant_mode'] = mode if q_op[1] not in \
                            ['LSTM', 'GRU', 'LSTMCell', 'GRUCell', 'RNNCell'] else 'dynamic'

                        # skip the op that only include fp32
                        if q_op[1] not in additional_skipped_module_classes:
                            if op_cfg not in q_capability['opwise'][q_op]:
                                q_capability['opwise'][q_op].append(op_cfg)
                            if op_cfg not in q_capability['optypewise'][q_op[1]]:
                                q_capability['optypewise'][q_op[1]].append(op_cfg)

                        if q_op[1] not in no_fp32_ops:
                            if fp32_config not in q_capability['opwise'][q_op]:
                                q_capability['opwise'][q_op].append(fp32_config)
                            if fp32_config not in q_capability['optypewise'][q_op[1]]:
                                q_capability['optypewise'][q_op[1]].append(fp32_config)

        # get bf16 capability
        if self.use_bf16 and (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1') and \
            (self.version.release >= Version("1.11.0").release):
            self.bf16_ops = self.query_handler.get_op_types_by_precision("bf16")
            bf16_ops = []
            self._get_bf16_ops_recursively(tmp_model, '', bf16_ops)
            mixed_capability = self._combine_capability(bf16_ops, q_capability)
            return mixed_capability
        return q_capability

    def _get_bf16_ops_recursively(self, model, prefix, bf16_ops):
        """This is a helper function for `query_fw_capability`,
           and it will get all quantizable ops from model.

        Args:
            model (object): input model
            prefix (string): prefix of op name
            bf16_ops (list): list of quantizable ops from model include op name and type.

        Returns:
            None
        """

        for name, child in model.named_children():
            op_name = prefix + '.' + name if prefix != '' else name
            if str(child.__class__.__name__) in self.bf16_ops \
               and type(child) != torch.nn.Sequential \
               and type(child) != torch.quantization.stubs.DeQuantStub:
                bf16_ops.append((op_name, unify_op_type_mapping[str(child.__class__.__name__)]
                                 if str(child.__class__.__name__) in unify_op_type_mapping else
                                 str(child.__class__.__name__)))
            elif self.is_fused_module(child):
                continue
            else:
                self._get_bf16_ops_recursively(child, op_name, bf16_ops)

    def _combine_capability(self, bf16_ops, q_capability):
        bf16_config = {'activation': {'dtype': 'bf16'}, 'weight': {'dtype': 'bf16'}}
        fp32_config = {'activation': {'dtype': 'fp32'}, 'weight': {'dtype': 'fp32'}}
        for bf16_op in bf16_ops:
            if bf16_op in q_capability['opwise'] and \
                bf16_config not in q_capability['opwise'][bf16_op]:
                q_capability['opwise'][bf16_op].append(bf16_config)
            else:
                q_capability['opwise'][bf16_op] = [bf16_config, fp32_config]
                if bf16_op[1] not in q_capability['optypewise']:
                    q_capability['optypewise'][bf16_op[1]] = [bf16_config, fp32_config]
        return q_capability

    def is_fused_module(self, module):
        """This is a helper function for `_propagate_qconfig_helper` to detecte
           if this module is fused.

        Args:
            module (object): input module

        Returns:
            (bool): is fused or not
        """
        op_type = str(type(module))
        if 'fused' in op_type:
            return True
        else:
            return False

    def calculate_hessian_trace(self,
                                fp32_model,
                                dataloader,
                                q_model,
                                criterion,
                                enable_act=False
                                ):
        """Calculate hessian trace.

        Args:
            fp32_model: The original fp32 model.
            criterion: The loss function for calculate the hessian trace. # loss = criterion(output, target)
            dataloader: The dataloader for calculate the gradient.
            q_model: The INT8 AMAP model.
            enable_act: Enabling quantization error or not.

        Return:
            hessian_trace(Dict[Tuple, float]), key: (op_name, op_type); value: hessian trace.
        """
        from .torch_utils.hawq_metric import hawq_top
        op_to_traces = hawq_top(fp32_model=fp32_model,
                                dataloader=dataloader,
                                q_model=q_model,
                                criterion=criterion,
                                enable_act=enable_act)
        return op_to_traces

    def smooth_quant(self, model, dataloader, calib_iter, tune_cfg=None, alpha=0.5,
                     percentile=None, op_types=None, scales_per_op=None, force_re_smooth=False):
        """ convert the model by smooth quant.

        Args:
            model: origin FP32 model
            dataloader: calib dataloader
            calib_iter: calib iters
            tune_cfg: quantization config
            alpha: smooth alpha in SmoothQuant, 1.0 will fallback to SPIQ
            percentile:Percentile of calibration to remove outliers, not supported now
            op_types: The op types whose input tensor will be dumped
            scales_per_op: True, each op will have an individual scale, mainly for accuracy
                           False, ops with the same input will share a scale, mainly for performance

        Returns:
            model: A modified fp32 model
        """
        if not hasattr(self, 'sq') or force_re_smooth:
            self.sq = TorchSmoothQuant(model._model, dataloader=dataloader)
        args = {}  ##different backends may have different default values
        if op_types != None:
            args["op_types"] = op_types
        if percentile != None:
            args['percentile'] = percentile
        if scales_per_op != None:
            args['scales_per_op'] = scales_per_op
        model._model = self.sq.transform(alpha=alpha, calib_iter=calib_iter, **args)
        return model



unify_op_type_mapping = {
    "ConvReLU2d": "Conv2d",
    "ConvReLU3d": "Conv3d",
    "LinearReLU": "Linear",
    "ConvBn2d": "Conv2d",
    "ConvBnReLU2d": "Conv2d"
}


@adaptor_registry
class PyTorchAdaptor(TemplateAdaptor):
    """Adaptor of PyTorch framework, all PyTorch API is in this class.

    Args:
        framework_specific_info (dict): dictionary of tuning configure from yaml file.
    """
    def __init__(self, framework_specific_info):
        super(PyTorchAdaptor, self).__init__(framework_specific_info)
        """
        # Map for swapping float module to quantized ones,
        # and this dictionary will change with different PoTorch versions
        DEFAULT_MODULE_MAPPING = {
            nn.Linear: nnq.Linear,
            nn.ReLU: nnq.ReLU,
            nn.ReLU6: nnq.ReLU6,
            nn.Conv2d: nnq.Conv2d,
            nn.Conv3d: nnq.Conv3d,
            QuantStub: nnq.Quantize,
            DeQuantStub: nnq.DeQuantize,
            # Wrapper Modules:
            nnq.FloatFunctional: nnq.QFunctional,
            # Intrinsic modules:
            nni.ConvReLU2d: nniq.ConvReLU2d,
            nni.ConvReLU3d: nniq.ConvReLU3d,
            nni.LinearReLU: nniq.LinearReLU,
            nniqat.ConvReLU2d: nniq.ConvReLU2d,
            nniqat.LinearReLU: nniq.LinearReLU,
            nniqat.ConvBn2d: nnq.Conv2d,
            nniqat.ConvBnReLU2d: nniq.ConvReLU2d,
            # QAT modules:
            nnqat.Linear: nnq.Linear,
            nnqat.Conv2d: nnq.Conv2d,
        }
        """

        self.tune_cfg = None
        if self.device == "cpu":
            query_config_file = "pytorch_cpu.yaml"
        elif self.device == "gpu":
            query_config_file = "pytorch_gpu.yaml"
        else:  # pragma: no cover
            assert False, "Unsupport this device {}".format(self.device)
        self.query_handler = PyTorchQuery(
            local_config_file=os.path.join(os.path.dirname(__file__), query_config_file))

        self.white_list = get_torch_white_list(self.approach)

        # for tensorboard
        self.dump_times = 0
        self.fused_dict = {}

        self.optype_statistics = None

    @dump_elapsed_time("Pass quantize model")
    def quantize(self, tune_cfg, model, dataloader, q_func=None):
        """Execute the quantize process on the specified model.

        Args:
            tune_cfg (dict): quantization config.
            model (object): model need to do quantization.
            dataloader (object): calibration dataset.
            q_func (objext, optional): training function for quantization aware training mode.

        Returns:
            (object): quantized model
        """

        assert isinstance(model._model, torch.nn.Module), \
               "The model passed in is not the instance of torch.nn.Module"

        # For tensorboard display
        self.tune_cfg = tune_cfg
        self.tune_cfg["approach"] = self.approach
        self.tune_cfg["reduce_range"] = REDUCE_RANGE
        self.tune_cfg["framework"] = "pytorch"
        op_cfgs = _cfg_to_qconfig(tune_cfg, self.approach)
        self.tune_cfg['bf16_ops_list'] = op_cfgs['bf16_ops_list']
        del op_cfgs['bf16_ops_list']
        gc.collect()

        if self.version.release < Version("2.0.0").release:
            from torch.quantization.quantize import add_observer_
        else:
            from torch.quantization.quantize import _add_observer_ as add_observer_

        if self.performance_only:
            q_model = model
        else:
            try:
                q_model = copy.deepcopy(model)
            except Exception as e:  # pragma: no cover
                logger.warning("Fail to deep copy the model due to {}, inplace is used now.".format(
                    repr(e)))
                q_model = model

        if self.approach == 'quant_aware_training':
            q_model._model.train()
        else:
            q_model._model.eval()
        if self.version.release < Version("1.7.0").release or \
                    self.approach != 'quant_aware_training':
            _propagate_qconfig(q_model._model, op_cfgs, approach=self.approach)
            # sanity check common API misusage
            if not any(hasattr(m, 'qconfig') and m.qconfig for m in q_model._model.modules()):
                logger.warn("None of the submodule got qconfig applied. Make sure you "
                            "passed correct configuration through `qconfig_dict` or "
                            "by assigning the `.qconfig` attribute directly on submodules.")

        if self.approach in ['post_training_static_quant', 'post_training_auto_quant']:
            add_observer_(q_model._model)
            if q_func is None:
                iterations = tune_cfg.get('calib_iteration', 1)
                self.model_calibration(q_model._model,
                                       dataloader,
                                       iterations,
                                       calib_sampling_size=tune_cfg.get('calib_sampling_size', 1))
            else:
                q_func(q_model._model)
        elif self.approach == 'quant_aware_training':
            if self.version.release >= Version("1.7.0").release:
                _propagate_qconfig(q_model._model, op_cfgs, is_qat_convert=True)
                torch.quantization.convert(q_model._model,
                                           mapping=self.q_mapping,
                                           inplace=True,
                                           remove_qconfig=False)
                _propagate_qconfig(q_model._model, op_cfgs)
                add_observer_(q_model._model, self.white_list,
                                                 set(self.q_mapping.values()))
            else:  # pragma: no cover
                add_observer_(q_model._model)
                torch.quantization.convert(q_model._model, self.q_mapping, inplace=True)
            # q_func can be created by neural_compressor internal or passed by user. It's critical to
            # distinguish how q_func is passed since neural_compressor built-in functions accept neural_compressor
            # model and user defined func should accept framework model.
            q_model._model = q_func(
                q_model if getattr(q_func, 'builtin', None) else q_model._model)
            assert q_model._model is not None, "Please return a trained model in train function!"
            q_model._model.eval()

        if self.approach == 'quant_aware_training':
            torch.quantization.convert(q_model._model, inplace=True)
        else:
            torch.quantization.convert(q_model._model, mapping=self.q_mapping, inplace=True)

        if len(self.tune_cfg['bf16_ops_list']) > 0 and \
            (self.version.release >= Version("1.11.0").release) and \
            (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1'): # pragma: no cover
            q_model._model = torch_utils.bf16_convert.Convert(q_model._model, self.tune_cfg)

        q_model.q_config = copy.deepcopy(self.tune_cfg)
        if self.approach != 'post_training_dynamic_quant':
            self._get_scale_zeropoint(q_model._model, q_model.q_config)
        q_model.is_quantized = True

        self._dump_model_op_stats(q_model._model, q_model.q_config)
        torch_utils.util.get_embedding_contiguous(q_model._model)
        return q_model

    def evaluate(self,
                 model,
                 dataloader,
                 postprocess=None,
                 metrics=None,
                 measurer=None,
                 iteration=-1,
                 tensorboard=False,
                 fp32_baseline=False):
        """Execute the evaluate process on the specified model.

        Args:
            model (object): model to run evaluation.
            dataloader (object): evaluation dataset.
            postprocess (object, optional): process function after evaluation.
            metrics (list, optional): list of metric function.
            measurer (object, optional): measurer function.
            iteration (int, optional): number of iterations to evaluate.
            tensorboard (bool, optional): dump output tensor to tensorboard summary files.
            fp32_baseline (boolen, optional): only for compare_label=False pipeline

        Returns:
            (object): accuracy
        """
        self.is_baseline = fp32_baseline
        if tensorboard:
            model = self._pre_eval_hook(model)

        model_ = model._model
        assert isinstance(
            model_, torch.nn.Module), "The model passed in is not the instance of torch.nn.Module"
        model_.eval()
        if self.device == "cpu":
            model_.to("cpu")
        elif self.device == "gpu":
            if self.is_baseline:
                model_.to("dpcpp")

        if metrics:
            self.fp32_preds_as_label = any([hasattr(metric, "compare_label") and \
                not metric.compare_label for metric in metrics])
        acc = self.model_eval(model_, dataloader, postprocess, metrics, measurer, iteration)

        if tensorboard:
            self._post_eval_hook(model, accuracy=acc)
        return acc if not isinstance(acc, list) or len(acc) > 1 else acc[0]

    def _pre_hook_for_qat(self, dataloader=None):
        # self.model._model is needed here.
        self.model._model.qconfig = torch.quantization.QConfig(
            activation=torch.quantization.FakeQuantize.with_args(dtype=torch.quint8,
                                                                 qscheme=torch.per_tensor_affine,
                                                                 reduce_range=REDUCE_RANGE),
            weight=torch.quantization.default_weight_fake_quant)
        self.non_quant_dict = self.get_non_quant_modules(self.model.kwargs)
        quantizable_ops = []
        self._get_quantizable_ops_recursively(self.model._model, '', quantizable_ops)
        bf16_ops = []
        if self.version.release >= Version("1.11.0").release and self.use_bf16 and \
            (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1'): # pragma: no cover
            self.bf16_ops = self.query_handler.get_op_types_by_precision("bf16")
            self._get_bf16_ops_recursively(self.model._model, '', bf16_ops)
        bf16_ops_list = [(op) for op in bf16_ops if op not in quantizable_ops]
        self.model.model.training = True
        torch.quantization.prepare_qat(self.model._model, inplace=True)

        # This is a flag for reloading
        self.model.q_config = {
            'is_oneshot': True,
            'framework': 'pytorch',
            'reduce_range': REDUCE_RANGE,
            'approach': 'quant_aware_training',
            'bf16_ops_list': bf16_ops_list,
        }

    def _post_hook_for_qat(self):
        torch.quantization.convert(self.model._model, inplace=True)
        if self.model.q_config is not None and len(self.model.q_config['bf16_ops_list']) > 0 and \
            self.version.release >= Version("1.11.0").release and self.use_bf16 and \
            (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1'): # pragma: no cover
            self.model._model = torch_utils.bf16_convert.Convert(self.model._model, self.model.q_config)

    def _pre_hook_for_hvd(self, dataloader=None):
        # TODO: lazy init here
        hvd.init()
        hvd.broadcast_parameters(self.model._model.state_dict(), root_rank=0)
        hvd.broadcast_optimizer_state(self.optimizer, root_rank=0)
        self.optimizer = hvd.DistributedOptimizer(
            self.optimizer, named_parameters=self.model._model.named_parameters())

    def train(self, model, dataloader, optimizer_tuple, criterion_tuple, hooks, **kwargs):
        """Execute the train process on the specified model.

        Args:
            model (object): model to run evaluation.
            dataloader (object): training dataset.
            optimizer (tuple): It is a tuple of (cls, parameters) for optimizer.
            criterion (tuple): It is a tuple of (cls, parameters) for criterion.
            kwargs (dict, optional): other parameters.

        Returns:
            None
        """
        model_ = model._model
        device = "cuda:0" if self.device != "GPU" and torch.cuda.is_available() else self.device
        # self.model is set to neural_compressor model here to hold the inplace change in FWK model.
        self.model = model
        optimizer = optimizer_tuple[0](model_.parameters(), **optimizer_tuple[1])
        self.optimizer = optimizer
        criterion = criterion_tuple[0](**criterion_tuple[1])
        start_epochs = kwargs['kwargs']['start_epoch']
        end_epochs = kwargs['kwargs']['end_epoch']
        iters = kwargs['kwargs']['iteration']
        if hooks is not None:
            on_train_begin = hooks['on_train_begin']
            on_train_end = hooks['on_train_end']
            on_epoch_begin = hooks['on_epoch_begin']
            on_epoch_end = hooks['on_epoch_end']
            on_step_begin = hooks['on_step_begin']
            on_step_end = hooks['on_step_end']
            on_after_compute_loss = hooks['on_after_compute_loss']
            on_before_optimizer_step = hooks['on_before_optimizer_step']
        if hooks is not None:
            on_train_begin()
        for nepoch in range(start_epochs, end_epochs):
            model_.to(device)
            model_.train()
            cnt = 0
            if hooks is not None:
                on_epoch_begin(nepoch)
            if getattr(dataloader, 'distributed', False) \
                    or isinstance(dataloader.sampler, \
                    torch.utils.data.distributed.DistributedSampler):
                dataloader.sampler.set_epoch(nepoch)
            for image, target in dataloader:
                # TODO: to support adjust lr with epoch
                target = target.to(device)
                if hooks is not None:
                    on_step_begin(cnt)
                print('.', end='', flush=True)
                cnt += 1
                output = pytorch_forward_wrapper(model_, image, device=device)
                loss = criterion(output, target)
                if hooks is not None:
                    loss = on_after_compute_loss(image, output, loss)
                self.optimizer.zero_grad()
                loss.backward()
                if hooks is not None:
                    on_before_optimizer_step()
                self.optimizer.step()
                if hooks is not None:
                    on_step_end()
                if cnt >= iters:
                    break
            if hooks is not None:
                on_epoch_end()

        if device != self.device:  # pragma: no cover
            model_.to(self.device)

        if hooks is not None:
            on_train_end()

        return model_

    def _dump_model_op_stats(self, model, tune_cfg):
        """This is a function to dump quantizable ops of model to user.
        Args:
            model (object): input model
            tune_cfg (dict): quantization config
        Returns:
            None
        """
        res = {}
        ignore_log = False
        modules = dict(model.named_modules())
        # fetch quantizable ops supported in Neural Compressor from tune_cfg
        for key in tune_cfg['op']:
            op_name = key[0]
            op_type = str(type(modules[op_name])).rstrip('\'>').split('.')[-1]
            if op_type == 'BF16ModuleWrapper':  # pragma: no cover
                op_type = str(type(modules[op_name].module)).rstrip('\'>').split('.')[-1]
            if op_type == 'DequantQuantWrapper':
                op_type = str(type(modules[op_name].module)).rstrip('\'>').split('.')[-1]
            if 'Functional' in op_type:
                op_type = op_name.split('.')[-1]
            if op_type not in res.keys():
                res[op_type] = {'INT8': 0, 'BF16': 0, 'FP32': 0}
            value = tune_cfg['op'][key]
            # Special cases: QuantStub, Embedding
            if ('weight' in value and value['weight']['dtype'] == 'fp32') or \
              ('weight' not in value and value['activation']['dtype'] == 'fp32'):
                res[op_type]['FP32'] += 1
            elif value['activation']['dtype'] == 'bf16':  # pragma: no cover
                res[op_type]['BF16'] += 1
            else:
                res[op_type]['INT8'] += 1
        # fetch other quantizable ops supported in PyTorch from model
        for name, child in modules.items():
            op_type = str(type(child)).rstrip('\'>').split('.')[-1]
            if tune_cfg['approach'] != 'post_training_dynamic_quant':
                if op_type == 'DeQuantize':
                    if op_type not in res.keys():
                        res[op_type] = {'INT8': 0, 'BF16': 0, 'FP32': 0}
                    res[op_type]['INT8'] += 1
                if op_type in self.non_quant_dict['skipped_module_classes']:
                    ignore_log = True
                    if op_type not in res.keys():
                        res[op_type] = {'INT8': 0, 'BF16': 0, 'FP32': 0}
                    res[op_type]['FP32'] += 1
        # show results to users
        if ignore_log:
            logger.info("Ignore LayerNorm, InstanceNorm3d and Embedding quantizable ops" \
                        " due to accuracy issue in PyTorch.")

        field_names=["Op Type", "Total", "INT8", "BF16", "FP32"]
        output_data = [[
            op_type, sum(res[op_type].values()),
            res[op_type]['INT8'], res[op_type]['BF16'], res[op_type]['FP32']]
        for op_type in res.keys()]

        Statistics(output_data,
                   header='Mixed Precision Statistics',
                   field_names=field_names).print_stat()
        self.optype_statistics = field_names, output_data


    def _get_quantizable_ops_recursively(self, model, prefix, quantizable_ops):
        """This is a helper function for `query_fw_capability`,
           and it will get all quantizable ops from model.

        Args:
            model (object): input model
            prefix (string): prefix of op name
            quantizable_ops (list): list of quantizable ops from model include op name and type.

        Returns:
            None
        """

        module_dict = dict(model.named_modules())
        for op_name, child in model.named_modules():
            if self.is_fused_module(child):
                for name, _ in child.named_children():
                    module_prefix = op_name + '.' + name
                    if module_prefix in module_dict:
                        module_dict.pop(module_prefix)  # remove sub-modules of fused modules
                    if op_name in self.fused_dict:
                        self.fused_dict[op_name] = [self.fused_dict[op_name], module_prefix]
                    else:
                        self.fused_dict[op_name] = module_prefix
        for op_name, child in module_dict.items():
            # there is accuracy issue in quantized LayerNorm op in pytorch <1.8.1,
            # so remove it here
            if op_name in self.non_quant_dict['skipped_module_names'] or \
              str(child.__class__.__name__) in \
              self.non_quant_dict['skipped_module_classes']:
                continue
            if type(child) in self.white_list and type(child) != torch.nn.Sequential and \
              type(child) != torch.quantization.stubs.DeQuantStub:
                quantizable_ops.append(
                    (op_name, unify_op_type_mapping[str(child.__class__.__name__)]
                     if str(child.__class__.__name__) in unify_op_type_mapping else str(
                         child.__class__.__name__)))

    def _get_scale_zeropoint(self, model, tune_cfg):
        """get activation scale and zero_point for converted model.

        Args:
            model (dir): Int8 model converted from fp32 model.
                        scale and zero_point is set with calibration for each module
            tune_cfg (object): This file saves scale and zero_point of \
                            output activation of each quantized module.

        Returns:
            None
        """
        modules = dict(model.named_modules())
        for key, value in tune_cfg['op'].items():
            if hasattr(modules[key[0]], 'scale'):
                value['activation']['scale'] = float(modules[key[0]].scale)
            if hasattr(modules[key[0]], 'zero_point'):
                value['activation']['zero_point'] = int(modules[key[0]].zero_point)

    def _pre_eval_hook(self, model, op_list=None, iteration_list=None):
        """The function is used to do some preprocession before evaluation phase.
           Here, it used to add hook for dump output tensor for quantizable ops.

        Args:
             model (object): input model

        Returns:
              model (object): model with hook
        """
        from abc import ABCMeta

        def _with_args(cls_or_self, **kwargs):
            r"""Wrapper that allows creation of class factories.

            This can be useful when there is a need to create classes with the same
            constructor arguments, but different instances.

            Example::

                >>> Foo.with_args = classmethod(_with_args)
                >>> foo_builder = Foo.with_args(a=3, b=4).with_args(answer=42)
                >>> foo_instance1 = foo_builder()
                >>> foo_instance2 = foo_builder()
                >>> id(foo_instance1) == id(foo_instance2)
                False
            """
            class _PartialWrapper(object):
                def __init__(self, p):
                    self.p = p

                def __call__(self, *args, **keywords):
                    return self.p(*args, **keywords)

                def __repr__(self):
                    return self.p.__repr__()

                with_args = _with_args

            r = _PartialWrapper(partial(cls_or_self, **kwargs))
            return r

        ABC = ABCMeta(str("ABC"), (object, ), {})  # compatible with Python 2 *and* 3:

        class _RecordingObserver(ABC, torch.nn.Module):
            """The module is mainly for debug and records the tensor values during runtime.

            Args:
                iteration_list (list, optional): indexs of iteration which to dump tensor.
            """
            def __init__(self, iteration_list=None, **kwargs):
                super(_RecordingObserver, self).__init__(**kwargs)
                self.output_tensors_dict = OrderedDict()
                self.current_iter = 1
                self.iteration_list = iteration_list

            def forward(self, x):
                if (self.iteration_list is None and self.current_iter == 1) or \
                    (self.iteration_list is not None and
                     self.current_iter in self.iteration_list):
                    if type(x) is tuple or type(x) is list:
                        self.output_tensors_dict[self.current_iter] = \
                            [i.to("cpu") if i.device != 'cpu' else i.clone() for i in x]
                    else:
                        self.output_tensors_dict[self.current_iter] = \
                            x.to("cpu") if x.device != "cpu" else x.clone()
                self.current_iter += 1
                return x

            @torch.jit.export
            def get_tensor_value(self):
                return self.output_tensors_dict

            with_args = classmethod(_with_args)

        def _observer_forward_hook(module, input, output):
            """Forward hook that calls observer on the output

            Args:
                module (object): input module
                input (object): module input
                output (object): module output

            Returns:
                module output tensor (object)
            """
            return module.activation_post_process(output)

        def _add_observer_(module, op_list=None, prefix=""):
            """Add observer for the leaf child of the module.

               This function insert observer module to all leaf child module that
               has a valid qconfig attribute.

            Args:
                module (object): input module with qconfig attributes for all the leaf modules that
                                 we want to dump tensor
                op_list (list, optional): list of ops which to be dumped in module
                prefix (string): name of module

            Returns:
                None, module is modified inplace with added observer modules and forward_hooks
            """
            for name, child in module.named_children():
                op_name = name if prefix == "" else prefix + "." + name
                if isinstance(child, torch.nn.quantized.FloatFunctional) and \
                             (op_list is None or op_name in op_list):
                    if hasattr(child, 'qconfig') and child.qconfig is not None and (
                            op_list is None or op_name in op_list):
                        child.activation_post_process = \
                            child.qconfig.activation()
                elif hasattr(child, 'qconfig') and child.qconfig is not None and \
                        (op_list is None or op_name in op_list):
                    # observer and hook will be gone after we swap the module
                    child.add_module('activation_post_process', child.qconfig.activation())
                    child.register_forward_hook(_observer_forward_hook)
                else:
                    _add_observer_(child, op_list, op_name)

        def _propagate_qconfig_helper(module,
                                      qconfig_dict,
                                      white_list=None,
                                      qconfig_parent=None,
                                      prefix='',
                                      fused=False):
            """This is a helper function for `propagate_qconfig_`

            Args:
                module (object): input module
                qconfig_dict (dictionary): dictionary that maps from name of submodule to
                                           quantization configuration
                white_list (list, optional): list of quantizable modules
                qconfig_parent (object, optional): config of parent module, we will fallback to
                                                   this config when there is no specified config
                                                   for current module
                prefix (string, optional): corresponding prefix of the current module,
                                           used as key in qconfig_dict
                fused (bool, optional): Indicates whether the module is fused or not

            Return:
                None, module is modified inplace with qconfig attached
            """
            if white_list is None:
                white_list = \
                   torch.quantization.default_mappings.DEFAULT_QCONFIG_PROPAGATE_WHITE_LIST \
                   if self.version.release < Version("1.7.0").release else \
                   torch.quantization.quantization_mappings.get_qconfig_propagation_list()

            if type(module) in white_list and type(module) != torch.nn.Sequential:
                module.qconfig = qconfig_parent
            else:
                module.qconfig = None
            if hasattr(module, '_modules'):
                for name, child in module.named_children():
                    module_prefix = prefix + '.' + name if prefix else name
                    _propagate_qconfig_helper(child, qconfig_dict, white_list, qconfig_parent,
                                              module_prefix)

        def _prepare(model, inplace=True, op_list=[], white_list=None):
            """The model will be attached with observer or fake quant modules, and qconfig
               will be propagated.

            Args:
                model (object): input model to be modified in-place
                inplace (bool, optional): carry out model transformations in-place,
                                          the original module is mutated
                op_list (list, optional): list of ops which to be dumped in module
                white_list (list, optional): list of quantizable modules

            Returns:
                model (object): model with qconfig
            """
            if not inplace:
                model = copy.deepcopy(model)
            _propagate_qconfig_helper(model,
                                      qconfig_dict={},
                                      white_list=white_list,
                                      qconfig_parent=model.qconfig)
            # sanity check common API misusage
            if not any(hasattr(m, 'qconfig') and m.qconfig for m in model.modules()):
                logger.warn("None of the submodule got qconfig applied. Make sure you "
                            "passed correct configuration through `qconfig_dict` or "
                            "by assigning the `.qconfig` attribute directly on submodules")
            _add_observer_(model, op_list=op_list)
            return model

        # create properties
        if self.version.release < Version("1.7.0").release:  # pragma: no cover
            white_list = self.white_list | \
                (set(torch.quantization.default_mappings.DEFAULT_MODULE_MAPPING.values()) |
                 set(torch.quantization.default_mappings.DEFAULT_QAT_MODULE_MAPPING.values()) |
                 set(torch.quantization.default_mappings.DEFAULT_DYNAMIC_MODULE_MAPPING.values()))
        elif self.version.release < Version("1.8.0").release:  # pragma: no cover
            white_list = torch.quantization.get_compare_output_module_list()
        else:
            white_list = torch.quantization.get_default_compare_output_module_list()

        model = model if model.is_quantized else copy.deepcopy(model)
        model._model.qconfig = torch.quantization.QConfig(
            weight=torch.quantization.default_debug_observer,
            activation=_RecordingObserver.with_args(iteration_list=iteration_list))
        _prepare(model._model, op_list=op_list, white_list=white_list)

        return model

    def is_fused_child(self, op_name):
        """This is a helper function for `_post_eval_hook`

        Args:
            op_name (string): op name

        Returns:
            (bool): if this op is fused

        """
        op = op_name[:op_name.rfind('.')]
        if op in self.fused_dict and op_name[op_name.rfind('.') + 1:].isdigit():
            return True
        else:
            return False

    def is_fused_op(self, op_name):
        """This is a helper function for `_post_eval_hook`

        Args:
            op_name (string): op name

        Returns:
            (bool): if this op is fused

        """
        op = op_name[:op_name.rfind('.')]
        if op in self.fused_dict:
            return True
        else:
            return False

    def is_last_fused_child(self, op_name):
        """This is a helper function for `_post_eval_hook`

        Args:
            op_name (string): op name

        Returns:
            (bool): if this op is last fused op

        """
        op = op_name[:op_name.rfind('.')]
        if op_name in self.fused_dict[op][-1]:
            return True
        else:
            return False

    def _post_eval_hook(self, model, **args):
        """The function is used to do some post process after complete evaluation.
           Here, it used to dump quantizable op's output tensor.

        Args:
            model (object): input model

        Returns:
            None
        """
        from torch.utils.tensorboard import SummaryWriter
        if self.version.release >= Version("2.0.0").release:
            from torch.quantization.quantize import _get_observer_dict as get_observer_dict
        else:
            from torch.quantization import get_observer_dict

        model = model._model

        if args is not None and 'accuracy' in args:
            accuracy = args['accuracy']
        else:
            accuracy = ''

        if self.dump_times == 0:
            writer = SummaryWriter('runs/eval/baseline' + '_acc' + str(accuracy), model)
        else:
            writer = SummaryWriter(
                'runs/eval/tune_' + str(self.dump_times) + '_acc' + str(accuracy), model)

        if self.dump_times == 0:
            for (input, _) in self.q_dataloader:
                if isinstance(input, dict) or isinstance(input, UserDict):
                    if self.device == "gpu":
                        for inp in input.keys():
                            input[inp] = input[inp].to("dpcpp")
                elif isinstance(input, list) or isinstance(input, tuple):
                    if self.device == "gpu":
                        input = [inp.to("dpcpp") for inp in input]
                else:
                    if self.device == "gpu":
                        input = input.to("dpcpp")
                writer.add_graph(model, input)
                break

        summary = OrderedDict()
        observer_dict = {}
        get_observer_dict(model, observer_dict)
        for key in observer_dict:
            if isinstance(observer_dict[key], torch.nn.modules.linear.Identity):
                continue
            op_name = key.strip(".activation_post_process")
            summary[op_name + ".output"] = observer_dict[key].get_tensor_value()
            for iter in summary[op_name + ".output"]:
                # Only collect last fused child output
                op = op_name
                if self.is_fused_child(op_name) == True and \
                   self.is_last_fused_child(op_name) == True:
                    op = op_name[:op_name.rfind('.')]
                else:
                    if self.is_fused_child(op_name) == True and \
                       self.is_last_fused_child(op_name) == False:
                        continue
                    else:
                        op = op_name

                if summary[op_name + ".output"][iter].is_quantized:
                    writer.add_histogram(op + "/Output/int8",
                                         torch.dequantize(summary[op_name + ".output"][iter]))
                else:
                    writer.add_histogram(op + "/Output/fp32", summary[op_name + ".output"][iter])

        state_dict = model.state_dict()
        for key in state_dict:
            if not isinstance(state_dict[key], torch.Tensor):
                continue

            op = key[:key.rfind('.')]
            if self.is_fused_child(op) is True:
                # fused child tensorboard tag will be merge
                weight = key[key.rfind('.') + 1:]
                op = op[:op.rfind('.')] + '/' + weight
            else:
                weight = key[key.rfind('.') + 1:]
                op = key[:key.rfind('.')] + '/' + weight

            # To merge ._packed_params
            op = op.replace('._packed_params', '')

            if state_dict[key].is_quantized:
                writer.add_histogram(op + "/int8", torch.dequantize(state_dict[key]))
            else:
                writer.add_histogram(op + "/fp32", state_dict[key])

        writer.close()
        self.dump_times = self.dump_times + 1

        return summary

    @dump_elapsed_time("Pass save quantized model")
    def save(self, model, path=None):
        pass

    def inspect_tensor(self,
                       model,
                       dataloader,
                       op_list=None,
                       iteration_list=None,
                       inspect_type='activation',
                       save_to_disk=False):
        if self.version.release >= Version("1.8.0").release:
            from torch.fx import GraphModule
            if type(model._model) == GraphModule:  # pragma: no cover
                assert False, "Inspect_tensor didn't support fx graph model now!"
        from torch import dequantize
        import numpy as np
        is_quantized = model.is_quantized
        op_list_ = []
        fp32_int8_map = {}
        for op_name in op_list:
            op_list_.append(op_name)
            for key in self.fused_dict:
                if op_name in self.fused_dict[key]:
                    fp32_int8_map[op_name] = \
                        {'activation': self.fused_dict[key][-1], 'weight': key}
                    if is_quantized:
                        op_list_.append(key)
                        op_list_.remove(op_name)
                    else:
                        op_list_.append(self.fused_dict[key][-1])

        new_model = model if is_quantized else copy.deepcopy(model)

        assert min(iteration_list) > 0, \
            "Iteration number should great zero, 1 means first iteration."
        iterations = max(iteration_list) if iteration_list is not None else -1
        new_model = self._pre_eval_hook(new_model, op_list=op_list_, iteration_list=iteration_list)
        self.evaluate(new_model, dataloader, iteration=iterations)
        observer_dict = {}
        ret = {}
        if inspect_type == 'activation' or inspect_type == 'all':
            if self.version.release >= Version("2.0.0").release:
                from torch.quantization.quantize import _get_observer_dict as get_observer_dict
            else:
                from torch.quantization import get_observer_dict
            ret['activation'] = []
            get_observer_dict(new_model._model, observer_dict)
            if iteration_list is None:
                iteration_list = [1]
            for i in iteration_list:
                summary = OrderedDict()
                for key in observer_dict:
                    if isinstance(observer_dict[key], torch.nn.modules.linear.Identity):
                        continue
                    op_name = key.replace(".activation_post_process", "")
                    value = observer_dict[key].get_tensor_value()[i]
                    if op_name in op_list:
                        if type(value) is list:
                            summary[op_name] = {}
                            for index in range(len(value)):
                                summary[op_name].update({
                                    op_name + ".output" + str(index):
                                    dequantize(value[index]).numpy()
                                    if value[index].is_quantized else value[index].numpy()
                                })
                        else:
                            summary[op_name] = {
                                op_name + ".output0":
                                dequantize(value).numpy() if value.is_quantized else value.numpy()
                            }
                    else:
                        if bool(self.fused_dict):
                            if is_quantized:
                                for a in fp32_int8_map:
                                    if op_name == fp32_int8_map[a]['weight']:
                                        if type(value) is list:
                                            summary[a] = {}
                                            for index in range(len(value)):
                                                summary[a].update({
                                                    op_name + ".output" + str(index):
                                                    dequantize(value[index]).numpy()
                                                    if value[index].is_quantized else
                                                    value[index].numpy()
                                                })
                                        else:
                                            summary[a] = {
                                                op_name + ".output0":
                                                dequantize(value).numpy()
                                                if value.is_quantized else value.numpy()
                                            }
                            else:
                                for a in fp32_int8_map:  # pragma: no cover
                                    if op_name == fp32_int8_map[a]['activation']:
                                        if type(value) is list:
                                            summary[a] = {}
                                            for index in range(len(value)):
                                                summary[a].update({
                                                    op_name + ".output" + str(index):
                                                    dequantize(value[index]).numpy()
                                                    if value[index].is_quantized else
                                                    value[index].numpy()
                                                })
                                        else:
                                            summary[a] = {
                                                op_name + ".output0":
                                                dequantize(value).numpy()
                                                if value.is_quantized else value.numpy()
                                            }

                if save_to_disk:
                    dump_dir = os.path.join(self.workspace_path, 'dump_tensor')
                    os.makedirs(dump_dir, exist_ok=True)
                    np.savez(os.path.join(dump_dir, 'activation_iter{}.npz'.format(i)), **summary)

                ret['activation'].append(summary)

        if inspect_type == 'weight' or inspect_type == 'all':
            ret['weight'] = {}
            state_dict = new_model._model.state_dict()

            for key in state_dict:
                if not isinstance(state_dict[key], torch.Tensor):
                    continue
                if 'weight' not in key and 'bias' not in key:
                    continue

                op = key[:key.rfind('.')]
                op = op.replace('._packed_params', '')

                if op in op_list:
                    if op in ret['weight']:
                        ret['weight'][op].update({
                            key:
                            dequantize(state_dict[key]).numpy()
                            if state_dict[key].is_quantized else state_dict[key].detach().numpy()
                        })
                    else:
                        ret['weight'][op] = {
                            key:
                            dequantize(state_dict[key]).numpy()
                            if state_dict[key].is_quantized else state_dict[key].detach().numpy()
                        }
                else:
                    if bool(self.fused_dict):
                        if is_quantized:
                            for a in fp32_int8_map:
                                if op == fp32_int8_map[a]['weight']:
                                    if a in ret['weight']:
                                        ret['weight'][a].update({
                                            key:
                                            dequantize(state_dict[key]).numpy()
                                            if state_dict[key].is_quantized else
                                            state_dict[key].detach().numpy()
                                        })
                                    else:
                                        ret['weight'][a] = \
                                            {key: dequantize(state_dict[key]).numpy()
                                                if state_dict[key].is_quantized else
                                                    state_dict[key].detach().numpy()}
                                    break

            if save_to_disk:
                np.savez(os.path.join(dump_dir, 'weight.npz'), **ret['weight'])
        else:
            ret['weight'] = None

        return ret

    def set_tensor(self, model, tensor_dict):
        state_dict = model._model.state_dict()
        tensor_name = None
        for key in tensor_dict.keys():
            end = key.rfind('.')
            op_name = key[:end]
            state_op_name = None
            weight_bias = key[end + 1:]
            for op in self.fused_dict:
                if op_name in self.fused_dict[op]:
                    state_op_name = op
            if state_op_name is None:
                state_op_name = op_name
            for state_dict_key in state_dict.keys():
                state_key_end = state_dict_key.rfind('.')
                state_key = state_dict_key[:state_key_end].replace('._packed_params', '')
                if weight_bias in state_dict_key and state_op_name == state_key:
                    tensor_name = state_dict_key
            assert tensor_name is not None, key + " is not in the state dict"
            tensor = torch.from_numpy(tensor_dict[key])
            dtype = state_dict[tensor_name].dtype
            if state_dict[tensor_name].is_quantized:
                if 'channel' in str(state_dict[tensor_name].qscheme()):
                    scales = state_dict[tensor_name].q_per_channel_scales()
                    zero_points = state_dict[tensor_name].q_per_channel_zero_points()
                    axis = state_dict[tensor_name].q_per_channel_axis()
                    state_dict[tensor_name] = torch.quantize_per_channel(tensor,
                                                                         scales,
                                                                         zero_points,
                                                                         axis,
                                                                         dtype=dtype)
                elif 'tensor' in str(state_dict[tensor_name].qscheme()):
                    scales = state_dict[tensor_name].q_scale()
                    zero_points = state_dict[tensor_name].q_zero_point()
                    state_dict[tensor_name] = torch.quantize_per_tensor(
                        tensor, scales, zero_points, dtype)
            else:
                state_dict[tensor_name] = tensor
        model._model.load_state_dict(state_dict)

    @dump_elapsed_time("Pass query framework capability")
    def query_fw_capability(self, model):
        """This is a helper function to get all quantizable ops from model.

        Args:
            model (object): input model which is Neural Compressor model

        Returns:
            q_capability (dictionary): tuning capability for each op from model.
        """
        self.pre_optimized_model = model
        self.non_quant_dict = self.get_non_quant_modules(model.kwargs)
        return self._get_quantizable_ops(model.model)

    def get_non_quant_modules(self, model_kwargs):
        """This is a helper function to get all non_quant_modules from customer and default.

        Args:
            model_kwargs (dictionary): keyword args from Neural Compressor model

        Returns:
            custom_non_quant_dict (dictionary): non_quant_modules for model.
        """
        if model_kwargs is None:
            model_kwargs = {}
        skipped_module_names = model_kwargs.get("non_quant_module_name", [])
        skipped_module_classes = model_kwargs.get("non_quant_module_class", [])
        custom_non_quant_dict = {
            'skipped_module_names': skipped_module_names,
            'skipped_module_classes': skipped_module_classes
        }
        # Ignore LayerNorm, InstanceNorm3d and Embedding quantizable ops,
        # due to huge accuracy regression in PyTorch.
        additional_skipped_module_classes = ['LayerNorm', 'InstanceNorm3d', 'Embedding', 'Dropout']
        if self.approach == 'post_training_dynamic_quant':
            additional_skipped_module_classes.remove('Embedding')
        custom_non_quant_dict['skipped_module_classes'] += additional_skipped_module_classes
        return custom_non_quant_dict


unify_op_type_mapping_ipex = {
    "Convolution_Relu": "conv2d",
    "Convolution_Sum_Relu": "conv2d",
    "Convolution_BatchNorm": "conv2d",
    "<class 'torch.nn.modules.conv.Conv1d'>": "conv1d",
    "<class 'torch.nn.modules.conv.Conv2d'>": "conv2d",
    "<class 'torch.nn.modules.conv.Conv3d'>": "conv3d",
    "<class 'torch.nn.modules.activation.ReLU'>": "relu",
    "<method 'add' of 'torch._C._TensorBase' objects>": "add",
    "<class 'torch.nn.modules.pooling.AdaptiveAvgPool2d'>": "adaptiveavgpool2d",
    "Linear_Relu": "linear",
    "<class 'torch.nn.modules.linear.Linear'>": "linear",
    "<class 'torch.nn.modules.pooling.MaxPool2d'>": "maxpool2d"
}


@adaptor_registry
class PyTorch_IPEXAdaptor(TemplateAdaptor):  # pragma: no cover
    """Adaptor of PyTorch framework with Intel PyTorch Extension,
       all PyTorch IPEX API is in this class.

    Args:
        framework_specific_info (dict): dictionary of tuning configure from yaml file.
    """
    def __init__(self, framework_specific_info):
        super(PyTorch_IPEXAdaptor, self).__init__(framework_specific_info)
        self.version = get_torch_version()
        query_config_file = "pytorch_ipex.yaml"
        self.query_handler = PyTorchQuery(
            local_config_file=os.path.join(os.path.dirname(__file__), query_config_file))
        self.cfgs = None
        self.fuse_ops = None
        self.op_infos_from_cfgs = None
        self.output_tensor_id_op_name = None
        self.ipex_config_path = \
            os.path.join(self.workspace_path, 'ipex_config_tmp.json')

        try:
            os.remove(self.ipex_config_path)
        except:
            logger.warning('Fail to remove {}.'.format(self.ipex_config_path))
        self.device = 'ipex'
        self.tmp_model = None

    @dump_elapsed_time("Pass quantize model")
    def quantize(self, tune_cfg, model, dataloader, q_func=None):
        """Execute the quantize process on the specified model.

        Args:
            tune_cfg (dict): quantization config.
            model (object): model need to do quantization, it is Neural Compressor model.
            dataloader (object): calibration dataset.
            q_func (objext, optional): training function for quantization aware training mode.

        Returns:
            (dict): quantized model
        """

        assert self.approach != 'quant_aware_training', \
            "Intel PyTorch Extension didn't support quantization aware training mode"
        assert not self.version.release < Version("1.10.0").release, \
                "INC support IPEX version >= 1.10.0"

        qscheme = self._cfg_to_qconfig(tune_cfg)
        iterations = tune_cfg.get('calib_iteration', 1)
        model.model.eval()

        if self.performance_only:
            if hasattr(model.model, "save_qconf_summary"):
                q_model = model.model
                q_model.load_qconf_summary(qconf_summary=self.ipex_config_path)
                if q_func is not None:
                    q_func(q_model)
                else:
                    self.model_calibration(q_model, dataloader, iterations, None,
                                           tune_cfg.get('calib_sampling_size', 1))
                q_model.save_qconf_summary(qconf_summary=self.ipex_config_path)
                if self.use_bf16 and (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1') and \
                    (self.version.release >= Version("1.11.0").release):
                    with torch.no_grad():
                        with torch.cpu.amp.autocast():
                            q_model = ipex.quantization.convert(q_model, inplace=True)
                            try:
                                q_model = torch.jit.trace(q_model, self.example_inputs)
                                q_model = torch.jit.freeze(q_model.eval())
                            except:
                                q_model = torch.jit.trace(q_model, self.example_inputs, strict=False)
                                q_model = torch.jit.freeze(q_model.eval())
                else:
                    q_model = ipex.quantization.convert(q_model, inplace=True)
                    with torch.no_grad():
                        try:
                            q_model = torch.jit.trace(q_model, self.example_inputs)
                            q_model = torch.jit.freeze(q_model.eval())
                        except:
                            q_model = torch.jit.trace(q_model, self.example_inputs, strict=False)
                            q_model = torch.jit.freeze(q_model.eval())
                # After freezing, run 1 time to warm up the profiling graph executor to insert prim::profile
                # At the 2nd run, the llga pass will be triggered and the model is turned into
                # an int8 model: prim::profile will be removed and will have LlgaFusionGroup in the graph
                self.calib_func(q_model, dataloader, tmp_iterations=2)
            else:
                assert not self.version.release < Version("1.10.0").release, \
                    "INC support IPEX version >= 1.10.0"
                if self.approach in ['post_training_static_quant', 'post_training_auto_quant']:
                    q_model = model.model
                    if self.version.release < Version("1.12.0").release:
                        ipex_conf = ipex.quantization.QuantConf(configure_file=self.ipex_config_path,  # pylint: disable=E1101
                                                                qscheme=qscheme)
                        self.model_calibration(q_model, dataloader, iterations, ipex_conf,
                                               tune_cfg.get('calib_sampling_size', 1))
                        ipex_conf.save(self.ipex_config_path)
                        ipex_conf = ipex.quantization.QuantConf(self.ipex_config_path)   # pylint: disable=E1101
                        q_model = ipex.quantization.convert(q_model,
                                                            ipex_conf,
                                                            self.example_inputs,
                                                            inplace=True)  # pylint: disable=E1121
                    else:
                        from torch.ao.quantization import MinMaxObserver, PerChannelMinMaxObserver, QConfig
                        static_qconfig = QConfig(activation=MinMaxObserver.with_args(
                            qscheme=torch.per_tensor_affine, dtype=torch.quint8),
                            weight=PerChannelMinMaxObserver.with_args(dtype=torch.qint8, \
                                        qscheme=torch.per_channel_symmetric))

                        q_model = ipex.quantization.prepare(model._model, static_qconfig, \
                                                example_inputs=self.example_inputs, inplace=True)
                        q_model.load_qconf_summary(qconf_summary=self.ipex_config_path)
                        if q_func is not None:
                            q_func(q_model)
                        else:
                            self.model_calibration(q_model, dataloader, iterations, None,
                                                   tune_cfg.get('calib_sampling_size', 1))
                        q_model.save_qconf_summary(qconf_summary=self.ipex_config_path)
                        if self.use_bf16 and (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1') and \
                            (self.version.release >= Version("1.11.0").release):
                            with torch.no_grad():
                                with torch.cpu.amp.autocast():
                                    q_model = ipex.quantization.convert(q_model, inplace=True)
                                    try:
                                        q_model = torch.jit.trace(q_model, self.example_inputs)
                                        q_model = torch.jit.freeze(q_model.eval())
                                    except:
                                        q_model = torch.jit.trace(q_model, self.example_inputs, strict=False)
                                        q_model = torch.jit.freeze(q_model.eval())
                        else:
                            q_model = ipex.quantization.convert(q_model, inplace=True)
                            with torch.no_grad():
                                try:
                                    q_model = torch.jit.trace(q_model, self.example_inputs)
                                    q_model = torch.jit.freeze(q_model.eval())
                                except:
                                    q_model = torch.jit.trace(q_model, self.example_inputs, strict=False)
                                    q_model = torch.jit.freeze(q_model.eval())
                        # After freezing, run 1 time to warm up the profiling graph executor to insert prim::profile
                        # At the 2nd run, the llga pass will be triggered and the model is turned into
                        # an int8 model: prim::profile will be removed and will have LlgaFusionGroup in the graph
                        self.calib_func(q_model, dataloader, tmp_iterations=2)
            model._model = q_model
            with open(self.ipex_config_path, 'r') as f:
                model.tune_cfg = json.load(f)
            model.ipex_config_path = self.ipex_config_path
            return model
        else:
            if self.tmp_model is None:
                try:
                    self.tmp_model = copy.deepcopy(model)
                except Exception as e:  # pragma: no cover
                    logger.warning("Fail to deep copy the model due to {}, inplace is used now.".format(
                        repr(e)))
                    self.tmp_model = model
            if hasattr(model.model, "save_qconf_summary"):
                if self.tmp_model is None:
                    try:
                        self.tmp_model = copy.deepcopy(model)
                    except Exception as e:  # pragma: no cover
                        logger.warning("Fail to deep copy the model due to {}, inplace is used now.".format(
                            repr(e)))
                        self.tmp_model = model
                q_model = model.model
                q_model.load_qconf_summary(qconf_summary=self.ipex_config_path)
                if q_func is not None:
                    q_func(q_model)
                else:
                    self.model_calibration(q_model, dataloader, iterations, None,
                                           tune_cfg.get('calib_sampling_size', 1))
                q_model.save_qconf_summary(qconf_summary=self.ipex_config_path)
                if self.use_bf16 and (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1') and \
                    (self.version.release >= Version("1.11.0").release):
                    with torch.no_grad():
                        with torch.cpu.amp.autocast():
                            q_model = ipex.quantization.convert(q_model, inplace=False)
                            try:
                                q_model = torch.jit.trace(q_model, self.example_inputs)
                                q_model = torch.jit.freeze(q_model.eval())
                            except:
                                q_model = torch.jit.trace(q_model, self.example_inputs, strict=False)
                                q_model = torch.jit.freeze(q_model.eval())
                else:
                    q_model = ipex.quantization.convert(q_model, inplace=False)
                    with torch.no_grad():
                        try:
                            q_model = torch.jit.trace(q_model, self.example_inputs)
                            q_model = torch.jit.freeze(q_model.eval())
                        except:
                            q_model = torch.jit.trace(q_model, self.example_inputs, strict=False)
                            q_model = torch.jit.freeze(q_model.eval())
                # After freezing, run 1 time to warm up the profiling graph executor to insert prim::profile
                # At the 2nd run, the llga pass will be triggered and the model is turned into
                # an int8 model: prim::profile will be removed and will have LlgaFusionGroup in the graph
                self.calib_func(q_model, dataloader, tmp_iterations=2)
            else:
                if self.approach in ['post_training_static_quant', 'post_training_auto_quant']:
                    if self.version.release < Version("1.12.0").release:
                        try:
                            self.tmp_model = copy.deepcopy(model)
                        except Exception as e:  # pragma: no cover
                            logger.warning("Fail to deep copy the model due to {}, inplace is used now.".format(
                                repr(e)))
                            self.tmp_model = model
                        ipex_conf = ipex.quantization.QuantConf(configure_file=self.ipex_config_path,  # pylint: disable=E1101
                                                                qscheme=qscheme)
                        self.model_calibration(self.tmp_model.model, dataloader, iterations, ipex_conf,
                                               tune_cfg.get('calib_sampling_size', 1))
                        ipex_conf.save(self.ipex_config_path)
                        ipex_conf = ipex.quantization.QuantConf(self.ipex_config_path)   # pylint: disable=E1101
                        q_model = ipex.quantization.convert(self.tmp_model.model,
                                                            ipex_conf,
                                                            self.example_inputs,
                                                            inplace=True)  # pylint: disable=E1121
                    else:
                        if self.tmp_model is None:
                            try:
                                self.tmp_model = copy.deepcopy(model)
                            except Exception as e:  # pragma: no cover
                                logger.warning("Fail to deep copy the model due to {}, inplace is used now.".format(
                                    repr(e)))
                                self.tmp_model = model
                        from torch.ao.quantization import MinMaxObserver, PerChannelMinMaxObserver, QConfig
                        static_qconfig = QConfig(activation=MinMaxObserver.with_args(
                            qscheme=torch.per_tensor_affine, dtype=torch.quint8),
                            weight=PerChannelMinMaxObserver.with_args(dtype=torch.qint8, \
                                        qscheme=torch.per_channel_symmetric))

                        q_model = ipex.quantization.prepare(model._model, static_qconfig, \
                                                example_inputs=self.example_inputs, inplace=False)
                        q_model.load_qconf_summary(qconf_summary=self.ipex_config_path)
                        if q_func is not None:
                            q_func(q_model)
                        else:
                            self.model_calibration(q_model, dataloader, iterations, None,
                                                   tune_cfg.get('calib_sampling_size', 1))
                        q_model.save_qconf_summary(qconf_summary=self.ipex_config_path)
                        if self.use_bf16 and (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1') and \
                            (self.version.release >= Version("1.11.0").release):
                            with torch.no_grad():
                                with torch.cpu.amp.autocast():
                                    q_model = ipex.quantization.convert(q_model, inplace=True)
                                    try:
                                        q_model = torch.jit.trace(q_model, self.example_inputs)
                                        q_model = torch.jit.freeze(q_model.eval())
                                    except:
                                        q_model = torch.jit.trace(q_model, self.example_inputs, strict=False)
                                        q_model = torch.jit.freeze(q_model.eval())
                        else:
                            q_model = ipex.quantization.convert(q_model, inplace=True)
                            with torch.no_grad():
                                try:
                                    q_model = torch.jit.trace(q_model, self.example_inputs)
                                    q_model = torch.jit.freeze(q_model.eval())
                                except:
                                    q_model = torch.jit.trace(q_model, self.example_inputs, strict=False)
                                    q_model = torch.jit.freeze(q_model.eval())
                        # After freezing, run 1 time to warm up the profiling graph executor to insert prim::profile
                        # At the 2nd run, the llga pass will be triggered and the model is turned into
                        # an int8 model: prim::profile will be removed and will have LlgaFusionGroup in the graph
                        self.calib_func(q_model, dataloader, tmp_iterations=2)

            self.tmp_model._model = q_model
            with open(self.ipex_config_path, 'r') as f:
                self.tmp_model.tune_cfg = json.load(f)
            self.tmp_model.ipex_config_path = self.ipex_config_path
            return self.tmp_model

    def _cfg_to_qconfig(self, tune_cfg):
        """Convert tune configure to quantization config for each op.

            Args:
                tune_cfg (dict): dictionary of tune configure for each op
                ipex_config_path: configure file of Intel PyTorch Extension

            tune_cfg should be a format like below:
            {
              'calib_iteration': 10,
              'op': {
                 ('op1', 'CONV2D'): {
                   'activation':  {'dtype': 'uint8',
                                   'algorithm': 'minmax',
                                   'scheme':'sym',
                                   'granularity': 'per_tensor'},
                   'weight': {'dtype': 'int8',
                              'algorithm': 'kl',
                              'scheme':'asym',
                              'granularity': 'per_channel'}
                 },
                 ('op2', 'RELU): {
                   'activation': {'dtype': 'int8',
                   'scheme': 'asym',
                   'granularity': 'per_tensor',
                   'algorithm': 'minmax'}
                 },
                 ('op3', 'CONV2D'): {
                   'activation':  {'dtype': 'fp32'},
                   'weight': {'dtype': 'fp32'}
                 },
                 ...
              }
            }
        """
        assert self.cfgs is not None, "No configure for IPEX int8 model..."
        if self.version.release < Version("1.12.0").release:
            for key in tune_cfg['op']:
                try:
                    scheme = tune_cfg['op'][key]['activation']['scheme']
                except:
                    scheme = 'asym'
                if scheme not in ['asym', 'sym']:
                    scheme = 'asym'
                break
            for key in tune_cfg['op']:
                value = tune_cfg['op'][key]
                pattern = self.get_pattern(key, self.fuse_ops)
                assert isinstance(value, dict)
                assert 'activation' in value
                if value['activation']['dtype'] == 'fp32':
                    if 'weight' in value:
                        assert value['weight']['dtype'] == 'fp32'
                    for op_cfg in self.cfgs:
                        if op_cfg["id"] == key[0]:
                            if key[1] in ['relu_', 'add_']:
                                continue
                            num_inputs = len(op_cfg["inputs_quantized"])
                            num_outputs = len(op_cfg["outputs_quantized"])
                            for i_num in range(num_inputs):
                                op_cfg["inputs_quantized"][i_num] = False
                            for o_num in range(num_outputs):
                                op_cfg["outputs_quantized"][o_num] = False
                            if pattern:
                                if pattern[1] in ['relu_', 'add_']:
                                    continue
                                tune_cfg['op'][pattern]['activation']['dtype'] = 'fp32'
                                if 'weight' in tune_cfg['op'][pattern]:
                                    tune_cfg['op'][pattern]['weight']['dtype'] = 'fp32'
                else:
                    for op_cfg in self.cfgs:
                        if op_cfg["id"] == key[0]:
                            if key[1] in ['relu_', 'add_']:
                                continue
                            num_inputs = len(op_cfg["inputs_quantized"])
                            num_outputs = len(op_cfg["outputs_quantized"])
                            for i_num in range(num_inputs):
                                op_cfg["inputs_quantized"][i_num] = \
                                              self.default_cfgs[key[0]]["inputs_quantized"][i_num]
                            for o_num in range(num_outputs):
                                op_cfg["outputs_quantized"][o_num] = \
                                             self.default_cfgs[key[0]]["outputs_quantized"][o_num]
            with open(self.ipex_config_path, "w") as write_f:
                json.dump(self.cfgs, write_f)
            if scheme == "asym":
                return torch.per_tensor_affine
            else:
                return torch.per_tensor_symmetric
        else:
            self.cfgs = torch_utils.util.check_cfg_and_qconfig(tune_cfg['op'],
                                              self.cfgs,
                                              self.op_infos_from_cfgs,
                                              self.output_tensor_id_op_name)

            with open(self.ipex_config_path, "w") as write_f:
                json.dump(self.cfgs, write_f, indent=4)
            return None

    def get_pattern(self, fallback_op, fuse_ops):
        for fuse_pattern in fuse_ops:
            if fuse_pattern[0] == fallback_op:
                if fuse_pattern[1] in ['relu_', 'add_']:
                    return None
                else:
                    return fuse_pattern[1]
        return None

    def evaluate(self,
                 model,
                 dataloader,
                 postprocess=None,
                 metrics=None,
                 measurer=None,
                 iteration=-1,
                 tensorboard=False,
                 fp32_baseline=False):
        """Execute the evaluate process on the specified model.

        Args:
            model (object): Neural Compressor model to run evaluation.
            dataloader (object): evaluation dataset.
            postprocess (object, optional): process function after evaluation.
            metrics (list, optional): list of metric function.
            measurer (object, optional): measurer function.
            iteration (int, optional): number of iterations to evaluate.
            tensorboard (bool, optional): dump output tensor to tensorboard summary
                                          files(IPEX unspport).
            fp32_baseline (boolen, optional): only for compare_label=False pipeline

        Returns:
            (dict): quantized model
        """

        assert not tensorboard, "Intel PyTorch Extension didn't tensor dump"
        self.is_baseline = fp32_baseline

        model_ = model._model
        model_.eval()

        if metrics:
            self.fp32_preds_as_label = any([hasattr(metric, "compare_label") and \
                not metric.compare_label for metric in metrics])

        ipex_config = (self.ipex_config_path if not self.benchmark else None)
        if self.version.release < Version("1.12.0").release:
            conf = (ipex.quantization.QuantConf(configure_file=ipex_config)   # pylint: disable=E1101
                    if not self.is_baseline else None)
        else:
            conf = None

        return self.model_eval(model_, dataloader, postprocess, metrics, measurer, iteration, conf)

    @dump_elapsed_time("Pass query framework capability")
    def query_fw_capability(self, model):
        """This is a helper function to get all quantizable ops from model.

        Args:
            model (object): input model which is Neural Compressor model

        Returns:
            q_capability (dictionary): tuning capability for each op from model.
        """
        self.pre_optimized_model = model
        return self._get_quantizable_ops(model.model)

    def _get_quantizable_ops_recursively(self, model, prefix, quantizable_ops):
        """This is a helper function for `query_fw_capability`,
           and it will get all quantizable ops from model.
        Args:
            model (object): input model
            prefix (string): prefix of op name
            quantizable_ops (list): list of quantizable ops from model include op name and type.
        Returns:
            None
        """

        if not os.path.exists(self.ipex_config_path):
            assert isinstance(model, torch.nn.Module), \
                    "The model passed in is not the instance of torch.nn.Module"

        if hasattr(model, "save_qconf_summary"):
            os.makedirs(os.path.dirname(self.ipex_config_path), exist_ok=True)
            model.save_qconf_summary(qconf_summary=self.ipex_config_path)
            if self.example_inputs is None:
                self.example_inputs = get_example_inputs(model, self.q_dataloader)
        else:
            if self.performance_only:
                tmp_model = model
            else:
                try:
                    tmp_model = copy.deepcopy(model)
                except Exception as e:  # pragma: no cover
                    logger.warning("Fail to deep copy the model due to {}, inplace is used now.".format(
                        repr(e)))
                    raise
            tmp_model.eval()
            # to record the origin batch_size
            if isinstance(self.q_dataloader, BaseDataLoader):
                batch_size = self.q_dataloader.batch_size

            # create a quantization config file for intel pytorch extension model
            os.makedirs(os.path.dirname(self.ipex_config_path), exist_ok=True)
            if self.version.release < Version("1.12.0").release:
                assert self.q_func is None, ("IPEX < 1.12.0 didn't support calibration function, "
                                                 "Please use IPEX >= 1.12.0!")
                ipex_conf = ipex.quantization.QuantConf(qscheme=torch.per_tensor_symmetric)   # pylint: disable=E1101
                self.model_calibration(
                    tmp_model,
                    self.q_dataloader,
                    conf=ipex_conf,
                )
                ipex_conf.save(self.ipex_config_path)
            else:
                if self.approach in ['post_training_static_quant', 'post_training_auto_quant']:
                    assert self.q_dataloader is not None, "IPEX need q_dataloader to prepare the model"
                    from torch.ao.quantization import MinMaxObserver, PerChannelMinMaxObserver, QConfig
                    static_qconfig = QConfig(activation=MinMaxObserver.with_args(
                        qscheme=torch.per_tensor_affine, dtype=torch.quint8),
                        weight=PerChannelMinMaxObserver.with_args(dtype=torch.qint8, \
                                   qscheme=torch.per_channel_symmetric))
                    if self.example_inputs is None:
                        self.example_inputs = get_example_inputs(tmp_model, self.q_dataloader)
                    tmp_model = ipex.quantization.prepare(tmp_model, static_qconfig, \
                                            example_inputs=self.example_inputs, inplace=True)
                if self.q_func is None:
                    self.model_calibration(tmp_model, self.q_dataloader)
                else:
                    self.q_func(tmp_model)
                tmp_model.save_qconf_summary(qconf_summary=self.ipex_config_path)
            if isinstance(self.q_dataloader, BaseDataLoader):
                self.q_dataloader.batch(batch_size)
                logger.info('Recovery `calibration.dataloader.batchsize` {} according \
                            to config.yaml'.format(batch_size))
            if not self.performance_only:
                del tmp_model
                import gc
                gc.collect()

        with open(self.ipex_config_path, 'r') as f:
            self.cfgs = json.load(f)
            if self.version.release < Version("1.12.0").release:
                self.default_cfgs = copy.deepcopy(self.cfgs)
                self.fuse_ops = self.get_fuse_ops(self.cfgs)
                for op_cfg in self.cfgs:
                    quantizable_ops.append(
                        (op_cfg["id"], unify_op_type_mapping_ipex[op_cfg["name"]]
                         if op_cfg["name"] in unify_op_type_mapping_ipex else op_cfg["name"]))
            else:
                ops_name, op_infos_from_cfgs, input_tensor_id_op_name, \
                                output_tensor_id_op_name = torch_utils.util.paser_cfgs(self.cfgs)
                quantizable_op_names = torch_utils.util.get_quantizable_ops_from_cfgs(ops_name,
                                                                     op_infos_from_cfgs,
                                                                     input_tensor_id_op_name)
                for name in quantizable_op_names:
                    # name : list
                    if len(name) == 1:
                        module_key = name[0][0]
                        op_cfg_id = name[0][2]
                        quantizable_ops.append((tuple(name), unify_op_type_mapping_ipex \
                                               [self.cfgs[module_key]['q_op_infos'][op_cfg_id]['op_type']] \
                                               if self.cfgs[module_key]['q_op_infos'][op_cfg_id]['op_type'] \
                                               in unify_op_type_mapping_ipex else \
                                               self.cfgs[module_key]['q_op_infos'][op_cfg_id]['op_type']))
                    else:
                        op_type = ""
                        for op_name in name:
                            module_key = op_name[0]
                            op_cfg_id = op_name[2]
                            op_type += self.cfgs[module_key]['q_op_infos'][op_cfg_id]['op_type']
                        quantizable_ops.append((tuple(name), op_type))
                self.op_infos_from_cfgs = op_infos_from_cfgs
                self.output_tensor_id_op_name = output_tensor_id_op_name
        os.remove(self.ipex_config_path)

    def get_fuse_ops(self, default_cfgs):
        elt_wise = ['relu', 'sigmoid', 'gelu']
        inplace_ops = ['relu_', 'add_']
        op_patterns = []
        num_ops = len(default_cfgs)
        for cur_id in range(num_ops):
            cur_op = default_cfgs[cur_id]['name']
            if cur_op == 'dropout':
                continue
            inputs = default_cfgs[cur_id]['inputs_flow']
            num_input = len(inputs)
            pre_ops = {}
            for i_num in range(num_input):
                inp = inputs[i_num]
                for pre_id in range(cur_id):
                    pre_op = default_cfgs[pre_id]['name']
                    pre_out = default_cfgs[pre_id]['outputs_flow']
                    num_out = len(pre_out)
                    for o_num in range(num_out):
                        if pre_out[o_num] == inp:
                            if cur_op in inplace_ops and (pre_op in ['conv2d', 'conv3d', 'linear'
                                                                     ]):
                                op_patterns.append([(pre_id, pre_op), (cur_id, cur_op)])
                            if cur_op in elt_wise and (pre_op
                                                       in ['conv2d', 'conv3d', 'linear', 'add']):
                                op_patterns.append([(pre_id, pre_op), (cur_id, cur_op)])
                            if cur_op == 'add':
                                pre_ops[i_num] = [pre_id, pre_op]
            if len(pre_ops) > 0:
                for key, value in pre_ops.items():
                    if value[1] in ['conv2d', 'conv3d', 'linear'] and \
                            default_cfgs[cur_id]['inputs_quantized'][key] == False:
                        op_patterns.append([(value[0], value[1]), (cur_id, cur_op)])
        return op_patterns

    @dump_elapsed_time("Pass save quantized model")
    def save(self, model, path=None):
        """The function is used by tune strategy class for set best configure in Neural Compressor model.

           Args:
               model (object): The Neural Compressor model which is best results.
               path (string): No used.

        Returns:
            None
        """

        pass

    def inspect_tensor(self,
                       model,
                       dataloader,
                       op_list=None,
                       iteration_list=None,
                       inspect_type='activation',
                       save_to_disk=False):
        assert False, "Inspect_tensor didn't support IPEX backend now!"


@adaptor_registry
class PyTorch_FXAdaptor(TemplateAdaptor):
    """Adaptor of PyTorch framework with FX graph mode, all PyTorch API is in this class.

    Args:
        framework_specific_info (dict): dictionary of tuning configure from yaml file.
    """
    def __init__(self, framework_specific_info):
        super(PyTorch_FXAdaptor, self).__init__(framework_specific_info)
        assert self.version.release >= Version("1.8.0").release, \
                      "Please use PyTroch 1.8 or higher version with pytorch_fx backend！"
        if self.approach == 'post_training_dynamic_quant':
            assert self.version.release >= Version("1.9.0").release, \
                        "Please use PyTroch 1.9 or higher version for dynamic " \
                        "quantization with pytorch_fx backend！"
        import torch.quantization as tq
        """
        # Map for swapping float module to quantized ones,
        # and this dictionary will change with different PoTorch versions
        DEFAULT_MODULE_MAPPING = {
            nn.Linear: nnq.Linear,
            nn.ReLU: nnq.ReLU,
            nn.ReLU6: nnq.ReLU6,
            nn.Conv2d: nnq.Conv2d,
            nn.Conv3d: nnq.Conv3d,
            QuantStub: nnq.Quantize,
            DeQuantStub: nnq.DeQuantize,
            # Wrapper Modules:
            nnq.FloatFunctional: nnq.QFunctional,
            # Intrinsic modules:
            nni.ConvReLU2d: nniq.ConvReLU2d,
            nni.ConvReLU3d: nniq.ConvReLU3d,
            nni.LinearReLU: nniq.LinearReLU,
            nniqat.ConvReLU2d: nniq.ConvReLU2d,
            nniqat.LinearReLU: nniq.LinearReLU,
            nniqat.ConvBn2d: nnq.Conv2d,
            nniqat.ConvBnReLU2d: nniq.ConvReLU2d,
            # QAT modules:
            nnqat.Linear: nnq.Linear,
            nnqat.Conv2d: nnq.Conv2d,
        }
        """

        self.tune_cfg = None
        if self.device == "cpu":
            query_config_file = "pytorch_cpu.yaml"
        else:  # pragma: no cover
            assert False, "Unsupport this device {}".format(self.device)
        self.query_handler = PyTorchQuery(
            local_config_file=os.path.join(os.path.dirname(__file__), query_config_file))

        if self.approach == 'post_training_dynamic_quant':
            self.white_list = \
                tq.quantization_mappings.get_default_dynamic_quant_module_mappings()
        elif self.approach == 'post_training_static_quant':
            self.white_list = tq.quantization_mappings.get_default_static_quant_module_mappings()
        else:
            self.white_list = tq.quantization_mappings.get_default_qconfig_propagation_list()

    @dump_elapsed_time("Pass quantize model")
    def quantize(self, tune_cfg, model, dataloader, q_func=None):
        """Execute the quantize process on the specified model.

        Args:
            tune_cfg (dict): quantization config.
            model (object): model need to do quantization.
            dataloader (object): calibration dataset.
            q_func (objext, optional): training function for quantization aware training mode.

        Returns:
            (object): quantized model
        """

        assert isinstance(model._model, torch.nn.Module), \
               "The model passed in is not the instance of torch.nn.Module"
        self.tune_cfg = tune_cfg
        self.tune_cfg["approach"] = self.approach
        self.tune_cfg["reduce_range"] = REDUCE_RANGE
        self.tune_cfg["framework"] = "pytorch_fx"

        # PyTorch 1.13 and above version, need example_inputs for fx trace, but it not realy used,
        # so set it to None.
        self.example_inputs = None

        if self.default_qconfig is not None:
            default_qconfig = copy.deepcopy(self.default_qconfig)
            default_qconfig['activation']['dtype'] = \
                self.default_qconfig['activation']['dtype'][0]
            default_qconfig['weight']['dtype'] = self.default_qconfig['weight']['dtype'][0]
            self.tune_cfg["op"][("default_qconfig", "")] = default_qconfig
        op_cfgs = _cfg_to_qconfig(self.tune_cfg, self.approach)
        self.tune_cfg['bf16_ops_list'] = op_cfgs['bf16_ops_list']
        del op_cfgs['bf16_ops_list']
        gc.collect()

        from torch.quantization.quantize_fx import prepare_fx, convert_fx, prepare_qat_fx
        if self.performance_only:
            q_model = model
        else:
            try:
                q_model = copy.deepcopy(model)
                q_model.fp32_model = model.fp32_model
            except Exception as e:  # pragma: no cover
                logger.warning("Fail to deep copy the model due to {}, inplace is used now.".format(
                    repr(e)))
                q_model = model
        q_model._model.eval()
        if q_model.kwargs is not None:
            self.prepare_custom_config_dict = q_model.kwargs.get('prepare_custom_config_dict',
                                                                 None)
            self.convert_custom_config_dict = q_model.kwargs.get('convert_custom_config_dict',
                                                                 None)
        else:
            self.prepare_custom_config_dict, self.convert_custom_config_dict = None, None
        self.fx_op_cfgs = _cfgs_to_fx_cfgs(op_cfgs, self.approach)
        self.tune_cfg['fx_sub_module_list'] = self.sub_module_list
        if self.approach == 'quant_aware_training':
            q_model._model.train()
            if self.sub_module_list is None:
                tmp_model = q_model._model
                if self.version > Version("1.12.1"):  # pragma: no cover
                    # pylint: disable=E1123
                    q_model._model = prepare_qat_fx(
                        q_model._model,
                        self.fx_op_cfgs,
                        example_inputs=self.example_inputs,
                        prepare_custom_config=self.prepare_custom_config_dict
                    )
                else:
                    q_model._model = prepare_qat_fx(
                        q_model._model,
                        self.fx_op_cfgs,
                        prepare_custom_config_dict=self.prepare_custom_config_dict
                    )
            else:
                logger.info('Fx trace of the entire model failed. ' + \
                            'We will conduct auto quantization')
                PyTorch_FXAdaptor.prepare_sub_graph(
                    self.sub_module_list,
                    self.fx_op_cfgs,
                    q_model._model,
                    prefix='',
                    is_qat=True,
                    example_inputs=self.example_inputs,
                    custom_config=self.prepare_custom_config_dict
                )
            # q_func can be created by neural_compressor internal or passed by user. It's critical to
            # distinguish how q_func is passed since neural_compressor built-in functions accept
            # neural_compressor model and user defined func should accept framework model.
            # For export API
            hook_list = torch_utils.util._set_input_scale_hook(q_model._model, op_cfgs)
            q_model._model = q_func(
                q_model if getattr(q_func, 'builtin', None) else q_model._model)
            assert q_model._model is not None, "Please return a trained model in train function!"
            q_model._model.eval()
        else:
            if self.sub_module_list is None:
                tmp_model = q_model._model
                if self.version.release >= Version("1.13.0").release:  # pragma: no cover
                    # pylint: disable=E1123
                    q_model._model = prepare_fx(
                        q_model._model,
                        self.fx_op_cfgs,
                        example_inputs=self.example_inputs,
                        prepare_custom_config=self.prepare_custom_config_dict
                    )
                else:
                    q_model._model = prepare_fx(
                        q_model._model,
                        self.fx_op_cfgs,
                        prepare_custom_config_dict=self.prepare_custom_config_dict
                    )
            else:
                logger.info('Fx trace of the entire model failed, ' + \
                            'We will conduct auto quantization')
                PyTorch_FXAdaptor.prepare_sub_graph(
                    self.sub_module_list,
                    self.fx_op_cfgs,
                    q_model._model,
                    prefix='',
                    example_inputs=self.example_inputs,
                    custom_config=self.prepare_custom_config_dict
                )
            if self.approach in ['post_training_static_quant', 'post_training_auto_quant']:
                # For export API
                hook_list = torch_utils.util._set_input_scale_hook(q_model._model, op_cfgs)
                iterations = tune_cfg.get('calib_iteration', 1)
                if q_func is not None:
                    q_func(q_model._model)
                else:
                    self.model_calibration(
                        q_model._model,
                        dataloader,
                        iterations,
                        calib_sampling_size=tune_cfg.get('calib_sampling_size', 1)
                    )

        if self.approach != 'post_training_dynamic_quant':
            # For export API
            scale_info = torch_utils.util._get_input_scale(q_model._model, hook_list)

        if self.sub_module_list is None:
            if self.version.release >= Version("1.13.0").release:  # pragma: no cover
                # pylint: disable=E1123
                q_model._model = convert_fx(
                    q_model._model,
                    convert_custom_config=self.convert_custom_config_dict
                )
            else:
                q_model._model = convert_fx(
                    q_model._model, 
                    convert_custom_config_dict=self.convert_custom_config_dict
                )
            torch_utils.util.append_attr(q_model._model, tmp_model)
            del tmp_model
            gc.collect()
        else:
            PyTorch_FXAdaptor.convert_sub_graph(
                self.sub_module_list,
                q_model._model,
                prefix='',
                custom_config=self.prepare_custom_config_dict
            )

        if len(self.tune_cfg['bf16_ops_list']) > 0 and \
            self.version.release >= Version("1.11.0").release and self.use_bf16 and \
            (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1'): # pragma: no cover
            q_model._model = torch_utils.bf16_convert.Convert(q_model._model, self.tune_cfg)

        q_model.q_config = copy.deepcopy(self.tune_cfg)
        if self.approach != 'post_training_dynamic_quant':
            self._get_scale_zeropoint(q_model._model, q_model.q_config)
            q_model.q_config['scale_info'] = scale_info

        self._dump_model_op_stats(q_model._model, q_model.q_config, self.approach)
        torch_utils.util.get_embedding_contiguous(q_model._model)
        return q_model

    def evaluate(self,
                 model,
                 dataloader,
                 postprocess=None,
                 metrics=None,
                 measurer=None,
                 iteration=-1,
                 tensorboard=False,
                 fp32_baseline=False):
        """Execute the evaluate process on the specified model.

        Args:
            model (object): model to run evaluation.
            dataloader (object): evaluation dataset.
            postprocess (object, optional): process function after evaluation.
            metric (object, optional): metric function.
            measurer (object, optional): measurer function.
            iteration (int, optional): number of iterations to evaluate.
            tensorboard (bool, optional): dump output tensor to tensorboard summary files.
            fp32_baseline (boolen, optional): only for compare_label=False pipeline

        Returns:
            (object): accuracy
        """
        if tensorboard:  # pragma: no cover
            assert False, "PyTorch FX mode didn't support tensorboard flag now!"
        self.is_baseline = fp32_baseline

        model_ = model._model
        assert isinstance(
            model_, torch.nn.Module), "The model passed in is not the instance of torch.nn.Module"
        model_.eval()
        model_.to(self.device)

        if metrics:
            self.fp32_preds_as_label = any([hasattr(metric, "compare_label") and \
                not metric.compare_label for metric in metrics])

        return self.model_eval(model_, dataloader, postprocess, metrics, measurer, iteration)

    def _pre_hook_for_qat(self, dataloader=None):
        q_cfgs = torch.quantization.QConfig(
                            activation=torch.quantization.FakeQuantize.with_args(
                                    dtype=torch.quint8,
                                    qscheme=torch.per_tensor_affine,
                                    reduce_range=REDUCE_RANGE,
                                    observer=torch.quantization.MovingAverageMinMaxObserver),
                            weight=torch.quantization.default_weight_fake_quant) \
                        if self.version.release < Version("1.10.0").release else \
                          torch.quantization.QConfig(
                            activation=torch.quantization.FusedMovingAvgObsFakeQuantize.with_args(
                                       dtype=torch.quint8,
                                       qscheme=torch.per_tensor_affine,
                                       reduce_range=REDUCE_RANGE),
                            weight=torch.quantization.default_fused_per_channel_wt_fake_quant)
        quantizable_ops = []
        tmp_model = self.fuse_fx_model(self.model, is_qat=True)
        self._get_quantizable_ops_recursively(tmp_model, '', quantizable_ops)
        bf16_ops = []
        if self.version.release >= Version("1.11.0").release and self.use_bf16 and \
            (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1'): # pragma: no cover
            self.bf16_ops = self.query_handler.get_op_types_by_precision("bf16")
            self._get_bf16_ops_recursively(tmp_model, '', bf16_ops)
        bf16_ops_list = [(op) for op in bf16_ops if op not in quantizable_ops]
        quantized_ops = OrderedDict()
        for op in quantizable_ops:
            if op[1] in [
                    'Embedding', 'EmbeddingBag', 'LSTM', 'GRU', 'LSTMCell', 'GRUCell', 'RNNCell'
            ]:
                quantized_ops[op[0]] = torch.quantization.default_dynamic_qconfig
            else:
                quantized_ops[op[0]] = q_cfgs
        # build op_config_dict to save module scale and zeropoint
        op_config_dict = {}
        for op in quantizable_ops:
            op_config_dict[op] = {'weight': {'dtype': 'int8'}, 'activation': {'dtype': 'uint8'}}

        if self.version.release < Version("1.11.0").release:
            quantized_ops["default_qconfig"] = None
        else:
            from torch.ao.quantization import default_embedding_qat_qconfig
            for op in quantizable_ops:
                if op[1] in ['Embedding', 'EmbeddingBag']:
                    quantized_ops[op[0]] = default_embedding_qat_qconfig
        from torch.quantization.quantize_fx import prepare_qat_fx
        fx_op_cfgs = _cfgs_to_fx_cfgs(quantized_ops, 'quant_aware_training')
        self.model._model.train()

        # PyTorch 1.13 and above version, need example_inputs for fx trace, but it not realy used,
        # so set it to None.
        self.example_inputs = None

        # For export API, deepcopy fp32_model
        try:
            self.model.fp32_model = copy.deepcopy(self.model.fp32_model)
        except Exception as e:  # pragma: no cover
            logger.warning("Fail to deep copy the model due to {}, inplace is used now.".format(
                repr(e)))

        if self.sub_module_list is None:
            if self.version.release >= Version("1.13.0").release:  # pragma: no cover
                # pylint: disable=E1123
                self.model._model = prepare_qat_fx(
                    self.model._model,
                    fx_op_cfgs,
                    example_inputs=self.example_inputs,
                    prepare_custom_config=self.model.kwargs.get(
                        'prepare_custom_config_dict', None) if self.model.kwargs is not None else None)
            else:
                self.model._model = prepare_qat_fx(
                    self.model._model,
                    fx_op_cfgs,
                    prepare_custom_config_dict=self.model.kwargs.get(
                        'prepare_custom_config_dict', None) if self.model.kwargs is not None else None)
        else:
            logger.info('Fx trace of the entire model failed. ' + \
                        'We will conduct auto quantization')
            PyTorch_FXAdaptor.prepare_sub_graph(self.sub_module_list,
                                                fx_op_cfgs,
                                                self.model._model,
                                                prefix='',
                                                is_qat=True,
                                                example_inputs=self.example_inputs)
        # This is a flag for reloading
        self.model.q_config = {
            'calib_sampling_size': 100, # tmp arg for export API
            'is_oneshot': True,
            'framework': 'pytorch_fx',
            'reduce_range': REDUCE_RANGE,
            'quantizable_ops': quantizable_ops,
            'bf16_ops_list': bf16_ops_list,
            'op': op_config_dict,
            'sub_module_list': self.sub_module_list,
            'approach': 'quant_aware_training'
        }
        # For export API
        global hook_list
        hook_list = torch_utils.util._set_input_scale_hook(self.model._model, quantized_ops)

    def _post_hook_for_qat(self):
        # For export API
        scale_info = torch_utils.util._get_input_scale(self.model._model, hook_list)
        self.model.q_config['scale_info'] = scale_info
        from torch.quantization.quantize_fx import convert_fx
        if self.sub_module_list is None:
            if self.version > Version("1.12.1"):  # pragma: no cover
                # pylint: disable=E1123
                self.model._model = convert_fx(
                    self.model._model,
                    convert_custom_config=self.model.kwargs.get(
                        'convert_custom_config_dict', None) if self.model.kwargs is not None else None)
            else:
                self.model._model = convert_fx(
                    self.model._model,
                    convert_custom_config_dict=self.model.kwargs.get(
                        'convert_custom_config_dict', None) if self.model.kwargs is not None else None)
        else:
            PyTorch_FXAdaptor.convert_sub_graph(self.sub_module_list, \
                                                self.model._model, prefix='')

        if self.approach != 'post_training_dynamic_quant':
            self._get_scale_zeropoint(self.model._model, self.model.q_config)
        if len(self.model.q_config['bf16_ops_list']) > 0 and \
            self.version.release >= Version("1.11.0").release and self.use_bf16 and \
            (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1'): # pragma: no cover
            self.model._model = torch_utils.bf16_convert.Convert(self.model._model, self.model.q_config)
        self._dump_model_op_stats(self.model._model, self.model.q_config, self.approach)
        torch_utils.util.get_embedding_contiguous(self.model._model)

    def train(self, model, dataloader, optimizer_tuple, criterion_tuple, hooks, **kwargs):
        """Execute the train process on the specified model.

        Args:
            model (object): model to run evaluation.
            dataloader (object): training dataset.
            optimizer (tuple): It is a tuple of (cls, parameters) for optimizer.
            criterion (tuple): It is a tuple of (cls, parameters) for criterion.
            kwargs (dict, optional): other parameters.

        Returns:
            None
        """
        device = "cuda:0" if self.device != "GPU" and torch.cuda.is_available() else self.device
        self.model = model
        optimizer = optimizer_tuple[0](model._model.parameters(), **optimizer_tuple[1])
        criterion = criterion_tuple[0](**criterion_tuple[1])
        # prepare hooks first to ensure model will be converted correctly
        if hooks is not None:  # pragma: no cover
            on_train_begin = hooks['on_train_begin']
            on_train_end = hooks['on_train_end']
            on_epoch_begin = hooks['on_epoch_begin']
            on_epoch_end = hooks['on_epoch_end']
            on_step_begin = hooks['on_step_begin']
            on_step_end = hooks['on_step_end']
            on_after_compute_loss = hooks['on_after_compute_loss']
            on_before_optimizer_step = hooks['on_before_optimizer_step']
        model._model.train()
        if hooks is not None:
            on_train_begin(dataloader)
        start_epochs = kwargs['kwargs']['start_epoch']
        end_epochs = kwargs['kwargs']['end_epoch']
        iters = kwargs['kwargs']['iteration']
        model._model.to(device)
        for nepoch in range(start_epochs, end_epochs):
            cnt = 0
            if hooks is not None:
                on_epoch_begin(nepoch)
            for input, target in dataloader:
                target = target.to(device)
                if hooks is not None:
                    on_step_begin(cnt)
                print('.', end='', flush=True)
                cnt += 1
                output = pytorch_forward_wrapper(model._model, input, device=device)
                loss = criterion(output, target)
                if hooks is not None:
                    loss = on_after_compute_loss(input, output, loss)
                optimizer.zero_grad()
                loss.backward()
                if hooks is not None:
                    loss = on_before_optimizer_step()
                optimizer.step()
                if hooks is not None:
                    on_step_end()
                if cnt >= iters:
                    break
            if hooks is not None:
                on_epoch_end()

        if device != self.device:  # pragma: no cover
            model._model.to(self.device)

        if hooks is not None:
            on_train_end()

        return model._model

    def _get_module_op_stats(self, model, tune_cfg, approach):
        """This is a function to get quantizable ops of model to user.
        Args:
            model (object): input model
            tune_cfg (dict): quantization config
            approach (str): quantization approach
        Returns:
            None
        """
        modules = dict(model.named_modules())
        res = dict()

        if approach == 'post_training_dynamic_quant':
            # fetch int8 and fp32 ops set by Neural Compressor from tune_cfg
            for key in tune_cfg['op']:
                op_type = key[1]
                #build initial dict
                if op_type not in res.keys():
                    res[op_type] = {'INT8': 0, 'BF16': 0, 'FP32': 0}
                value = tune_cfg['op'][key]
                # Special cases: QuantStub, Embedding
                if ('weight' in value and value['weight']['dtype'] == 'fp32') or \
                  ('weight' not in value and value['activation']['dtype'] == 'fp32'):
                    res[op_type]['FP32'] += 1
                elif value['activation']['dtype'] == 'bf16':  # pragma: no cover
                    res[op_type]['BF16'] += 1
                else:
                    res[op_type]['INT8'] += 1
        else:
            quantized_mode = False
            for node in model.graph.nodes:
                if node.op == 'call_module':
                    if node.target not in modules:  # pragma: no cover
                        continue
                    op_class = type(modules[node.target])
                    op_type = str(op_class.__name__)
                    if 'quantized' in str(op_class) or quantized_mode:
                        if op_type not in res.keys():
                            res[op_type] = {'INT8': 0, 'BF16': 0, 'FP32': 0}
                        res[op_type]['INT8'] += 1
                    elif op_class in self.white_list:
                        if op_type not in res.keys():
                            res[op_type] = {'INT8': 0, 'BF16': 0, 'FP32': 0}
                        res[op_type]['FP32'] += 1
                    continue
                elif node.op == 'call_function':
                    op_type = str(node.target.__name__)
                else:
                    op_type = node.target
                # skip input and output
                if not "quantize_per" in op_type and not quantized_mode:
                    continue
                # skip zero_pioint and scale
                if "zero_point" in op_type or "scale" in op_type:
                    continue
                #build initial dict
                if op_type not in res.keys():
                    res[op_type] = {'INT8': 0, 'BF16': 0, 'FP32': 0}

                if "quantize_per" in op_type and not quantized_mode:
                    quantized_mode = True
                elif "dequantize" in op_type and quantized_mode:
                    quantized_mode = False
                res[op_type]['INT8'] += 1
        return res

    def _get_sub_module_op_stats(self, model, tune_cfg, approach, res, prefix=''):
        """This is a function to get quantizable ops of sub modules to user recursively.
        Args:
            model (object): input model
            tune_cfg (dict): quantization config
            approach (str): quantization approach
            res (dict) : contains result of quantizable ops
            prefix (string): prefix of op name
        Returns:
            None
        """
        for name, module in model.named_children():
            op_name = prefix + '.' + name if prefix != '' else name
            if op_name in self.sub_module_list:
                module_res = self._get_module_op_stats(module, tune_cfg, approach)
                for key, value in module_res.items():
                    if key in res:
                        res[key] = {k: res[key][k] + v for k, v in value.items()}
                    else:
                        res[key] = value
            else:
                self._get_sub_module_op_stats(module, tune_cfg, approach, res, op_name)

    def _dump_model_op_stats(self, model, tune_cfg, approach):
        """This is a function to dump quantizable ops of model to user.
        Args:
            model (object): input model
            tune_cfg (dict): quantization config
            approach (str): quantization approach
        Returns:
            None
        """
        if self.sub_module_list is None or \
          self.approach == 'post_training_dynamic_quant':
            res = self._get_module_op_stats(model, tune_cfg, approach)
        else:
            res = dict()
            self._get_sub_module_op_stats(model, tune_cfg, approach, res)

        if self.use_bf16 and (self.version.release >= Version("1.11.0").release) and \
            (CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1'): # pragma: no cover
            bf16_ops_list = tune_cfg['bf16_ops_list']
            if len(bf16_ops_list) > 0:
                for bf16_op in bf16_ops_list:
                    op_type = bf16_op[1]
                    if op_type in res.keys():
                        res[op_type]['BF16'] += 1
                        if res[op_type]['FP32'] > 0:
                            res[op_type]['FP32'] -= 1
                    else:
                        res[op_type] = {'INT8': 0, 'BF16': 1, 'FP32': 0}


        output_data = [[
            op_type,
            sum(res[op_type].values()), res[op_type]['INT8'], res[op_type]['BF16'],
            res[op_type]['FP32']
        ] for op_type in res.keys()]

        Statistics(output_data,
                   header='Mixed Precision Statistics',
                   field_names=["Op Type", "Total", "INT8", "BF16", "FP32"]).print_stat()

    def _get_quantizable_ops_recursively(self, model, prefix, quantizable_ops):
        """This is a helper function for `query_fw_capability`,
           and it will get all quantizable ops from model.

        Args:
            model (object): input model
            prefix (string): prefix of op name
            quantizable_ops (list): list of quantizable ops from model include op name and type.

        Returns:
            None
        """
        module_dict = dict(model.named_modules())
        for op_name, child in model.named_modules():
            if self.is_fused_module(child):
                for name, _ in child.named_children():
                    module_prefix = op_name + '.' + name
                    if module_prefix in module_dict:
                        module_dict.pop(module_prefix)  # remove sub-modules of fused modules

        for op_name, child in module_dict.items():
            if type(child) in self.white_list \
               and type(child) != torch.nn.Sequential \
               and type(child) != torch.quantization.stubs.DeQuantStub:
                quantizable_ops.append(
                    (op_name, unify_op_type_mapping[str(child.__class__.__name__)]
                     if str(child.__class__.__name__) in unify_op_type_mapping else str(
                         child.__class__.__name__)))

    def _get_module_scale_zeropoint(self, model, tune_cfg, prefix=''):
        """get activation scale and zero_point for converted module.

        Args:
            model (dir): Int8 model converted from fp32 model.
                         scale and zero_point is set with calibration for each module
            tune_cfg (object): This file saves scale and zero_point of 
                               output activation of each quantized module.
            prefix (string): prefix of op name

        Returns:
            None
        """
        # get scale and zero_point of modules.
        modules = dict(model.named_modules())
        for key in tune_cfg['op']:
            if prefix:
                sub_name = key[0].replace(prefix + '.', '', 1)
            else:
                sub_name = key[0]
            if sub_name in modules:
                value = tune_cfg['op'][key]
                assert isinstance(value, dict)
                if hasattr(modules[sub_name], 'scale'):
                    value['activation']['scale'] = float(modules[sub_name].scale)
                if hasattr(modules[sub_name], 'zero_point'):
                    value['activation']['zero_point'] = int(modules[sub_name].zero_point)
        # get scale and zero_point of getattr ops (like quantize ops).
        for node in model.graph.nodes:
            if node.op == 'get_attr':
                if prefix:
                    sub_name = prefix + '--' + node.target
                else:
                    sub_name = node.target
                if not hasattr(model, node.target):
                    continue
                if 'scale' in node.target:
                    tune_cfg['get_attr'][sub_name] = float(getattr(model, node.target))
                elif 'zero_point' in node.target:
                    tune_cfg['get_attr'][sub_name] = int(getattr(model, node.target))
                else:
                    pass

    def _get_sub_module_scale_zeropoint(self, model, tune_cfg, prefix=''):
        """get activation scale and zero_point for converted sub modules recursively.

        Args:
            model (dir): Int8 model converted from fp32 model.
                        scale and zero_point is set with calibration for each module
            tune_cfg (object): This file saves scale and zero_point of \
                            output activation of each quantized module.
            prefix (string): prefix of op name

        Returns:
            None
        """
        for name, module in model.named_children():
            op_name = prefix + '.' + name if prefix != '' else name
            if op_name in self.sub_module_list:
                self._get_module_scale_zeropoint(module, tune_cfg, op_name)
            else:
                self._get_sub_module_scale_zeropoint(module, tune_cfg, op_name)

    def _get_scale_zeropoint(self, model, tune_cfg):
        """get activation scale and zero_point for converted model.

        Args:
            model (dir): Int8 model converted from fp32 model.
                        scale and zero_point is set with calibration for each module
            tune_cfg (object): This file saves scale and zero_point of \
                            output activation of each quantized module.

        Returns:
            None
        """
        tune_cfg['get_attr'] = {}
        if self.sub_module_list is None:
            self._get_module_scale_zeropoint(model, tune_cfg)
        else:
            self._get_sub_module_scale_zeropoint(model, tune_cfg)

    @staticmethod
    def prepare_sub_graph(sub_module_list,
                          fx_op_cfgs,
                          model,
                          prefix,
                          is_qat=False,
                          example_inputs=None,
                          custom_config=None):
        """Static method to prepare sub modules recursively.

        Args:
            sub_module_list (list): contains the name of traceable sub modules
            fx_op_cfgs (dict, QConfigMapping): the configuration for prepare_fx quantization.
            model (dir): input model which is PyTorch model.
            prefix (string): prefix of op name
            is_qat (bool): whether it is a qat quantization
            example_inputs (tensor / tupe of tensor): example inputs
            custom_config (dict): custom non traceable module dict

        Returns:
            model (dir): output model which is a prepared PyTorch model.
        """
        from torch.quantization.quantize_fx import prepare_fx, prepare_qat_fx
        import torch.quantization.quantization_mappings as tqqm
        version = get_torch_version()
        fx_white_list = tqqm.get_default_qconfig_propagation_list()
        for name, module in model.named_children():
            op_name = prefix + '.' + name if prefix != '' else name
            # skip custom non traceable module in fine-grained FX
            if custom_config:
                if ('non_traceable_module_name' in custom_config \
                  and op_name in custom_config['non_traceable_module_name']) \
                  or ('non_traceable_module_class' in custom_config \
                  and isinstance(module, tuple(custom_config['non_traceable_module_class']))):
                    continue
            if op_name in sub_module_list:
                # remove prefix in fx_op_cfgs
                version = get_torch_version()
                if version > Version("1.12.1"):  # pragma: no cover
                    from torch.ao.quantization import QConfigMapping
                    fx_sub_op_cfgs = QConfigMapping()
                    fx_sub_op_cfgs.set_global(None)
                    fx_op_cfgs_dict = fx_op_cfgs.to_dict()
                else:
                    fx_sub_op_cfgs = dict()
                    fx_sub_op_cfgs[''] = None
                    fx_sub_op_cfgs['module_name'] = []
                    fx_op_cfgs_dict = fx_op_cfgs

                for k, v in fx_op_cfgs_dict['module_name']:
                    if op_name in k:
                        sub_name = k.replace(op_name + '.', '', 1)
                        if version > Version("1.12.1"):  # pragma: no cover
                            # pylint: disable=no-member
                            fx_sub_op_cfgs.set_module_name(sub_name, v)
                        else:
                            fx_sub_op_cfgs['module_name'].append((sub_name, v))

                if type(module) in fx_white_list and type(module) != torch.nn.Sequential:
                    # Don't really need a quant/dequant, just move nn.Embedding \
                    # to lower level for fx detection.
                    tmp_module = torch.quantization.QuantWrapper(module)
                else:
                    tmp_module = module
                # pylint: disable=E1123
                # pragma: no cover
                if is_qat:
                    module_pre = prepare_qat_fx(
                        tmp_module,
                        fx_sub_op_cfgs) if version <= Version("1.12.1") else prepare_qat_fx(
                            tmp_module, fx_sub_op_cfgs, example_inputs=example_inputs)
                # pylint: disable=E1123
                # pragma: no cover
                else:
                    module_pre = prepare_fx(
                        tmp_module,
                        fx_sub_op_cfgs) if version <= Version("1.12.1") else prepare_fx(
                            tmp_module, fx_sub_op_cfgs, example_inputs=example_inputs)
                torch_utils.util.append_attr(module_pre, module, fx_white_list)
                setattr(model, name, module_pre)
            else:
                PyTorch_FXAdaptor.prepare_sub_graph(sub_module_list,
                                                    fx_op_cfgs,
                                                    module,
                                                    op_name,
                                                    is_qat,
                                                    example_inputs=example_inputs)

    @staticmethod
    def convert_sub_graph(sub_module_list, model, prefix, custom_config=None):
        """Static method to convert sub modules recursively.

        Args:
            sub_module_list (list): contains the name of traceable sub modules
            model (dir): input model which is prepared PyTorch model.
            prefix (string): prefix of op name
            custom_config (dict): custom non traceable module dict

        Returns:
            model (dir): output model which is a converted PyTorch int8 model.
        """
        from torch.quantization.quantize_fx import convert_fx
        for name, module in model.named_children():
            op_name = prefix + '.' + name if prefix != '' else name
            # skip custom non traceable module in fine-grained FX
            if custom_config:
                if ('non_traceable_module_name' in custom_config \
                  and op_name in custom_config['non_traceable_module_name']) \
                  or ('non_traceable_module_class' in custom_config \
                  and isinstance(module, tuple(custom_config['non_traceable_module_class']))):
                    continue
            if op_name in sub_module_list:
                module_con = convert_fx(module)
                torch_utils.util.append_attr(module_con, module)
                setattr(model, name, module_con)
            else:
                PyTorch_FXAdaptor.convert_sub_graph(sub_module_list, \
                                                    module, op_name)

    @dump_elapsed_time("Pass query framework capability")
    def query_fw_capability(self, model):
        """This is a helper function to get all quantizable ops from model.

        Args:
            model (object): input model which is Neural Compressor model

        Returns:
            q_capability (dictionary): tuning capability for each op from model.
        """
        self.pre_optimized_model = model
        tmp_model = model._model
        tmp_model = self.fuse_fx_model(model, is_qat=(self.approach == "quant_aware_training"))
        return self._get_quantizable_ops(tmp_model)

    def fuse_fx_model(self, model, is_qat):
        """This is a helper function to get fused fx model for PyTorch_FXAdaptor.

        Args:
            model (object): input model which is Neural Compressor model.
            is_qat (bool): check quantization approach is qat or not.

        Returns:
            fused_model (GraphModule): fused GraphModule model from torch.fx.
        """
        try:
            tmp_model = copy.deepcopy(model._model)
        except Exception as e:
            tmp_model = model._model
            logger.warning("Deepcopy failed: {}, inplace=True now!".format(repr(e)))

        tmp_model.train() if is_qat else tmp_model.eval()
        from torch.fx import GraphModule
        from torch.quantization.quantize_fx import _fuse_fx, QuantizationTracer
        if model.kwargs is not None:
            prepare_custom_config_dict = model.kwargs.get('prepare_custom_config_dict', {})
        else:
            prepare_custom_config_dict = {}
        skipped_module_names = prepare_custom_config_dict.get(\
                                            'non_traceable_module_name', [])
        skipped_module_classes = prepare_custom_config_dict.get(\
                                            'non_traceable_module_class', [])
        try:
            tracer = QuantizationTracer(skipped_module_names, skipped_module_classes)
            graph_module = GraphModule(tmp_model, tracer.trace(tmp_model))
            if self.version.release >= Version("1.13.0").release:  # pragma: no cover
                # pylint: disable=E1124, E1123
                fused_model = _fuse_fx(graph_module,
                                        is_qat,
                                        fuse_custom_config=prepare_custom_config_dict)
            elif self.version.release >= Version("1.11.0").release:  # pragma: no cover
                # pylint: disable=E1124
                fused_model = _fuse_fx(graph_module,
                                        is_qat,
                                        fuse_custom_config_dict=prepare_custom_config_dict)
            else:
                fused_model = _fuse_fx(graph_module, prepare_custom_config_dict)
        except:
            self.sub_module_list = []
            module_dict = dict(tmp_model.named_modules())
            self._fuse_sub_graph(tmp_model, module_dict, prefix='', is_qat=is_qat)
            fused_model = tmp_model
        return fused_model

    def _fuse_sub_graph(self, model, module_dict, prefix, is_qat):
        """This is a helper function to get fused fx sub modules recursively for PyTorch_FXAdaptor.

        Args:
            model (object): input model which is PyTorch model.
            module_dict (dict): module dict of input model.
            prefix (string): prefix of op name.
            is_qat (bool): check quantization approach is qat or not.

        Returns:
            fused_model (GraphModule): fused GraphModule model from torch.fx.
        """
        from torch.quantization.quantize_fx import _fuse_fx
        import torch.quantization.quantization_mappings as tqqm
        fx_white_list = tqqm.get_default_qconfig_propagation_list()
        for name, module in model.named_children():
            # FX QAT cannot fallback nn.Dropout from train mode to eval
            if type(module) == torch.nn.Dropout:  # pragma: no cover
                continue
            op_name = prefix + '.' + name if prefix != '' else name
            if op_name not in module_dict:
                continue
            if type(module) in fx_white_list \
              and type(module) != torch.nn.Sequential:
                module = torch.quantization.QuantWrapper(module)
            if self._check_dynamic_control(module):
                self._fuse_sub_graph(module, module_dict, op_name, is_qat=is_qat)
            else:
                try:
                    graph_module = torch.fx.symbolic_trace(module)
                    if self.version.release >= Version("1.11.0").release:  # pragma: no cover
                        fused_model = _fuse_fx(graph_module, is_qat)
                    else:
                        fused_model = _fuse_fx(graph_module)  # pylint: disable=E1120
                    setattr(model, name, fused_model)
                    self.sub_module_list.append(op_name)
                except:
                    self._fuse_sub_graph(module, module_dict, op_name, is_qat)

    @staticmethod
    def _check_dynamic_control(module):
        """This is a helper function to check dynamic control in forward function of module.

        Args:
            module (object): input module which is PyTorch Module.

        Returns:
            fused_model (GraphModule): fused GraphModule model from torch.fx.
        """
        import inspect
        import re
        try:
            lines = inspect.getsource(module.forward)
            # Proxy obj. will always be detectd as `not None`.
            # Other situations could be detected by prepare_fx function.
            pattern = "is( not)? None"
            anws = re.search(pattern, lines)
            if anws:
                return True
        except:  # pragma: no cover
            logger.info('Module has no forward function')
        return False

    def get_output_op_names(self, *args, **kwargs):
        return None

    def calculate_op_sensitivity(self, model, dataloader, tune_cfg, output_op_names,
                                 confidence_batches, fallback=True, requantize_cfgs=None):
        """This is a helper function for `query_fw_capability`,
           and it will get all quantizable ops from model.

        Args:
            model (object): INC model containing fp32 model
            dataloader (string): dataloader contains real data.
            tune_cfg (dict): dictionary of tune configure for each op.
            fallback (bool): switch method in fallback stage and re-quantize stage

        Returns:
            ops_lst (list): sorted op list by sensitivity
        """
        from .torch_utils.util import get_fallback_order
        ordered_ops = get_fallback_order(self, model.model, dataloader, tune_cfg,
                                         confidence_batches, fallback, requantize_cfgs)
        return ordered_ops


class PyTorchQuery(QueryBackendCapability):
    def __init__(self, local_config_file=None):
        super().__init__()
        self.version = get_torch_version()
        self.cfg = local_config_file
        self.cur_config = None
        self._one_shot_query()

    def _get_specified_version_cfg(self, data):
        """Get the configuration for the current runtime.
        If there's no matched configuration in the input yaml, we'll
        use the `default` field of yaml.

        Args:
            data (Yaml content): input yaml file.

        Returns:
            [dictionary]: the content for specific version.
        """
        # default_config = None
        for sub_data in data:
            if sub_data['version']['name'] == 'default':
                return sub_data
            sub_data_version = Version(sub_data['version']['name'])
            if self.version >= sub_data_version:
                return sub_data

    def _one_shot_query(self):
        with open(self.cfg) as f:
            content = yaml.safe_load(f)
            try:
                self.cur_config = self._get_specified_version_cfg(content)
            except Exception as e:  # pragma: no cover
                logger.info("Fail to parse {} due to {}".format(self.cfg, str(e)))
                self.cur_config = None
                raise ValueError("Please check if the format of {} follows "
                                 "Neural Compressor yaml scheme.".format(self.cfg))
        self._update_cfg_with_usr_definition()

    def _update_cfg_with_usr_definition(self):
        from neural_compressor.conf.pythonic_config import pytorch_config
        if pytorch_config.precisions is not None:
            self.cur_config['precisions']['names'] = ','.join(pytorch_config.precisions)

    def get_quantization_capability(self, datatype='int8'):
        """Get the supported op types' quantization capability.

        Args:
            datatype: the data type. Defaults to 'int8'.

        Returns:
            [dictionary list]: A list composed of dictionary which key is precision
            and value is a dict that describes all op types' quantization capability.
        """
        assert datatype in self.get_quant_datatypes(), \
            f"The target data type should be one of {self.get_quant_datatypes()}"
        return self.cur_config[datatype]

    def get_quant_datatypes(self):
        """Got low-precision data types for quantization.
        
        Collects all data types for quantization, such as int8, int4.
        """
        # TODO to handle other data types such FP8, FP8E4M3
        datatype_lst = []
        for key in self.cur_config:
            if key.startswith('int'):
                datatype_lst.append(key)
        return datatype_lst

    def get_op_types(self):
        """Get the supported op types by all precisions.
        Returns:
            [dictionary list]: A list composed of dictionary which key is precision
            and value is the op types.
        """
        return self.cur_config

    def get_op_types_by_precision(self, precision):
        """Get op types per precision
        Args:
            precision (string): precision name
        Returns:
            [string list]: A list composed of op type.
        """
        return self.cur_config[precision]
