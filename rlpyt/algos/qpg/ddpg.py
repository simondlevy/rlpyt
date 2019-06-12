
import torch
from collections import namedtuple

from rlpyt.algos.base import RlAlgorithm
from rlpyt.utils.quick_args import save__init__args
from rlpyt.utils.logging import logger
from rlpyt.replays.uniform import UniformReplayBuffer
from rlpyt.utils.collections import namedarraytuple
from rlpyt.utils.tensor import valid_mean

OptInfo = namedtuple("OptInfo",
    ["muLoss", "qLoss", "muGradNorm", "qGradNorm"])
SamplesToReplay = namedarraytuple("SamplesToReplay",
    ["observation", "action", "reward", "done"])


class DDPG(RlAlgorithm):

    opt_info_fields = tuple(f for f in OptInfo._fields)  # copy

    def __init__(
            self,
            discount=0.99,
            batch_size=64,
            min_steps_learn=int(1e4),
            replay_size=int(1e6),
            training_ratio=64,  # data_consumption / data_generation
            target_update_tau=0.01,
            target_update_interval=1,
            policy_update_interval=1,
            mu_learning_rate=1e-4,
            q_learning_rate=1e-3,
            OptimCls=torch.optim.Adam,
            optim_kwargs=None,
            initial_mu_optim_state_dict=None,
            initial_q_optim_state_dict=None,
            grad_norm_clip=1e6,
            q_target_clip=1e6,
            ):
        if optim_kwargs is None:
            optim_kwargs = dict()
        save__init__args(locals())
        self.update_counter = 0

    def initialize(self, agent, n_itr, batch_spec, mid_batch_reset, examples):
        if agent.recurrent:
            raise NotImplementedError
        self.agent = agent
        self.n_itr = n_itr
        self.mid_batch_reset = mid_batch_reset
        self.mu_optimizer = self.OptimCls(agent.mu_parameters(),
            lr=self.mu_learning_rate, **self.optim_kwargs)
        self.q_optimizer = self.OptimCls(agent.q_parameters(),
            lr=self.q_learning_rate, **self.optim_kwargs)
        if self.initial_mu_optim_state_dict is not None:
            self.mu_optimizer.load_state_dict(self.initial_mu_optim_state_dict)
        if self.initial_q_optim_state_dict is not None:
            self.q_optimizer.load_state_dict(self.initial_q_optim_state_dict)

        sample_bs = batch_spec.size
        train_bs = self.batch_size
        assert (self.training_ratio * sample_bs) % train_bs == 0
        self.updates_per_optimize = int((self.training_ratio * sample_bs) //
            train_bs)
        logger.log(f"From sampler batch size {sample_bs}, training "
            f"batch size {train_bs}, and training ratio "
            f"{self.training_ratio}, computed {self.updates_per_optimize} "
            f"updates per iteration.")
        self.min_itr_learn = self.min_steps_learn // sample_bs
        self.agent.give_min_itr_learn(self.min_itr_learn)

        example_to_replay = SamplesToReplay(
            observation=examples["observation"],
            action=examples["action"],
            reward=examples["reward"],
            done=examples["done"],
        )
        replay_kwargs = dict(
            example=example_to_replay,
            size=self.replay_size,
            B=batch_spec.B,
            store_valid=not mid_batch_reset,
        )
        self.replay_buffer = UniformReplayBuffer(**replay_kwargs)

    def optimize_agent(self, samples, itr):
        samples_to_replay = SamplesToReplay(
            observation=samples.env.observation,
            action=samples.agent.action,
            reward=samples.env.reward,
            done=samples.env.done,
        )
        self.replay_buffer.append_samples(samples_to_replay)
        opt_info = OptInfo(*([] for _ in range(len(OptInfo._fields))))
        if itr < self.min_itr_learn:
            return opt_info
        for _ in range(self.updates_per_optimize):
            self.update_counter += 1
            samples_from_replay = self.replay_buffer.sample(self.batch_size)
            self.q_optimizer.zero_grad()
            q_loss = self.q_loss(samples_from_replay)
            q_loss.backward()
            q_grad_norm = torch.nn.utils.clip_grad_norm_(
                self.agent.q_parameters(), self.clip_grad_norm)
            self.q_optimizer.step()
            opt_info.qLoss.append(q_loss.item())
            opt_info.qGradNorm.append(q_grad_norm)
            if self.update_counter % self.policy_update_interval == 0:
                self.mu_optimizer.zero_grad()
                mu_loss = self.mu_loss(samples_from_replay)
                mu_loss.backward()
                mu_grad_norm = torch.nn.utils.clip_grad_norm_(
                    self.agent.mu_parameters(), self.clip_grad_norm)
                self.mu_optimizer.step()
                opt_info.muLoss.append(mu_loss.item())
                opt_info.muGradNorm.append(mu_grad_norm)
            if self.update_counter % self.target_update_interval == 0:
                self.agent.update_target(self.target_update_tau)
        return opt_info

    def mu_loss(self, samples):
        mu_losses = self.agent.q_at_mu(*samples.agent_inputs)
        mu_loss = valid_mean(mu_losses, samples.valid)  # samples.valid can be None.
        return -mu_loss

    def q_loss(self, samples):
        """Samples have leading batch dimension [B,..] (but not time)."""
        q = self.agent.q(*samples.agent_inputs, samples.action)
        with torch.no_grad():
            target_q = self.agent.target_q_at_mu(*samples.target_inputs)
        y = samples.reward + (1 - samples.done.float()) * self.discount * target_q
        y = torch.clamp(y, -self.q_target_clip, self.q_target_clip)
        q_losses = 0.5 * (y - q) ** 2
        q_loss = valid_mean(q_losses, samples.valid)  # samples.valid can be None.
        return q_loss
