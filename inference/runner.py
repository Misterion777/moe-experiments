from collections import defaultdict
from typing import Any, Dict, List, Literal, Optional

import torch
from inference.hooks import set_router_hook
from inference.utils import set_openmoe_args
from models.modelling_openmoe import OpenMoeForCausalLM
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    MixtralForCausalLM,
    PreTrainedTokenizer,
    PreTrainedModel,
)
from transformers.modeling_outputs import MoeCausalLMOutputWithPast
import torch.nn.functional as F

# Dictionary where keys are layer names and values are batched selected experts
LayerSelectedExperts = Dict[str, List[torch.LongTensor]]


class MoERunner:
    """
    Wrapper class for outputing routed experts during MoE inference
    """

    activated_experts = defaultdict(list)
    tokenizer: PreTrainedTokenizer
    model: PreTrainedModel

    def __call__(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        *args: Any,
        **kwds: Any,
    ) -> LayerSelectedExperts:
        raise NotImplementedError()

    @classmethod
    def from_name(
        cls, name: Literal["openmoe", "mixtral"], seq_len: int, **kwargs
    ) -> "MoERunner":
        if name == "openmoe":
            return OpenMoERunner(seq_len, **kwargs)
        if name == "mixtral":
            return MixtralRunner(seq_len, **kwargs)
        raise NotImplementedError()


class OpenMoERunner(MoERunner):
    """
    Wrapper class for outputing routed experts during Mixtral inference
    """

    def __init__(self, seq_len: int, **kwargs) -> None:
        self.model_name = f"OrionZheng/openmoe-8b-1T"
        self.tokenizer = AutoTokenizer.from_pretrained(
            "google/umt5-small", model_max_length=seq_len
        )
        config = AutoConfig.from_pretrained(self.model_name)
        set_openmoe_args(
            config,
            num_experts=config.num_experts,
            moe_layer_interval=config.moe_layer_interval,
            enable_kernel=False,
        )
        self.model = OpenMoeForCausalLM.from_pretrained(
            self.model_name,
            config=config,
            device_map="auto",
            torch_dtype=torch.float16,
        )
        self.activated_experts, hooks = set_router_hook(self.model)

    @torch.no_grad()
    def __call__(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        **kwargs: Any,
    ) -> LayerSelectedExperts:
        if torch.cuda.is_available():
            input_ids = input_ids.cuda()
            attention_mask = attention_mask.cuda()
        self.model(
            input_ids=input_ids, attention_mask=attention_mask, **kwargs
        )
        return self.activated_experts


class MixtralRunner(MoERunner):
    """
    Wrapper class for outputing routed experts during Mixtral inference
    """

    def __init__(self, seq_len: int, use_quant=True, **kwargs) -> None:
        if use_quant:
            self.model_name = "TheBloke/mixtral-8x7b-v0.1-AWQ"
        else:
            self.model_name = "mistralai/Mixtral-8x7B-v0.1"
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_name, model_max_length=seq_len
        )
        self.tokenizer.pad_token = self.tokenizer.eos_token

        self.model = MixtralForCausalLM.from_pretrained(
            self.model_name,
            device_map="auto",
            torch_dtype=torch.float16,
            resume_download=True,
        )

    # @torch.no_grad()
    def __call__(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        **kwargs: Any,
    ) -> LayerSelectedExperts:
        if torch.cuda.is_available():
            input_ids = input_ids.cuda()
            attention_mask = attention_mask.cuda()
        outputs: MoeCausalLMOutputWithPast = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            output_router_logits=True,
            **kwargs,
        )
        for i, layer_router in enumerate(outputs.router_logits):
            routing_weights = F.softmax(layer_router, dim=1, dtype=torch.float)
            _, selected_experts = torch.topk(
                routing_weights, self.model.config.num_experts_per_tok, dim=-1
            )
            self.activated_experts[f"layer_{i}.router"].append(
                selected_experts
            )
        return self.activated_experts