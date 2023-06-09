# -*- coding: utf-8 -*-
# Copyright (c) 2022 Intel Corporation
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
"""Mixed precision optimization script."""

import argparse
import sys
from typing import Any, List, Optional


def parse_args() -> Any:
    """Parse input arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=str,
        required=False,
        default=None,
        help="Path to yaml config.",
    )
    parser.add_argument(
        "--input-graph",
        type=str,
        required=True,
        help="Path to input model.",
    )
    parser.add_argument(
        "--output-graph",
        type=str,
        required=True,
        help="Path to optimized model.",
    )
    parser.add_argument(
        "--input-nodes",
        type=str,
        required=False,
        default=None,
        help="Input node names.",
    )
    parser.add_argument(
        "--output-nodes",
        type=str,
        required=False,
        default=None,
        help="Output node names.",
    )
    parser.add_argument(
        "--precisions",
        type=str,
        required=False,
        help="Optimized model precisions.",
    )
    parser.add_argument(
        "--framework",
        type=str,
        required=False,
        help="Framework to use.",
    )
    return parser.parse_args()


def optimize_graph(
    input_graph: str,
    output_graph: str,
    framework: str,
    precisions: str,
    input: Optional[List[str]] = None,
    output: Optional[List[str]] = None,
) -> None:
    """Execute mixed precision optimization."""
    from neural_compressor.experimental import MixedPrecision

    if framework == "onnxrt":
        import onnx

        input_graph = onnx.load(input_graph)

    converter = MixedPrecision()
    converter.precisions = precisions

    if input is not None:
        converter.input = input
    if output is not None:
        converter.output = output

    converter.model = input_graph
    optimized_model = converter()

    if optimized_model is not None:
        optimized_model.save(output_graph)
    else:
        sys.exit(100)


def optimize_graph_config(
    input_graph: str,
    output_graph: str,
    framework: str,
    config: str,
) -> None:
    """Execute mixed precision optimization using config file."""
    from neural_compressor.experimental import MixedPrecision

    if framework == "onnxrt":
        import onnx

        input_graph = onnx.load(input_graph)

    converter = MixedPrecision(config)
    converter.model = input_graph
    optimized_model = converter()
    if optimized_model is not None:
        optimized_model.save(output_graph)
    else:
        sys.exit(100)


def set_eager_execution(input_graph: str) -> None:
    """Set eager execution as required by model."""
    from neural_compressor.ux.components.model.model_type_getter import get_model_type

    model_type = get_model_type(input_graph)

    try:
        import tensorflow as tf

        if "keras" == model_type:
            tf.compat.v1.enable_eager_execution()
        else:
            tf.compat.v1.disable_eager_execution()
    except Exception as err:
        print(err)


if __name__ == "__main__":
    args = parse_args()

    input_nodes = args.input_nodes
    if isinstance(input_nodes, str):
        input_nodes = input_nodes.split(",")

    output_nodes = args.output_nodes
    if isinstance(output_nodes, str):
        output_nodes = output_nodes.split(",")

    set_eager_execution(args.input_graph)
    if args.config is None:
        optimize_graph(
            input_graph=args.input_graph,
            output_graph=args.output_graph,
            input=args.input_nodes,
            output=args.output_nodes,
            framework=args.framework,
            precisions=args.precisions,
        )
    else:
        optimize_graph_config(
            input_graph=args.input_graph,
            output_graph=args.output_graph,
            config=args.config,
            framework=args.framework,
        )
