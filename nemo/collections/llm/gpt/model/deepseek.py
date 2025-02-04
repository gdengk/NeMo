# Copyright (c) 2025, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from dataclasses import dataclass, field
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional, Union

import torch
import torch.nn.functional as F
from megatron.core.models.gpt.gpt_layer_specs import get_gpt_decoder_block_spec
from megatron.core.transformer.transformer_config import MLATransformerConfig
from torch import nn

from nemo.collections.llm.gpt.model.base import HAVE_TE, GPTConfig, GPTModel
from nemo.lightning import io, teardown
from nemo.lightning.io.state import TransformFns, _ModelState
from nemo.lightning.pytorch.optim import OptimizerModule
from nemo.lightning.pytorch.utils import dtype_from_hf
from nemo.utils import logging

if TYPE_CHECKING:
    from megatron.core.transformer import ModuleSpec

    from nemo.collections.common.tokenizers.huggingface.auto_tokenizer import AutoTokenizer
    from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec


@dataclass
class DeepSeekConfig(MLATransformerConfig, GPTConfig):
    """
    Base config for DeepSeek V2 and V3 models.
    """

    transformer_layer_spec: Union['ModuleSpec', Callable[["GPTConfig"], 'ModuleSpec']] = partial(
        get_gpt_decoder_block_spec, use_transformer_engine=HAVE_TE
    )

    # Model
    normalization: str = "RMSNorm"
    activation_func: Callable = F.silu
    gated_linear_unit: bool = True  # swiglu
    position_embedding_type: str = "rope"
    add_bias_linear: bool = False
    share_embeddings_and_output_weights: bool = False
    num_attention_heads: int = 128
    kv_channels: int = 128
    max_position_embeddings: int = 4096
    seq_length: int = 4096
    rotary_base: float = 10000.0
    make_vocab_size_divisible_by: int = 3200

    # Regularization
    attention_dropout: float = 0.0
    hidden_dropout: float = 0.0
    qk_layernorm: bool = True

    # MoE
    moe_grouped_gemm: bool = True
    moe_router_pre_softmax: bool = True
    moe_token_dispatcher_type: str = "alltoall"
    moe_router_load_balancing_type: str = 'seq_aux_loss'
    moe_shared_expert_overlap: bool = True

    # MLA
    q_lora_rank: int = 1536
    kv_lora_rank: int = 512
    qk_head_dim: int = 128
    qk_pos_emb_head_dim: int = 64
    v_head_dim: int = 128
    rotary_scaling_factor: float = 40
    mscale: float = 1.0
    mscale_all_dim: float = 1.0

    # Miscellaneous
    init_method_std: float = 0.006
    layernorm_epsilon: float = 1e-6
    bf16: bool = True
    params_dtype: torch.dtype = torch.bfloat16
    async_tensor_model_parallel_allreduce = True
    attention_softmax_in_fp32 = False
    persist_layer_norm = True

    # fusions
    apply_rope_fusion = False
    bias_activation_fusion = True
    bias_dropout_fusion = True
    masked_softmax_fusion = True
    gradient_accumulation_fusion = True

    def __post_init__(self):
        if self.moe_router_topk_limited_devices is not None:
            self.moe_router_topk_limited_devices = min(
                self.moe_router_topk_limited_devices, self.expert_model_parallel_size
            )
        super().__post_init__()


@dataclass
class DeepSeekV2Config(DeepSeekConfig):
    """
    DeepSeek-V2 Model: https://github.com/deepseek-ai/DeepSeek-V2
    """

    num_layers: int = 60
    hidden_size: int = 5120
    ffn_hidden_size: int = 12288
    num_moe_experts: int = 160
    moe_ffn_hidden_size: int = 1536
    moe_shared_expert_intermediate_size: int = 3072  # 1536 * 2 shared experts
    moe_layer_freq: Union[int, List[int]] = field(default_factory=lambda: [0] + [1] * 59)  # first layer is dense
    moe_router_topk: int = 6
    moe_router_topk_limited_devices: int = 3
    moe_router_topk_scaling_factor: float = 16.0
    moe_aux_loss_coeff: float = 1e-3
    moe_router_score_function: str = "softmax"


@dataclass
class DeepSeekV3Config(DeepSeekConfig):
    """
    DeepSeek-V3 Model: https://github.com/deepseek-ai/DeepSeek-V3
    """

    num_layers: int = 61
    hidden_size: int = 7168
    ffn_hidden_size: int = 18432
    num_moe_experts: int = 256
    moe_ffn_hidden_size: int = 2048
    moe_shared_expert_intermediate_size: int = 2048  # 2048 * 1 shared expert
    moe_layer_freq: Union[int, List[int]] = field(
        default_factory=lambda: [0] * 3 + [1] * 58
    )  # first three layers are dense
    # moe_layer_freq: Union[int, List[int]] = field(
    #     default_factory=lambda: [0] * 2 + [1] * 2
    # )  # first three layers are dense
    moe_router_topk: int = 8
    moe_router_topk_limited_devices: int = 4
    moe_router_topk_scaling_factor: float = 2.5
    moe_aux_loss_coeff: float = 1e-4
    make_vocab_size_divisible_by: int = 1280
    moe_router_score_function: str = "sigmoid"
    moe_router_enable_expert_bias: bool = True
    moe_router_bias_update_rate: float = 1e-3


class DeepSeekModel(GPTModel):
    # pylint: disable=C0115,C0116
    def __init__(
        self,
        config: Optional[DeepSeekConfig] = None,
        optim: Optional[OptimizerModule] = None,
        tokenizer: Optional["TokenizerSpec"] = None,
        model_transform: Optional[Callable[[nn.Module], nn.Module]] = None,
    ):
        super().__init__(
            config or DeepSeekV2Config(), optim=optim, tokenizer=tokenizer, model_transform=model_transform
        )


@io.model_importer(DeepSeekModel, ext="hf")
class HFDeepSeekImporter(io.ModelConnector["AutoModelForCausalLM", DeepSeekModel]):
    # pylint: disable=C0115,C0116
    def init(self) -> DeepSeekModel:
        return DeepSeekModel(self.config, tokenizer=self.tokenizer)

    def apply(self, output_path: Path) -> Path:
        from transformers import AutoModelForCausalLM

        source = AutoModelForCausalLM.from_pretrained(str(self), trust_remote_code=True, torch_dtype='auto')
        source = self._modify_source_state(source)
        target = self.init()
        trainer = self.nemo_setup(target)
        self.convert_state(source, target)
        self.nemo_save(output_path, trainer)

        logging.info(f"Converted DeepSeek model to Nemo, model saved to {output_path}")

        teardown(trainer, target)
        del trainer, target

        return output_path

    def _modify_source_state(self, source: nn.Module) -> _ModelState:
        """
        In deepseek, HF weight `model.layers.*.post_attention_layernorm.weight` is mapped to mcore weight
        a) `decoder.layers.*.mlp.linear_fc1.layer_norm_weight`, if the layer is dense
        b) `decoder.layers.*.pre_mlp_layernorm.weight`, if the layer is MoE

        We rename model.layers.*.post_attention_layernorm.weight in the first case to prevent a one-to-many mapping
        """

        state_dict = source.state_dict()

        for layer_i, use_moe in enumerate(self.config.moe_layer_freq):
            if use_moe == 0:
                weight = state_dict.pop(f"model.layers.{layer_i}.post_attention_layernorm.weight")
                state_dict[f"model.layers.{layer_i}.dense-post_attention_layernorm.weight"] = weight

        source = _ModelState(state_dict)
        return source

    def convert_state(self, source, target):
        # pylint: disable=C0301
        mapping = {
            ## Embed
            "model.embed_tokens.weight": "embedding.word_embeddings.weight",
            ## Attention
            "model.layers.*.input_layernorm.weight": "decoder.layers.*.input_layernorm.weight",
            "model.layers.*.self_attn.o_proj.weight": "decoder.layers.*.self_attention.linear_proj.weight",
            "model.layers.*.self_attn.q_a_proj.weight": "decoder.layers.*.self_attention.linear_q_down_proj.weight",
            "model.layers.*.self_attn.q_b_proj.weight": "decoder.layers.*.self_attention.linear_q_up_proj.weight",
            "model.layers.*.self_attn.kv_a_proj_with_mqa.weight": "decoder.layers.*.self_attention.linear_kv_down_proj.weight",
            "model.layers.*.self_attn.kv_b_proj.weight": "decoder.layers.*.self_attention.linear_kv_up_proj.weight",
            "model.layers.*.self_attn.q_a_layernorm.weight": "decoder.layers.*.self_attention.q_layernorm.weight",
            "model.layers.*.self_attn.kv_a_layernorm.weight": "decoder.layers.*.self_attention.kv_layernorm.weight",
            "model.layers.*.dense-post_attention_layernorm.weight": "decoder.layers.*.mlp.linear_fc1.layer_norm_weight",
            "model.layers.*.post_attention_layernorm.weight": "decoder.layers.*.pre_mlp_layernorm.weight",
            ## Dense MLP
            # model.layers.*.mlp.{gate|up}_proj.weight: decoder.layers.*.mlp.linear_fc1.weight
            "model.layers.*.mlp.down_proj.weight": "decoder.layers.*.mlp.linear_fc2.weight",
            ## MoE
            "model.layers.*.mlp.gate.weight": "decoder.layers.*.mlp.router.weight",
            # model.layers.*.mlp.experts.*.{gate|up}_proj.weight: decoder.layers.*.mlp.experts.linear_fc1.weight*
            "model.layers.*.mlp.experts.*.down_proj.weight": "decoder.layers.*.mlp.experts.linear_fc2.weight*",
            # model.layers.*.mlp.shared_experts.{gate|up}_proj.weight： decoder.layers.*.mlp.shared_experts.linear_fc1.weight
            "model.layers.*.mlp.shared_experts.down_proj.weight": "decoder.layers.*.mlp.shared_experts.linear_fc2.weight",
            ## LM Head
            "model.norm.weight": "decoder.final_layernorm.weight",
            "lm_head.weight": "output_layer.weight",
        }
        if hasattr(self.config, "moe_router_enable_expert_bias") and self.config.moe_router_enable_expert_bias:
            mapping.update(
                {
                    "model.layers.*.mlp.gate.e_score_correction_bias": "decoder.layers.*.mlp.router.expert_bias",
                }
            )

        transforms = [
            io.state_transform(
                source_key=("model.layers.*.mlp.gate_proj.weight", "model.layers.*.mlp.up_proj.weight"),
                target_key="decoder.layers.*.mlp.linear_fc1.weight",
                fn=TransformFns.merge_fc1,
            ),
            io.state_transform(
                source_key=(
                    "model.layers.*.mlp.experts.*.gate_proj.weight",
                    "model.layers.*.mlp.experts.*.up_proj.weight",
                ),
                target_key="decoder.layers.*.mlp.experts.linear_fc1.weight*",
                fn=TransformFns.merge_fc1,
            ),
            io.state_transform(
                source_key=(
                    "model.layers.*.mlp.shared_experts.gate_proj.weight",
                    "model.layers.*.mlp.shared_experts.up_proj.weight",
                ),
                target_key="decoder.layers.*.mlp.shared_experts.linear_fc1.weight",
                fn=TransformFns.merge_fc1,
            ),
        ]

        return io.apply_transforms(
            source,
            target,
            mapping=mapping,
            transforms=transforms,
        )

    @cached_property
    def tokenizer(self) -> "AutoTokenizer":
        from nemo.collections.common.tokenizers.huggingface.auto_tokenizer import AutoTokenizer

        return AutoTokenizer(self.save_hf_tokenizer_assets(str(self)), use_fast=True)

    @cached_property
    def config(self) -> DeepSeekConfig:
        from transformers import AutoConfig as HFAutoConfig

        source = HFAutoConfig.from_pretrained(str(self), trust_remote_code=True)
        n_moe_layers = source.num_hidden_layers - source.first_k_dense_replace
        is_v3 = source.scoring_func == "sigmoid"
        if is_v3:
            v3_kwargs = {
                "moe_router_score_function": "sigmoid",
                "moe_router_enable_expert_bias": True,
            }
        else:
            v3_kwargs = {}
        return DeepSeekConfig(
            num_layers=source.num_hidden_layers,
            hidden_size=source.hidden_size,
            ffn_hidden_size=source.intermediate_size,
            num_moe_experts=source.n_routed_experts,
            moe_ffn_hidden_size=source.moe_intermediate_size,
            moe_shared_expert_intermediate_size=source.moe_intermediate_size * source.n_shared_experts,
            moe_layer_freq=[0] * source.first_k_dense_replace + [1] * n_moe_layers,
            moe_router_topk=source.num_experts_per_tok,
            moe_router_topk_limited_devices=source.topk_group,
            moe_router_topk_scaling_factor=source.routed_scaling_factor,
            moe_aux_loss_coeff=source.aux_loss_alpha,
            make_vocab_size_divisible_by=1280 if is_v3 else 3200,
            fp16=(dtype_from_hf(source) == torch.float16),
            bf16=(dtype_from_hf(source) == torch.bfloat16),
            params_dtype=dtype_from_hf(source),
            **v3_kwargs,
        )


__all__ = [
    "DeepSeekConfig",
    "DeepSeekV2Config",
    "DeepSeekV3Config",
    "DeepSeekModel",
]
