"""Pruner."""
# !/usr/bin/env python
# -*- coding: utf-8 -*-
#
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

import copy
from .utils import torch, F
from functools import partial
from .patterns import get_pattern
from .schedulers import get_scheduler
from .criteria import get_criterion, CRITERIA
from .regs import get_reg
from .utils import logger
# model slim related
from .model_slim.pattern_analyzer import Linear2LinearSearcher, RecipeSearcher
from .model_slim.weight_slim import LinearCompressionIterator, MHACompression

PRUNERS = {}

def register_pruner(name):
    """Class decorator to register a Pruner subclass to the registry.

    Decorator function used before a Pattern subclass.
    Make sure that the Pruner class decorated by this function can be registered in PRUNERS.

    Args:
        cls (class): The subclass of register.
        name: A string. Define the pruner type.

    Returns:
        cls: The class of register.
    """

    def register(pruner):
        PRUNERS[name] = pruner
        return pruner

    return register

def parse_valid_pruner_types():
    """Get all valid pruner names."""
    valid_pruner_types = []
    for x in CRITERIA.keys():
        for p in ["", "_progressive"]:
            valid_pruner_types.append(x + p)
    valid_pruner_types.append("pattern_lock")
    return valid_pruner_types

def get_pruner(config, modules):
    """Get registered pruner class.

    Get a Pruner object from PRUNERS.

    Args:
        modules: A dict {"module_name": Tensor} that stores the pruning modules' weights.
        config: A config dict object that contains the pruner information.

    Returns:
        A Pruner object.

    Raises: AssertionError: Cuurently only support pruners that have been registered in PRUNERS.
    """
    ## do the ugly work here
    if "progressive" not in config["pruning_type"]:
        name = config["pruning_type"]
        config["progressive"] = False
    else:
        # if progressive, delete "progressive" words and reset config["progressive"]
        name = config["pruning_type"][0:-12]
        config["progressive"] = True
    if name in CRITERIA:
        if config["progressive"] == False:
            config['criterion_type'] = name
            if "block" in name or "free" in name:
                assert ":" not in config["pattern"], f"{name} pruner type does not support {config['pattern']} pattern."
            else :
                name = "basic"  ##return the basic pruner
        else:
            config['criterion_type'] = name
            name = "progressive"  ## return the progressive pruner

    if name not in PRUNERS.keys():
        assert False, f"does not support {name}, currently only support {parse_valid_pruner_types()}"
    return PRUNERS[name](config, modules)

def model_slim(model, round_multiplier=0):
    """Slim the sparse model automatically."""
    model = model_slim_ffn2(model, round_multiplier)
    model = model_slim_mha(model)
    return model

def model_slim_ffn2(model, round_multiplier=0):
    """Remove some sparse part in the model permanently and obtain acceleration directly.

    Args:
        model: a sprase model.
        round_multiplier(int): the channel number after slimming should be multiple of this number.
    """
    logger.warning(f"You are using model slim methods, some weight channels will be removed permanently.")
    pa_obj = Linear2LinearSearcher(model)
    layers = pa_obj.search()
    linear_pruner = LinearCompressionIterator(layers)
    linear_pruner(masks=None, round_value=round_multiplier)
    return model

def model_slim_mha(model):
    """Remove some sparse part in the model permanently and obtain acceleration directly.

    Args:
        model: a sprase model.
    """
    logger.warning(f"You are using model slim methods, some attention heads will be removed permanently.")
    recipe = {'BertLayer': ["attention"]}
    searcher = RecipeSearcher(model, recipe)
    layers = searcher.search('BertLayer')
    if "PyTorchFXModel" in type(model).__name__:
        config = model.model.config
    else:
        config = model.config
    # linear_pruner = LinearCompressionIterator(layers)
    for item in layers:
        mha_compression = MHACompression(
            item[0], config.num_attention_heads, config.hidden_size // config.num_attention_heads
        )
        mha_compression()
    return model

class BasePruner:
    """Pruning Pruner.

    The class which executes pruning process.

    Args:
        modules: A dict {"module_name": Tensor} that stores the pruning modules' weights.
        config: A config dict object that contains the pruner information.

    Attributes:
        modules: A dict {"module_name": Tensor} that stores the pruning modules' weights.
        config: A config dict object that contains the pruner information.
        masks: A dict {"module_name": Tensor} that stores the masks for modules' weights.
        scores: A dict {"module_name": Tensor} that stores the score for modules' weights,
            which are used to determine what parts to be pruned by a criterion.
        pattern: A Pattern object defined in ./patterns.py
        scheduler: A scheduler object defined in ./scheduler.py
        current_sparsity_ratio: A float representing the current model's sparsity ratio; it is initialized to be zero.
        global_step: An integer representing the total steps the model has run.
        start_step: An integer representing when to trigger pruning process.
        end_step: An integer representing when to end pruning process.
        pruning_frequency: An integer representing the pruning frequency; it is valid when iterative
            pruning is enabled.
        target_sparsity_ratio: A float showing the final sparsity after pruning.
        max_sparsity_ratio_per_op: A float showing the maximum sparsity ratio for every module.
    """

    def __init__(self, config, modules):
        """Initialize."""
        self.modules = modules
        self.config = config
        self.masks = {}
        self.global_step = 0
        self.handled_global_step = -1
        self.start_step = self.config['start_step']
        self.end_step = self.config['end_step']
        self.pruning_frequency = self.config['pruning_frequency']
        ##this is different with original code
        self.total_prune_cnt = (self.end_step - self.start_step + self.pruning_frequency) \
                               // self.pruning_frequency
        self.completed_pruned_cnt = 0
        self.total_prune_cnt -= 1  ## not pruning at step 0
        if self.total_prune_cnt == 0:
            self.total_prune_cnt = 1
            self.completed_pruned_cnt = 1

        for key in self.modules.keys():
            module = self.modules[key]
            self.masks[key] = torch.ones(module.weight.shape).to(module.weight.device)  ##TODO support bias or others

        self.target_sparsity_ratio = self.config['target_sparsity']
        self.current_sparsity_ratio = 0.0
        self.init_sparsity_ratio = 0.0
        self._init()

    def _init(self):
        """Auxiliary function for initializing."""
        pass

    def on_epoch_begin(self, epoch):
        """Implement at the beginning of each epoch."""
        pass

    def mask_weights(self):
        """Apply masks to corresponding modules' weights.

        Weights are multipled with masks. This is the formal pruning process.
        """
        with torch.no_grad():
            for key in self.modules.keys():
                module = self.modules[key]
                module.weight.data = module.weight.data * self.masks[key]

    def mask_weights_general(self, input_masks):
        """Apply input masks to corresponding modules' weights.

        Weights are multipled with input_masks.

        Args:
            input_masks: A dict {"module_name": Tensor} that stores the masks for modules' weights.
        """
        with torch.no_grad():
            for key in self.modules.keys():
                module = self.modules[key]
                module.weight.data = module.weight.data * input_masks[key]

    def on_step_begin(self, local_step):
        """Implement at the start of each step."""
        if self.handled_global_step == self.global_step:
            return
        self.update_masks(local_step)
        self.handled_global_step = self.global_step

    def update_masks(self, local_step):
        """Update the masks at a given local step."""
        pass

    def on_epoch_end(self):
        """Implement at the end of each epoch."""
        pass

    def on_step_end(self):
        """Implement at the end of each step."""
        pass

    def on_before_optimizer_step(self):
        """Implement before optimizer.step()."""
        pass

    def on_after_optimizer_step(self):
        """Implement after optimizer.step().

        Prune the model after optimization.
        """
        self.mask_weights()
        self.global_step += 1

    def on_train_begin(self, dataloader=None):
        """Implement at the beginning of training phase."""
        pass

    def on_train_end(self):
        """Implement at the end of training phase."""
        pass

    def on_before_eval(self):
        """Implement at the beginning of evaluation phase."""
        pass

    def on_after_eval(self):
        """Implement at the end of evaluation phase."""
        pass

    def check_is_pruned_step(self, step):
        """Check if a pruning process should be performed at the current step.

        Args:
            step: an integer representing the number of current step.

        Returns:
            A Boolean.
        """
        if step < self.start_step or step > self.end_step:
            return False
        if int(step - self.start_step) % self.pruning_frequency == 0:
            return True
        return False

    def rewrite_forward(self):
        """Rewrite forward to implement block mask operation"""
        def forward(self, input):
            block_size = [self.weight.shape[0]//self.block_mask.shape[0], \
                    self.weight.shape[1]//self.block_mask.shape[1]]
            mask = self.block_mask.repeat_interleave(block_size[0], dim=0).repeat_interleave(\
                                                        block_size[1], dim=-1).to(self.weight.device)
            return F.linear(input, self.weight*mask, self.bias)
        
        for key in self.modules.keys():
                if not hasattr(self.modules[key], 'block_mask'):
                    continue # No corresponding block mask, skip.
                module = self.modules[key]
                module.forward = partial(forward, module)
                
    def recover_forward(self):
        """Restore the forward format at the end of pruning"""
        with torch.no_grad():
            for key in self.modules.keys():
                if not hasattr(self.modules[key], 'block_mask'):
                    continue # No corresponding block mask, skip.
                module = self.modules[key]
                module.forward = partial(torch.nn.Linear.forward, module)
                

@register_pruner("basic")
class BasicPruner(BasePruner):
    """Pruning Pruner.

    The class which executes pruning process.
    1. Defines pruning functions called at step begin/end, epoch begin/end.
    2. Defines the pruning criterion.

    Args:
        modules: A dict {"module_name": Tensor} that stores the pruning modules' weights.
        config: A config dict object that contains the pruner information.

    Attributes:
        pattern: A Pattern object that defines pruning weights' arrangements within space.
        criterion: A Criterion Object that defines which weights are to be pruned
        scheduler: A Scheduler object that defines how the model's sparsity changes as training/pruning proceeds.
        reg: A Reg object that defines regulization terms.
    """

    def __init__(self, config, modules):
        """Initialize."""
        # self.modules = modules
        # self.config = config
        # self.masks = {}
        super(BasicPruner, self).__init__(config, modules)

    def _init(self):
        """Auxiliary function for initializing."""
        self.pattern = get_pattern(self.config, self.modules)
        self.scheduler = get_scheduler(self.config)
        self.criterion = get_criterion(self.config, self.modules)
        self.reg = get_reg(self.config, self.modules, self.pattern)
        # if switch off progressive but use per-channel pruning, give a warn
        if "channel" in self.pattern.pattern:
            logger.info("UserWarning: use per-channel pruning pattern without progressive pruning!")
            logger.info("Instead, enabling progressive pruning would be a better choice.")
        else:
            pass

    def set_global_step(self, global_step):
        """Set global step number."""
        self.global_step = global_step

    # def on_step_begin(self, local_step):
    #     """Implement at the start of each step.
    #
    #     Update the masks at a given local_step.
    #     """
    #     self.update_masks(local_step)

    def update_masks(self, local_step):
        """Update the masks at a given local step."""
        if self.global_step == self.start_step:
            if self.config['lock_init_sparsity']:
                self.masks = self.pattern.get_pattern_lock_masks(self.modules)
                self.init_sparsity_ratio = self.pattern.get_sparsity_ratio(self.masks)
                self.current_sparsity_ratio = self.init_sparsity_ratio

        if not self.check_is_pruned_step(self.global_step):
            return

        if self.current_sparsity_ratio > self.target_sparsity_ratio:
            return

        self.criterion.on_step_begin()
        current_target_sparsity_ratio = self.scheduler.update_sparsity_ratio(self.target_sparsity_ratio,
                                                                             self.completed_pruned_cnt,
                                                                             self.total_prune_cnt, self.masks,
                                                                             self.init_sparsity_ratio)
        logger.info(f"current target ratio is {current_target_sparsity_ratio}")

        self.completed_pruned_cnt += 1
        if self.criterion.scores == {}:
            return
        self.masks = self.pattern.get_masks(self.criterion.scores, current_target_sparsity_ratio, self.masks)
        self.mask_weights()

        self.current_sparsity_ratio = self.pattern.get_sparsity_ratio(self.masks)
        logger.info(f"current sparsity ratio is {self.current_sparsity_ratio}")

    def on_before_optimizer_step(self):
        """Implement before optimizer.step()."""
        self.reg.on_before_optimizer_step()
        self.criterion.on_before_optimizer_step()

    def on_after_optimizer_step(self):
        """Prune the model after optimization."""
        ##the order of the following three lines can't not be exchanged
        if self.global_step >= self.start_step and self.global_step <= self.end_step:
            self.reg.on_after_optimizer_step()
        self.mask_weights()

        self.global_step += 1


@register_pruner('pattern_lock')
class PatternLockPruner(BasePruner):
    """Pruning Pruner.

    A Pruner class derived from BasePruner.
    In this pruner, original model's sparsity pattern will be fixed while training.
    This pruner is useful when a user trains a sparse model without changing its original structure.

    Args:
        modules: A dict {"module_name": Tensor} that stores the pruning modules' weights.
        config: A config dict object that contains the pruner information.

    Attributes:
        Inherit from parent class Pruner.
    """

    def __init__(self, config, modules):
        """Initialize."""
        super(PatternLockPruner, self).__init__(config, modules)
        self.pattern = get_pattern(self.config, modules)
        assert self.config.end_step == self.config.start_step, "pattern_lock pruner only supports one shot mode"

    def update_masks(self, local_step):
        """Update the masks at a given local step."""
        if not self.check_is_pruned_step(self.global_step):
            return
        self.masks = self.pattern.get_pattern_lock_masks(self.modules)

    def on_after_optimizer_step(self):
        """Implement after optimizer.step().

        Prune the model after optimization.
        """
        self.mask_weights()
        self.global_step += 1


@register_pruner('block_mask')
class BlockMaskPruner(BasePruner):
    """Pruning Pruner.

    The class which executes pruning process.
    1. Defines pruning functions called at step begin/end, before/after optimize and epoch begin/end.
    2. Defines the pruning criterion.
    3. Obtain block masks and its grads.

    Args:
        modules: A dict {"module_name": Tensor} that stores the pruning modules' weights.
        config: A config dict object that contains the pruner information.

    Attributes:
        pattern: A Pattern object that defines pruning weights' arrangements within space.
        criterion: A Criterion Object that defines which weights are to be pruned
        scheduler: A Scheduler object that defines how the model's sparsity changes as training/pruning proceeds.
        reg: A Reg object that defines regulization terms.
    """
    def __init__(self, config, modules):
        """Initialize."""
        super(BlockMaskPruner, self).__init__(config, modules)
        
    def _init(self):
        """Initialize."""
        self.pattern = get_pattern(self.config, self.modules)
        self.masks = self.pattern.get_block_masks(self.modules)
        self.rewrite_forward()
        self.scheduler = get_scheduler(self.config)
        self.criterion = get_criterion(self.config, self.modules)
        self.reg = get_reg(self.config, self.modules, self.pattern)
        
        if "channel" not in self.pattern.pattern:
            logger.info("Enabling channel-wise pattern would be a better choice.")
    
    # def on_step_begin(self, local_step):
    #     """Implement at the start of each step.
    
    #     Update the masks at a given local_step.
    #     """
    #     self.update_masks(local_step)
        
    def update_masks(self, local_step):
        """Update the masks at a given local step."""
        if self.global_step == self.start_step:
            if self.config['lock_init_sparsity']:
                self.init_sparsity_ratio = self.pattern.get_sparsity_ratio(self.masks)
                self.current_sparsity_ratio = self.init_sparsity_ratio

        if not self.check_is_pruned_step(self.global_step):
            return

        if self.current_sparsity_ratio > self.target_sparsity_ratio:
            return

        self.criterion.on_step_begin()
        current_target_sparsity_ratio = self.scheduler.update_sparsity_ratio(self.target_sparsity_ratio,
                                                                             self.completed_pruned_cnt,
                                                                             self.total_prune_cnt, self.masks,
                                                                             self.init_sparsity_ratio)
        logger.info(f"current target ratio is {current_target_sparsity_ratio}")

        self.completed_pruned_cnt += 1
        if self.criterion.scores == {}:
            return
        self.masks = self.pattern.get_masks(self.criterion.scores, current_target_sparsity_ratio, self.masks)
        self.update_block_masks(self.masks)
        self.mask_weights()

        self.current_sparsity_ratio = self.pattern.get_sparsity_ratio(self.masks)
        logger.info(f"current sparsity ratio is {self.current_sparsity_ratio}")
        
        if (self.end_step-self.global_step) / self.pruning_frequency < 1:
            self.recover_forward()
        
    def on_before_optimizer_step(self):
        """Implement before optimizer.step()."""
        self.reg.on_before_optimizer_step()
        self.criterion.on_before_optimizer_step()
    
    def on_after_optimizer_step(self):
        """Prune the model after optimization."""
        ##the order of the following four lines can't not be exchanged
        if self.global_step >= self.start_step and self.global_step <= self.end_step:
            self.reg.on_after_optimizer_step()
        self.zero_mask_grad()
        self.mask_weights()
        self.global_step += 1
                
    def mask_weights(self):
        """Apply block masks to corresponding modules' weights.

        Weights are multipled with masks. This is the formal pruning process.
        """
        with torch.no_grad():
            self.pattern.mask_block_weights()
        
    def update_block_masks(self, masks):
        """Update the block mask parameters."""
        with torch.no_grad():
            for key in self.masks.keys():
                module = self.modules[key]
                module.block_mask.data = masks[key].data
                
    def zero_mask_grad(self):
        with torch.no_grad():
            for key in self.modules.keys():
                if not hasattr(self.modules[key], 'block_mask'):
                    continue # No corresponding block mask, skip.
                mask = self.modules[key].block_mask
                if mask.grad is not None:
                    if mask.grad.grad_fn is not None:
                        mask.grad.detach_()
                    else:
                        mask.grad.requires_grad_(False)
                    mask.grad.zero_()
        
    
@register_pruner('retrain_free')
class RetrainFreePruner(BasePruner):
    """Pruning Pruner.
    The retrain_free pruner_class is derived from BasePruner.
    This pruner references the mask search and mask rearrangement strategies in fast retraining free.
    RetrainFreePruner supports one-shot pruning (same effect as fast retraining free) and iterative pruning.
    Please refer to A Fast Post-Training Pruning Framework for Transformers
        (https://arxiv.org/abs/2204.09656)
        
    1. Defines pruning functions called at step begin/end, before/after optimize and epoch begin/end.
    2. Defines the pruning criterion and fixed weight parameters.
    3. Obtain block masks and its grads.
    4. Rearrange block masks.

    Args:
        modules: A dict {"module_name": Tensor} that stores the pruning modules' weights.
        config: A config dict object that contains the pruner information.

    Attributes:
        pattern: A Pattern object that defines pruning weights' arrangements within space.
        criterion: A Criterion Object that defines which weights are to be pruned
        scheduler: A Scheduler object that defines how the model's sparsity changes as training/pruning proceeds.
        reg: A Reg object that defines regulization terms.
    """
    def __init__(self, config, modules):
        """Initialize."""
        super(RetrainFreePruner, self).__init__(config, modules)
        
    def _init(self):
        """Initialize."""
        self.pattern = get_pattern(self.config, self.modules)
        self.masks = self.pattern.get_block_masks(self.modules)
        self.rewrite_forward()
        self.scheduler = get_scheduler(self.config)
        self.criterion = get_criterion(self.config, self.modules)
        self.reg = get_reg(self.config, self.modules, self.pattern)
        
        logger.warning("Retrain-free pruner fixed the weights, please DO NOT turn on gradient update.")
        assert "channel" in self.pattern.pattern, \
                "retrain-free pruner only supports large patterns like channel-wise pruning."
        
    # def on_step_begin(self, local_step):
    #     """Implement at the start of each step.
    
    #     Update the masks at a given local_step.
    #     """
    #     self.update_masks(local_step)
        
    def update_masks(self, local_step):
        """Update the masks at a given local step."""
        if self.global_step == self.start_step:
            if self.config['lock_init_sparsity']:
                self.init_sparsity_ratio = self.pattern.get_sparsity_ratio(self.masks)
                self.current_sparsity_ratio = self.init_sparsity_ratio

        if not self.check_is_pruned_step(self.global_step):
            return

        if self.current_sparsity_ratio > self.target_sparsity_ratio:
            return

        self.criterion.on_step_begin()
        current_target_sparsity_ratio = self.scheduler.update_sparsity_ratio(self.target_sparsity_ratio,
                                                                             self.completed_pruned_cnt,
                                                                             self.total_prune_cnt, self.masks,
                                                                             self.init_sparsity_ratio)
        logger.info(f"current target ratio is {current_target_sparsity_ratio}")

        self.completed_pruned_cnt += 1
        if self.criterion.scores == {}:
            return
        self.masks = self.pattern.get_masks(self.criterion.scores, current_target_sparsity_ratio, self.masks)
        self.rearrange_masks(self.masks)
        self.update_block_masks(self.masks)
        # support iterative rearrangement
        if (self.end_step-self.global_step) / self.pruning_frequency < 1:
            self.mask_weights()
            logger.info(f"mask weights at end_step: {self.global_step}")
            self.recover_forward()

        self.current_sparsity_ratio = self.pattern.get_sparsity_ratio(self.masks)
        logger.info(f"current sparsity ratio is {self.current_sparsity_ratio}")
        
    def on_before_optimizer_step(self):
        """Implement before optimizer.step()."""
        self.reg.on_before_optimizer_step()
        self.criterion.on_before_optimizer_step()
    
    def on_after_optimizer_step(self):
        """Prune the model after optimization."""
        ##the order of the following four lines can't not be exchanged
        if self.global_step >= self.start_step and self.global_step <= self.end_step:
            self.reg.on_after_optimizer_step()
        self.zero_mask_grad()
        # self.mask_weights() #done on update_masks
        self.global_step += 1
                
    def mask_weights(self):
        """Apply block masks to corresponding modules' weights.

        Weights are multipled with masks. This is the formal pruning process.
        """
        with torch.no_grad():
            self.pattern.mask_block_weights()
        
    def update_block_masks(self, masks):
        """Update the block mask parameters."""
        with torch.no_grad():
            for key in self.masks.keys():
                module = self.modules[key]
                module.block_mask.data = masks[key].data
            
    def rearrange_masks(self, masks):
        """Rearrange the masks of each layer with constant sparsity."""
        with torch.no_grad():
            new_masks = {}
            for key in masks.keys():
                block_mask = masks[key]
                num_pruned = torch.sum(block_mask == 0.0).data.item()
                grads = torch.stack(self.criterion.collected_grads[key], dim=0).squeeze()
                if not num_pruned:
                    new_masks[key] = block_mask
                    continue
                grads = grads.permute(1, 0).contiguous()
                grads_sq = grads.pow(2).sum(dim=1)
                _, indicies = grads_sq.sort(descending=False)
                indicies = indicies.tolist()
                masked_indicies = indicies[:num_pruned]
                for index in indicies[num_pruned:]:
                    masked_indicies.append(index)
                    grad_vectors = grads[masked_indicies]
                    grad_sum = grad_vectors.sum(dim=0)
                    complement = grad_sum - grad_vectors
                    grad_sum_length = complement.pow(2).sum(dim=1)
                    removed = grad_sum_length.argmin()
                    del masked_indicies[removed]

                new_masks[key] = torch.ones(len(indicies)).to(block_mask.device)
                new_masks[key][masked_indicies] = 0
                new_masks[key] = new_masks[key] * torch.ones_like(block_mask).to(block_mask.device)
            self.masks = new_masks
            
    def zero_mask_grad(self):
        with torch.no_grad():
            for key in self.modules.keys():
                if not hasattr(self.modules[key], 'block_mask'):
                    continue # No corresponding block mask, skip.
                mask = self.modules[key].block_mask
                if mask.grad is not None:
                    if mask.grad.grad_fn is not None:
                        mask.grad.detach_()
                    else:
                        mask.grad.requires_grad_(False)
                    mask.grad.zero_()
    

@register_pruner('progressive')
class ProgressivePruner(BasicPruner):
    """Pruning Pruner.

    A Pruner class derived from BasePruner. In this pruner, mask interpolation will be applied.
    Mask interpolation is a fine-grained improvement for NxM structured pruning by adding interval
        masks between masks of two pruning steps.

    Args:
        modules: A dict {"module_name": Tensor} that stores the pruning modules' weights.
        config: A config dict object that contains the pruner information.

    Attributes:
        Inherit from parent class Pruner.
    """

    def __init__(self, config, modules):
        """Initialize."""
        super(ProgressivePruner, self).__init__(config, modules)

    def _init(self):
        """Auxiliary function for initialization."""
        self.pattern = get_pattern(self.config, self.modules)
        self.scheduler = get_scheduler(self.config)
        self.criterion = get_criterion(self.config, self.modules)
        self.reg = get_reg(self.config, self.modules, self.pattern)
        # progressive pruning set up, including check up paramters.
        self.use_progressive = self.config["progressive"]
        # progressive parameters
        # dict passed to Pattern's functions
        self.progressive_configs = {
            "progressive_steps": 4,
            "progressive_type": "scores",
            "use_global": True
        }
        self.progressive_steps = self.progressive_configs["progressive_steps"]
        self.progressive_type = self.progressive_configs["progressive_type"]
        self.use_global = self.progressive_configs["use_global"]
        self.progressive_logger = False
        self._init_for_progressive()

    def _init_for_progressive(self):
        """Auxiliary function for initializing progressive pruning."""
        # detailed progressive parameters will stored at patterns.py
        # step 1: check if pattern is NxM
        if "x" not in self.pattern.pattern:
            raise NotImplementedError(f"Currently progressive only " \
                                      f"support NxM and per-channel pruning patterns.")

        # step 2: check if current set up will "degrade" into non-progressive
        degrading_flag = False
        if (self.end_step - self.start_step) <= self.progressive_steps or self.progressive_steps <= 1:
            logger.info("Current progressive setting will degrading to non-progressive pruning.")
            self.use_progressive = False
            return

        # step 3: log hyper-parameters. and check validity.
        if self.use_progressive:
            logger.info(f"Progressive pruning is enabled!")
            logger.info(f"Progressive pruning steps: {self.progressive_steps}")
            logger.info(f"Progressive type: {self.progressive_type}")
            logger.info(f"Progressive balance: {self.use_global}")
            self.check_progressive_validity()
            self.pre_masks = copy.deepcopy(self.masks)
            self.progressive_masks = copy.deepcopy(self.masks)
            if self.pruning_frequency < self.progressive_steps:  ##TODO trick
                self.progressive_steps = self.pruning_frequency
                # if self.progressive_steps == 3:
                #     self.progressive_steps = 2
                self.pruning_frequency_progressive = self.progressive_steps
            else:
                self.pruning_frequency_progressive = self.pruning_frequency // self.progressive_steps
            # this is a structural pruning step, it fits self.pruning_frequency
            self.structured_update_step = 0

    def check_progressive_validity(self):
        """Check if the settings of progressive pruning are valid."""
        # check some problematic settings
        if self.progressive_type == "linear":
            if self.use_global:
                # when global progressive is applied, linear type is contradict.
                raise NotImplementedError("Global progressive pruning do not support linear pattern")
            # When linear, progressive_step should not meet a indivisible
            for key in self.pattern.block_size.keys():
                block_size = self.pattern.block_size[key]
                progressive_direction = max(block_size)
                if progressive_direction % self.progressive_steps != 0:
                    raise ValueError(
                        f"In layer {key}, its pruning pattern is {block_size}, " \
                        f"while progressive steps {self.progressive_steps} is indivisible.")
        else:
            for key in self.pattern.block_size.keys():
                block_size = self.pattern.block_size[key]
                total_block_size = block_size[0] * block_size[1]
                if total_block_size < self.progressive_steps:
                    raise ValueError(
                        f"In layer {key}, its pruning pattern is {block_size}, " \
                        f"while progressive steps {self.progressive_steps} is overflowing.")

    def check_is_pruned_progressive_step(self, step):
        """Check if a progressive pruning process should be performed at the current step.

        Args:
            step: an integer representing the number of current step.

        Returns:
            A Boolean.
        """
        # used in progressive pruning
        if step < self.start_step or step > self.end_step:
            return False
        if int(step - self.start_step) % self.pruning_frequency_progressive == 0:
            return True
        return False

    def update_masks_progressive(self, local_step):
        """Update the masks in progressive pruning mode at a given local step."""
        if self.global_step == self.start_step:
            if self.config['lock_init_sparsity']:
                self.masks = self.pattern.get_pattern_lock_masks(self.modules)
                self.init_sparsity_ratio = self.pattern.get_sparsity_ratio(self.masks)
                self.current_sparsity_ratio = self.init_sparsity_ratio

        # case 1: step is not in [start_step, end_step] or it is not either pruning or progressive pruning step.
        if (self.check_is_pruned_step(self.global_step) == False) and (
                self.check_is_pruned_progressive_step(self.global_step) == False):
            return
        if self.current_sparsity_ratio > self.target_sparsity_ratio:
            return

        # case 2: step which does progressive update, but it is not a pruning step in case 3
        if self.check_is_pruned_progressive_step(self.global_step) \
                and self.check_is_pruned_step(self.global_step) == False:
            # do not do global pruning, only do the progressive mask update.
            step_offset = self.global_step - self.structured_update_step
            progressive_idx = step_offset // self.pruning_frequency_progressive
            if progressive_idx < (self.progressive_steps - 1):
                self.progressive_masks = self.pattern.update_progressive_masks(self.pre_masks, self.masks, \
                                                                               self.criterion.scores, \
                                                                               progressive_idx + 1, \
                                                                               self.progressive_configs)
            else:
                # in the end, directly use new masks.
                for n in self.masks.keys():
                    self.progressive_masks[n] = self.masks[n].clone()
            self.mask_weights_general(self.progressive_masks)
            if self.progressive_logger:
                self.print_progressive_sparsity()
            return

        # case 3: a pruning step, generate new masks, progressive masks also update.
        tmp_step = self.global_step
        self.structured_update_step = tmp_step
        current_target_sparsity_ratio = self.scheduler.update_sparsity_ratio(self.target_sparsity_ratio,
                                                                             self.completed_pruned_cnt,
                                                                             self.total_prune_cnt, self.masks)
        logger.info(f"current target ratio is {current_target_sparsity_ratio}")
        self.criterion.on_step_begin()
        self.completed_pruned_cnt += 1
        if self.criterion.scores == {}:
            return
        for n in self.masks.keys():
            self.pre_masks[n] = self.masks[n].clone()
        # update new masks
        self.masks = self.pattern.get_masks(self.criterion.scores, current_target_sparsity_ratio, self.masks, )
        self.progressive_masks = self.pattern.update_progressive_masks(self.pre_masks, self.masks, \
                                                                       self.criterion.scores, 1, \
                                                                       self.progressive_configs)
        self.mask_weights_general(self.progressive_masks)
        if self.progressive_logger:
            self.print_progressive_sparsity()
        return

    def on_step_begin(self, local_step):
        """Update the masks at a given local_step.

        Implement at the start of each step.
        """
        if self.handled_global_step == self.global_step:
            return

        if not self.use_progressive:
            # As _init_for_progressive() works, when degrades to non-progressive
            # just call BasicPruner's update_masks().
            self.update_masks(local_step)
        else:
            self.update_masks_progressive(local_step)
        self.handled_global_step = self.global_step

    def on_before_optimizer_step(self):
        """Implement before optimizer.step()."""
        self.reg.on_before_optimizer_step()
        self.criterion.on_before_optimizer_step()

    def on_after_optimizer_step(self):
        """Prune the model after optimization."""
        ##the order of the following three lines can't not be exchanged
        if self.global_step >= self.start_step and self.global_step <= self.end_step:
            self.reg.on_after_optimizer_step()
        if not self.use_progressive:
            self.mask_weights()
        else:
            self.mask_weights_general(self.progressive_masks)

        self.global_step += 1

    def print_progressive_sparsity(self):
        """Output the progressive sparsity."""
        cur_sp = self.pattern.get_sparsity_ratio_progressive(self.progressive_masks)
        logger.info("Step: {} -> Current progressive sparsity: {}".format(self.global_step, cur_sp))


