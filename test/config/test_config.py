"""Tests for config file"""
import unittest
import os
from neural_compressor.conf import config as conf
from neural_compressor.utils.constant import *

def helper(content):
    with open('fake_conf.yaml', 'w', encoding="utf-8") as f:
        f.write(content)

class TestConfig(unittest.TestCase):
    def test_config(self):
        from neural_compressor import PostTrainingQuantConfig
        config = PostTrainingQuantConfig()
        self.assertEqual(config.recipes['smooth_quant'], False)
        self.assertEqual(config.recipes['fast_bias_correction'], False)
        self.assertEqual(config.recipes['weight_correction'], False)
        self.assertEqual(config.recipes['dedicated_qdq_pair'], False)
        self.assertEqual(config.recipes['add_qdq_pair_to_weight'], False)
        self.assertEqual(config.recipes['graph_optimization_level'], None)

class TestPyConf(unittest.TestCase):
    def test_config(self):
        from neural_compressor import conf
        from neural_compressor.conf.config import QuantConf, PruningConf, \
            GraphOptConf, BenchmarkConf, DistillationConf
        
        conf.tuning.accuracy_criterion.relative = 0.2
        a = QuantConf(conf)
        self.assertEqual(a.usr_cfg.tuning.accuracy_criterion.relative, 0.2)

        conf.quantization.op_wise = {
            'op1': FP32,
            'op2': {'activation': INT8_SYM_KL_PERTENSOR},
            'op3': {'activation': INT8_SYM_KL_PERCHANNEL, 'weight': INT8_SYM_MINMAX_PERTENSOR}}
        conf.quantization.model_wise = {
            'activation': INT8_SYM_KL_PERTENSOR,
            'weight': INT8_SYM_MINMAX_PERTENSOR}
        a = QuantConf(conf)
        self.assertEqual(a.usr_cfg.quantization.model_wise.weight.scheme, ['sym'])
 
        conf.evaluation.performance.dataloader.dataset = {'dummy': {'shape': '224,224,3'}}
        conf.evaluation.accuracy.dataloader.dataset = {'dummy': {'shape': '224,224,3', 'low': '0.1'}}
 
        conf.evaluation.performance.dataloader.transform = {
            'Resize': {'size': [100, 100]},
            'BilinearImagenet': {'height':300, 'width':300, 'mean_value':[0.2,0.2,0.2]}
            }
        conf.evaluation.performance.dataloader.batch_size = 6
        conf.evaluation.accuracy.metric = {'RMSE': {}}
        conf.tuning.strategy.name = 'mse'
        a = BenchmarkConf(conf)
        self.assertEqual(a.usr_cfg.evaluation.performance.dataloader.batch_size, 6)
        self.assertEqual(a.usr_cfg.evaluation.performance.dataloader.dataset, {'dummy': {'shape': (224,224,3)}})
        self.assertEqual(a.usr_cfg.evaluation.accuracy.metric, {'RMSE': {}})
        a = QuantConf(conf)
        self.assertEqual(a.usr_cfg.tuning.strategy.name, 'mse')
 
        conf.evaluation.accuracy.metric = {'topk': 5}
        conf.graph_optimization.precisions = 'bf16'
        conf.pruning.train.criterion = {'CrossEntropyLoss': {}}
        conf.pruning.train.optimizer = {}
        a = PruningConf(conf)
        self.assertEqual(a.usr_cfg.pruning.train.criterion, {'CrossEntropyLoss': {'from_logits': False, 'reduction': 'mean'}})

        self.assertEqual(a.usr_cfg.evaluation.accuracy.metric, {'topk': 5})
        conf.graph_optimization.op_wise = BF16
        a = GraphOptConf(conf)
        self.assertEqual(a.usr_cfg.graph_optimization.op_wise, {'weight': {'dtype': ['bf16']}, 'activation': {'dtype': ['bf16']}})

        conf.distillation.train.iteration = 900
        a = DistillationConf(conf)
        self.assertEqual(a.usr_cfg.distillation.train.iteration, 900)

class TestConf(unittest.TestCase):
    @classmethod
    def tearDownClass(self):
        os.remove('fake_conf.yaml')

    def test_main_key(self):
        test = '''
        model:
          name: main_key_yaml
          framework: pytorch
        test: cpu
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_framework(self):
        test = '''
        model:
          name: framework_yaml 
          framework: pytorch, mxnet
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        device: cpu
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_device(self):
        test = '''
        model:
          name: device_yaml 
          framework: mxnet
        device: xpu
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: device_yaml 
          framework: mxnet
        device: cpu, gpu
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_version(self):
        test = '''
        model:
          name: version_yaml 
          framework: mxnet
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.version, 2.0)

        test = '''
        version: 2.0

        model:
          name: version_yaml 
          framework: mxnet
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.version, 2.0)

    def test_calibration(self):
        test = '''
        model:
          name: calib_yaml 
          framework: mxnet
        quantization:
          calibration:
            sampling_sizes: 10
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: calib_yaml 
          framework: mxnet
        quantization:
          calibration:
            sampling_size:
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: calib_yaml 
          framework: mxnet
        quantization:
          calibration:
            dataloader:
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: calib_yaml 
          framework: mxnet
        quantization:
          calibration:
          op_wise: {
            'test': {
                'activation': [{'dtype': 'uint8'}, {'algorithm': 'minmax'}]
            }
          }

        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_quantization(self):
        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          model_wise:
            weights:
            granularity: per_channel
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          model_wise:
          approach:
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          approach: post_training_static_quant, quant_aware_training
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          model_wise:
            activation:
              scheme: asym
              dtype: int8
            weight:
              scheme: asym
              dtype: int8
        '''
        helper(test)
        conf.Conf('fake_conf.yaml')

        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          model_wise:
            activation:
              scheme:
              dtype: int8
            weight:
              scheme: asym
              dtype: int8
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_tuning(self):
        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
            relative: 0.01
          strategy:
            name: basic, mse
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
          relative: 0.01
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
            relative: 0.01
          strategy:
            name: fake
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
            relative:
          strategy:
            name: basic
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
          exit_policy:
            timeout: 3
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
            relative: 0.01
            absolute: 0.01
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_workspace(self):
        test = '''
        model:
          name: workspace_yaml 
          framework: mxnet
        tuning:
          workspace: 
            -path: ./workspace
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_inputs_outputs(self):
        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
          inputs: x, y
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.model.inputs, ['x', 'y'])

    def test_objective(self):
        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
          inputs: x, y
        tuning:
          multi_objectives:
            objective: accuracy
            higher_is_better: True
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.tuning.multi_objectives.higher_is_better, [True])

        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
          inputs: x, y
        tuning:
          multi_objectives:
            objective: accuracy, performance
            higher_is_better: True, False
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.tuning.multi_objectives.higher_is_better, [True, False])

        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
          inputs: x, y
        tuning:
          multi_objectives:
            objective: accuracy, performance
            higher_is_better: True False
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.tuning.multi_objectives.higher_is_better, [True, False])

        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
          inputs: x, y
        tuning:
          multi_objectives:
            objective: accuracy, performance
            higher_is_better: [True, False]
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.tuning.multi_objectives.higher_is_better, [True, False])

        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
          inputs: x, y
        tuning:
          multi_objectives:
            objective: accuracy, performance
            higher_is_better: True False
            weight: [0.2, 0.1, 0.7]
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_modelwise_conf_merge(self):
        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
        quantization:
          model_wise:
            weight:
              algorithm:  minmax
            activation:
              algorithm:  minmax
        '''
        helper(test)
        config = conf.QuantConf('fake_conf.yaml')

        framework_modelwise_capability = {
            'CONV2D': {
                'activation': {
                    'dtype': ['uint8', 'fp32'],
                    'scheme': ['asym', 'sym'],
                    'granularity': ['per_tensor'],
                    'algorithm': ['minmax', 'kl']
                },
                'weight': {
                    'dtype': ['int8', 'fp32'],
                    'scheme': [
                        'sym',
                    ],
                    'granularity': ['per_channel', 'per_tensor'],
                    'algorithm': ['minmax']
                },
            },
        }

        tune_space = config.modelwise_tune_space(framework_modelwise_capability)
        self.assertEqual(tune_space['CONV2D']['activation']['algorithm'], ['minmax'])

    def test_metric(self):
        test = '''
        model:
          name: metric_yaml 
          framework: mxnet
        evaluation:
          accuracy:
            multi_metrics:
              topk: 1
              MSE: {}
        '''
        helper(test)

        metrics = {'topk': 1, 'MSE': {}}
        config = conf.QuantConf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.evaluation.accuracy.multi_metrics, metrics)

        test = '''
        model:
          name: metric_yaml 
          framework: mxnet
        evaluation:
          accuracy:
            multi_metrics:
              weight: 0.5 0.5 0.6
              topk: 1
              MSE: {}
        '''
        helper(test)
        self.assertRaises((AssertionError, RuntimeError), conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: metric_yaml 
          framework: mxnet
        evaluation:
          accuracy:
            multi_metrics:
              higher_is_better: True, False
              topk: 1
              MSE: {}
        '''
        helper(test)
        config = conf.QuantConf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.evaluation.accuracy.multi_metrics.higher_is_better, [True, False])

    def test_modelwise_conf_merge2(self):
        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
        quantization:
          model_wise:
            weight:
              algorithm:  minmax
            activation:
              algorithm:  minmax
              dtype: ['uint8', 'fp32']
        '''
        helper(test)
        config = conf.QuantConf('fake_conf.yaml')

        framework_modelwise_capability = {
            'CONV2D': {
                'activation': {
                    'dtype': ['iint8', 'fp32'],
                    'scheme': ['asym', 'sym'],
                    'granularity': ['per_tensor'],
                    'algorithm': ['minmax', 'kl']
                },
                'weight': {
                    'dtype': ['int8', 'fp32'],
                    'scheme': [
                        'sym',
                    ],
                    'granularity': ['per_channel', 'per_tensor'],
                    'algorithm': ['minmax']
                },
            },
        }

        tune_space = config.modelwise_tune_space(framework_modelwise_capability)
        self.assertEqual(tune_space['CONV2D']['activation']['dtype'], ['fp32'])
  
    def test_prune(self):
        test_pytorch_prune = '''
        model:
          name: imagenet_prune
          framework: pytorch
        
        pruning:
          train:
            start_epoch: 0
            end_epoch: 4
            dataloader:
              batch_size: 30
              dataset:
                ImageFolder:
                  root: /path/to/training/dataset
            optimizer:
              SGD:
                learning_rate: 0.1
                momentum: 0.1   
                nesterov: True
                weight_decay: 0.1     
            criterion:
              CrossEntropyLoss:
                reduction: sum
          approach:
            weight_compression:
              initial_sparsity: 0.0
              target_sparsity: 0.97
              pruners:
                - !Pruner
                    start_epoch: 1
                    end_epoch: 3
                    names: ['layer1.0.conv1.weight']
        
                - !Pruner
                    start_epoch: 0
                    end_epoch: 4
                    target_sparsity: 0.6
                    update_frequency: 2
                    names: ['layer1.0.conv2.weight']
        '''
        helper(test_pytorch_prune)
        config = conf.Conf('fake_conf.yaml')
        test_tensorflow_prune = '''
        model:
          name: vit
          framework: tensorflow

        pruning:
          train:
            epoch: 15
            optimizer:
              AdamW:
                learning_rate: 0.001
                weight_decay: 0.0001
            criterion:
              CrossEntropyLoss:
                reduction: sum_over_batch_size
                from_logits: True
          approach:
            weight_compression:
              initial_sparsity: 0.0
              target_sparsity: 0.7
              start_epoch: 0
              end_epoch: 9
              pruners:
                - !Pruner
                    start_epoch: 0
                    end_epoch: 9
                    prune_type: basic_magnitude

        evaluation:
          accuracy:
            metric:
              topk: 1

        tuning:
          accuracy_criterion:
            relative: 0.01
          exit_policy:
            timeout: 0
          random_seed: 9527
        '''
        helper(test_tensorflow_prune)
        config = conf.Conf('fake_conf.yaml')
   
    def test_data_type(self):
        test = '''
        model:
          name: test
          framework: tensorflow

        quantization:
          calibration:
            sampling_size: 20
            dataloader:
              batch_size: 1
              dataset:
                dummy:
                  shape: [[224,224], [256,256]]
                  high: [128., 127]
                  low: 1, 0
                  dtype: ['float32', 'int8']
        '''
        helper(test)
        cfg = conf.Conf('fake_conf.yaml').usr_cfg
        dataset = cfg['quantization']['calibration']['dataloader']['dataset']['dummy']
        self.assertTrue(isinstance(dataset['shape'][0], tuple))
        self.assertTrue(isinstance(dataset['shape'], list))
        self.assertTrue(isinstance(dataset['high'][1], float))
        self.assertTrue(isinstance(dataset['high'][0], float))
        self.assertTrue(isinstance(dataset['low'][0], float))

        test = '''
        model:
          name: test
          framework: tensorflow

        quantization:
          calibration:
            sampling_size: 20
            dataloader:
              batch_size: 1
              dataset:
                dummy:
                  shape: [224,224]
                  high: 128
                  low: 0.1
                  dtype: ['float32', 'int8']
        '''
        helper(test)
        cfg = conf.Conf('fake_conf.yaml').usr_cfg
        dataset = cfg['quantization']['calibration']['dataloader']['dataset']['dummy']
        self.assertTrue(isinstance(dataset['shape'], tuple))
        self.assertTrue(isinstance(dataset['high'], float)) 

        test = '''
        model:
          name: test
          framework: tensorflow

        quantization:
          calibration:
            sampling_size: 20
            dataloader:
              batch_size: 1
              dataset:
                style_transfer:
                  content_folder: test
                  style_folder: test
                  crop_ratio: 0.5
                  resize_shape: 10,10
              transform:
                RandomResizedCrop:
                  size: 10
                  scale: [0.07, 0.99]
                  ratio: [0.6, 0.8]
        '''
        helper(test)
        cfg = conf.Conf('fake_conf.yaml').usr_cfg
        shape_cfg = cfg['quantization']['calibration']['dataloader']['dataset']['style_transfer']['resize_shape']
        self.assertTrue(isinstance(shape_cfg, list)) 
        transform_cfg = cfg['quantization']['calibration']['dataloader']['transform']['RandomResizedCrop']
        self.assertTrue(isinstance(transform_cfg['scale'], list))
        self.assertTrue(isinstance(transform_cfg['ratio'], list))

        test = '''
        model:
          name: test
          framework: tensorflow

        quantization:
          calibration:
            sampling_size: 20
            dataloader:
              batch_size: 1
              dataset:
                style_transfer:
                  content_folder: test
                  style_folder: test
                  crop_ratio: 0.5
                  resize_shape: [10,10]
        '''
        helper(test)
        cfg = conf.Conf('fake_conf.yaml').usr_cfg
        shape_cfg = cfg['quantization']['calibration']['dataloader']['dataset']['style_transfer']['resize_shape']
        self.assertTrue(isinstance(shape_cfg, list)) 

        test = '''
        model:
          name: test
          framework: tensorflow

        quantization:
          calibration:
            sampling_size: 20
            dataloader:
              batch_size: 1
              dataset:
                dummy:
                  shape: 224,224
              transform:
                BilinearImagenet:
                  height: 224
                  width: 224
                  mean_value: 123.68 116.78 103.94
        '''
        helper(test)
        cfg = conf.Conf('fake_conf.yaml').usr_cfg
        shape_cfg = cfg['quantization']['calibration']['dataloader']['dataset']['dummy']['shape']
        self.assertTrue(isinstance(shape_cfg, tuple)) 
        transform_cfg = cfg['quantization']['calibration']['dataloader']['transform']['BilinearImagenet']
        self.assertTrue(isinstance(transform_cfg['mean_value'], list))

    def test_yaml_detection(self):
        try:
            cfg = conf.Conf('not_exist.yaml').usr_cfg
        except:
            pass

    def test_deep_set(self):
      from neural_compressor.conf.dotdict import DotDict, deep_set
      cfg = {'evaluation': {'accuracy': {}}}
      dot_cfg = DotDict(cfg)
      deep_set(dot_cfg, 'evaluation.accuracy.metric', 'iou')
      deep_set(dot_cfg, 'evaluation.accuracy.multi_metrics.weight', [0.1, 0,9])
      deep_set(dot_cfg, 'evaluation.accuracy.multi_metrics.mAP.anno_path', 'anno_path_test')
      self.assertTrue(dot_cfg.evaluation == dot_cfg['evaluation'])
      self.assertTrue(dot_cfg.evaluation.accuracy == dot_cfg['evaluation']['accuracy'])
      self.assertTrue(dot_cfg.evaluation.accuracy.metric == dot_cfg['evaluation']['accuracy']['metric'])
      self.assertTrue(dot_cfg.evaluation.accuracy.multi_metrics == dot_cfg['evaluation']['accuracy']['multi_metrics'])
      self.assertTrue(dot_cfg.evaluation.accuracy.multi_metrics.weight == [0.1, 0,9])
      self.assertTrue(dot_cfg.evaluation.accuracy.multi_metrics.mAP.anno_path == 'anno_path_test')
      multi_metrics1 = dot_cfg.evaluation.accuracy.multi_metrics 
      multi_metrics2 = dot_cfg['evaluation']['accuracy']['multi_metrics']
      self.assertTrue(multi_metrics1 == multi_metrics2)
      self.assertTrue(list(multi_metrics1.keys()) == ['weight', 'mAP'])

if __name__ == "__main__":
    unittest.main()
