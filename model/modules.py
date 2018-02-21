import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.optim as optim
import numpy as np
import math
from functools import reduce
import operator
from collections import namedtuple, deque, OrderedDict


class VisionModule(nn.Module):

    def __init__(self, channels=[3, 1], kernels=[8], strides=None):
        '''
            Use the same hyperparameter settings denoted in the paper
        '''

        super(VisionModule, self).__init__()
        assert len(channels) > 1, "The channels length must be greater than 1"
        assert (
            len(channels) - 1 == len(kernels)
        ), "The array lengths must be equals"
        if strides is None:
            strides = [1] * len(kernels)
        assert len(kernels) == len(strides), "The array lengths must be equals"

        self.channels = list(channels)
        self.kernels = list(kernels)
        self.strides = list(strides)

        ordered_modules = OrderedDict()
        for i in range(len(channels) - 1):
            conv = nn.Conv2d(
                in_channels=channels[i],
                out_channels=channels[i + 1],
                kernel_size=kernels[i],
                stride=strides[i]
            )
            ordered_modules["conv{}".format(i + 1)] = conv
            # setattr(self, "conv{}".format(i + 1), conv)
        # self.num_layers = len(channels) - 1

        self.conv = nn.Sequential(ordered_modules)

    def forward(self, x):
        # # x is input image with shape [3, 84, 84]
        # out = x
        # for i in range(self.num_layers):
        #    out = getattr(self, "conv{}".format(i + 1))(out)
        # return out
        return self.conv(x)

    def get_output_shape(self, input_shape):
        if len(input_shape) == 1:
            h, w = input_shape[0], input_shape[0]
        elif len(input_shape) == 2:
            h, w = input_shape[0], input_shape[1]
        elif len(input_shape) == 3:
            h, w = input_shape[1], input_shape[2]
        else:
            h, w = input_shape[-2], input_shape[-1]

        for i in range(len(self.channels) - 1):
            kernel_size = self.kernels[i]
            stride = self.strides[i]
            if isinstance(kernel_size, int):
                kernel_size = (kernel_size, kernel_size)
            if isinstance(stride, int):
                stride = (stride, stride)

            h = math.floor((h - 1 * (kernel_size[0] - 1) - 1) / stride[0] + 1)
            w = math.floor((w - 1 * (kernel_size[1] - 1) - 1) / stride[1] + 1)

            h = int(h)
            w = int(w)

        return [self.channels[-1], h, w]

    def build_deconv(self):
        ordered_modules = OrderedDict()
        for i in range(len(self.channels) - 2, -1, -1):
            deconv = nn.ConvTranspose2d(
                in_channels=self.channels[i + 1],
                out_channels=self.channels[i],
                kernel_size=self.kernels[i],
                stride=self.strides[i]
            )
            ordered_modules["deconv{}".format(
                len(self.channels) - i - 1)] = deconv
        return nn.Sequential(ordered_modules)


class LanguageModule(nn.Module):

    def __init__(self, vocab_size=10, embed_dim=128, hidden_size=128):
        '''
            Use the same hyperparameter settings denoted in the paper
        '''

        super(LanguageModule, self).__init__()
        self.vocab_size = vocab_size
        self.embed_dim = embed_dim
        self.hidden_size = hidden_size

        self.embeddings = nn.Embedding(vocab_size, embed_dim)
        self.lstm = nn.LSTM(
            embed_dim,
            hidden_size,
            num_layers=1,
            batch_first=True)

    def forward(self, x, input_lengths=None):
        embedded_input = self.embeddings(x)
        if input_lengths is not None:  # Variable lenghts
            embedded_input = nn.utils.rnn.pack_padded_sequence(
                embedded_input, input_lengths, batch_first=True)
        out, hn = self.lstm(embedded_input)
        if input_lengths is not None:  # Variable lenghts
            out, _ = nn.utils.rnn.pad_packed_sequence(
                out, batch_first=True)
        h, c = hn

        return h

    def forward_reordering(self, x, input_lengths):
        if len(set(input_lengths)) == 1:
            return self(x)
        else:
            maxlen = max(input_lengths)
            sorted_x = Variable(
                torch.zeros(
                    len(input_lengths),
                    maxlen).type_as(
                    x[0].data))
            len_tensor = torch.from_numpy(np.array(input_lengths)).type_as(
                x[0].data)
            sorted_len, sorted_index = torch.sort(len_tensor, descending=True)
            inputs_len = []
            for i in range(len(input_lengths)):
                sorted_x[i, 0:sorted_len[i]] = x[sorted_index[i]][
                    0:sorted_len[i]
                ].view(-1)
                inputs_len.append(sorted_len[i])

            result = self(sorted_x, inputs_len)
            result = torch.index_select(
                result, dim=1, index=Variable(sorted_index)
            )
            return result

    def LP_Inv_Emb(self, x):
        return F.linear(x, self.embeddings.weight)

# Conditional Batch norm is a classical Batch Norm Module
# with the affine parameter set to False


class ConditionalBatchNorm(nn.Module):

    def __init__(self, num_features, eps=1e-5, momentum=0.1):
        super(ConditionalBatchNorm, self).__init__()
        self.num_features = num_features
        self.eps = eps
        self.momentum = momentum
        self.register_buffer('running_mean', torch.zeros(num_features))
        self.register_buffer('running_var', torch.ones(num_features))
        self.reset_parameters()

    def reset_parameters(self):
        self.running_mean.zero_()
        self.running_var.fill_(1)

    def _check_input_dim(self, input):
        if input.dim() != 4:
            raise ValueError(
                'expected 4D input (got {}D input)'.format(input.dim()))

    def forward(self, input, gamma, beta):
        return F.batch_norm(
            input, self.running_mean, self.running_var, gamma, beta,
            self.training, self.momentum, self.eps)

    def __repr__(self):
        return (
            '{name}({num_features}, eps={eps}, momentum={momentum}'.format(
                name=self.__class__.__name__, **self.__dict__)
        )


class MixingModule(nn.Module):

    def __init__(self):
        super(MixingModule, self).__init__()

    def forward(self, visual_encoded, instruction_encoded=None):
        '''
            Argument:
                visual_encoded: output of vision module, shape [batch_size, 64, 7, 7]
                instruction_encoded: hidden state of language module, shape [batch_size, 1, 128]
        '''
        batch_size = visual_encoded.size()[0]
        visual_flatten = visual_encoded.view(batch_size, -1)
        if instruction_encoded is not None:
            instruction_flatten = instruction_encoded.view(batch_size, -1)
            mixed = torch.cat([visual_flatten, instruction_flatten], dim=1)
            return mixed
        else:
            return visual_flatten


class ActionModule(nn.Module):

    def __init__(self, input_size=3264, hidden_size=256):
        super(ActionModule, self).__init__()
        self.hidden_size = hidden_size

        self.lstm_1 = nn.LSTM(
            input_size=input_size, hidden_size=hidden_size, batch_first=True)
        self.lstm_2 = nn.LSTM(
            input_size=hidden_size, hidden_size=hidden_size, batch_first=True)

        self.hidden_1 = None
        self.hidden_2 = None

    def reset_hidden_states(self):
        self.hidden_1 = None
        self.hidden_2 = None

    def repackage_hidden(self, h):
        """ Wraps hidden states in new Variables,
            to detach them from their history.
        """
        if isinstance(h, torch.autograd.Variable):
            return torch.autograd.Variable(h.data)
        else:
            return tuple(self.repackage_hidden(v) for v in h)

    def detach_hidden_states(self):
        if not self.hidden_1 is None:
            self.hidden_1 = self.repackage_hidden(self.hidden_1)
        if not self.hidden_2 is None:
            self.hidden_2 = self.repackage_hidden(self.hidden_2)

    # def detach_hidden_states(self):
    #     if not self.hidden_1 is None:
    #         self.hidden_1 = (l.detach() for l in self.hidden_1)
    #     if not self.hidden_2 is None:
    #         self.hidden_2 = (l.detach() for l in self.hidden_2)

    def forward(self, x):
        '''
            Argument:
                x: x is output from the Mixing Module, as shape [batch_size, 1, 3264]
        '''
        # Feed forward
        if x.dim() == 2:
            x = x.view(x.size(0), 1, x.size(1))

        _, s1 = self.lstm_1(x, self.hidden_1)
        h1, c1 = s1

        x1 = h1.transpose(0, 1).view(h1.size(1), 1, -1)

        _, s2 = self.lstm_2(x1, self.hidden_2)
        h2, c2 = s2

        # Update current hidden state
        self.hidden_1 = (h1, c1)
        self.hidden_2 = (h2, c2)

        # Return the hidden state of the upper layer
        x2 = h2.transpose(0, 1).view(h2.size(1), -1)
        return x2

    def forward_with_hidden_states(self, x, hidden_states=None):
        '''
            Argument:
                x: x is output from the Mixing Module, as shape [batch_size, 1, 3264]
        '''
        # Feed forward
        if x.dim() == 2:
            x = x.view(x.size(0), 1, x.size(1))

        if hidden_states is None:
            hidden_states_1 = None
            hidden_states_2 = None
        else:
            hidden_states_1, hidden_states_2 = hidden_states

        _, s1 = self.lstm_1(x, hidden_states_1)
        h1, c1 = s1

        x1 = h1.transpose(0, 1).view(h1.size(1), 1, -1)

        _, s2 = self.lstm_2(x1, hidden_states_2)
        h2, c2 = s2

        # Update current hidden state
        hidden_states_1 = (h1, c1)
        hidden_states_2 = (h2, c2)

        # Return the hidden state of the upper layer
        x2 = h2.transpose(0, 1).view(h2.size(1), -1)
        return x2, (hidden_states_1, hidden_states_2)

# We should do a ConditionalBatchNormModule, ClassifierModule, HistoricalRNN Module
# RL Module


SavedAction = namedtuple('SavedAction', ['action', 'value'])


class Policy(nn.Module):

    def __init__(self, action_space, input_size=256, hidden_size=128):
        super(Policy, self).__init__()
        self.action_space = action_space

        self.affine1 = nn.Linear(input_size, hidden_size)
        self.action_head = nn.Linear(hidden_size, action_space)
        self.value_head = nn.Linear(hidden_size, 1)

        self.saved_actions = []
        self.rewards = []

    def forward(self, x):
        x = F.relu(self.affine1(x))
        action_scores = self.action_head(x)
        state_values = self.value_head(x)

        return action_scores, state_values

    def tAE(self, action_logits):
        '''
        Temporal Autoencoder sub-task
        Argument:
            action_logits: shape [1, action_space]

        Return:
            output has shape: [1, hidden_size] # [1, 128]
                which is the inverse transformation of the Linear
                module corresponding to the policy (action_head)
        '''
        bias = torch.unsqueeze(
            self.action_head.bias, 0).repeat(
            action_logits.size()[0], 1)

        output = action_logits - bias
        output = F.linear(
            output, torch.transpose(
                self.action_head.weight, 0, 1))

        return output


class TemporalAutoEncoder(nn.Module):

    def __init__(self, policy_network, vision_module,
                 input_size=128, vision_encoded_shape=[64, 7, 7]):
        super(TemporalAutoEncoder, self).__init__()

        self.policy_network = policy_network
        self.vision_module = vision_module
        self.vision_encoded_shape = vision_encoded_shape
        self.input_size = input_size
        self.hidden_size = reduce(operator.mul, vision_encoded_shape, 1)

        self.linear_1 = nn.Linear(input_size, self.hidden_size)
        self.deconv = self.vision_module.build_deconv()

    def forward(self, visual_input, logit_action, deconvFlag=True):
        '''
        Argument:
            visual_encoded: output from the visual module, has shape [1, 64, 7, 7]
            logit_action: output action logit from policy, has shape [1, 10]
        '''
        visual_encoded = self.vision_module(visual_input)

        action_out = self.policy_network.tAE(logit_action)  # [1, 128]
        action_out = self.linear_1(action_out)
        action_out = action_out.view(
            action_out.size()[0], *self.vision_encoded_shape)

        out = torch.mul(action_out, visual_encoded)

        if deconvFlag:
            out = self.deconv(out)
        return out


class ICM(nn.Module):

    def __init__(self, policy_network, action_space,
                 input_size=128, hidden_size=3136, act_hid_size=128):
        super(ICM, self).__init__()

        self.policy_network = policy_network
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.action_space = action_space
        self.act_hid_size = act_hid_size
        self.action_space = action_space

        self.linear_1 = nn.Linear(input_size, self.hidden_size)
        self.linear_2 = nn.Linear(self.hidden_size, self.hidden_size)

        self.linear_3 = nn.Linear(2 * self.hidden_size, self.act_hid_size)
        self.linear_4 = nn.Linear(self.act_hid_size, self.action_space)

    def forward(self, state, next_state, logit_action):
        '''
        Argument:
            visual_encoded: output from the visual module, has shape [1, 64, 7, 7]
            logit_action: output action logit from policy, has shape [1, 10]
        '''

        action_out = self.policy_network.tAE(logit_action)  # [1, 128]
        action_out = self.linear_1(action_out)
        action_out = action_out.view_as(state)

        out = torch.mul(action_out, state)

        out = self.linear_2(out.view(out.size(0), -1)).view_as(state)

        concatState = torch.cat(
            [
                state.view(state.size(0), -1),
                next_state.view(next_state.size(0), -1)
            ], dim=1
        )
        act_pred = self.linear_3(concatState)
        act_pred = self.linear_4(act_pred)

        return out, act_pred


class RNNStatePredictor(nn.Module):

    def __init__(self, policy_network, vision_module,
                 input_size=128, vision_encoded_shape=[64, 7, 7],
                 ouput_size=1024):
        super(TemporalAutoEncoder, self).__init__()

        self.policy_network = policy_network
        self.vision_module = vision_module
        self.vision_encoded_shape = vision_encoded_shape
        self.input_size = input_size
        self.hidden_size = reduce(operator.mul, vision_encoded_shape, 1)
        self.ouput_size = ouput_size

        self.linear_1 = nn.Linear(input_size, self.hidden_size)
        self.linear_2 = nn.Linear(self.hidden_size, self.ouput_size)

    def forward(self, visual_input, logit_action):
        '''
        Argument:
            visual_encoded: output from the visual module, has shape [1, 64, 7, 7]
            logit_action: output action logit from policy, has shape [1, 10]
        '''
        visual_encoded = self.vision_module(visual_input)

        action_out = self.policy_network.tAE(logit_action)  # [1, 128]
        action_out = self.linear_1(action_out)
        action_out = action_out.view(
            action_out.size()[0], *self.vision_encoded_shape)

        combine = torch.mul(action_out, visual_encoded)

        out = self.linear_2(combine)
        return out


class LanguagePrediction(nn.Module):

    def __init__(self, language_module, vision_module,
                 vision_encoded_shape=[64, 7, 7],
                 hidden_size=128):
        super(LanguagePrediction, self).__init__()
        self.language_module = language_module
        self.vision_module = vision_module

        input_size = reduce(operator.mul, vision_encoded_shape, 1)

        self.vision_transform = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.ReLU())

    def forward(self, visual_input):

        vision_encoded = self.vision_module(visual_input)

        vision_encoded_flatten = vision_encoded.view(
            vision_encoded.size()[0], -1)
        vision_out = self.vision_transform(vision_encoded_flatten)

        language_predict = self.language_module.LP_Inv_Emb(vision_out)

        return language_predict


class RewardPrediction(nn.Module):

    def __init__(self, vision_module, language_module, mixing_module,
                 num_elts=3, vision_encoded_shape=[64, 7, 7],
                 language_encoded_size=128):
        super(RewardPrediction, self).__init__()

        self.vision_module = vision_module
        self.language_module = language_module
        self.mixing_module = mixing_module
        self.linear = nn.Linear(
            num_elts * (
                reduce(
                    operator.mul,
                    vision_encoded_shape, 1) + language_encoded_size
            ),
            1
        )

    def forward(self, x):
        '''
            x: state including image and instruction,
                    each batch contains 3 (num_elts) images in sequence
                    with the instruction to be encoded
        '''
        batch_visual = []
        batch_instruction = []
        batch_instruction_len = []

        for batch in x:
            visual = [b.image for b in batch]
            instruction = [b.mission for b in batch]
            instruction_len = [b.mission.numel() for b in batch]

            batch_visual.append(torch.cat(visual, 0))
            # batch_instruction.append(torch.cat(instruction, 0))
            batch_instruction.extend(instruction)
            batch_instruction_len.extend(instruction_len)

        if not (self.language_module is None):
            if len(set(batch_instruction_len)) == 1:
                batch_instruction = torch.cat(batch_instruction, 0)
                inputs_len = None
            else:
                maxlen = max(batch_instruction_len)
                sorted_instruction = Variable(torch.zeros(
                    len(batch_instruction_len), maxlen
                ).type_as(
                    batch_instruction[0].data
                ))
                len_tensor = torch.from_numpy(
                    np.array(batch_instruction_len)
                ).type_as(batch_instruction[0].data)
                sorted_len, sorted_index = torch.sort(
                    len_tensor, descending=True)
                inputs_len = []
                for i in range(len(batch_instruction_len)):
                    sorted_instruction[i, 0:sorted_len[i]] = batch_instruction[
                        sorted_index[i]].view(-1)
                    inputs_len.append(sorted_len[i])
                batch_instruction = sorted_instruction

        batch_visual_encoded = self.vision_module(torch.cat(batch_visual, 0))
        batch_instruction_encoded = None
        if not (self.language_module is None):
            if inputs_len is None:
                batch_instruction_encoded = self.language_module(
                    batch_instruction)
            else:
                batch_instruction_encoded = self.language_module(
                    batch_instruction, inputs_len)
                batch_instruction_encoded = torch.index_select(
                    batch_instruction_encoded,
                    dim=1,
                    index=Variable(sorted_index)
                )

        batch_mixed = self.mixing_module(
            batch_visual_encoded, batch_instruction_encoded)
        batch_mixed = batch_mixed.view(len(batch_visual), -1)

        out = self.linear(batch_mixed)
        return out


class VisualTargetClassification(nn.Module):

    def __init__(self, vision_module, language_module, mixing_module,
                 vision_encoded_shape=[64, 7, 7],
                 language_encoded_size=128):
        super(VisualTargetClassification, self).__init__()

        self.vision_module = vision_module
        self.language_module = language_module
        self.mixing_module = mixing_module
        self.linear = nn.Linear(
            reduce(
                operator.mul,
                vision_encoded_shape, 1
            ) + language_encoded_size,
            1
        )

    def forward(self, x):
        '''
            x: states including image and instruction,
        '''
        batch_visual = [b.image for b in x]
        batch_instruction = [b.mission for b in x]
        batch_instruction_len = [b.mission.numel() for b in x]

        if not (self.language_module is None):
            if len(set(batch_instruction_len)) == 1:
                batch_instruction = torch.cat(batch_instruction, 0)
                inputs_len = None
            else:
                maxlen = max(batch_instruction_len)
                sorted_instruction = Variable(torch.zeros(
                    len(batch_instruction_len), maxlen
                ).type_as(
                    batch_instruction[0].data
                ))
                len_tensor = torch.from_numpy(
                    np.array(batch_instruction_len)
                ).type_as(batch_instruction[0].data)
                sorted_len, sorted_index = torch.sort(
                    len_tensor, descending=True)
                inputs_len = []
                for i in range(len(batch_instruction_len)):
                    sorted_instruction[i, 0:sorted_len[i]] = batch_instruction[
                        sorted_index[i]].view(-1)
                    inputs_len.append(sorted_len[i])
                batch_instruction = sorted_instruction

        batch_visual_encoded = self.vision_module(torch.cat(batch_visual, 0))
        batch_instruction_encoded = None
        if not (self.language_module is None):
            if inputs_len is None:
                batch_instruction_encoded = self.language_module(
                    batch_instruction)
            else:
                batch_instruction_encoded = self.language_module(
                    batch_instruction, inputs_len)
                batch_instruction_encoded = torch.index_select(
                    batch_instruction_encoded,
                    dim=1,
                    index=Variable(sorted_index)
                )

        batch_mixed = self.mixing_module(
            batch_visual_encoded, batch_instruction_encoded)
        batch_mixed = batch_mixed.view(len(batch_visual), -1)

        out = self.linear(batch_mixed)

        # Use F.binary_cross_entropy_with_logits() for computing the
        # classification loss on this output
        return out
