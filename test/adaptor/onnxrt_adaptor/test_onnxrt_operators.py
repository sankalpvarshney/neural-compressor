import os
import shutil
import unittest
import copy
import onnx
import numpy as np
from onnx import helper, TensorProto, numpy_helper, onnx_pb
from neural_compressor.adaptor.ox_utils.operators import QOPERATORS
from neural_compressor.adaptor.ox_utils.quantizer import Quantizer
from neural_compressor.adaptor.ox_utils.util import QuantizedInitializer, QuantizedValue, QuantizationMode
import onnxruntime as ort
from neural_compressor.config import ONNXQlinear2QDQConfig

def build_model():
    initializers = []
    input = helper.make_tensor_value_info('input', TensorProto.FLOAT, [1, 3, 15, 15])
    output = helper.make_tensor_value_info('add_out_2', TensorProto.FLOAT, [88, 11])
    
    add_node = onnx.helper.make_node('Add', ['input', 'add_init'], ['add_out'], name='add')

    conv1_weight_initializer = numpy_helper.from_array(
        np.random.randint(-1, 2, [3, 3, 3, 3]).astype(np.float32), name='conv1_weight')
    conv1_node = helper.make_node('Conv', ['add_out', 'conv1_weight'], ['conv1_output'], name='conv1')

    conv2_weight_initializer = numpy_helper.from_array(
        np.random.randint(-1, 2, [5, 3, 3, 3]).astype(np.float32), name='conv2_weight')
    conv2_node = helper.make_node('Conv', ['add_out', 'conv2_weight'], ['conv2_output'], name='conv2')

    # 1, 8, 13, 13
    concat_node = helper.make_node('Concat', ['conv1_output', 'conv2_output'], [
                                        'concat_output'], name='Concat', axis=1)
    # 1, 8, 11, 11
    avg_args = {'kernel_shape': [3, 3]}
    avgpool_node = helper.make_node('AveragePool', ['concat_output'], ['avg_output'], name='AveragePool', **avg_args)
    reshape_node = onnx.helper.make_node('Reshape', ['avg_output', 'shape'], ['reshape_output'], name='Reshape')

    add_node_2 = onnx.helper.make_node('Add', ['reshape_output', 'add_init_2'], ['add_out_2'], name='add_2')

    initializers = [conv1_weight_initializer, conv2_weight_initializer]
    initializers.append(onnx.numpy_helper.from_array(np.array([88, 11], dtype=np.int64), name='shape'))
    initializers.append(onnx.numpy_helper.from_array(np.zeros((1, 3, 15, 15)).astype('float32'), name='add_init'))
    initializers.append(onnx.numpy_helper.from_array(np.zeros((88, 11)).astype('float32'), name='add_init_2'))
 
    graph = helper.make_graph([conv1_node, conv2_node, concat_node, avgpool_node, reshape_node, add_node, add_node_2],
                              'test', [input], [output], initializer=initializers)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    return model

class TestAdaptorONNXRT(unittest.TestCase):

    qlinear_backend = QuantizationMode.QLinearOps
    qdq_backend = 'qdq'
    integer_backend = QuantizationMode.IntegerOps
    static_q_config = {"weight":{'dtype': 3, 
                          'algorithm': 'minmax', 
                          'scheme':'sym', 
                          'granularity': 'per_tensor'},
                       'activation':{'dtype': 2, 
                                     'algorithm': 'minmax', 
                                     'scheme':'asym', 
                                     'granularity':'per_tensor',
                                     'quant_mode': 'static'}
                       }
    dynamic_q_config = {"weight":{'dtype': 3, 
                          'algorithm': 'minmax', 
                          'scheme':'sym', 
                          'granularity': 'per_tensor'},
                        'activation':{'dtype': 2, 
                                      'algorithm': 'minmax', 
                                      'scheme':'asym', 
                                      'granularity':'per_tensor',
                                      'quant_mode': 'dynamic'}
                        }

    @classmethod
    def setUpClass(cls):
        os.makedirs('./onnxrt_test')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree("./onnxrt_test", ignore_errors=True)

    def qlinear_test(self, model, q_config, quantize_params, quantizable_op_types, **kwargs):
        quantizer = Quantizer(copy.deepcopy(model),
            q_config,
            self.qlinear_backend,
            True,
            quantize_params,
            quantizable_op_types,
            **kwargs)
        quantizer.quantize_model()
        assert quantizer.model.model
        return quantizer.model

    def qdq_test(self, model, q_config, quantize_params, quantizable_op_types, **kwargs):
        quantizer = Quantizer(copy.deepcopy(model),
            q_config,
            self.qdq_backend,
            True,
            quantize_params,
            quantizable_op_types,
            **kwargs)
        quantizer.quantize_model()
        assert quantizer.model.model

    def dynamic_test(self, model, q_config, quantize_params, quantizable_op_types):
        quantizer = Quantizer(copy.deepcopy(model),
            q_config,
            self.integer_backend,
            False,
            quantize_params,
            quantizable_op_types)
        quantizer.quantize_model()
        assert quantizer.model.model

    def test_resize(self):
        input_tensor = helper.make_tensor_value_info('input', TensorProto.FLOAT, [1, 2, 26, 42])

        conv_weight_arr = np.random.randint(-1, 2, [3, 2, 3, 3]).astype(np.float32)
        conv_weight_initializer = onnx.numpy_helper.from_array(conv_weight_arr, name='conv1_weight')
        conv_node = onnx.helper.make_node('Conv', ['input', 'conv1_weight'], ['conv_output'], name='conv_node')

        initializers = [conv_weight_initializer]

        output_tensor = helper.make_tensor_value_info('output', TensorProto.FLOAT, [1, 3, 48, 80])
        resize_inputs = ['conv_output']  # resize_roi_name, resize_scales_name, resize_sizes_name]
        resize_attrs = {'coordinate_transformation_mode': 'asymmetric', 'mode': 'nearest', 'nearest_mode': 'floor'}
        resize_node = helper.make_node('Resize', resize_inputs, ['output'], name='resize_node', **resize_attrs)
        resize_roi = [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
        resize_roi_name = 'resize_roi'
        resize_roi_initializer = helper.make_tensor(resize_roi_name, TensorProto.FLOAT, [len(resize_roi)], resize_roi)
        initializers.extend([resize_roi_initializer])
        resize_node.input.extend([resize_roi_name])

        resize_scales = [1.0, 1.0, 2.0, 2.0]
        resize_scales_name = 'resize_scales'
        resize_scales_initializer = helper.make_tensor(resize_scales_name, TensorProto.FLOAT, [
                                                       len(resize_scales)], resize_scales)
        initializers.extend([resize_scales_initializer])
        resize_node.input.extend([resize_scales_name])

        graph = helper.make_graph([conv_node, resize_node], 'TestOpQuantizerResize_test_model',
                                  [input_tensor], [output_tensor], initializer=initializers)
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 7 # use stable onnx ir version
        
        q_config = {'conv_node': self.static_q_config,
                    'resize_node': self.static_q_config}
        quantize_params = {'input': [np.float32(10.), np.uint8(0)],
                           'conv1_weight': [np.float32(10.), np.uint8(0)],
                           'conv_output': [np.float32(10.), np.uint8(0)],
                           'output': [np.float32(10.), np.uint8(0)],
                           }
        self.qlinear_test(model, q_config, quantize_params, ['Resize', 'Conv'])                        
        self.qdq_test(model, q_config, quantize_params, ['Resize', 'Conv'])              

        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 10)])
        model.ir_version = 7 # use stable onnx ir version
        self.qlinear_test(model, q_config, quantize_params, ['Resize', 'Conv'])                        
        self.qdq_test(model, q_config, quantize_params, ['Resize', 'Conv'])      
        
    def test_argmax(self):
        input_name = "input"
        output_name = "output"
        input_shape = [1, 256, 128, 128]
        output_shape = [1, 32, 128]
        initializers = []

        # make Conv node
        conv_weight_name = "conv_weight"
        conv_weight_arr = np.random.randint(-1, 2, [32, 256, 1, 1]).astype(np.float32)
        conv_weight_initializer = onnx.numpy_helper.from_array(conv_weight_arr, name=conv_weight_name)
        conv_output_name = "conv_output"
        conv_inputs = [input_name, conv_weight_name]
        conv_outputs = [conv_output_name]
        conv_name = "conv_node"
        conv_node = onnx.helper.make_node(
            "Conv",
            conv_inputs,
            conv_outputs,
            dilations=[1, 1],
            kernel_shape=[1, 1],
            pads=[0, 0, 0, 0],
            strides=[1, 1],
            name=conv_name,
        )

        # make ArgMax node
        argmax_inputs = [conv_output_name]
        argmax_outputs = [output_name]
        argmax_name = "argmax_node"
        argmax_node = onnx.helper.make_node(
            "ArgMax",
            argmax_inputs,
            argmax_outputs,
            axis=3,
            keepdims=0,
            name=argmax_name,
        )

        initializers = [conv_weight_initializer]

        # make graph
        input_tensor = helper.make_tensor_value_info(input_name, TensorProto.FLOAT, input_shape)
        output_tensor = helper.make_tensor_value_info(output_name, TensorProto.INT64, output_shape)
        graph_name = "ArgMax_Quant_Test"
        graph = helper.make_graph(
            [conv_node, argmax_node],
            graph_name,
            [input_tensor],
            [output_tensor],
            initializer=initializers,
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 7  # use stable onnx ir version
        q_config = {'conv_node': self.static_q_config,
                    'argmax_node': self.static_q_config}
        quantize_params = {'input': [np.float32(10.), np.uint8(0)],
                           'conv_weight': [np.float32(10.), np.uint8(0)],
                           'conv_output': [np.float32(10.), np.uint8(0)],
                           'output': [np.float32(10.), np.uint8(0)],
                           }
        self.qlinear_test(model, q_config, quantize_params, ['Conv', 'ArgMax'])

    def test_gemm(self):
        input_name = "input"
        output_name = "output"
        initializers = []
        weight_shape = [100, 10]
        weight_name = "linear1.weight"
        bias_shape = [100]
        bias_name = "linear1.bias"
        node_name = "gemm"

        weight_data = np.random.normal(0, 0.1, weight_shape).astype(np.float32)
        initializers.append(onnx.numpy_helper.from_array(weight_data, name=weight_name))

        bias_data = np.random.normal(0, 0.1, bias_shape).astype(np.float32)
        initializers.append(onnx.numpy_helper.from_array(bias_data, name=bias_name))

        gemm1_node =  onnx.helper.make_node(
            "Gemm",
            [input_name, weight_name, bias_name],
            [output_name],
            alpha=1.0,
            beta=1.0,
            transB=1,
            name=node_name
        )

        gemm1_output_name = "gemm1_output"
        input_tensor = helper.make_tensor_value_info(input_name, TensorProto.FLOAT, [-1, 10])
        output_tensor = helper.make_tensor_value_info(output_name, TensorProto.FLOAT, [-1, 100])
        graph_name = "gemm_test"
        graph = helper.make_graph(
            [gemm1_node],
            graph_name,
            [input_tensor],
            [output_tensor],
            initializer=initializers,
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 7  # use stable onnx ir version
        q_config = {'gemm': self.static_q_config}
        quantize_params = {'input': [np.float32(10.), np.uint8(0)],
                           'linear1.weight': [np.float32(10.), np.uint8(0)],
                           'linear1.bias': [np.float32(10.), np.uint8(0)],
                           'output': [np.float32(10.), np.uint8(0)],
                           }
        self.qlinear_test(model, q_config, quantize_params, ['Gemm'])
        self.qdq_test(model, q_config, quantize_params, ['Gemm'])

        bias_tensor = helper.make_tensor_value_info(bias_name, TensorProto.FLOAT, [100])
        gemm2_node =  onnx.helper.make_node(
            "Gemm",
            [input_name, weight_name, bias_name],
            [output_name],
            alpha=1.0,
            beta=1.0,
            transB=1,
            name=node_name
        )
        initializers = []
        initializers.append(onnx.numpy_helper.from_array(weight_data, name=weight_name))
        graph_name = "gemm_test"
        graph = helper.make_graph(
            [gemm2_node],
            graph_name,
            [input_tensor, bias_tensor],
            [output_tensor],
            initializer=initializers,
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 7
        self.qlinear_test(model, q_config, quantize_params, ['Gemm'])
        self.qdq_test(model, q_config, quantize_params, ['Gemm'])

    def test_embed(self):
        input_ids_shape = [1, 4]
        input_ids_tensor = helper.make_tensor_value_info('input_ids', TensorProto.INT32, input_ids_shape)

        segment_ids_shape = [1, 4]
        segment_ids_tensor = helper.make_tensor_value_info('segment_ids', TensorProto.INT32, segment_ids_shape)

        # EmbedLayerNormalization Node Constants and Weights:
        word_embed_shape = [32, 4]
        word_embed_weights = np.random.random_sample(word_embed_shape).astype(dtype='float32')
        word_embed_initializer = onnx.numpy_helper.from_array(word_embed_weights, name='word_embed')

        pos_embed_shape = [16, 4]
        pos_embed_weights = np.random.random_sample(pos_embed_shape).astype(dtype='float32')
        pos_embed_initializer = onnx.numpy_helper.from_array(pos_embed_weights, name='pos_embed')

        seg_embed_shape = [2, 4]
        seg_embed_weights = np.random.random_sample(seg_embed_shape).astype(dtype='float32')
        seg_embed_initializer = onnx.numpy_helper.from_array(seg_embed_weights, name='seg_embed')

        gamma_shape = [4]
        gamma = np.random.random_sample(gamma_shape).astype(dtype='float32')
        gamma_initializer = onnx.numpy_helper.from_array(gamma, name='gamma')

        beta_shape = [4]
        beta = np.random.random_sample(beta_shape).astype(dtype='float32')
        beta_initializer = onnx.numpy_helper.from_array(beta, name='beta')

        # EmbedLayerNormalization Outputs:
        layernorm_out_shape = [1, 4, 4]
        layernorm_out_tensor = helper.make_tensor_value_info('layernorm_out', TensorProto.FLOAT, layernorm_out_shape)

        mask_index_out_shape = [1]
        mask_index_out_tensor = helper.make_tensor_value_info('mask_index_out', TensorProto.INT32, mask_index_out_shape)

        # EmbedLayerNormalization Node:
        embed_layer_norm_inputs = [
            'input_ids', 'segment_ids', 'word_embed', 'pos_embed', 'seg_embed', 'gamma', 'beta'
        ]
        embed_layer_norm_outputs = ['layernorm_out', 'mask_index_out']
        embed_layer_norm_node = helper.make_node('EmbedLayerNormalization',
                                                 embed_layer_norm_inputs,
                                                 embed_layer_norm_outputs,
                                                 domain='com.microsoft',
                                                 name='Embed')

        # Construct the Graph and Model:
        nodes = [embed_layer_norm_node]
        graph_name = 'embed_layernorm_graph'
        inputs = [input_ids_tensor, segment_ids_tensor]
        outputs = [layernorm_out_tensor, mask_index_out_tensor]
        initializers = [
            word_embed_initializer, pos_embed_initializer, seg_embed_initializer, gamma_initializer, beta_initializer
        ]

        graph = helper.make_graph(nodes, graph_name, inputs, outputs, initializer=initializers)
        model = helper.make_model(graph, 
            opset_imports=[helper.make_opsetid("com.microsoft", 14), helper.make_opsetid("ai.onnx", 14)])
        model.ir_version = 7 # use stable onnx ir version
        
        q_config = {'Embed': self.static_q_config}
        quantize_params = {'word_embed': [np.uint8(10.), np.float32(0)],
                           'pos_embed': [np.uint8(10.), np.float32(0)],
                           'seg_embed': [np.uint8(10.), np.float32(0)],
                           'gamma': [np.uint8(10.), np.float32(0)],
                           'beta': [np.uint8(10.), np.float32(0)],
                           'layernorm_out': [np.uint8(10.), np.float32(0)],
                           'mask_index_out': [np.uint8(10.), np.float32(0)],
                           'input_ids': [np.uint8(10.), np.float32(0)],
                           } 
        self.qlinear_test(model, q_config, quantize_params, ['EmbedLayerNormalization']) 

        q_model = self.qlinear_test(model, q_config, quantize_params, ['EmbedLayerNormalization'])
        converter = QOPERATORS["QEmbedLayerNormalization"]([i for i in q_model.nodes() if i.op_type == "QEmbedLayerNormalization"][0],
                None, q_model.initializer())
        done, add_node, init = converter.convert()
        self.assertTrue("EmbedLayerNormalization" in [i.op_type for i in add_node])

        self.qdq_test(model, q_config, quantize_params, ['EmbedLayerNormalization']) 

    def test_LSTM(self):
        input_shape = [1, 1, 200]
        input_tensor = helper.make_tensor_value_info('input', TensorProto.FLOAT, input_shape)

        w_shape = [2, 400, 200]
        w_weights = np.random.random_sample(w_shape).astype(dtype='float32')
        w_init = onnx.numpy_helper.from_array(w_weights, name='w')

        r_shape = [2, 400, 100]
        r_weights = np.random.random_sample(r_shape).astype(dtype='float32')
        r_init = onnx.numpy_helper.from_array(r_weights, name='r')

        b_shape = [2, 800]
        b_weights = np.random.random_sample(b_shape).astype(dtype='float32')
        b_init = onnx.numpy_helper.from_array(b_weights, name='b')

        out_shape = [1, 2, 1, 100]
        out_tensor = helper.make_tensor_value_info('out', TensorProto.FLOAT, out_shape)

        kwargs = {}
        kwargs['direction'] = "bidirectional"
        kwargs['activations'] = ["Sigmoid", "Tanh", "Tanh", "Sigmoid", "Tanh", "Tanh"]
        kwargs['hidden_size'] = 100
        kwargs['input_forget'] = 0

        lstm_node = helper.make_node('LSTM',
                                     ['input', 'w', 'r', 'b'],
                                     ['out'],
                                     name='lstm',
                                     domain='',
                                     **kwargs)
        graph = helper.make_graph([lstm_node], 'test', [input_tensor], [out_tensor], initializer=[w_init, r_init, b_init])
        model = helper.make_model(graph, 
            opset_imports=[helper.make_opsetid("", 11)])
        model.ir_version = 7 # use stable onnx ir version
        
        q_config = {'lstm': self.dynamic_q_config}
        self.dynamic_test(model, q_config, None, ['LSTM']) 

    def test_concat_reshape_pooling(self):
        model = build_model()
 
        q_config = {'Reshape':self.static_q_config, 'conv1':self.static_q_config, 'conv2':self.static_q_config, \
                    'Concat':self.static_q_config, 'AveragePool':self.static_q_config, 'add':self.static_q_config}
        quantize_params = {'input': [np.uint8(10.), np.float32(0)],
                           'conv1_weight': [np.uint8(10.), np.float32(0)],
                           'conv1_output': [np.uint8(10.), np.float32(0)],
                           'conv2_weight': [np.uint8(10.), np.float32(0)],
                           'conv2_output': [np.uint8(10.), np.float32(0)],
                           'concat_output': [np.uint8(10.), np.float32(0)],
                           'avg_output': [np.uint8(10.), np.float32(0)],
                           'add_out': [np.uint8(10.), np.float32(0)],
                           'add_init': [np.uint8(10.), np.float32(0)],
                           'shape': [np.uint8(10.), np.float32(0)],
                           'reshape_output': [np.uint8(10.), np.float32(0)],
                           'add_init_2': [np.uint8(10.), np.float32(0)],
                           'add_out_2': [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = ['Reshape', 'Conv', 'Concat', 'AveragePool', 'Add']
        q_model = self.qlinear_test(model, q_config, quantize_params, quantizable_op_types, **{'dedicated_qdq_pair': True})

        q_model.export('test.onnx', ONNXQlinear2QDQConfig())
        export_model = onnx.load('test.onnx')
        self.assertEqual(len(export_model.graph.node), 20)
        os.remove('test.onnx')
 
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types, **{'dedicated_qdq_pair': True})

        q_config = {'Reshape':self.static_q_config, 'conv1':'fp32', 'conv2':self.static_q_config, \
                    'Concat':self.static_q_config, 'AveragePool':self.static_q_config}
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)

        q_config = {'Reshape':self.static_q_config, 'conv1':'fp32', 'conv2':'fp32', \
                    'Concat':self.static_q_config, 'AveragePool':self.static_q_config}
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)

        q_config = {'Reshape':self.static_q_config, 'conv1':self.static_q_config, 'conv2':self.static_q_config, \
                    'Concat':self.static_q_config, 'AveragePool':'fp32'}
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
 
        quantize_params = {'input': [np.uint8(10.), np.float32(0)],
                           'conv1_weight': [np.uint8(10.), np.float32(0)],
                           'conv1_output': [np.uint8(10.), np.float32(0)],
                           'conv2_weight': [np.uint8(10.), np.float32(0)],
                           'conv2_output': [np.uint8(10.), np.float32(0)],
                           'concat_output': [np.uint8(10.), np.float32(0)],
                           'avg_output': [np.uint8(10.), np.float32(0)],
                           'shape': [np.uint8(10.), np.float32(0)],
                           'add_out': [np.uint8(10.), np.float32(0)],
                           'add_init': [np.uint8(10.), np.float32(0)],
                           'reshape_output': [np.uint8(10.), np.float32(0)]}
        q_config = {'Reshape':self.static_q_config, 'conv1':self.static_q_config, 'conv2':self.static_q_config, \
                    'Concat':self.static_q_config, 'AveragePool':self.static_q_config}
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
 
    def test_conv(self):
        for op in ['Conv', 'FusedConv']:
            A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 5, 5, 1])
            B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 3, 3, 1])
            C = helper.make_tensor_value_info('C', TensorProto.FLOAT, [1, 5, 5, 1])
            D = helper.make_tensor_value_info('D', TensorProto.FLOAT, [1, 1, 5, 1])
            conv_node = onnx.helper.make_node(op, ['A', 'B', 'C'], ['D'], 
                                              name=op, 
                                              kernel_shape=[3, 3], 
                                              pads=[1, 1, 1, 1])
            graph = helper.make_graph([conv_node], 'test_graph_1', [A, B, C], [D])
            model = helper.make_model(graph)
            q_config = {op: self.static_q_config},
            quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                               "B": [np.uint8(10.), np.float32(0)],
                               "C": [np.uint8(10.), np.float32(0)],
                               "D": [np.uint8(10.), np.float32(0)]}       
            quantizable_op_types = [op]
            self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
            self.qdq_test(model, q_config, quantize_params, quantizable_op_types)

    def test_matmul(self):
        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 1, 5, 5])
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 1, 5, 1])
        C = helper.make_tensor_value_info('C', TensorProto.FLOAT, [1, 1, 5, 1])
        matmul_node = onnx.helper.make_node('MatMul', ['A', 'B'], ['C'], name='Matmul')
        graph = helper.make_graph([matmul_node], 'test_graph_1', [A, B], [C])
        model = helper.make_model(graph)
        q_config = {"Matmul": self.static_q_config}
        quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                           "B": [np.uint8(10.), np.float32(0)],
                           "C": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = ["Matmul"]
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
        q_config = {"Matmul": self.dynamic_q_config}
        self.dynamic_test(model, q_config, None, quantizable_op_types)
        quantize_params = {"A": [np.float32(10.)],
                           "B": [np.float32(10.)],
                           "C": [np.float32(10.)]}
        with self.assertRaises(ValueError):
            self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        with self.assertRaises(ValueError):
            self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
 
        q_config = {"Matmul": {"weight":{'dtype': 3,
                               'algorithm': 'minmax',
                               'scheme':'sym',
                               'granularity': 'per_tensor'},
                     'activation':{'dtype': 2,
                                   'algorithm': 'minmax',
                                   'scheme':'asym',
                                   'granularity':'per_tensor',
                                   'quant_mode': 'dynamic'}}}
        quantize_params = {}
        self.dynamic_test(model, q_config, quantize_params, quantizable_op_types)

    def test_attention(self):
        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 1, 5, 5])
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 1, 5, 5])
        C = helper.make_tensor_value_info('C', TensorProto.FLOAT, [1, 1, 5, 5])
        D = helper.make_tensor_value_info('D', TensorProto.FLOAT, [1, 1, 5, 5])
        node = onnx.helper.make_node('Attention', ['A', 'B', 'C'], ['D'], name='Attention')
        graph = helper.make_graph([node], 'test_graph_1', [A, B, C], [D])
        model = helper.make_model(graph)
        q_config = {"Attention": self.static_q_config}
        quantize_params = {"A": [np.uint8(0), np.float32(0.5)],
                           "B": [np.uint8(0), np.float32(0.5)],
                           "C": [np.uint8(0), np.float32(0.5)],
                           "D": [np.uint8(0), np.float32(0.5)]}
        quantizable_op_types = ["Attention"]

        q_model = self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        converter = QOPERATORS["QAttention"]([i for i in q_model.nodes() if i.op_type == "QAttention"][0],
                None, q_model.initializer())
        done, add_node, init = converter.convert()
        self.assertTrue("Attention" in [i.op_type for i in add_node])

        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
        q_config = {"Attention": self.dynamic_q_config}
        self.dynamic_test(model, q_config, quantize_params, quantizable_op_types)

        E = helper.make_tensor_value_info('E', TensorProto.INT32, [1, 1, 5, 5])
        F = helper.make_tensor_value_info('F', TensorProto.FLOAT, [1, 1, 5, 5])
        node = onnx.helper.make_node('Attention', ['A', 'B', 'C', 'F', 'E'], ['D'], name='Attention')
        graph = helper.make_graph([node], 'test_graph_1', [A, B, C, F, E], [D])
        model = helper.make_model(graph)
        q_config = {"Attention": self.static_q_config}
        quantize_params = {"A": [np.uint8(0), np.float32(0.5)],
                           "B": [np.uint8(0), np.float32(0.5)],
                           "C": [np.uint8(0), np.float32(0.5)],
                           "D": [np.uint8(0), np.float32(0.5)]}
        quantizable_op_types = ["Attention"]
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
        q_config = {"Attention": self.dynamic_q_config}
        self.dynamic_test(model, q_config, quantize_params, quantizable_op_types)

    def test_gather(self):
        a_value = np.random.randn(100, 4).astype(np.float32)
        A_init = helper.make_tensor('A', TensorProto.FLOAT, [100, 4], 
                                    a_value.reshape(400).tolist())
        b_value = np.random.randint(2, size=(1, 10)).astype(np.int32)
        B_init = helper.make_tensor('B', TensorProto.INT32, [1, 10],
                                    b_value.reshape(10).tolist())
        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [100, 4])
        B = helper.make_tensor_value_info('B', TensorProto.INT32, [1, 10])
        C = helper.make_tensor_value_info('C', TensorProto.FLOAT, [1, 10, 4])
        node = onnx.helper.make_node('Gather', ['A', 'B'], ['C'], name='Gather')
        graph = helper.make_graph([node], 'test_graph_1', [A, B], [C], [A_init, B_init])
        model = helper.make_model(graph)
        q_config = {'Gather': {"weight":{'dtype': 3,
                                         'algorithm': 'minmax',
                                         'scheme':'sym',
                                         'granularity': 'per_tensor'},
                              'activation':{'dtype': 2,
                                         'algorithm': 'minmax',
                                         'scheme':'asym',
                                         'granularity':'per_tensor',
                                         'quant_mode': 'static'}
                  }} 
        quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                           "C": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = ["Gather"]
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
        q_config = {'Gather': {"weight":{'dtype': 3,
                                         'algorithm': 'minmax',
                                         'scheme':'sym',
                                         'granularity': 'per_tensor'},
                              'activation':{'dtype': 2,
                                         'algorithm': 'minmax',
                                         'scheme':'asym',
                                         'granularity':'per_tensor',
                                         'quant_mode': 'dynamic'}
                  }} 
        self.dynamic_test(model, q_config, quantize_params, quantizable_op_types)
        graph = helper.make_graph([node], 'test_graph_1', [A, B], [C])
        model = helper.make_model(graph)
        q_config = {'Gather': {"weight":{'dtype': 3,
                                         'algorithm': 'minmax',
                                         'scheme':'sym',
                                         'granularity': 'per_tensor'},
                              'activation':{'dtype': 2,
                                         'algorithm': 'minmax',
                                         'scheme':'asym',
                                         'granularity':'per_tensor',
                                         'quant_mode': 'dynamic'}
                  }}
        quantize_params = {}
        self.dynamic_test(model, q_config, quantize_params, quantizable_op_types)

    def test_split(self):
        D = helper.make_tensor_value_info('D', TensorProto.FLOAT, [100, 2])
        e_value = np.random.randn(2, 2).astype(np.float32)
        E_init = helper.make_tensor('E', TensorProto.FLOAT, [2, 2],
                                    e_value.reshape(4).tolist())
 
        matmul_node = onnx.helper.make_node('MatMul', ['D', 'E'], ['A'], name='Matmul')
 
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [50, 2])
        C = helper.make_tensor_value_info('C', TensorProto.FLOAT, [50, 2])
        node = onnx.helper.make_node('Split', ['A'], ['B', 'C'], name='Split', **{'num_outputs': 2})
        graph = helper.make_graph([matmul_node, node], 'test_graph_1', [D], [B, C], [E_init])
        model = helper.make_model(graph)
        q_config = {'Split': {"weight":{'dtype': 3,
                                         'algorithm': 'minmax',
                                         'scheme':'sym',
                                         'granularity': 'per_tensor'},
                              'activation':{'dtype': 2,
                                         'algorithm': 'minmax',
                                         'scheme':'asym',
                                         'granularity':'per_tensor',
                                         'quant_mode': 'static'}
                        },
                    'Matmul': {"weight":{'dtype': 3,
                                         'algorithm': 'minmax',
                                         'scheme':'sym',
                                         'granularity': 'per_tensor'},
                              'activation':{'dtype': 2,
                                         'algorithm': 'minmax',
                                         'scheme':'asym',
                                         'granularity':'per_tensor',
                                         'quant_mode': 'static'}
                        },
 
                    }
        quantize_params = {"A": [np.uint8(0), np.float32(0.5)],
                           "B": [np.uint8(0), np.float32(0.5)],
                           "C": [np.uint8(0), np.float32(0.5)],
                           "D": [np.uint8(0), np.float32(0.5)],
                           "E": [np.uint8(0), np.float32(0.5)],
                           }
        quantizable_op_types = ["Split", "MatMul"]
        q_model = self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        q_model.export('test.onnx', ONNXQlinear2QDQConfig())
        export_model = onnx.load('test.onnx')
        self.assertEqual(len(export_model.graph.node), 11)
        os.remove('test.onnx')

        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
 
    def test_pad(self):
        b_value = np.array([0, 1, 1, 0, 1, 1]).astype(np.int64)
        B_init = helper.make_tensor('B', TensorProto.INT64, [6],
                                    b_value.reshape(6).tolist())
        B = helper.make_tensor_value_info('B', TensorProto.INT64, [6])
        C = helper.make_tensor_value_info('C', TensorProto.FLOAT, [1, 7, 7])

        d_value = np.random.randn(1).astype(np.float32)
        D_init = helper.make_tensor('D', TensorProto.FLOAT, [1],
                                    d_value.reshape(1).tolist())
        D = helper.make_tensor_value_info('D', TensorProto.FLOAT, [1])

        e_value = np.random.randn(1, 5, 5).astype(np.float32)
        E_init = helper.make_tensor('E', TensorProto.FLOAT, [1, 1, 5, 5],
                                    e_value.reshape(25).tolist())
        E = helper.make_tensor_value_info('E', TensorProto.FLOAT, [1, 1, 5, 5])
        f_value = np.random.randn(1, 3, 3).astype(np.float32)
        F_init = helper.make_tensor('F', TensorProto.FLOAT, [1, 1, 3, 3],
                                    f_value.reshape(9).tolist())
        F = helper.make_tensor_value_info('F', TensorProto.FLOAT, [1, 1, 3, 3])
        for mode in ["constant", "edge", "reflect", "constant_value", "constant_value_wo_init"]:
            conv_node = onnx.helper.make_node('Conv', ['E', 'F'], ['A'], 
                                              name='Conv', 
                                              kernel=[3, 3],
                                              padding=[1, 1, 1, 1])
            if mode == "constant_value":
                node = onnx.helper.make_node('Pad', ['A', 'B', 'D'], ['C'], name='Pad', mode="constant")
                graph = helper.make_graph([conv_node, node], 'test_graph_1', [E, F, B, D], [C], [E_init, F_init, B_init, D_init])
            elif mode == "constant_value_wo_init":
                node = onnx.helper.make_node('Pad', ['A', 'B', 'D'], ['C'], name='Pad', mode="constant")
                graph = helper.make_graph([conv_node, node], 'test_graph_1', [E, F, B, D], [C], [E_init, F_init, B_init])
            else:
                node = onnx.helper.make_node('Pad', ['A', 'B'], ['C'], name='Pad', mode=mode)
                graph = helper.make_graph([conv_node, node], 'test_graph_1', [E, F, B], [C], [E_init, F_init, B_init])
            model = helper.make_model(graph)
            pad_config = {"weight":{'dtype': 3,
                                    'algorithm': 'minmax',
                                    'scheme':'sym',
                                    'granularity': 'per_tensor'},
                         'activation':{'dtype': 2,
                                    'algorithm': 'minmax',
                                    'scheme':'asym',
                                    'granularity':'per_tensor',
                                    'quant_mode': 'static'}}
            conv_config = {"weight":{'dtype': 3,
                                    'algorithm': 'minmax',
                                    'scheme':'sym',
                                    'granularity': 'per_channel'},
                         'activation':{'dtype': 2,
                                    'algorithm': 'minmax',
                                    'scheme':'asym',
                                    'granularity':'per_tensor',
                                    'quant_mode': 'static'}}
            q_config = {'Conv': conv_config,
                        'Pad': pad_config}
            quantize_params = {"A": [np.uint8(10.), np.float32(1)],
                               "C": [np.uint8(10.), np.float32(1)],
                               "D": [np.uint8(10.), np.float32(1)],
                               "E": [np.uint8(10.), np.float32(1)],
                               "F": [np.uint8(10.), np.float32(1)]}
            quantizable_op_types = ["Conv", "Pad"]
            self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
            self.qdq_test(model, q_config, quantize_params, quantizable_op_types, **{'dedicated_qdq_pair': True})

        node = onnx.helper.make_node('Pad', ['E', 'B', 'D'], ['C'], name='Pad', mode="constant")
        graph = helper.make_graph([node], 'test_graph_1', [E, B, D], [C], [E_init, B_init, D_init])
        model = helper.make_model(graph)
        q_config = {'Pad': {'activation':{'dtype': 2,
                                         'algorithm': 'minmax',
                                         'scheme':'asym',
                                         'granularity':'per_tensor',
                                         'quant_mode': 'static'}
                  }}
        quantize_params = {"C": [np.uint8(10.), np.float32(0)],
                           "E": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = ["Pad"]
        self.qlinear_test(model, pad_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, pad_config, quantize_params, quantizable_op_types)

    def test_binary(self):
        for op in ['Mul', 'Add']:
            A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 10])
            B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1])
            C = helper.make_tensor_value_info('C', TensorProto.FLOAT, [1, 10])
            node = onnx.helper.make_node(op, ['A', 'B'], ['C'], name=op)
            graph = helper.make_graph([node], 'test_graph_1', [A, B], [C])
            model = helper.make_model(graph)
            q_config = {op: self.static_q_config}
            quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                               "B": [np.uint8(10.), np.float32(0)],
                               "C": [np.uint8(10.), np.float32(0)]}
            quantizable_op_types = [op]
            self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
            self.qlinear_test(model, q_config, {}, quantizable_op_types)
            self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
            self.qdq_test(model, q_config, {}, quantizable_op_types)
    
    def test_relu(self):
        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 1, 5, 5])
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 1, 3, 3])
        D = helper.make_tensor_value_info('D', TensorProto.FLOAT, [1, 1, 5, 5])
        E = helper.make_tensor_value_info('E', TensorProto.FLOAT, [1, 1, 5, 5])
        F = helper.make_tensor_value_info('F', TensorProto.FLOAT, [1, 1, 5, 5])
  
        conv_node = onnx.helper.make_node('Conv', ['A', 'B'], ['C'], 
                                          name='Conv', 
                                          kernel_shape=[3, 3], 
                                          pads=[1, 1, 1, 1])
        relu_node = onnx.helper.make_node('Relu', ['C'], ['D'], name='Relu')
        add_node = onnx.helper.make_node('Add', ['D', 'E'], ['F'], name='Add')
        graph = helper.make_graph([conv_node, relu_node], 'test_graph_1', [A, B], [D])
        model = helper.make_model(graph, **{'opset_imports': [helper.make_opsetid('', 13)]})
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
        sess_options.optimized_model_filepath = "./onnxrt_test/optimized_model.onnx"  
        session = ort.InferenceSession(model.SerializeToString(), sess_options, providers=ort.get_available_providers())
        tmp_model = onnx.load(sess_options.optimized_model_filepath)
 
        q_config = {"Conv": self.static_q_config, "Relu": self.static_q_config}
        quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                           "B": [np.uint8(10.), np.float32(0)],
                           "C": [np.uint8(10.), np.float32(0)],
                           "D": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = ["Conv", "Relu"]
        self.qlinear_test(tmp_model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(tmp_model, q_config, quantize_params, quantizable_op_types)
        
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        session = ort.InferenceSession(model.SerializeToString(), sess_options, providers=ort.get_available_providers())
        tmp_model = onnx.load(sess_options.optimized_model_filepath)
        self.qlinear_test(tmp_model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(tmp_model, q_config, quantize_params, quantizable_op_types)
 
        graph = helper.make_graph([conv_node, relu_node, add_node], 'test_graph_2', [A, B, E], [F])
        model = helper.make_model(graph, **{'opset_imports': [helper.make_opsetid('', 13)]})
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        session = ort.InferenceSession(model.SerializeToString(), sess_options, providers=ort.get_available_providers())
        tmp_model = onnx.load(sess_options.optimized_model_filepath)
        self.qlinear_test(tmp_model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(tmp_model, q_config, quantize_params, quantizable_op_types)
 
    def test_clip(self):
        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 1, 5, 5])
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 1, 3, 3])
        D = helper.make_tensor_value_info('D', TensorProto.FLOAT, [1, 1, 5, 5])
        conv_node = onnx.helper.make_node('Conv', ['A', 'B'], ['C'],
                                          name='Conv',
                                          kernel_shape=[3, 3],
                                          pads=[1, 1, 1, 1])
        clip_node = onnx.helper.make_node('Clip', ['C'], ['D'], name='Clip')
        graph = helper.make_graph([conv_node, clip_node], 'test_graph_1', [A, B], [D])
        model = helper.make_model(graph, **{'opset_imports': [helper.make_opsetid('', 13)]})

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
        sess_options.optimized_model_filepath = "./onnxrt_test/optimized_model.onnx"
        session = ort.InferenceSession(model.SerializeToString(), sess_options, providers=ort.get_available_providers())
        model = onnx.load(sess_options.optimized_model_filepath)

        q_config = {"Conv": self.static_q_config, "Clip": self.static_q_config}
        quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                           "B": [np.uint8(10.), np.float32(0)],
                           "C": [np.uint8(10.), np.float32(0)],
                           "D": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = ["Conv", "Clip"]
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)

    def test_activation(self):
        for op in ["Relu", "LeakyRelu", "Sigmoid"]:
            B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 10])
            A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 10])
            node = onnx.helper.make_node(op, ['A'], ['B'], name=op)
            graph = helper.make_graph([node], 'test_graph_1', [A], [B])
            model = helper.make_model(graph)
            q_config = {op: self.static_q_config}
            quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                               "B": [np.uint8(10.), np.float32(0)]}
            quantizable_op_types = [op]
            self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
            self.qdq_test(model, q_config, quantize_params, quantizable_op_types)

            a_value = np.random.randn(1, 10).astype(np.float32)
            A_init = helper.make_tensor('A', TensorProto.FLOAT, [1, 10],
                                        a_value.reshape(10).tolist())
            graph = helper.make_graph([node], 'test_graph_1', [A], [B], [A_init])
            model = helper.make_model(graph)
            self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
            self.qdq_test(model, q_config, quantize_params, quantizable_op_types)
            self.qlinear_test(model, q_config, {}, quantizable_op_types)
            self.qdq_test(model, q_config, {}, quantizable_op_types)

    def test_pooling(self):
        op = "MaxPool"
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 5, 5, 1])
        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 5, 5, 1])
        node = onnx.helper.make_node(op, ['A'], ['B'], 
                                     name=op,
                                     kernel_shape=[3, 3],
                                     pads=[1, 1, 1, 1])
        graph = helper.make_graph([node], 'test_graph_1', [A], [B]) 
        q_config = {op: self.static_q_config}
        quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                           "B": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = [op]
        for opset_version in [12, 13]:
            opset = onnx.OperatorSetIdProto()
            opset.version = opset_version
            model = helper.make_model(graph, opset_imports=[opset])
            self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
            self.qdq_test(model, q_config, quantize_params, quantizable_op_types)

        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 1, 5, 5])
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 1, 3, 3])
        D = helper.make_tensor_value_info('D', TensorProto.FLOAT, [1, 1, 5, 5])
        conv_node = onnx.helper.make_node('Conv', ['A', 'B'], ['C'],
                                          name='Conv',
                                          kernel_shape=[3, 3],
                                          pads=[1, 1, 1, 1])
        pool_node = onnx.helper.make_node(op, ['C'], ['D'], name=op)
        graph = helper.make_graph([conv_node, pool_node], 'test_graph_1', [A, B], [D])
        model = helper.make_model(graph)
 
        q_config = {"Conv": self.static_q_config, op: self.static_q_config}
        quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                           "B": [np.uint8(10.), np.float32(0)],
                           "C": [np.uint8(10.), np.float32(0)],
                           "D": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = ["Conv", op]
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)

        op = "GlobalAveragePool"
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 5, 1, 1])
        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 5, 5, 1])
        node = onnx.helper.make_node(op, ['A'], ['B'], 
                                     name=op,
                                     kernel_shape=[3, 3],
                                     pads=[1, 1, 1, 1])
        graph = helper.make_graph([node], 'test_graph_1', [A], [B]) 
        q_config = {op: self.static_q_config}
        quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                           "B": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = [op]
        for opset_version in [12, 13]:
            opset = onnx.OperatorSetIdProto()
            opset.version = opset_version
            model = helper.make_model(graph, opset_imports=[opset])
            self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
            self.qdq_test(model, q_config, quantize_params, quantizable_op_types)

        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 1, 5, 5])
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [1, 1, 3, 3])
        D = helper.make_tensor_value_info('D', TensorProto.FLOAT, [1, 1, 1, 1])
        conv_node = onnx.helper.make_node('Conv', ['A', 'B'], ['C'],
                                          name='Conv',
                                          kernel_shape=[3, 3],
                                          pads=[1, 1, 1, 1])
        pool_node = onnx.helper.make_node(op, ['C'], ['D'], name=op)
        graph = helper.make_graph([conv_node, pool_node], 'test_graph_1', [A, B], [D])
        model = helper.make_model(graph)
 
        q_config = {"Conv": self.static_q_config, op: self.static_q_config}
        quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                           "B": [np.uint8(10.), np.float32(0)],
                           "C": [np.uint8(10.), np.float32(0)],
                           "D": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = ["Conv", op]
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)


    def test_exclude_node(self):
        A = helper.make_tensor_value_info('A', TensorProto.FLOAT, [1, 5, 5, 1])
        B = helper.make_tensor_value_info('B', TensorProto.FLOAT, [3, 3, 1, 1])
        D = helper.make_tensor_value_info('D', TensorProto.FLOAT, [1, 1, 3, 3])
        conv_node = onnx.helper.make_node('Conv', ['A', 'B'], ['C'],
                                          name='Conv',
                                          kernel_shape=[3, 3],
                                          pads=[1, 1, 1, 1])
        pool_node = onnx.helper.make_node("MaxPool", ['C'], ['D'], name="MaxPool")
        graph = helper.make_graph([conv_node, pool_node], 'test_graph_1', [A, B], [D])
        model = helper.make_model(graph)

        q_config = {"Conv": self.static_q_config, "MaxPool": "fp32"}
        quantize_params = {"A": [np.uint8(10.), np.float32(0)],
                           "B": [np.uint8(10.), np.float32(0)],
                           "C": [np.uint8(10.), np.float32(0)],
                           "D": [np.uint8(10.), np.float32(0)]}
        quantizable_op_types = ["Conv", "MaxPool"]
        self.qlinear_test(model, q_config, quantize_params, quantizable_op_types)
        self.qdq_test(model, q_config, quantize_params, quantizable_op_types)

if __name__ == "__main__":
    unittest.main()
