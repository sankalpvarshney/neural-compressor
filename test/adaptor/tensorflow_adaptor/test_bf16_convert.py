import os
import platform
import shutil
import unittest
from unittest import result
import numpy as np
from neural_compressor.adaptor.tf_utils.graph_rewriter.bf16.bf16_convert import BF16Convert

import tensorflow as tf
from tensorflow.core.framework import attr_value_pb2
from tensorflow.core.framework import graph_pb2
from tensorflow.core.framework import node_def_pb2
from tensorflow.python.framework import tensor_util
from tensorflow.python.framework import dtypes


def build_fake_yaml():
    fake_yaml = '''
        model:
          name: fake_yaml
          framework: tensorflow
          inputs: input
          outputs: final
        device: cpu
        use_bf16: True
        evaluation:
          accuracy:
            metric:
              topk: 1
        tuning:
            strategy:
              name: basic
            exit_policy:
              max_trials: 2
            accuracy_criterion:
              relative: 0.01
            workspace:
              path: saved
        '''
    with open('fake_yaml.yaml',"w",encoding="utf-8") as f:
        f.write(fake_yaml)
    f.close()

def build_newapi_fake_yaml():
    fake_yaml = '''
        model:
          name: fake_yaml
          framework: tensorflow
          inputs: input
          outputs: final
        device: cpu
        use_bf16: True
        evaluation:
          accuracy:
            metric:
              topk: 1
        tuning:
            strategy:
              name: basic
            exit_policy:
              max_trials: 2
            accuracy_criterion:
              relative: 0.01
            workspace:
              path: saved
        '''
    with open('newapi_fake_yaml.yaml',"w",encoding="utf-8") as f:
        f.write(fake_yaml)
    f.close()

def build_fake_bf16_rnn_yaml():
    fake_yaml = '''
        model:
          name: fake_yaml
          framework: tensorflow
          inputs: input_1
          outputs: dense/BiasAdd
        device: cpu
        use_bf16: True
        quantization:
          op_wise: {
                     \"lstm/while/MatMul\": {
                       \"activation\":  {\"dtype\": [\"bf16\"]},
                     },
                    \"lstm/while/MatMul_1\": {
                       \"activation\":  {\"dtype\": [\"bf16\"]},
                     },
                    \"lstm/while/MatMul_2\": {
                       \"activation\":  {\"dtype\": [\"bf16\"]},
                     },
                    \"lstm/while/MatMul_3\": {
                       \"activation\":  {\"dtype\": [\"bf16\"]},
                     },
                     \"lstm_1/while/MatMul\": {
                       \"activation\":  {\"dtype\": [\"bf16\"]},
                     },
                    \"lstm_1/while/MatMul_1\": {
                       \"activation\":  {\"dtype\": [\"bf16\"]},
                     },
                    \"lstm_1/while/MatMul_2\": {
                       \"activation\":  {\"dtype\": [\"bf16\"]},
                     },
                    \"lstm_1/while/MatMul_3\": {
                       \"activation\":  {\"dtype\": [\"bf16\"]},
                     },
                   }
        evaluation:
          accuracy:
            metric:
              topk: 1
        tuning:
            accuracy_criterion:
              relative: 0.05
            exit_policy:
              performance_only: True
        '''
    with open('fake_bf16_rnn.yaml',"w",encoding="utf-8") as f:
        f.write(fake_yaml)
    f.close()

def create_test_graph(bf16_graph=True):
    input_node = node_def_pb2.NodeDef()
    input_node.name = "input"
    input_node.op = "Placeholder"
    input_node.attr["dtype"].CopyFrom(attr_value_pb2.AttrValue(
        type=dtypes.float32.as_datatype_enum))

    conv1_weight_node = node_def_pb2.NodeDef()
    conv1_weight_node.name = "conv1_weights"
    conv1_weight_node.op = "Const"
    conv1_weight_value = np.float32(np.abs(np.random.randn(3,3,3,32)))
    conv1_weight_node.attr['dtype'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
    conv1_weight_node.attr['value'].CopyFrom(attr_value_pb2.AttrValue(
        tensor=tensor_util.make_tensor_proto(
            conv1_weight_value, conv1_weight_value.dtype.type, conv1_weight_value.shape)))

    conv1_node = node_def_pb2.NodeDef()
    conv1_node.name = "conv1"
    conv1_node.op = "Conv2D"
    conv1_node.attr['T'].CopyFrom(attr_value_pb2.AttrValue(
        type=dtypes.float32.as_datatype_enum))
    conv1_node.input.extend([input_node.name, conv1_weight_node.name])
    conv1_node.attr['strides'].CopyFrom(attr_value_pb2.AttrValue(
        list=attr_value_pb2.AttrValue.ListValue(i=[1,1,1,1])))
    conv1_node.attr['dilations'].CopyFrom(attr_value_pb2.AttrValue(
        list=attr_value_pb2.AttrValue.ListValue(i=[1,1,1,1])))
    conv1_node.attr['padding'].CopyFrom(attr_value_pb2.AttrValue(s=b'SAME'))
    conv1_node.attr['data_format'].CopyFrom(attr_value_pb2.AttrValue(s=b'NHWC'))

    bias_node = node_def_pb2.NodeDef()
    bias_node.name = "conv1_bias"
    bias_node.op = "Const"
    bias_value = np.float32(np.abs(np.random.randn(32)))
    bias_node.attr['dtype'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
    bias_node.attr['value'].CopyFrom(attr_value_pb2.AttrValue(tensor=tensor_util.make_tensor_proto(
        bias_value, bias_value.dtype.type, bias_value.shape)))

    bias_add_node = node_def_pb2.NodeDef()
    bias_add_node.name = "conv1_bias_add"
    bias_add_node.op = "BiasAdd"
    bias_add_node.attr['T'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
    bias_add_node.input.extend([conv1_node.name, bias_node.name])
    bias_add_node.attr['data_format'].CopyFrom(attr_value_pb2.AttrValue(s=b'NHWC'))

    if bf16_graph:
        cast_node = node_def_pb2.NodeDef()
        cast_node.op = "Cast"
        cast_node.name = "cast"
        cast_node.attr['SrcT'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
        cast_node.attr['DstT'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.bfloat16.as_datatype_enum))
        cast_node.input.extend([bias_add_node.name])

    relu_node = node_def_pb2.NodeDef()
    relu_node.op = "Relu"
    relu_node.name = "relu"
    relu_node.attr['T'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.bfloat16.as_datatype_enum if bf16_graph else dtypes.float32.as_datatype_enum))
    relu_node.input.extend([cast_node.name if bf16_graph else bias_add_node.name])

    if bf16_graph:
        cast2_node = node_def_pb2.NodeDef()
        cast2_node.op = "Cast"
        cast2_node.name = "cast2"
        cast2_node.attr['SrcT'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.bfloat16.as_datatype_enum))
        cast2_node.attr['DstT'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
        cast2_node.input.extend([relu_node.name])

    conv2_weight_node = node_def_pb2.NodeDef()
    conv2_weight_node.name = "conv2_weights"
    conv2_weight_node.op = "Const"
    conv2_weight_value = np.float32(np.abs(np.random.randn(3,3,32,32)))
    conv2_weight_node.attr['dtype'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
    conv2_weight_node.attr['value'].CopyFrom(attr_value_pb2.AttrValue(
        tensor=tensor_util.make_tensor_proto(
            conv2_weight_value, conv2_weight_value.dtype.type, conv2_weight_value.shape)))

    conv2_node = node_def_pb2.NodeDef()
    conv2_node.name = "conv2"
    conv2_node.op = "Conv2D"
    conv2_node.attr['T'].CopyFrom(attr_value_pb2.AttrValue(
        type=dtypes.float32.as_datatype_enum))
    conv2_node.input.extend([cast2_node.name if bf16_graph else relu_node.name, conv2_weight_node.name])
    conv2_node.attr['strides'].CopyFrom(attr_value_pb2.AttrValue(
        list=attr_value_pb2.AttrValue.ListValue(i=[1,1,1,1])))
    conv2_node.attr['dilations'].CopyFrom(attr_value_pb2.AttrValue(
        list=attr_value_pb2.AttrValue.ListValue(i=[1,1,1,1])))
    conv2_node.attr['padding'].CopyFrom(attr_value_pb2.AttrValue(s=b'SAME'))
    conv2_node.attr['data_format'].CopyFrom(attr_value_pb2.AttrValue(s=b'NHWC'))

    bias_node2 = node_def_pb2.NodeDef()
    bias_node2.name = "conv2_bias"
    bias_node2.op = "Const"
    bias_value2 = np.float32(np.abs(np.random.randn(32)))
    bias_node2.attr['dtype'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
    bias_node2.attr['value'].CopyFrom(attr_value_pb2.AttrValue(tensor=tensor_util.make_tensor_proto(
        bias_value2, bias_value2.dtype.type, bias_value2.shape)))

    bias_add_node2 = node_def_pb2.NodeDef()
    bias_add_node2.name = "conv2_bias_add"
    bias_add_node2.op = "BiasAdd"
    bias_add_node2.attr['T'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
    bias_add_node2.input.extend([conv2_node.name, bias_node2.name])
    bias_add_node2.attr['data_format'].CopyFrom(attr_value_pb2.AttrValue(s=b'NHWC'))

    relu_node2 = node_def_pb2.NodeDef()
    relu_node2.op = "Relu"
    relu_node2.name = "relu2"
    relu_node2.attr['T'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
    relu_node2.input.extend([bias_add_node2.name])

    log_node = node_def_pb2.NodeDef()
    log_node.name = "log1"
    log_node.op = "Log"
    log_node.attr['T'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
    log_node.input.extend([relu_node2.name])

    conv3_weight_node = node_def_pb2.NodeDef()
    conv3_weight_node.name = "conv3_weights"
    conv3_weight_node.op = "Const"
    conv3_weight_value = np.float32(np.abs(np.random.randn(3,3,32,32)))
    conv3_weight_node.attr['dtype'].CopyFrom(attr_value_pb2.AttrValue(type=dtypes.float32.as_datatype_enum))
    conv3_weight_node.attr['value'].CopyFrom(attr_value_pb2.AttrValue(
        tensor=tensor_util.make_tensor_proto(
            conv3_weight_value, conv3_weight_value.dtype.type, conv3_weight_value.shape)))

    conv3_node = node_def_pb2.NodeDef()
    conv3_node.name = "conv3"
    conv3_node.op = "Conv2D"
    conv3_node.attr['T'].CopyFrom(attr_value_pb2.AttrValue(
        type=dtypes.float32.as_datatype_enum))
    conv3_node.input.extend([log_node.name, conv3_weight_node.name])
    conv3_node.attr['strides'].CopyFrom(attr_value_pb2.AttrValue(
        list=attr_value_pb2.AttrValue.ListValue(i=[1,1,1,1])))
    conv3_node.attr['dilations'].CopyFrom(attr_value_pb2.AttrValue(
        list=attr_value_pb2.AttrValue.ListValue(i=[1,1,1,1])))
    conv3_node.attr['padding'].CopyFrom(attr_value_pb2.AttrValue(s=b'SAME'))
    conv3_node.attr['data_format'].CopyFrom(attr_value_pb2.AttrValue(s=b'NHWC'))

    identity_node = node_def_pb2.NodeDef()
    identity_node.name = "final"
    identity_node.op = "Identity"
    identity_node.attr['T'].CopyFrom(attr_value_pb2.AttrValue(
        type=dtypes.float32.as_datatype_enum))
    identity_node.input.extend([conv3_node.name])

    test_graph = graph_pb2.GraphDef()

    if bf16_graph:
        test_graph.node.extend([input_node,
                                 conv1_weight_node,
                                 conv1_node,
                                 bias_node,
                                 bias_add_node,
                                 cast_node,
                                 relu_node,
                                 cast2_node,
                                 conv2_weight_node,
                                 conv2_node,
                                 bias_node2,
                                 bias_add_node2,
                                 log_node,
                                 relu_node2,
                                 conv3_weight_node,
                                 conv3_node,
                                 identity_node
                                ])
    else:
        test_graph.node.extend([input_node,
                                 conv1_weight_node,
                                 conv1_node,
                                 bias_node,
                                 bias_add_node,
                                 relu_node,
                                 conv2_weight_node,
                                 conv2_node,
                                 bias_node2,
                                 bias_add_node2,
                                 log_node,
                                 relu_node2,
                                 conv3_weight_node,
                                 conv3_node,
                                 identity_node
                                ])
    return test_graph

class TestBF16Convert(unittest.TestCase):
    rn50_fp32_pb_url = 'https://storage.googleapis.com/intel-optimized-tensorflow/models/v1_6/resnet50_fp32_pretrained_model.pb'
    pb_path = '/tmp/.neural_compressor/resnet50_fp32_pretrained_model.pb'
    platform = platform.system().lower()
    if platform == "windows":
        pb_path = 'C:\\tmp\.neural_compressor\\resnet50_fp32_pretrained_model.pb'
    @classmethod
    def setUpClass(self):
        if not os.path.exists(self.pb_path):
            if self.platform == "linux":
                os.system('mkdir -p /tmp/.neural_compressor && wget {} -O {} '.format(self.rn50_fp32_pb_url, self.pb_path))
            elif self.platform == "windows":
                os.system('md C:\\tmp\.neural_compressor && cd C:\\tmp\.neural_compressor')
                from urllib import request
                request.urlretrieve(self.rn50_fp32_pb_url)

        self.input_graph = tf.compat.v1.GraphDef()
        with open(self.pb_path, "rb") as f:
            self.input_graph.ParseFromString(f.read())
        self.test_graph = create_test_graph()
        self.test_fp32_graph = create_test_graph(False)
        build_fake_yaml()
        build_newapi_fake_yaml()
        build_fake_bf16_rnn_yaml()

    @classmethod
    def tearDownClass(self):
        os.remove('fake_yaml.yaml')
        os.remove('newapi_fake_yaml.yaml')
        os.remove('fake_bf16_rnn.yaml')
        shutil.rmtree("saved", ignore_errors=True)

    def test_bf16_transpose_b_matmul(self):
        from tensorflow.core.framework import attr_value_pb2
        os.environ['FORCE_BF16'] = '1'
        DT_BFLOAT16 = attr_value_pb2.AttrValue(type=dtypes.bfloat16.as_datatype_enum)
        g = tf.Graph()
        with g.as_default():

            x_data = np.array([[0.1, 0.2], [0.2, 0.3]])
            y_data = np.array([[1, 2], [3, 4]], dtype=float)
            x = tf.compat.v1.placeholder(tf.float32, shape=[2, 2], name='x')
            y = tf.constant(y_data, dtype=tf.float32, shape=[2, 2])
            z = tf.matmul(x, y, name='no_quant_matmul', transpose_b=True)
            z = tf.nn.relu6(z, name='op_to_store')
            is_bf16 = False
            with tf.compat.v1.Session() as sess:
                sess.run(z, feed_dict={x: x_data, y: y_data})
                float_graph_def = sess.graph.as_graph_def()

                from neural_compressor.experimental import Quantization, common
                quantizer = Quantization('fake_yaml.yaml')
                dataset = quantizer.dataset('dummy', shape=(2, 2), label=True)
                quantizer.calib_dataloader = common.DataLoader(dataset, batch_size=2)
                quantizer.eval_dataloader = common.DataLoader(dataset, batch_size=2)
                quantizer.model = float_graph_def
                output_graph = quantizer.fit()
                for i in output_graph.graph_def.node:
                    if i.op == 'MatMul' and i.attr["T"] == DT_BFLOAT16:
                        is_bf16 = True
                        break
            self.assertEqual(is_bf16, True)

    @unittest.skipIf(tf.__version__ < "2.0", "currently bf16 convert does not support 1.15up3")
    def test_rn50_convert(self):
        bf16_nodes = [node.name for node in self.input_graph.node if node.op in ["Conv2D", "AvgPool", "MatMul"]]
        bf16_nodes.remove("v0/resnet_v13/conv14/conv2d/Conv2D")
        rn50_bf16_converter = BF16Convert(self.input_graph, ["v0/resnet_v13/conv14/conv2d/Conv2D"], bf16_nodes)
        rn50_bf16_converter.do_transformation()
        new_conv11 = rn50_bf16_converter.cur_graph.node_name_details["v0/resnet_v13/conv11/conv2d/Conv2D"].node
        new_conv14 = rn50_bf16_converter.cur_graph.node_name_details["v0/resnet_v13/conv14/conv2d/Conv2D"].node
        new_conv52 = rn50_bf16_converter.cur_graph.node_name_details["v0/resnet_v115/conv52/conv2d/Conv2D"].node
        self.assertEqual(new_conv11.attr["T"].type, new_conv52.attr["T"].type)
        self.assertNotEqual(new_conv11.attr["T"].type, new_conv14.attr["T"].type)

    @unittest.skipIf(tf.__version__ < "2.0", "currently bf16 convert does not support 1.15up3")
    def test_do_transform(self):
        bf16_converter = BF16Convert(self.test_graph, ["conv3"], ["conv2", "relu2"])
        new_graph = bf16_converter.do_transformation()
        new_conv2 = bf16_converter.cur_graph.node_name_details["conv2"].node
        new_conv3 = bf16_converter.cur_graph.node_name_details["conv3"].node
        new_relu2 = bf16_converter.cur_graph.node_name_details["relu2"].node
        self.assertEqual(new_conv2.attr["T"].type, dtypes.bfloat16)
        self.assertEqual(new_relu2.attr["T"].type, dtypes.bfloat16)
        self.assertEqual(new_conv3.attr["T"].type, dtypes.float32)

    def test_bf16_fallback(self):
        os.environ['FORCE_BF16'] = '1'
        from neural_compressor.experimental import Quantization, common
        quantizer = Quantization('newapi_fake_yaml.yaml')
        dataset = quantizer.dataset('dummy', shape=(1, 224, 224, 3), label=True)
        quantizer.eval_dataloader = common.DataLoader(dataset)
        quantizer.calib_dataloader = common.DataLoader(dataset)
        quantizer.model = self.test_fp32_graph
        output_graph = quantizer.fit()
        # TODO enable the below check after enable PR #1464 merged
        # cast_op_count = 0
        # for node in output_graph.graph_def.node:
        #     if node.op == 'Cast':
        #         cast_op_count += 1
        #     if node.op == 'Log':
        #         self.assertEqual(node.attr["T"].type, dtypes.bfloat16.as_datatype_enum)
        # self.assertTrue(cast_op_count == 0)

    @unittest.skipIf(tf.version.VERSION.find('up') == -1, "Only supports tf 1.x")
    def test_bf16_rnn(self):
        os.environ['FORCE_BF16'] = '1'
        try:
            inp = tf.keras.layers.Input(shape=(None, 4))
            lstm_1 = tf.keras.layers.LSTM(units=10,
                                          return_sequences=True)(inp)
            dropout_1 = tf.keras.layers.Dropout(0.2)(lstm_1)
            lstm_2 = tf.keras.layers.LSTM(units=10,
                                          return_sequences=False)(dropout_1)
            dropout_2 = tf.keras.layers.Dropout(0.2)(lstm_2)
            out = tf.keras.layers.Dense(1)(dropout_2)
            model = tf.keras.models.Model(inputs=inp, outputs=out)

            model.compile(loss="mse",
                          optimizer=tf.keras.optimizers.RMSprop())

            # input_names = [t.name.split(":")[0] for t in model.inputs]
            output_names = [t.name.split(":")[0] for t in model.outputs]

            q_data = np.random.randn(64, 10, 4)
            label = np.random.randn(64, 1)
            model.predict(q_data)

            sess = tf.keras.backend.get_session()

            graph = sess.graph

            from tensorflow.python.framework import graph_util
            graph_def = graph_util.convert_variables_to_constants(
                sess,
                graph.as_graph_def(),
                output_names,
            )
            quant_data = (q_data, label)
            evl_data = (q_data, label)

            from neural_compressor.experimental import Quantization, common

            quantizer = Quantization('fake_bf16_rnn.yaml')
            quantizer.calib_dataloader = common.DataLoader(
                dataset=list(zip(quant_data[0], quant_data[1])))
            quantizer.eval_dataloader = common.DataLoader(
                dataset=list(zip(evl_data[0], evl_data[1])))
            quantizer.model = graph_def
            quantized_model = quantizer.fit()

            convert_to_bf16_flag = False
            for i in quantized_model.graph_def.node:
                if i.name == 'lstm/while/MatMul_3' and \
                        i.attr['T'].type == dtypes.bfloat16.as_datatype_enum:
                    convert_to_bf16_flag = True

            self.assertEqual(convert_to_bf16_flag, True)
        except (NotImplementedError):
            # Kernel bug, happens when the version of python is 3.7 and the version of numpy is >= 1.20.0
            pass

if __name__ == "__main__":
    unittest.main()
