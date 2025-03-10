from time import time

import ray
import torch
import torch.nn.functional as F

from trlx.data.accelerate_base_datatypes import PromptBatch
from trlx.data.ppo_types import PPORLElement
from trlx.orchestrator import Orchestrator, register_orchestrator
from trlx.pipeline import BasePipeline
from trlx.trainer import BaseRLTrainer
from trlx.utils import Clock
from trlx.utils.modeling import RunningMoments, logprobs_from_logits


@register_orchestrator
class PPOOrchestrator(Orchestrator):
    """
    Orchestrator prepares data for PPO training.
    Transforms samples from `pipeline` into `PPOBatch` and pushes them into trainer's `store`
    """

    def __init__(
        self,
        trainer: BaseRLTrainer,
        pipeline: BasePipeline,
        chunk_size: int = 512,
    ):
        self.pipeline = pipeline
        self.trainer = trainer
        self.chunk_size = chunk_size

        self.pipeline_loader = self.pipeline.create_loader(
            self.chunk_size, shuffle=True
        )
        self.pipeline_loader = self.trainer.accelerator.prepare(self.pipeline_loader)
        self.pipeline_iterator = iter(self.pipeline_loader)

        if not hasattr(self.trainer.model, "frozen_head"):
            self.ref_model = self.trainer.get_arch(self.trainer.config)
            self.ref_model.to(self.trainer.accelerator.device)

        self.trainer.orch = self

        self.running = RunningMoments()
        self.ref_mean = self.trainer.config.method.ref_mean
        self.ref_std = self.trainer.config.method.ref_std

    def score(self, samples):
        """
        Batched scoring function taking text and generating scalar
        """
        return self.trainer.reward_fn(samples)

    def make_experience(self, num_rollouts: int = 1024, iter_count: int = 0):  # noqa:
        """
        Takes `num_rollouts` prompts from `pipeline`, samples model and computes the
        KL againts a reference model. It then appends PPOElements to trainer's `store`
        """
        ppo_rl_elements = []
        stats = {}
        clock = Clock()
        while len(ppo_rl_elements) < num_rollouts:
            # Get next batch in prompt dataset and refresh if exhausted
            try:
                batch: PromptBatch = next(self.pipeline_iterator)
            except StopIteration:
                self.pipeline_iterator = iter(self.pipeline_loader)
                batch = next(self.pipeline_iterator)

            exp_generate_time = time()
            samples = self.trainer.generate(**batch)
            stats["time/exp_generate"] = time() - exp_generate_time

            query_tensors = batch.input_ids
            device = samples.device
            str_samples, str_prompts, str_outputs = self.trainer.decode(
                query_tensors, samples
            )

            # Convert trimmed samples back into tensors for another head pass
            # This can be defered, instead letting the pass to made over the original samples
            # after unbinding and truncating operations lower are fixed
            outputs = self.trainer.tokenizer(str_outputs).input_ids
            outputs = list(map(torch.LongTensor, outputs))
            maxsize = max(map(len, outputs))
            outputs = [
                F.pad(
                    output,
                    (0, maxsize - len(output)),
                    value=self.trainer.tokenizer.pad_token_id,
                )
                for output in outputs
            ]
            response_tensors = torch.vstack(outputs).to(device)

            exp_score_time = time()

            scores = torch.tensor(
                self.trainer.reward_fn(
                    samples=str_samples,
                    prompts=str_prompts,
                    outputs=str_outputs,
                ),
                dtype=float,
            ).to(device)
            stats["time/exp_score"] = time() - exp_score_time

            # store statistics of the initial rollout as reference
            if self.ref_mean is None:
                self.ref_mean, self.ref_std = scores.mean(), scores.std()
            all_scores_mean, all_scores_std = self.running.update(scores)
            stats["exp_scores/mean"] = all_scores_mean
            stats["exp_scores/std"] = all_scores_std
            stats["exp_scores/running_mean"] = self.running.mean
            stats["exp_scores/running_std"] = self.running.std

            if self.trainer.config.method.scale_reward == "running":
                scores /= self.running.std
            elif self.trainer.config.method.scale_reward == "ref":
                scores /= self.ref_std

            clip_reward = self.trainer.config.method.cliprange_reward
            if clip_reward:
                scores = torch.clip(scores, -clip_reward, clip_reward)

            # Precompute logprobs, values
            if self.trainer.config.model.model_arch_type == "seq2seq":
                attention_mask = batch.attention_mask.to(device)
                query_tensors = batch.input_ids.to(device)
                with torch.no_grad():
                    outputs = self.trainer.model(
                        input_ids=query_tensors,
                        attention_mask=attention_mask,
                        decoder_input_ids=response_tensors,
                    )
                    logits = outputs.logits
                    values = outputs.value
                    if hasattr(self.trainer.model, "frozen_head"):
                        ref_logits = self.trainer.model.forward_hydra(
                            input_ids=query_tensors,
                            attention_mask=attention_mask,
                            decoder_input_ids=response_tensors,
                        )
                    else:
                        ref_logits = self.ref_model(
                            input_ids=query_tensors,
                            attention_mask=attention_mask,
                            decoder_input_ids=response_tensors,
                        ).logits
            else:
                all_tokens = torch.cat(
                    (query_tensors.to(device), response_tensors), dim=1
                )
                attention_mask = (
                    all_tokens.not_equal(self.trainer.tokenizer.pad_token_id)
                    .long()
                    .to(device)
                )
                with torch.no_grad():
                    logits, *_, values = self.trainer.model(
                        all_tokens,
                        attention_mask=attention_mask,
                    )
                    # TODO(dahoas): When hydra model works need to also support generation on hydra head
                    if hasattr(self.trainer.model, "frozen_head"):
                        ref_logits = self.trainer.model.forward_hydra(
                            all_tokens,
                            attention_mask=attention_mask,
                            return_dict=False,
                        )
                    else:
                        ref_logits, _, *_ = self.ref_model(
                            all_tokens,
                            attention_mask=attention_mask,
                            return_dict=False,
                        )
                        ref_logits = ref_logits.to(device)

            if self.trainer.config.model.model_arch_type == "seq2seq":
                logprobs = logprobs_from_logits(
                    logits[:, :-1, :], response_tensors[:, 1:]
                )
                ref_logprobs = logprobs_from_logits(
                    ref_logits[:, :-1, :], response_tensors[:, 1:]
                )
                ref_logprobs_vocab = torch.log_softmax(ref_logits[:, :-1, :], dim=-1)
            else:
                logprobs = logprobs_from_logits(logits, all_tokens)
                ref_logprobs = logprobs_from_logits(ref_logits, all_tokens)

            n = samples.shape[0]
            logprobs = logprobs.cpu()
            ref_logprobs = ref_logprobs.cpu()
            query_tensors = query_tensors.cpu()
            response_tensors = response_tensors.cpu()

            if self.trainer.config.model.model_arch_type == "seq2seq":
                start = 1  # skip the <s> token
                ends = (response_tensors[:, start:] != 0).sum(1)

                # Calculate the KL Divergence
                logprobs_all_vocab = F.log_softmax(
                    logits[:, start:-1, :], dim=-1
                )  # [sample x tokens x vocab]
                ref_logprobs_all_vocab = F.log_softmax(
                    ref_logits[:, start - 1 : -2, :], dim=-1
                )  # [sample x tokens x vocab]
                kl_divergence = -torch.sum(
                    torch.exp(logprobs_all_vocab)
                    * (ref_logprobs_all_vocab - logprobs_all_vocab),
                    dim=-1,
                )
                kl_score = -self.trainer.kl_ctl.value * kl_divergence

                rewards = [torch.zeros(ends[ix]-start-1) for ix in range(n)]
                if self.trainer.config.method.kl_mode == "reward":
                    rewards = [
                        rs[start : ends[ix]] for ix, rs in enumerate(kl_score)
                    ]  # [sample x tokens]

                # Save ref_logprobs_vocab for
                ref_logprobs_vocab = [
                    ref_logprobs_all_vocab[ix, start:ends[ix], :] for ix in range(n)
                ]

            else:
                logprobs = logprobs_from_logits(logits[:, :-1, :], all_tokens[:, 1:])

                n = samples.shape[0]
                values = values.cpu()[:, :-1]
                logprobs = logprobs.cpu()
                query_tensors = query_tensors.cpu()
                response_tensors = response_tensors.cpu()

                start = query_tensors.shape[1] - 1
                ends = start + attention_mask[:, start:].sum(1)
                all_values = [values[ix, start : ends[ix]] for ix in range(n)]
                all_logprobs = [logprobs[ix, start : ends[ix]] for ix in range(n)]

                # Calculate the KL Divergence
                logprobs_all_vocab = F.log_softmax(
                    logits[:, :-1, :], dim=-1
                )  # [sample x tokens x vocab]
                ref_logprobs_all_vocab = F.log_softmax(
                    ref_logits[:, :-1, :], dim=-1
                )  # [sample x tokens x vocab]
                kl_divergence = -torch.sum(
                    torch.exp(logprobs_all_vocab)
                    * (ref_logprobs_all_vocab - logprobs_all_vocab),
                    dim=-1,
                )
                kl_score = -self.trainer.kl_ctl.value * kl_divergence

                rewards = [torch.zeros_like(v) for v in all_values]
                if self.trainer.config.method.kl_mode == "reward":
                    rewards = [
                        rs[start : ends[ix]] for ix, rs in enumerate(kl_score)
                    ]  # [sample x tokens]

                # Save for potential use in loss
                ref_logprobs_vocab = [
                    ref_logprobs_all_vocab[ix, start:ends[ix], :] for ix in range(n)
                ]

            # Compute rewards
            all_rewards = [None] * n

            for ix in range(n):
                rs = rewards[ix]
                if len(rs) == 0:
                    rs = torch.tensor([0.0])
                rs[-1] += scores[ix].cpu()
                all_rewards[ix] = rs

            new_ppo_rl_elements = [
                PPORLElement(
                    query_tensor=query_tensors[i],
                    response_tensor=response_tensors[i],
                    logprobs=all_logprobs[i],
                    ref_logprobs_vocab=ref_logprobs_vocab[i],
                    values=all_values[i],
                    rewards=all_rewards[i],
                )
                for i in range(n)
            ]
            ppo_rl_elements += new_ppo_rl_elements
            exp_time = clock.tick()

        stats["kl_ctl_value"] = self.trainer.kl_ctl.value
        stats["time/exp"] = exp_time

        if not ray.is_initialized():
            self.trainer.accelerator.log(stats, step=iter_count)

        # Push samples and rewards to trainer's rollout storage
        self.trainer.push_to_store(ppo_rl_elements)
