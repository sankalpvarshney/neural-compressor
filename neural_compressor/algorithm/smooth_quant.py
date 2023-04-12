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

"""Build SmoothQuant algorithm class."""

import numpy as np
from .algorithm import Algorithm, algorithm_registry
from ..utils import logger


@algorithm_registry(algorithm_type='smooth_quant', location='pre_quantization')
class SmoothQuant(Algorithm):
    """Fake input channel quantization.

    for more details please refer to
    [1] SmoothQuant: Accurate and Efficient Post-Training Quantization for Large Language Models
    [2] SPIQ: Data-Free Per-Channel Static Input Quantization
    For torch backend, we only handle the layers whose smooth scale could be absorbed, we will support other layers
    later. For onnx backend, we insert MUL layer before conv/linear layers, the op fusing and kernel will be
    supported in the future.
    """

    def __init__(self, alpha=0.5):
        """Initialize SmoothQuant class.

        Args:
            alpha:Alpha value to balance the quantization difficulty of activation and weight,
                please refer to the paper for more details
        """
        # percentile:Percentile of calibration to remove outliers,float(0->100)
        # op_types: The op types whose input tensor will be dumped,['Conv', 'Linear']
        # scales_per_op: True, each op will have an individual scale, mainly for accuracy
        #                False, ops with the same input will share a scale, mainly for performance
        self.alpha = alpha
        self.percentile = None
        self.op_types = None
        self.scales_per_op = None
        self.tune_cfg = None

    def __call__(self, origin_model, q_model, adaptor, dataloader, calib_iter):
        """Return the processed model via SmoothQuant algorithm.

        Fake input channel quantization, for more details please refer to:
        [1] SmoothQuant: Accurate and Efficient Post-Training Quantization for Large Language Models
        [2] SPIQ: Data-Free Per-Channel Static Input Quantization
        inert Mul op before each conv/matmul with adjusted weights

        Args:
            origin_model: origin_model
            q_model: q_model
            adaptor: adaptor
            dataloader: dataloader
            calib_iter: calib_iter

        Returns:
            model: A modified onnx model
        """
        args = {}  ##different backends may have different default values
        if self.op_types != None:
            args["op_types"] = self.op_types
        if self.percentile != None:
            args['percentile'] = self.percentile
        if self.scales_per_op != None:
            args['scales_per_op'] = self.scales_per_op
        q_model = adaptor.smooth_quant(origin_model, dataloader, calib_iter, self.tune_cfg, self.alpha,
                                       **args)
        return q_model
