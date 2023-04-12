#
#  -*- coding: utf-8 -*-
#
import unittest
import os
import yaml
import numpy as np
import tensorflow as tf
import logging
from tensorflow.python.framework import graph_util
from tensorflow.core.framework import attr_value_pb2
from tensorflow.python.framework import dtypes
from neural_compressor.adaptor.tf_utils.util import disable_random
from neural_compressor.utils.utility import CpuInfo
from neural_compressor.experimental import Quantization, common
from neural_compressor.utils import logger

def build_fake_yaml_1():
    fake_yaml_1 = '''
        model:
          name: fake_yaml_1
          framework: tensorflow
          inputs: input
        device: cpu
        quantization:
          model_wise:
            weight:
                granularity: per_tensor
                scheme: sym
                dtype: int8
                algorithm: minmax
        evaluation:
          accuracy:
            metric:
              topk: 1
        tuning:
            strategy:
              name: basic
            accuracy_criterion:
              relative: 0.1
            exit_policy:
              performance_only: True
            workspace:
              path: saved
        '''
    y = yaml.load(fake_yaml_1, Loader=yaml.SafeLoader)
    with open('fake_yaml_1.yaml', "w", encoding="utf-8") as f:
        yaml.dump(y, f)
    f.close()

def build_fake_yaml_2():
    fake_yaml_2 = '''
        model:
          name: fake_yaml_2
          framework: tensorflow
          inputs: input
        device: cpu
        quantization:
          model_wise:
            weight:
                granularity: per_tensor
                scheme: sym
                dtype: int8
                algorithm: minmax
        evaluation:
          accuracy:
            metric:
              topk: 1
        tuning:
            strategy:
              name: basic
            accuracy_criterion:
              relative: 0.1
            workspace:
              path: saved
        '''
    y = yaml.load(fake_yaml_2, Loader=yaml.SafeLoader)
    with open('fake_yaml_2.yaml', "w", encoding="utf-8") as f:
        yaml.dump(y, f)
    f.close()

class TestTensorflowQdqConvFusion(unittest.TestCase):

    @classmethod
    def setUpClass(self):
        build_fake_yaml_1()
        build_fake_yaml_2()

    @classmethod
    def tearDownClass(self):
        os.remove('fake_yaml_1.yaml')
        os.remove('fake_yaml_2.yaml')

    @disable_random()
    def test_bn_relu_depthwiseconv_biasadd_relu6_fusion(self):
        logger.info("test_bn_relu_depthwiseconv_biasadd_relu6_fusion")
        x = tf.compat.v1.placeholder(tf.float32, [1, 56, 56, 16], name="input")
        conv_weights = tf.compat.v1.get_variable("weight", [3, 3, 16, 16],
                                                 initializer=tf.compat.v1.random_normal_initializer())
        normed_0 = tf.compat.v1.layers.batch_normalization(x)
        relu = tf.nn.relu(normed_0, name='op_to_store_0')
        conv = tf.compat.v1.nn.depthwise_conv2d_native(relu, conv_weights, strides=[1, 2, 2, 1], padding="VALID")
        normed_1 = tf.compat.v1.layers.batch_normalization(conv)
        relu6 = tf.nn.relu6(normed_1, name='op_to_store_1')
        out_name = relu6.name.split(':')[0]
        with tf.compat.v1.Session() as sess:
            sess.run(tf.compat.v1.global_variables_initializer())
            output_graph_def = graph_util.convert_variables_to_constants(
                sess=sess,
                input_graph_def=sess.graph_def,
                output_node_names=[out_name])

        quantizer = Quantization('fake_yaml_1.yaml')
        dataset = quantizer.dataset('dummy', shape=(100, 56, 56, 16), label=True)
        quantizer.eval_dataloader = common.DataLoader(dataset)
        quantizer.calib_dataloader = common.DataLoader(dataset)
        quantizer.model = output_graph_def
        output_graph = quantizer.fit()
        conv_input_type = True
        found_fusion = True
        qbn_num = 0
        dq_num = 0
        for i in output_graph.graph_def.node:
            if i.op == '_FusedQuantizedDepthwiseConv2D' \
                and i.attr['Thost_inputs'].list.type != [11, 11, 1, 1, 1, 1, 1, 1, 1]:
                conv_input_type = False
                break
            if i.op in ['Relu', 'Relu6', 'FusedBatchNormV3']:
                found_fusion = False
                break
            if i.op == '_QuantizedFusedBatchNorm':
                qbn_num += 1
            if i.op == 'Dequantize':
                dq_num += 1
        self.assertEqual(conv_input_type, True)
        self.assertEqual(found_fusion, True)
        self.assertEqual(qbn_num, 1)
        self.assertEqual(dq_num, 0)

    @disable_random()
    def test_training_bn_relu_depthwiseconv_biasadd_relu6_fusion(self):
        logger.info("test_training_bn_relu_depthwiseconv_biasadd_relu6_fusion")
        x = tf.compat.v1.placeholder(tf.float32, [1, 56, 56, 16], name="input")
        conv_weights = tf.compat.v1.get_variable("weight", [3, 3, 16, 16],
                                                 initializer=tf.compat.v1.random_normal_initializer())
        normed_0 = tf.compat.v1.layers.batch_normalization(x, training=True)
        relu = tf.nn.relu(normed_0, name='op_to_store_0')
        conv = tf.compat.v1.nn.depthwise_conv2d_native(relu, conv_weights, strides=[1, 2, 2, 1], padding="VALID")
        normed_1 = tf.compat.v1.layers.batch_normalization(conv)
        relu6 = tf.nn.relu6(normed_1, name='op_to_store_1')
        out_name = relu6.name.split(':')[0]
        with tf.compat.v1.Session() as sess:
            sess.run(tf.compat.v1.global_variables_initializer())
            output_graph_def = graph_util.convert_variables_to_constants(
                sess=sess,
                input_graph_def=sess.graph_def,
                output_node_names=[out_name])

        quantizer = Quantization('fake_yaml_1.yaml')
        dataset = quantizer.dataset('dummy', shape=(100, 56, 56, 16), label=True)
        quantizer.eval_dataloader = common.DataLoader(dataset)
        quantizer.calib_dataloader = common.DataLoader(dataset)
        quantizer.model = output_graph_def
        output_graph = quantizer.fit()
        bn_num, bf16_bn_num, qbn_num, dq_num = 0, 0, 0, 0
        for i in output_graph.graph_def.node:
            if i.op == 'FusedBatchNormV3':
                bn_num += 1
                if i.attr['T'].type == dtypes.bfloat16.as_datatype_enum:
                    bf16_bn_num += 1
            if i.op == '_QuantizedFusedBatchNorm':
                qbn_num += 1
            if i.op == 'Dequantize':
                dq_num += 1
        self.assertEqual(bn_num, 1)
        self.assertEqual(qbn_num, 0)
        self.assertEqual(dq_num, 0)
        bf16_enabled = bool(CpuInfo().bf16 or os.getenv('FORCE_BF16') == '1')
        if bf16_enabled:
            self.assertEqual(bf16_bn_num, 1)

    @disable_random()
    def test_bn_leakyrelu_conv_biasadd_relu(self):
        logger.info("test_bn_leakyrelu_conv_biasadd_relu")
        x = tf.compat.v1.placeholder(tf.float32, [1, 56, 56, 16], name="input")
        conv_weights = tf.compat.v1.get_variable("weight", [3, 3, 16, 16],
                                                 initializer=tf.compat.v1.random_normal_initializer())
        normed_0 = tf.compat.v1.layers.batch_normalization(x)
        leaky_relu = tf.nn.leaky_relu(normed_0, alpha=0.3, name='op_to_store_0')
        conv = tf.nn.conv2d(leaky_relu, conv_weights, strides=[1, 2, 2, 1], padding="VALID")
        normed_1 = tf.compat.v1.layers.batch_normalization(conv)
        relu = tf.nn.relu(normed_1, name='op_to_store_1')
        out_name = relu.name.split(':')[0]
        with tf.compat.v1.Session() as sess:
            sess.run(tf.compat.v1.global_variables_initializer())
            output_graph_def = graph_util.convert_variables_to_constants(
                sess=sess,
                input_graph_def=sess.graph_def,
                output_node_names=[out_name])

        quantizer = Quantization('fake_yaml_1.yaml')
        dataset = quantizer.dataset('dummy', shape=(100, 56, 56, 16), label=True)
        quantizer.eval_dataloader = common.DataLoader(dataset)
        quantizer.calib_dataloader = common.DataLoader(dataset)

        quantizer.model = output_graph_def
        output_graph = quantizer.fit()
        conv_input_type = True
        found_fusion = True
        qbn_num = 0
        dq_num = 0
        qbn_output_max_name = 'batch_normalization/FusedBatchNormV3_eightbit_quantized_bn/frozen_bn_output_max'
        for i in output_graph.graph_def.node:
            if i.op == '_FusedQuantizedConv2D' \
                and i.attr['Thost_inputs'].list.type != [11, 11, 1, 1, 1, 1, 1, 1, 1]:
                conv_input_type = False
                break
            if i.op in ['Relu', 'LeakyRelu', 'FusedBatchNormV3']:
                found_fusion = False
                break
            if i.op == '_QuantizedFusedBatchNorm':
                is_offset_const = i.attr["is_offset_const"].b
                is_mean_const = i.attr["is_mean_const"].b
                qbn_alpha = i.attr["alpha"].f
                frozen_qbn_output_max = i.input[8]
                qbn_num += 1
            if i.name == qbn_output_max_name:
                frozen_qbn_output_max_value = i.attr["value"].tensor.float_val[0]
            if i.op == 'Dequantize':
                dq_num += 1
        self.assertEqual(conv_input_type, True)
        self.assertEqual(found_fusion, True)
        self.assertEqual(qbn_num, 1)
        self.assertEqual(dq_num, 0)
        self.assertEqual(is_offset_const, True)
        self.assertEqual(is_mean_const, True)
        self.assertEqual(round(qbn_alpha, 7), 0.3)
        self.assertEqual(frozen_qbn_output_max, qbn_output_max_name)
        self.assertGreater(frozen_qbn_output_max_value, 126)

    @disable_random()
    def test_bn_relu_conv_biasadd_relu(self):
        logger.info("test_bn_relu_conv_biasadd_relu")
        x = tf.compat.v1.placeholder(tf.float32, [1, 56, 56, 16], name="input")
        conv_weights = tf.compat.v1.get_variable("weight", [3, 3, 16, 16],
                                                 initializer=tf.compat.v1.random_normal_initializer())
        normed_0 = tf.compat.v1.layers.batch_normalization(x)
        relu_0 = tf.nn.relu(normed_0, name='op_to_store_0')
        conv = tf.nn.conv2d(relu_0, conv_weights, strides=[1, 2, 2, 1], padding="VALID")
        normed_1 = tf.compat.v1.layers.batch_normalization(conv)
        relu_1 = tf.nn.relu(normed_1, name='op_to_store_1')
        out_name = relu_1.name.split(':')[0]
        with tf.compat.v1.Session() as sess:
            sess.run(tf.compat.v1.global_variables_initializer())
            output_graph_def = graph_util.convert_variables_to_constants(
                sess=sess,
                input_graph_def=sess.graph_def,
                output_node_names=[out_name])

        quantizer = Quantization('fake_yaml_1.yaml')
        dataset = quantizer.dataset('dummy', shape=(100, 56, 56, 16), label=True)
        quantizer.eval_dataloader = common.DataLoader(dataset)
        quantizer.calib_dataloader = common.DataLoader(dataset)

        quantizer.model = output_graph_def
        output_graph = quantizer.fit()
        conv_input_type = True
        found_fusion = True
        qbn_num = 0
        dq_num = 0
        qbn_output_max_name = 'batch_normalization/FusedBatchNormV3_eightbit_quantized_bn/frozen_bn_output_max'
        for i in output_graph.graph_def.node:
            if i.op == '_FusedQuantizedConv2D' \
                and i.attr['Thost_inputs'].list.type != [11, 11, 1, 1, 1, 1, 1, 1, 1]:
                conv_input_type = False
                break
            if i.op in ['Relu', 'FusedBatchNormV3']:
                found_fusion = False
                break
            if i.op == '_QuantizedFusedBatchNorm':
                is_offset_const = i.attr["is_offset_const"].b
                is_mean_const = i.attr["is_mean_const"].b
                frozen_qbn_output_max = i.input[8]
                qbn_num += 1
            if i.name == qbn_output_max_name:
                frozen_qbn_output_max_value = i.attr["value"].tensor.float_val[0]
            if i.op == 'Dequantize':
                dq_num += 1
        self.assertEqual(conv_input_type, True)
        self.assertEqual(found_fusion, True)
        self.assertEqual(qbn_num, 1)
        self.assertEqual(dq_num, 0)
        self.assertEqual(is_offset_const, True)
        self.assertEqual(is_mean_const, True)
        self.assertEqual(frozen_qbn_output_max, qbn_output_max_name)
        self.assertGreater(frozen_qbn_output_max_value, 126)

    @disable_random()
    def test_bn_performance_only_false(self):
        logger.info("test_bn_performance_only_false")
        x = tf.compat.v1.placeholder(tf.float32, [1, 56, 56, 16], name="input")
        conv_weights = tf.compat.v1.get_variable("weight", [3, 3, 16, 16],
                                                 initializer=tf.compat.v1.random_normal_initializer())
        normed_0 = tf.compat.v1.layers.batch_normalization(x)
        relu_0 = tf.nn.relu(normed_0, name='op_to_store_0')
        conv = tf.nn.conv2d(relu_0, conv_weights, strides=[1, 2, 2, 1], padding="VALID")
        normed_1 = tf.compat.v1.layers.batch_normalization(conv)
        relu_1 = tf.nn.relu6(normed_1, name='op_to_store_1')
        out_name = relu_1.name.split(':')[0]
        with tf.compat.v1.Session() as sess:
            sess.run(tf.compat.v1.global_variables_initializer())
            output_graph_def = graph_util.convert_variables_to_constants(
                sess=sess,
                input_graph_def=sess.graph_def,
                output_node_names=[out_name])

        quantizer = Quantization('fake_yaml_2.yaml')
        dataset = quantizer.dataset('dummy', shape=(100, 56, 56, 16), label=True)
        quantizer.eval_dataloader = common.DataLoader(dataset)
        quantizer.calib_dataloader = common.DataLoader(dataset)

        quantizer.model = output_graph_def
        output_graph = quantizer.fit()
        found_fusion = True
        qconv_num = 0
        qbn_num = 0
        dq_num = 0
        for i in output_graph.graph_def.node:
            if i.op in ['Relu6']:
                found_fusion = False
                break
            if i.op == '_FusedQuantizedConv2D':
                qconv_num += 1
            if i.op == '_QuantizedFusedBatchNorm':
                qbn_num += 1
            if i.op == 'Dequantize':
                dq_num += 1
        self.assertEqual(found_fusion, True)
        self.assertEqual(qconv_num, 1)
        self.assertEqual(qbn_num, 0)
        self.assertEqual(dq_num, 1)

    @disable_random()
    def test_bnex_performance_only_false(self):
        logger.info("test_bnex_performance_only_false")
        x = tf.compat.v1.placeholder(tf.float32, [1, 56, 56, 16], name="input")
        conv_weights_0 = tf.compat.v1.get_variable("weight_0", [3, 3, 16, 16],
                                                 initializer=tf.compat.v1.random_normal_initializer())
        normed_0 = tf.compat.v1.layers.batch_normalization(x)
        relu_0 = tf.nn.relu(normed_0, name='op_to_store_0')
        conv_0 = tf.nn.conv2d(relu_0, conv_weights_0, strides=[1, 2, 2, 1], padding="VALID")
        normed_1 = tf.compat.v1.layers.batch_normalization(conv_0)
        conv_weights_1 = tf.compat.v1.get_variable("weight_1", [5, 5, 16, 2],
                                                 initializer=tf.compat.v1.random_normal_initializer())
        conv_1 = tf.nn.conv2d(normed_1, conv_weights_1, strides=[1, 3, 3, 1], padding="VALID")
        relu_1 = tf.nn.relu6(conv_1, name='op_to_store_1')
        out_name = relu_1.name.split(':')[0]
        """
        graph_def = tf.compat.v1.get_default_graph().as_graph_def()
        for node in graph_def.node:
            if node.name == "batch_normalization_1/FusedBatchNormV3":
                node.op = "_FusedBatchNormEx"
        with tf.Graph().as_default() as graph:
            tf.import_graph_def(graph_def, name='')
        """
        with tf.compat.v1.Session() as sess:
            sess.run(tf.compat.v1.global_variables_initializer())
            output_graph_def = graph_util.convert_variables_to_constants(
                sess=sess,
                input_graph_def=sess.graph_def,
                output_node_names=[out_name])
        for node in output_graph_def.node:
            if node.name == "batch_normalization_1/FusedBatchNormV3":
                node.op = "_FusedBatchNormEx"
                node.attr["activation_mode"].CopyFrom(attr_value_pb2.AttrValue(s=b"Relu"))

        quantizer = Quantization('fake_yaml_2.yaml')
        dataset = quantizer.dataset('dummy', shape=(100, 56, 56, 16), label=True)
        quantizer.eval_dataloader = common.DataLoader(dataset)
        quantizer.calib_dataloader = common.DataLoader(dataset)

        quantizer.model = output_graph_def
        output_graph = quantizer.fit()
        found_fusion = True
        qconv_num = 0
        qbn_num = 0
        dq_num = 0
        for i in output_graph.graph_def.node:
            if i.op in ['Relu6', '_FusedBatchNormEx']:
                found_fusion = False
                break
            if i.op == '_FusedQuantizedConv2D':
                qconv_num += 1
            if i.op == '_QuantizedFusedBatchNorm':
                qbn_num += 1
            if i.op == 'Dequantize':
                dq_num += 1
        self.assertEqual(found_fusion, True)
        self.assertEqual(qconv_num, 2)
        self.assertEqual(qbn_num, 0)
        self.assertEqual(dq_num, 1)


if __name__ == '__main__':
    unittest.main()
